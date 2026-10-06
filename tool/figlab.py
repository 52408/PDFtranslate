# -*- coding: utf-8 -*-
"""figlab.py —— 图表区域探测与图内文字提取。

目标（按用户要求）：
  1) 判断图表的结构与文字所在位置
  2) 生成"翻译后的图表"：白色覆盖原文字，再把中文按原位置写回去
  3) 纯图片（截图/照片）不翻译，整块剪裁即可

做法：
  * 图表区域 = 图注（Figure N: / Table N:）上方的一段纵向带（横向整页，
    因为图表通常横跨整页）。带内按元素类型分：
      - 矢量图：文字是 PDF 文本（可用 page.get_text 直接取到 span 坐标）
      - 位图  ：文字在图片像素里（只能用白色色块覆盖）
  * 图内文字块：字号明显小于正文（≤ 正文 − 1.6pt）的 span，或落在图片矩形内的 span
"""

import re

import pymupdf

FIG_CAP = re.compile(r"^(fig(?:ure)?\.?|图)\s*\.?\s*\d+\s*[:：]", re.I)
TAB_CAP = re.compile(r"^(tab(?:le)?\.?|表)\s*\.?\s*\d+\s*[:：]", re.I)


def page_body_size(page):
    """页面正文字号（按字符数加权）。"""
    weight = {}
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                t = sp.get("text", "")
                if t.strip():
                    k = round(sp.get("size", 0), 1)
                    weight[k] = weight.get(k, 0) + len(t)
    return max(weight, key=weight.get) if weight else 10.0


def find_captions(page):
    """返回本页的图/表标题块 [(rect, text, kind)]，kind 为 'fig' 或 'table'。"""
    blocks = []
    raw = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        lines = []
        for ln in blk.get("lines", []):
            s = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            if s.strip():
                lines.append(s)
        t = "\n".join(lines).strip()
        if not t:
            continue
        r = pymupdf.Rect(blk["bbox"])
        blocks.append((r, t))
        if TAB_CAP.match(t):
            raw.append((r, t, "table"))
        elif FIG_CAP.match(t):
            raw.append((r, t, "fig"))
    raw.sort(key=lambda x: (x[0].y0, x[0].x0))
    blocks.sort(key=lambda x: (x[0].y0, x[0].x0))

    # A printed caption can be split across adjacent text blocks (for example
    # "...correspond to" + "treatment effect. * p<0.05 ...").
    out = []
    for r, t, kind in raw:
        if out:
            pr, pt, pk = out[-1]
            gap = r.y0 - pr.y1
            overlap = max(0.0, min(pr.x1, r.x1) - max(pr.x0, r.x0))
            same_col = overlap > 0.5 * max(1.0, min(pr.width, r.width))
            unfinished = not pt.rstrip().endswith((".", "。", "!", "?", "！", "？"))
            if pk == kind and same_col and gap <= 4.5 and unfinished:
                out[-1] = (pr | r, pt.rstrip() + " " + t.strip(), kind)
                continue
        out.append((r, t, kind))

    def unfinished(t):
        return not t.rstrip().endswith((".", "。", "!", "?", "！", "？"))

    changed = True
    guard = 0
    while changed and guard < 20:
        changed = False
        guard += 1
        for i, (cr, ct, ck) in enumerate(out):
            if not unfinished(ct):
                continue
            best = None
            for br, bt in blocks:
                gap = br.y0 - cr.y1
                if gap < -0.5 or gap > 4.5:
                    continue
                if abs(br.y0 - cr.y0) < 0.5 and abs(br.x0 - cr.x0) < 0.5:
                    continue
                overlap = max(0.0, min(cr.x1, br.x1) - max(cr.x0, br.x0))
                if overlap <= 0.5 * max(1.0, min(cr.width, br.width)):
                    continue
                head = bt.strip()[:1]
                if not (head.islower() or head in "*†‡(（[【"):
                    continue
                best = (br, bt)
                break
            if best is not None:
                out[i] = (cr | best[0], ct.rstrip() + " " + best[1].strip(), ck)
                changed = True
                break
    return out


def figure_regions(page, split_columns=True):
    """推断本页的图表区域。

    返回 [(clip_rect, caption_rect, caption_text, kind), ...]。
    split_columns=True 时会把"并排两图"的横带切成左右两个区域，各自配自己的图注。
    """
    ph = page.rect.height
    caps = find_captions(page)
    if not caps:
        return []
    # 正文块底部（用于确定图表带的上边界）：字号接近正文的块
    body_size = page_body_size(page)
    pw = page.rect.width
    body_bottoms = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        sizes = [sp.get("size", 0) for ln in blk.get("lines", []) for sp in ln.get("spans", [])]
        if not sizes:
            continue
        txt = " ".join(sp.get("text", "") for ln in blk.get("lines", [])
                       for sp in ln.get("spans", [])).strip()
        r = pymupdf.Rect(blk["bbox"])
        # A real body paragraph is wide or long.  In-figure labels (axis names,
        # legends, short callouts) must not push the figure top edge downward.
        if (max(set(sizes), key=sizes.count) >= body_size - 1.0
                and (r.width >= pw * 0.35 or len(txt) >= 80)):
            body_bottoms.append(r.y1)

    def x_overlap(a, b):
        inter = min(a.x1, b.x1) - max(a.x0, b.x0)
        return inter / max(1.0, min(a.width, b.width))

    regions = []
    used = []                       # 已被认领的区域
    for cap_rect, cap_text, kind in caps:
        top = max(0.0, cap_rect.y0 - ph * 0.60)
        for yb in body_bottoms:
            if yb <= cap_rect.y0 - 4 and yb > top:
                top = max(top, yb + 2)
        # Never let a later figure swallow the tail of an earlier caption.
        for other_rect, _ot, _ok in caps:
            if other_rect.y1 <= cap_rect.y0 - 2:
                top = max(top, other_rect.y1 + 2)
        clip = pymupdf.Rect(0, top, page.rect.width, cap_rect.y0 - 1)
        if clip.height <= 20:
            continue
        # Only split into left/right panels when another caption sits in the
        # same horizontal band.  A single wide caption must stay full-width.
        same_band = False
        for other_rect, _ot, _ok in caps:
            if (abs(other_rect.x0 - cap_rect.x0) < 1.0
                    and abs(other_rect.y0 - cap_rect.y0) < 1.0):
                continue
            if abs(other_rect.y0 - cap_rect.y0) < max(25.0, cap_rect.height * 1.5):
                same_band = True
                break
        parts = (split_by_columns(page, clip)
                 if (same_band and split_columns) else [clip])
        # 选与图注横向重叠最大的那一块（并排两图时，各配自己那一半）
        best, best_ov = None, -1.0
        for part in parts:
            ov = x_overlap(part, cap_rect)
            free = not any(abs(part.y0 - u.y0) < 3 and abs(part.x0 - u.x0) < 3 for u in used)
            if ov > best_ov and (free or len(parts) == 1):
                best, best_ov = part, ov
        if best is None:
            best = parts[0]
        used.append(best)
        regions.append((best, cap_rect, cap_text, kind))
    return regions


def texts_in_region(page, clip, body_size, max_size_delta=1.6):
    """取出图表区域内的文字 span。

    返回 [(rect, text, size, kind)]，kind: 'label'（图内标签）或 'caption'（标题文字）。
    """
    out = []
    for blk in page.get_text("dict", clip=clip)["blocks"]:
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                t = sp.get("text", "")
                if not t.strip():
                    continue
                r = pymupdf.Rect(sp["bbox"])
                size = round(sp.get("size", 0), 1)
                out.append((r, t, size, "label"))
    return out


def tight_bbox(page, clip, zoom=0.7, thresh=244):
    """把区域收紧到实际内容边界（排除四周空白），这样按列宽缩放时图形能占满。

    做法：把区域栅格化，找非白像素的最小外接矩形，再映射回页面坐标。
    """
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    except Exception:
        return clip
    w, h, n, s = pix.width, pix.height, pix.n, pix.samples
    minx, miny, maxx, maxy = w, h, -1, -1
    step = max(1, int(1 / max(zoom, 0.1) / 4))       # 抽样步长，够快也够准
    for y in range(0, h, step):
        row = y * pix.stride
        for x in range(0, w, step):
            i = row + x * n
            if s[i] < thresh or s[i + 1] < thresh or s[i + 2] < thresh:
                if x < minx:
                    minx = x
                if y < miny:
                    miny = y
                if x > maxx:
                    maxx = x
                if y > maxy:
                    maxy = y
    if maxx <= minx or maxy <= miny:
        return clip
    # 像素坐标 → 页面坐标（加上 2pt 余量）
    pad = 2.0
    box = pymupdf.Rect(clip.x0 + minx / zoom - pad, clip.y0 + miny / zoom - pad,
                       clip.x0 + (maxx + 1) / zoom + pad, clip.y0 + (maxy + 1) / zoom + pad)
    return box & clip


def split_by_columns(page, clip, min_gap=18.0, min_side=60.0):
    """并排多图：用横向空隙把图区切成左右两半。

    论文里常见"左半张图 + 右半张图"，各自有独立图注。若不切分，
    两张图会被当成一个区域处理，插回文档时严重变形。
    返回 [左区, 右区] 或 [原区]（找不到明显空隙时）。
    """
    xs = []
    for blk in page.get_text("dict", clip=clip)["blocks"]:
        if blk.get("type") != 0:
            continue
        r = pymupdf.Rect(blk["bbox"])
        xs.append((r.x0, r.x1))
    if not xs:
        return [clip]
    xs.sort()
    # 找最大空隙
    best_gap, best_x = 0.0, None
    right_edge = xs[0][1]
    for x0, x1 in xs[1:]:
        gap = x0 - right_edge
        if gap > best_gap:
            best_gap, best_x = gap, (right_edge + x0) / 2
        right_edge = max(right_edge, x1)
    if best_gap < min_gap or best_x is None:
        return [clip]
    left = pymupdf.Rect(clip.x0, clip.y0, best_x - 2, clip.y1)
    right = pymupdf.Rect(best_x + 2, clip.y0, clip.x1, clip.y1)
    if left.width < min_side or right.width < min_side:
        return [clip]
    return [left, right]


def has_real_image(page, clip):
    """区域内是否有位图（截图/照片）。"""
    for img in page.get_images(full=True):
        try:
            rects = page.get_image_rects(img[0])
        except Exception:
            continue
        for r in rects:
            if (r & clip).get_area() > 0.3 * r.get_area():
                return True
    return False


def analyze(doc, pno):
    """输出某一页的图表结构报告。"""
    page = doc[pno]
    body = page_body_size(page)
    regs = figure_regions(page)
    print("原第 %d 页：正文基准字号 %.1f，识别到 %d 个图表区域" % (pno + 1, body, len(regs)))
    for i, (clip, cap_rect, cap_text, kind) in enumerate(regs):
        spans = texts_in_region(page, clip, body)
        small = [s for s in spans if s[2] <= body - 1.6]
        img = has_real_image(page, clip)
        print("  [%d] %s 区域 y=%.0f~%.0f (高 %.0f) | 文字 span %d 个（小字号 %d）| 位图=%s"
              % (i, kind, clip.y0, clip.y1, clip.height, len(spans), len(small), img))
        print("      标题: %s" % cap_text[:56].replace("\n", " "))
        for r, t, size, _ in small[:8]:
            print("      y=%6.1f x=%5.1f sz=%4.1f w=%5.1f | %s"
                  % (r.y0, r.x0, size, r.width, t[:46]))
    return regs


if __name__ == "__main__":
    import sys
    d = pymupdf.open(sys.argv[1])
    for a in sys.argv[2:] or ["1"]:
        analyze(d, int(a) - 1)
