# -*- coding: utf-8 -*-
"""figpipeline.py —— 把"翻译后的图表页"接入主排版。

流程：
  1) 对每一页找图表区域（figlab）
  2) 用图内文字译文生成一张"中文图表页"（figbuild）：白底覆盖原文 + 中文回贴
  3) 主排版在右栏对应位置插入这张图表页（矢量嵌入，清晰且不额外膨胀）
  4) 该页的图注不再以文字重复输出（已嵌在图里）
"""

import os
import sys

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import figbuild as FB
import figlab as FL
from layout2 import KIND_BODY


class FigurePages:
    """保存每页的"翻译后图表"页与它的原始区域，供排版时嵌入。"""

    def __init__(self):
        self.doc = pymupdf.open()      # 收集所有翻译后的图表页
        self.by_source = {}            # 原页码 -> [(新页号, clip_rect, kind, page_w, page_h)]
        self._anchor = {}              # 原页码 -> 锚点块 id（或 "top"）
        self.stats = {"figures": 0, "labels": 0, "raster": 0, "ocr": 0, "ocr_translated": 0}

    def add_page(self, src_doc, pno, zh_font_path, cache):
        """为原第 pno 页生成"翻译后的图表/表格页"。

        图表：并排两图放进同一图页，各自占右栏一行、保持原相对位置。
        表格：单独一页，按原行列逐格重建中文表格（与原表逐格对照）。
        """
        added = []
        pw, ph = src_doc[pno].rect.width, src_doc[pno].rect.height
        # ① 图形
        # 保留"带"作为整体（一张图页），但在 by_source 里按 x 记录每张图的位置：
        #   * 整体裁剪 → 图注（中文比原文长，会超出原栏宽）不会被切掉
        #   * x 记录   → 避免同一张内容在两条记录里重复绘制造成重影
        regs = FL.figure_regions(src_doc[pno])
        if regs:
            page, st = FB.build_translated_page_all(
                src_doc, pno, zh_font_path, cache=cache, clips=[r[0] for r in regs])
            if page is not None:
                new_no = self.doc.page_count
                self.doc.insert_pdf(page.parent)
                for clip, cap_rect, _ct, _k in regs:
                    c = pymupdf.Rect(clip)
                    cr = pymupdf.Rect(cap_rect)
                    full = pymupdf.Rect(min(c.x0, cr.x0), min(c.y0, cr.y0),
                                        max(c.x1, cr.x1), max(c.y1, cr.y1 + 14.0))
                    added.append((new_no, full, "fig", pw, ph))
                self.stats["figures"] += len(regs)
                self.stats["labels"] += st.get("translated", 0)
                self.stats["ocr"] += st.get("ocr", 0)
                self.stats["ocr_translated"] += st.get("ocr_translated", 0)        # ② 表格（按原行列重建）
        try:
            import tablelab as TL
            tabs = TL.table_cells(src_doc, pno)
        except Exception:
            tabs = []
        for cap_rect, cap_text, band, rows in tabs:
            page, st = FB.build_translated_table(src_doc, pno, zh_font_path,
                                                 cache=cache,
                                                 tab=(cap_rect, cap_text, band, rows))
            if page is None or st.get("tables", 0) == 0:
                continue
            new_no = self.doc.page_count
            self.doc.insert_pdf(page.parent)
            clip = pymupdf.Rect(band.x0, band.y0, band.x1, cap_rect.y1)
            added.append((new_no, clip, "table", pw, ph))
            self.stats["figures"] += 1
            self.stats["labels"] += st.get("translated", 0)
            self.stats["tables"] = self.stats.get("tables", 0) + 1
        if not added:
            return False
        self.by_source[pno] = added
        return True

    def has(self, pno):
        return bool(self.by_source.get(pno))

    def source_top(self, pno):
        """该页第一个图表在原页中的相对纵向位置（0=页顶，1=页底）。"""
        if not self.has(pno):
            return 0.0
        first = self.by_source[pno][0]
        clip, page_h = first[1], first[4]
        return max(0.0, min(1.0, clip.y0 / page_h)) if page_h else 0.0

    # ---------------------------------------------------------------- 文本流位置
    def anchor_of(self, pno, per_page):
        """找到"这张图在原文文本流里跟在哪个正文块之后"。

        背景：PDF 里图片区域没有自己的正文块，所以用几何关系判断——
        图上方最近的那个正文块，就是它在阅读顺序里的前驱。
        返回该块的 id()；若图在本页正文之前（页顶图），返回 None（表示放在本页正文之前）。
        """
        if not self.has(pno):
            return None
        clip = self.by_source[pno][0][1]
        cands = [p for p in per_page.get(pno, []) if p.kind == KIND_BODY]
        before = [p for p in cands if p.bbox[3] <= clip.y0 + 4]      # 图上方
        if not before:
            self._anchor[pno] = "top"
            return None
        anchor = max(before, key=lambda p: p.bbox[3])
        self._anchor[pno] = id(anchor)
        return id(anchor)

    def anchor_kind(self, pno):
        return self._anchor.get(pno)

    def place_at(self, out_page, pno, cfg, y_top, y_bottom, max_w=None):
        """就地插入该页图表（供"紧随锚点块之后"调用）。"""
        return self.place(out_page, pno, cfg, y_top, y_bottom, max_w=max_w)

    def plan(self, pno, col_w, max_scale=1.0, gap=6.0):
        """算出每个图表的放置尺寸。

        目标：右栏图表与**左栏原文图**显示尺寸一致 —— 即 scale = 1.0
        （图区在原页里的实际尺寸就是左栏的显示尺寸）。
        只有在超过右栏可用宽度时才等比收缩，保证不越界压到左栏。
        """
        out = []
        if not self.has(pno):
            return out
        for pg_no, clip, kind, page_w, page_h in self.by_source[pno]:
            if clip.width <= 0:
                continue
            scale = 1.0                                   # 与左栏一致
            if clip.width * scale > col_w:                # 超宽才收缩
                scale = col_w / clip.width
            disp_w = clip.width * scale
            disp_h = clip.height * scale
            out.append((pg_no, clip, kind, disp_w, disp_h + gap, page_w, page_h, scale))
        return out

    def needed_height(self, pno, col_w, max_scale=1.0):
        """该页所有图合计占用的显示高度。"""
        total = 0.0
        for _pg, clip, _k, _tw, _th, _pw, _ph, scale in self.plan(pno, col_w, max_scale):
            total += clip.height * scale + 6
        return total

    def group(self, pno):
        """把本页所有图表/表格当一个"整组"看待。

        同页多个"图形"保持原相对位置（并排的仍并排）；表格若与图形同页，
        作为额外一行追加在下方（表格通常独占一行，不与图形并排）。
        """
        items = self.by_source.get(pno) or []
        if not items:
            return None, [], 0, 0
        figs = [it for it in items if it[2] != "table"]
        tabs = [it for it in items if it[2] == "table"]
        box = None
        for _pg, clip, _k, _pw, _ph in figs:
            box = pymupdf.Rect(clip) if box is None else (box | clip)
        for _pg, clip, _k, _pw, _ph in tabs:
            box = pymupdf.Rect(clip) if box is None else (box | clip)
        return box, items, items[0][3], items[0][4]

    def plan_group(self, pno, col_w, col_h=None, allow_shrink=True):
        """整组的放置方案：缩放系数 + 每张图的目标位置（相对整组左上角）。

        allow_shrink=False 时只按原始比例算（用于判断"能否原生放下"）。
        """
        box, items, page_w, page_h = self.group(pno)
        if box is None:
            return None
        scale = 1.0                                   # 与左栏一致：原始尺寸
        if allow_shrink:
            if col_h is not None and box.height > col_h and box.height > 0:
                scale = min(scale, col_h / box.height)    # 高度放不下才缩（整组等比）
            if box.width * scale > col_w and box.width > 0:
                scale = min(scale, col_w / box.width)     # 宽度放不下才缩
        disp_w = box.width * scale
        disp_h = box.height * scale
        # 每张图相对整组左上角的偏移（保持原相对位置 → 并排仍并排）
        offs = []
        for pg_no, clip, kind, _pw, _ph in items:
            offs.append((pg_no, clip, kind,
                         (clip.x0 - box.x0) * scale, (clip.y0 - box.y0) * scale,
                         clip.width * scale, clip.height * scale))
        return box, offs, scale, disp_w, disp_h

    def needed_height_group(self, pno, col_w, col_h=None):
        pl = self.plan_group(pno, col_w, col_h)
        return pl[4] + 6 if pl else 0.0

    def place_group(self, out_page, pno, cfg, y_top, y_bottom, max_w=None):
        """按原相对位置把本页整组图放进右栏 [y_top, y_bottom]。返回 (结束 y, 占用高度)。"""
        if not self.has(pno):
            return y_top, 0.0
        right_x0 = cfg["right_x0"]
        avail_h = max(0.0, y_bottom - y_top)
        col_w = max_w or cfg["text_width"]
        pl = self.plan_group(pno, col_w, col_h=avail_h)
        if pl is None:
            return y_top, 0.0
        box, offs, scale, disp_w, disp_h = pl
        if disp_h <= 4 or disp_w <= 4:
            return y_top, 0.0
        for pg_no, clip, kind, dx, dy, w, h in offs:
            if w <= 2 or h <= 2:
                continue
            zoom = 2.2
            try:
                pix = self.doc[pg_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                                                 clip=clip, alpha=False)
            except Exception:
                continue
            try:
                pix.set_compression("jpeg", jpg_quality=90)
            except Exception:
                pass
            target = pymupdf.Rect(right_x0 + 4 + dx, y_top + dy,
                                  right_x0 + 4 + dx + w, y_top + dy + h)
            out_page.insert_image(target, pixmap=pix)
        return y_top + disp_h + 6, disp_h + 6

    def place(self, out_page, pno, cfg, y_top, y_bottom, max_scale=1.0, max_w=None,
              idx=None):
        """把该页的翻译图表嵌入右栏 [y_top, y_bottom]。

        idx=None：依次放置该页所有图；idx=k：只放第 k 张（一页多图时逐张调用）。
        返回 (结束 y, 实际占用高度)。
        """
        if not self.has(pno):
            return y_top, 0.0
        right_x0, margin = cfg["right_x0"], cfg["margin"]
        col_w = max_w or cfg["text_width"]
        plan = self.plan(pno, col_w, max_scale=max_scale)
        if idx is not None:
            plan = plan[idx:idx + 1]
        disp = [clip.height * scale + 6 for _pg, clip, _k, _tw, _th, _pw, _ph, scale in plan]
        total = sum(disp)
        avail = max(0.0, y_bottom - y_top)
        shrink = min(1.0, avail / total) if total > 0 else 1.0
        y = y_top
        for (pg_no, clip, kind, _tgt_w, _tgt_h, page_w, page_h, scale) in plan:
            w2 = clip.width * scale * shrink
            h2 = clip.height * scale * shrink
            if h2 <= 4 or w2 <= 4:
                break
            # 栅格化后嵌入：目标框与显示尺寸一致，图片绝不会越界压到上方文字
            zoom = 2.2
            try:
                pix = self.doc[pg_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                                                 clip=clip, alpha=False)
            except Exception:
                y += h2 + 6 * shrink
                continue
            try:
                pix.set_compression("jpeg", jpg_quality=90)
            except Exception:
                pass
            target = pymupdf.Rect(right_x0 + 4, y, right_x0 + 4 + w2, y + h2)
            out_page.insert_image(target, pixmap=pix)
            y += h2 + 6 * shrink
        return y, y - y_top

    def count(self, pno):
        return len(self.by_source.get(pno, []))


def build_all(src_doc, pages, zh_font_path, cache, log=print, on_page=None):
    """为所有页生成翻译图表页。on_page(当前索引, 总页数, 是否成功) 用于进度条。"""
    fp = FigurePages()
    total = len(pages)
    for i, pno in enumerate(pages):
        ok = False
        try:
            ok = fp.add_page(src_doc, pno, zh_font_path, cache)
            if ok:
                log("   第 %d 页：图表已翻译并生成嵌入页" % (pno + 1))
        except Exception as e:
            log("   第 %d 页图表处理失败（跳过）：%s" % (pno + 1, str(e)[:100]))
        if on_page:
            try:
                on_page(i + 1, total, ok)
            except Exception:
                pass
    return fp
