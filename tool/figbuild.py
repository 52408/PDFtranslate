# -*- coding: utf-8 -*-
"""figbuild.py —— 生成"翻译后的图表"：白色覆盖原文位置，再写入中文。

用法（单页验证）：
    python figbuild.py <输入.pdf> <页码(1-based)> <输出.pdf> [--cache 缓存.json]

原理：
  * 用 figlab 找到图表区域与图内文字 span（含坐标、字号）
  * 每个 span：先用白色矩形盖住原文，再按原位写入译文
      - 译文比原框宽 → 自动缩小字号（下限 4.5pt），保证不溢出
      - 译文比原框窄 → 保持原字号（视觉一致）
  * 纯图片区域（截图/照片）：整块剪裁，不做文字处理
"""

import json
import os
import sys

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import figlab as FL
import ocrlab as OL
import layout2 as L

ZH_FONT = r"C:\Windows\Fonts\msyh.ttc"


def merge_label_spans(spans, body_size):
    """把相邻的同标签多行 span 合并成一个整体（如 "Conceptual " + "Inquiry"）。

    spans: [(rect, text, size)]  →  [(rect, "Conceptual Inquiry", size)]
    合并条件：x 起点接近、纵向间距小于一个行高、字号相同。
    """
    items = []
    for r, t, size in spans:
        if not t.strip():
            continue
        if any(it[0].y0 <= r.y0 <= it[0].y1 + 0.6 * size and abs(it[0].x0 - r.x0) < 2
               for it in items):
            # 续行：并入上一项
            prev = next(it for it in items
                        if it[0].y0 <= r.y0 <= it[0].y1 + 0.6 * size and abs(it[0].x0 - r.x0) < 2)
            prev[0] = prev[0] | r
            prev[1] = prev[1].rstrip() + (" " if not prev[1].endswith("-") else "") + t.strip()
            continue
        items.append([pymupdf.Rect(r), t.strip(), size])
    return [(it[0], it[1], it[2]) for it in items]


def build_translated_page_all(doc, pno, zh_font_path, translator_map=None,
                              cache=None, pad=0.8, with_caption=True, clips=None):
    """把原第 pno 页的若干图表区域（clips）翻译成**一张**图页。

    并排两图会一起处理：各自复制到自己的坐标，互不干扰，插回右栏时纵向堆叠。
    """
    src = doc[pno]
    body_size = FL.page_body_size(src)
    if clips is None:
        regs = FL.figure_regions(src)
    else:
        all_regs = FL.figure_regions(src)
        regs = []
        for c in clips:
            hit = [r for r in all_regs
                   if abs(r[0].x0 - c.x0) < 3 and abs(r[0].y0 - c.y0) < 3]
            if hit:
                regs.append(hit[0])
            else:
                regs.append((pymupdf.Rect(c), pymupdf.Rect(c), "", "fig"))
    caps = FL.find_captions(src)
    if not regs:
        return None, {"regions": 0}
    return _build(doc, pno, zh_font_path, translate_factory(translator_map, cache),
                  regs, caps, pad=pad, with_caption=with_caption)


def translate_factory(translator_map=None, cache=None):
    def translate(text):
        t = text.strip()
        if not t:
            return ""
        if translator_map is not None:
            return translator_map(t)
        if cache is not None:
            import hashlib
            # Cache keys written by figterms are whitespace-normalized, while
            # PDF spans/captions often contain multiple spaces.  Try both forms.
            cands = [t, " ".join(t.split())]
            for c in cands:
                k = hashlib.sha1(c.encode("utf-8")).hexdigest()[:16]
                if k in cache:
                    return cache[k]
            return ""
        return ""
    return translate


def _build(doc, pno, zh_font_path, translate, regs, caps, pad=0.8, with_caption=True):
    """真正的构建逻辑：复制图区 → redaction 删英文 → 写中文（含图注重排）。"""
    src = doc[pno]
    body_size = FL.page_body_size(src)
    out = pymupdf.open()
    page = out.new_page(width=src.rect.width, height=src.rect.height)
    zh_font = pymupdf.Font(fontfile=zh_font_path)
    stats = {"regions": len(regs), "labels": 0, "translated": 0, "raster": 0,
             "covered_only": 0, "ocr": 0, "ocr_translated": 0}

    def span_boxes(clip):
        res = []
        for blk in src.get_text("dict", clip=clip)["blocks"]:
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    if sp.get("text", "").strip():
                        res.append((pymupdf.Rect(sp["bbox"]), sp.get("text", ""),
                                    round(sp.get("size", 10), 1)))
        return res

    def draw_box_text(box, tr, size_hint):
        # Tall/narrow boxes are usually rotated axis labels.
        if box.height > box.width * 1.8 and len(tr) > 1:
            fs = min(size_hint, max(4.0, box.width * 0.82))
            while fs > 4.0 and zh_font.text_length(tr, fs) > box.height:
                fs -= 0.2
            try:
                page.insert_textbox(box, tr, fontname="zhf", fontfile=zh_font_path,
                                    fontsize=fs, color=(0, 0, 0), rotate=90, align=0)
                return
            except Exception:
                pass
        fs = size_hint
        while fs > 4.0 and zh_font.text_length(tr, fs) > box.width:
            fs -= 0.15
        fs = min(fs, max(box.height / 1.15, 4.0))
        asc = zh_font.ascender * fs
        y = box.y0 + (box.height + asc) / 2 - asc * 0.32
        page.insert_text(pymupdf.Point(box.x0, y), tr, fontname="zhf",
                         fontfile=zh_font_path, fontsize=fs, color=(0, 0, 0))

    jobs = []          # (rect, text, size, translation); None = redact only
    ocr_jobs = []
    for clip, cap_rect, cap_text, kind in regs:
        page.show_pdf_page(clip, doc, pno, clip=clip)
        merged = merge_label_spans(span_boxes(clip), body_size)
        for r, text, size in merged:
            if not text.strip():
                continue
            tr = translate(text)
            if tr:
                jobs.append((pymupdf.Rect(r), text, size, tr))
            # No translation: leave original text untouched.  This preserves
            # numeric ticks, formulas and symbols that must not disappear.
        if not merged and OL.enabled() and OL.is_ocr_candidate(src, clip):
            try:
                ocr_jobs.extend(OL.ocr_region(src, clip))
            except Exception:
                pass

    cap_clips = [(pymupdf.Rect(cap_rect), pymupdf.Rect(clip))
                 for clip, cap_rect, _ct, _kind in regs]
    cap_jobs = []
    if with_caption:
        for cap_rect, cap_text, kind in caps:
            cr = pymupdf.Rect(cap_rect)
            cap_tr = translate(cap_text)
            below = None
            for blk in src.get_text("dict")["blocks"]:
                if blk.get("type") != 0:
                    continue
                br = pymupdf.Rect(blk["bbox"])
                if br.y0 > cr.y1 - 2:
                    below = br.y0 if below is None else min(below, br.y0)
            bottom = min(src.rect.height - 40, below - 2) if below else min(src.rect.height - 40,
                                                                            cr.y1 + 60)
            clip_cap = pymupdf.Rect(0, cr.y0 - 2, src.rect.width, max(bottom, cr.y1 + 10))
            page.show_pdf_page(clip_cap, doc, pno, clip=clip_cap)
            boxes = [b for b, t, s in span_boxes(clip_cap) if t.strip()]
            if cap_tr:
                cclip = None
                for cr0, cl0 in cap_clips:
                    if abs(cr0.x0 - cr.x0) < 1.0 and abs(cr0.y0 - cr.y0) < 1.0:
                        cclip = cl0
                        break
                # Constrain the Chinese caption to the original caption column.
                # Using the whole figure width makes side-by-side captions overlap.
                cap_w = max(80.0, min(cr.width + 10.0,
                                      src.rect.width - cr.x0 - 20.0))
                cap_jobs.append((cr, cap_text, boxes, cap_tr, cap_w))
                for b, t, s in span_boxes(clip_cap):
                    if t.strip():
                        jobs.append((b, t, s, None))
            # If no caption translation exists, keep the original caption.

    if not jobs:
        stats["raster"] = len(regs)
        return page, stats

    merged_boxes = []
    for r, text, size, tr in jobs:
        b = pymupdf.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad)
        hit = None
        for mb in merged_boxes:
            if b.intersects(mb):
                hit = mb
                break
        if hit is not None:
            hit |= b
        else:
            merged_boxes.append(pymupdf.Rect(b))
    for b in merged_boxes:
        page.add_redact_annot(b, fill=(1, 1, 1))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

    for r, text, size, tr in jobs:
        stats["labels"] += 1
        if not tr:
            continue
        if any(cap[0].contains(r) for cap in cap_jobs):
            continue
        stats["translated"] += 1
        box = pymupdf.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad)
        draw_box_text(box, tr, size)

    stats["ocr"] = len(ocr_jobs)
    for r, text, _size in ocr_jobs:
        tr = translate(text)
        if not tr:
            continue
        box = pymupdf.Rect(r.x0 - 1.0, r.y0 - 1.0, r.x1 + 1.0, r.y1 + 1.0)
        box &= pymupdf.Rect(0, 0, src.rect.width, src.rect.height)
        if box.width <= 0 or box.height <= 0:
            continue
        stats["ocr_translated"] += 1
        page.draw_rect(box, color=None, fill=(1, 1, 1), overlay=True)
        fs = min(10.0, max(4.5, box.height * 0.78))
        lines = L.wrap_text(tr, zh_font, fs, box.width)
        while fs > 4.0 and len(lines) * fs * 1.15 > box.height:
            fs -= 0.3
            lines = L.wrap_text(tr, zh_font, fs, box.width)
        lead = fs * 1.15
        total_h = len(lines) * lead
        yy = box.y0 + max(0.0, (box.height - total_h) / 2.0) + fs
        for ln in lines:
            page.insert_text(pymupdf.Point(box.x0, yy), ln, fontname="zhf",
                             fontfile=zh_font_path, fontsize=fs, color=(0, 0, 0))
            yy += lead

    for cr, cap_text, boxes, cap_tr, cap_w in cap_jobs:
        stats["translated"] += 1
        width = cap_w
        y = cr.y0 + 9
        for ln in L.wrap_text(cap_tr, zh_font, 8.5, width):
            page.insert_text(pymupdf.Point(cr.x0, y), ln, fontname="zhf",
                             fontfile=zh_font_path, fontsize=8.5, color=(0.15, 0.15, 0.15))
            y += 11.8
    return page, stats

    merged_boxes = []
    for r, text, size in jobs:
        b = pymupdf.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad)
        hit = None
        for mb in merged_boxes:
            if b.intersects(mb):
                hit = mb
                break
        if hit is not None:
            hit |= b
        else:
            merged_boxes.append(pymupdf.Rect(b))
    for b in merged_boxes:
        page.add_redact_annot(b, fill=(1, 1, 1))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

    for r, text, size in jobs:
        stats["labels"] += 1
        tr = translate(text)
        if not tr:
            if any(ch.isalpha() for ch in text):
                stats["covered_only"] += 1
            continue
        if any(cap[0].contains(r) for cap in cap_jobs):
            continue
        stats["translated"] += 1
        box = pymupdf.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad)
        fs = size
        while fs > 4.0 and zh_font.text_length(tr, fs) > box.width:
            fs -= 0.15
        fs = min(fs, max(box.height / 1.15, 4.0))
        asc = zh_font.ascender * fs
        y = box.y0 + (box.height + asc) / 2 - asc * 0.32
        page.insert_text(pymupdf.Point(box.x0, y), tr, fontname="zhf",
                         fontfile=zh_font_path, fontsize=fs, color=(0, 0, 0))

    stats["ocr"] = len(ocr_jobs)
    for r, text, _size in ocr_jobs:
        tr = translate(text)
        if not tr:
            continue
        box = pymupdf.Rect(r.x0 - 1.0, r.y0 - 1.0, r.x1 + 1.0, r.y1 + 1.0)
        box &= pymupdf.Rect(0, 0, src.rect.width, src.rect.height)
        if box.width <= 0 or box.height <= 0:
            continue
        stats["ocr_translated"] += 1
        page.draw_rect(box, color=None, fill=(1, 1, 1), overlay=True)
        fs = min(10.0, max(4.5, box.height * 0.78))
        lines = L.wrap_text(tr, zh_font, fs, box.width)
        while fs > 4.0 and len(lines) * fs * 1.15 > box.height:
            fs -= 0.3
            lines = L.wrap_text(tr, zh_font, fs, box.width)
        lead = fs * 1.15
        total_h = len(lines) * lead
        yy = box.y0 + max(0.0, (box.height - total_h) / 2.0) + fs
        for ln in lines:
            page.insert_text(pymupdf.Point(box.x0, yy), ln, fontname="zhf",
                             fontfile=zh_font_path, fontsize=fs, color=(0, 0, 0))
            yy += lead

    for cr, cap_text, boxes in cap_jobs:
        tr = translate(cap_text)
        if not tr:
            continue
        stats["translated"] += 1
        width = src.rect.width - cr.x0 - 60
        y = cr.y0 + 9
        for ln in L.wrap_text(tr, zh_font, 9.0, width):
            page.insert_text(pymupdf.Point(cr.x0, y), ln, fontname="zhf",
                             fontfile=zh_font_path, fontsize=9.0, color=(0.15, 0.15, 0.15))
            y += 12.8
    return page, stats


def build_translated_table(doc, pno, zh_font_path, translator_map=None, cache=None,
                           tab=None, pad=0.8):
    """按原表格重建中文表格：逐格 redaction 删英文 → 中文回贴到同一格。

    与图表同一套手法，保留行列位置，所以右栏表格与左栏表格可逐格对照。
    """
    import tablelab as TL
    src = doc[pno]
    tabs = TL.table_cells(doc, pno) if tab is None else [tab]
    if not tabs:
        return None, {"tables": 0}
    translate = translate_factory(translator_map, cache)
    zh_font = pymupdf.Font(fontfile=zh_font_path)

    def draw_cell(box, tr, base_fs=9.0):
        fs = base_fs
        lines = L.wrap_text(tr, zh_font, fs, box.width)
        while fs > 4.2 and len(lines) * fs * 1.15 > box.height:
            fs -= 0.3
            lines = L.wrap_text(tr, zh_font, fs, box.width)
        lead = fs * 1.15
        total_h = len(lines) * lead
        yy = box.y0 + max(0.0, (box.height - total_h) / 2.0) + fs
        for ln in lines:
            page.insert_text(pymupdf.Point(box.x0, yy), ln, fontname="zhf",
                             fontfile=zh_font_path, fontsize=fs, color=(0, 0, 0))
            yy += lead

    out = pymupdf.open()
    page = out.new_page(width=src.rect.width, height=src.rect.height)
    stats = {"tables": 0, "cells": 0, "translated": 0}

    for cap_rect, cap_text, band, rows in tabs:
        # 把整个表格带连同图注一起复制过来（保留原有横线/底纹等矢量元素）
        copy_clip = pymupdf.Rect(band.x0, band.y0, band.x1, cap_rect.y1)
        page.show_pdf_page(copy_clip, doc, pno, clip=copy_clip)
        stats["tables"] += 1

        # 逐格：只对已有译文的格做 redaction；缺译文时保留英文原格，
        # 避免缓存不完整时把表格清成空白。
        all_cell_texts = [text for row in rows for _r, text in row if text.strip()]
        max_cell_len = max((len(t) for t in all_cell_texts), default=0)
        n_cells = len(all_cell_texts)
        header_text = " ".join(t for _r, t in rows[0]) if rows else ""
        grid_table = isinstance(rows, getattr(TL, "GridRows", ()))
        if not grid_table:
            grid_table = (("Pilot" in header_text and "Challenges" in header_text)
                          or ("Treatment" in header_text and "Control" in header_text
                              and "Difference" in header_text))
        complex_table = (not grid_table) and (max_cell_len > 160
                         or (len(rows) <= 3 and n_cells > 10)
                         or (len(rows) >= 5 and n_cells >= 15)
                         or any("\n" in t for t in all_cell_texts))
        jobs = []
        cell_count = 0
        for row in rows:
            for r, text in row:
                if not text.strip():
                    continue
                cell_count += 1
                # In complex/merged tables, only translate short labels and
                # headers in place.  Long prose cells stay in the original
                # table image instead of being squeezed into broken columns.
                if complex_table and (len(text) > 60 or "\n" in text):
                    continue
                tr = translate(text) or translate(" ".join(text.split()))
                if tr:
                    jobs.append((pymupdf.Rect(r), text, tr))
        if cap_text.strip():
            cell_count += 1
            tr = translate(cap_text) or translate(" ".join(cap_text.split()))
            if tr:
                jobs.append((pymupdf.Rect(cap_rect), cap_text, tr))
        stats["cells"] += cell_count

        for r, text, tr in jobs:
            page.add_redact_annot(pymupdf.Rect(r.x0 - pad, r.y0 - pad,
                                                r.x1 + pad, r.y1 + pad), fill=(1, 1, 1))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

        for r, text, tr in jobs:
            stats["translated"] += 1
            box = pymupdf.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad)
            if r is cap_rect or abs(r.y0 - cap_rect.y0) < 2:
                # 图注：整段按可用宽度换行
                yy = box.y0 + 9
                for ln in L.wrap_text(tr, zh_font, 9.0, src.rect.width - box.x0 - 60):
                    page.insert_text(pymupdf.Point(box.x0, yy), ln, fontname="zhf",
                                     fontfile=zh_font_path, fontsize=9.0,
                                     color=(0.15, 0.15, 0.15))
                    yy += 12.8
                continue
            if grid_table:
                draw_cell(box, tr, base_fs=9.0)
            else:
                fs = min(9.5, max(5.0, box.height * 0.78))
                while fs > 4.2 and zh_font.text_length(tr, fs) > box.width:
                    fs -= 0.15
                asc = zh_font.ascender * fs
                yy = box.y0 + (box.height + asc) / 2 - asc * 0.32
                page.insert_text(pymupdf.Point(box.x0, yy), tr, fontname="zhf",
                                 fontfile=zh_font_path, fontsize=fs, color=(0, 0, 0))
    return page, stats


def build_translated_page(doc, pno, zh_font_path, translator_map=None,
                          cache=None, pad=0.8, with_caption=True, only_clip=None):
    """兼容入口：翻译指定（或全部）图表区域，返回 (page, stats)。"""
    clips = [only_clip] if only_clip is not None else None
    return build_translated_page_all(doc, pno, zh_font_path, translator_map,
                                     cache, pad, with_caption, clips)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pdf, pno = args[0], int(args[1])
    outp = args[2] if len(args) > 2 else "fig_page.pdf"
    cache_path = None
    for i, a in enumerate(sys.argv):
        if a == "--cache" and i + 1 < len(sys.argv):
            cache_path = sys.argv[i + 1]
    cache = json.load(open(cache_path, encoding="utf-8")) if cache_path else None

    doc = pymupdf.open(pdf)
    page, stats = build_translated_page(doc, pno - 1, ZH_FONT, cache=cache)
    print("统计:", stats)
    if page is None:
        print("该页没有识别到图表区域")
        return
    out = pymupdf.open()
    out.insert_pdf(page.parent)
    out.save(outp, garbage=4, deflate=True)
    print("已输出: %s" % outp)
    pix = out[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
    png = os.path.splitext(outp)[0] + ".png"
    pix.save(png)
    print("预览图: %s" % png)


if __name__ == "__main__":
    main()
