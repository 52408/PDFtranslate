# -*- coding: utf-8 -*-
"""regionlayout.py —— 按"栏结构"排版的渲染器（v2）。

与旧渲染器（layout2.render_bilingual2）的区别
--------------------------------------------
旧：右半页 = 一条整宽正文流（中文塞进半页），图片按锚点插入。
新：**先检测原文的栏结构，再让译文与原文逐带对应**。

最终页面形态（双栏源页）
    左半页 = 原文整页（原样）
    右半页 = 译文左栏 + 译文右栏，与原文左右栏严格对齐
             ┌───────────┬───────────┐
             │  原文左栏  │  原文右栏  │
             ├───────────┼───────────┤
             │  译文左栏  │  译文右栏  │
             └───────────┴───────────┘

规则（已与用户确认）
    * 按纵向带切分、逐带跟随原文：整页宽带（标题/摘要）横跨译文两栏；
      双栏带分译文两栏；图表区域在右半页**同高度同宽度**
    * 逐段对齐原文 y 位置（中文更短 → 会有空白，这是接受的取舍）
    * 单栏源页保持旧形态（左原文 | 右整宽译文）
    * 译文溢出 → 顺延到**下一页的对应栏**，页脚标注
"""

import os
import sys
import time

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import structlab as SL
from layout2 import KIND_BODY, KIND_CAPTION, KIND_NOTE, KIND_TABLE, wrap_text

GAP = 16            # 左右半页之间的间隙
MIN_FS = 7.6        # 译文最小字号（再小改顺延）
COL_PAD = 8         # 译文栏相对原文栏的内缩
LEAD = 1.42         # 行距倍率


class Strip:
    """一条纵向译文栏。"""

    __slots__ = ("x0", "x1", "items", "name")

    def __init__(self, x0, x1, name=""):
        self.x0 = float(x0)
        self.x1 = float(x1)
        self.items = []
        self.name = name

    @property
    def width(self):
        return self.x1 - self.x0


class MapPage:
    """一页的排版映射：带结构 + 栏目 + 图表区域。"""

    def __init__(self, pno, pw, ph, regions, blocks):
        self.pno = pno
        self.pw = pw
        self.ph = ph
        self.regions = regions
        self.blocks = blocks                     # [(piece, x0, x1), ...]
        self.figs = [r for r in regions if r.kind in ("figure", "table")]
        self.kind = SL.page_kind(regions)
        self.layer = []          # 视觉层矩形（图片/表格）；正文必须避让
        self.inside = []         # 落在视觉层内的块（不参与正文流）
        self.cap_rects = []      # 已随图表绘制的图注矩形（正文流里要排除，避免画两遍）


def strip_geometry(pw, margin=46):
    """译文栏的几何（方案甲：半页内等分两栏，各约 290pt）。

    为什么不照搬原文栏宽（约 242pt）：中文在 218pt 里每行只放得下约 15 字，
    段落行数远超原文块高度，会被压到字号下限还叠在一起。
    等分后每栏约 290pt，每行多放约 5 字，重叠与顺延都大幅下降；
    栏数与栏位置仍与原文一一对应，段落 y 位置仍逐段对齐。
    """
    avail = pw - margin * 2
    col_w = (avail - 10) / 2.0
    x0 = margin
    x1 = margin + col_w
    x2 = x1 + 10
    x3 = pw - margin
    return [(x0, x1), (x2, x3)]


def _strip_cols(regions, pw, margin=46, equal=True):
    """译文栏的 x 区间：方案甲用等分，否则照搬原文栏边界。"""
    if equal:
        return strip_geometry(pw, margin)
    best = None
    for r in regions:
        if r.kind == "two" and len(r.cols) == 2:
            span = r.cols[1][1] - r.cols[0][0]
            if best is None or span > (best.cols[1][1] - best.cols[0][0]):
                best = r
    if best:
        return [(best.cols[0][0], best.cols[0][1]), (best.cols[1][0], best.cols[1][1])]
    return strip_geometry(pw, margin)


def _two_cols(regions, pw):
    """取双栏带里的左右栏边界（选最宽的那条双栏带）——用于判断块属于哪一栏。"""
    best = None
    for r in regions:
        if r.kind == "two" and len(r.cols) == 2:
            span = r.cols[1][1] - r.cols[0][0]
            if best is None or span > (best.cols[1][1] - best.cols[0][0]):
                best = r
    if best:
        return best.cols
    return [(pw * 0.10, pw * 0.47), (pw * 0.53, pw * 0.90)]


def _merge_table_rows(blocks, pw):
    """把**真正的表格单元格**按行合并成一个块。

    注意：extract_document 的 kind 分类并不完全可靠（标题/页码/正文也可能被标成
    table），所以这里必须再加结构判据，否则会把"标题+页眉+页码"粘成一条：

      1) 只处理**小字号块**（size ≤ 9.6pt）—— 表格正文一般 7~9pt
      2) 只处理**小块**（高度 ≤ 16pt、宽度 ≤ 一栏宽的 0.62）
      3) 同一行要求 y0 相差 ≤ 3pt
      4) 必须"有邻居"：至少 2 个格同 y，或多个格在 y 上紧邻（间隔 ≤ 14pt）

    返回 (merged_pieces, absorbed_ids, kept_pieces)：kept_pieces 是没被合并的
    表格块（按普通文本处理）。
    """
    tables = [(p, x0, x1) for (p, x0, x1) in blocks if p.kind == KIND_TABLE]
    if len(tables) < 2:
        return [], set(), [p for p, _a, _b in tables]

    col_max = pw * 0.62
    cells = []
    not_cells = []
    for p, x0, x1 in tables:
        h = p.bbox[3] - p.bbox[1]
        size = p.size or 10.0
        if h <= 16.0 and (x1 - x0) <= col_max and size <= 9.6:
            cells.append((p, x0, x1))
        else:
            not_cells.append(p)
    if len(cells) < 2:
        return [], set(), [p for p, _a, _b in tables]

    # 按 y 聚类成"行候选"
    rows = []
    for p, x0, x1 in sorted(cells, key=lambda t: (t[0].bbox[1], t[0].bbox[0])):
        y0, y1 = p.bbox[1], p.bbox[3]
        hit = None
        for row in rows:
            if abs(y0 - row["y0"]) <= 3.0:
                hit = row
                break
        if hit is None:
            rows.append({"y0": y0, "y1": y1, "cells": [(x0, x1, p)]})
        else:
            hit["cells"].append((x0, x1, p))
            hit["y1"] = max(hit["y1"], y1)
            hit["y0"] = min(hit["y0"], y0)
    rows.sort(key=lambda r: r["y0"])

    # 结构判据 4：必须有邻居（多格行，或与上下行紧邻）
    good = []
    for i, row in enumerate(rows):
        multi = len(row["cells"]) >= 2
        near = False
        if i > 0 and row["y0"] - rows[i - 1]["y1"] <= 14:
            near = True
        if i + 1 < len(rows) and rows[i + 1]["y0"] - row["y1"] <= 14:
            near = True
        if multi and near:
            good.append(row)
        elif multi and len(rows) >= 3:
            good.append(row)

    merged = []
    absorbed = set()
    kept = list(not_cells)
    good_ids = {id(c[2]) for row in good for c in row["cells"]}
    for row in rows:
        if id(row["cells"][0][2]) not in good_ids:
            kept.extend(c[2] for c in row["cells"])
            continue
        row["cells"].sort(key=lambda c: c[0])
        text = "　".join(c[2].body_text() for c in row["cells"] if c[2].body_text().strip())
        if not text.strip():
            continue
        rp = _RowPiece()
        rp.bbox = (min(c[0] for c in row["cells"]), row["y0"],
                   max(c[1] for c in row["cells"]), row["y1"])
        rp.text = text
        rp.translation = text
        rp.size = 8.6
        rp.kind = KIND_TABLE
        rp.start_line = 0
        rp.table_rows = []
        rp.table_cells = []
        rp.table_owner = None
        rp.body_text = lambda self=rp: self.translation or self.text
        merged.append(rp)
        absorbed |= {id(c[2]) for c in row["cells"]}
    return merged, absorbed, kept


class _RowPiece:
    """合并后的表格行（带 Piece 需要的最小接口）。"""

    page = None
    bbox = (0, 0, 0, 0)
    text = ""
    translation = ""
    size = 8.6
    kind = KIND_TABLE
    start_line = 0
    table_rows = []
    table_cells = []
    table_owner = None

    def body_text(self):
        return self.translation or self.text


def _mark_span(piece, spans):
    """标记该块要跨栏（Piece 是 dataclass，可能没有 __dict__）。"""
    try:
        piece._full_span = True
    except Exception:
        spans.add(id(piece))
    spans.add(id(piece))


# ------------------------------------------------------------------ 图层计算

def visual_layer(page, regions, col_ranges=None):
    """本页"视觉层"（图片/表格所占的矩形）**并集**。

    这是唯一的真相来源：
      * 落在层内的文字块 = 图内标签/表格格 → 由图表流程翻译（图片里已带中文），
        **绝不能再当正文排一遍**
      * 正文流 = 完全落在层外的块
      * 排版时也要避让层内的矩形

    col_ranges: 本页的栏区间 [(x0,x1), ...]。表格带**不能**占满整页宽——
    否则会把两栏的正文流全堵死，导致大量顺延和整页留白。
    所以把表格带按栏切成矩形；单栏页只有一栏时自然就是整宽。
    """
    rects = []
    for r in regions:
        if r.kind in ("figure", "table"):
            rects.append(pymupdf.Rect(r.cols[0][0], r.y0, r.cols[0][1], r.y1))
    return rects


def in_visual_layer(rect, layer, thresh=0.75):
    """块是否属于视觉层。

    用 0.75 的较高阈值：只有**确实在图/表里**的块才排除。
    阈值太低会误伤紧贴图表上下的正文段落，导致它们被排除后整页留白。
    """
    area = max(1.0, rect.get_area())
    for lr in layer:
        inter = (rect & lr).get_area()
        if inter <= 0:
            continue
        if inter >= thresh * area:
            return True
        # 或者：块几乎包含在层内（层比块大，块被层包住）
        if rect.get_area() > 0 and inter >= 0.9 * min(area, lr.get_area()):
            return True
    return False


def split_blocks(blocks, layer):
    """把块分成 (正文流, 视觉层内块)。"""
    flow, inside = [], []
    for p, x0, x1 in blocks:
        r = pymupdf.Rect(p.bbox)
        (inside if in_visual_layer(r, layer) else flow).append((p, x0, x1))
    return flow, inside


def build_strips(page, regions, blocks, pw):
    """把译文块分配到译文栏，返回 (strips, mode, spans)。

    mode="two"：strips=[左译文栏, 右译文栏]（整页宽块的译文横跨两者）
    mode="one"：strips=[整宽带]（旧形态）
    spans：需要横跨两栏的块 id 集合
    """
    kind = SL.page_kind(regions)
    two = [r for r in regions if r.kind == "two"]

    # 表格先按行合并，避免同 y 的单元格互相叠放
    merged_rows, absorbed, kept_tables = _merge_table_rows(blocks, pw)
    if absorbed:
        blocks = [(p, x0, x1) for (p, x0, x1) in blocks
                  if id(p) not in absorbed and p.kind != KIND_TABLE]
        for rp in merged_rows:
            bx0, _by0, bx1, _by1 = rp.bbox
            blocks.append((rp, bx0, bx1))
        for p in kept_tables:
            blocks.append((p, p.bbox[0], p.bbox[2]))

    if kind == "单栏" or not two:
        lo = min((x0 for _p, x0, _x1 in blocks), default=pw * 0.10)
        hi = max((x1 for _p, _x0, x1 in blocks), default=pw * 0.90)
        # 单栏论文走旧形态：整宽一条译文流。**必须把块放进去**，
        # 否则右栏会几乎空白（曾因此丢掉 92% 的中文）。
        strip = Strip(lo, hi, "整宽")
        for piece, x0, x1 in blocks:
            strip.items.append(piece)
        return [strip], "one", set()

    cols = _two_cols(regions, pw)                 # 判断块属于原文哪一栏
    scols = _strip_cols(regions, pw, equal=True)  # 方案甲：译文栏等分（更宽）
    left = Strip(scols[0][0] + 2, scols[0][1] - 2, "译文左栏")
    right = Strip(scols[1][0] + 2, scols[1][1] - 2, "译文右栏")
    mid = (cols[0][1] + cols[1][0]) / 2.0
    spans = set()

    for piece, x0, x1 in blocks:
        # 整页宽内容（标题/摘要/跨栏说明/合并后的表格行）→ 横跨两栏。
        # 直接按"块宽 > 0.72 页宽"判定，比依赖带的归属更可靠。
        wide = (x1 - x0) > pw * 0.72
        if not wide:
            cy = (piece.bbox[1] + piece.bbox[3]) / 2.0
            for rg in regions:
                if rg.kind in ("figure", "table"):
                    continue
                if rg.y0 - 4 <= cy <= rg.y1 + 4 and rg.kind == "full":
                    wide = True
                    break
        if wide:
            _mark_span(piece, spans)
            left.items.append(piece)
            right.items.append(piece)
            continue
        if x0 < mid:
            left.items.append(piece)
        else:
            right.items.append(piece)
    return [left, right], "two", spans


# ------------------------------------------------------------------ 字号与换行

def _fit(zh_font, text, width, height, want, min_fs=MIN_FS):
    """从 want 往下找能同时满足宽/高的字号；找不到返回 None。"""
    fs = want
    while fs >= min_fs - 1e-6:
        lines = wrap_text(text, zh_font, fs, width)
        if lines and len(lines) * fs * LEAD <= height and \
           all(zh_font.text_length(l, fs) <= width + 0.5 for l in lines):
            return fs, lines
        fs -= 0.15
    return None


def _fit_tight(zh_font, text, width, height, want):
    """三级降级的第二级：收紧行距（1.42 → 1.22）。"""
    for lead_ratio in (1.34, 1.28, 1.22):
        fs = want
        while fs >= MIN_FS - 1e-6:
            lines = wrap_text(text, zh_font, fs, width)
            if lines and len(lines) * fs * lead_ratio <= height and \
               all(zh_font.text_length(l, fs) <= width + 0.5 for l in lines):
                return fs, lines, fs * lead_ratio
            fs -= 0.15
    return None


def _draw_lines(np, lines, x, y, fs, zh_path, lead=None):
    lead = lead or fs * LEAD
    for ln in lines:
        np.insert_text(pymupdf.Point(x, y), ln, fontname="zhcn",
                       fontfile=zh_path, fontsize=fs, color=(0, 0, 0))
        y += lead
    return y


def _overlaps(y0, y1, x0, x1, taken):
    """与已放置内容是否有真正的矩形相交（横向+纵向都相交）。"""
    for (ty0, ty1, tx0, tx1) in taken:
        if y0 < ty1 - 1 and y1 > ty0 + 1 and x0 < tx1 - 1 and x1 > tx0 + 1:
            return True
    return False


def is_flow_page(blocks, ph):
    """判断这页是否该走"整栏流式排满"（而不是逐段对齐原文 y）。

    参考文献页的特征：条目多、每段都很短、且段落之间几乎没有空隙。
    这类页若强行逐段对齐 y，中文行数远超原文块高度，字号会被压到下限，
    还会出现英文单词被切断的现象。改为流式排满即可恢复正常字号。
    """
    if len(blocks) < 5:
        return False
    small = [p for p, _x0, _x1 in blocks if (p.bbox[3] - p.bbox[1]) <= 44]
    if len(small) < 0.7 * len(blocks):
        return False
    # 段落之间的平均纵向空隙
    ys = sorted((p.bbox[1], p.bbox[3]) for p, _x0, _x1 in blocks)
    gaps = [ys[i + 1][0] - ys[i][1] for i in range(len(ys) - 1)]
    gaps = [g for g in gaps if g > -200]
    if not gaps:
        return False
    avg = sum(gaps) / len(gaps)
    return avg < 4.0


def merge_continuations(blocks):
    """把被 PDF 切断的片段合并成完整段落。

    参考文献页常出现：一段被页面/抽取边界切成 "…Ben Sh" + "nd Sack," 两块，
    各自翻译后就变成孤立的一行（译文里的 "nd"、"A"、"ation" 这类碎片）。
    这里按"前段以字母结尾 + 后段以小写/短续写片段开头"合并，且要求两段相邻
    （纵向间距很小、横向重叠）。
    """
    if len(blocks) < 2:
        return blocks
    items = sorted(blocks, key=lambda t: (round(t[0].bbox[1], 1), t[0].bbox[0]))
    out = []
    for p, x0, x1 in items:
        if not out:
            out.append([p, x0, x1])
            continue
        prev = out[-1]
        pp = prev[0]
        txt = (pp.body_text() or "").rstrip()
        nxt = (p.body_text() or "").lstrip()
        gap = p.bbox[1] - pp.bbox[3]
        same_col = not (x1 < prev[1] or x0 > prev[2])
        cont = False
        if same_col and txt and nxt:
            # 很短的 ASCII 续写片段（如 "nd"、"ation"）：间距放宽到 40pt
            short_frag = len(nxt) <= 14 and nxt.isascii()
            near = (-4 <= gap <= 40) if short_frag else (-4 <= gap <= 10)
            if near:
                if txt[-1].isalpha() and (nxt[0].islower() or short_frag):
                    cont = True
                elif txt[-1] in ",;:" and nxt[0].islower():
                    cont = True
        if cont:
            # 合并文本与译文
            sep = "" if txt[-1] == "-" else " "
            if txt[-1] == "-":
                pp.text = txt[:-1] + nxt
            else:
                pp.text = txt + sep + nxt
            pt = (pp.translation or "").rstrip()
            nt = (p.translation or "").lstrip()
            if pt or nt:
                pp.translation = (pt + "　" + nt).strip() if (pt and nt) else (pt or nt)
            pp.bbox = (min(pp.bbox[0], p.bbox[0]), min(pp.bbox[1], p.bbox[1]),
                       max(pp.bbox[2], p.bbox[2]), max(pp.bbox[3], p.bbox[3]))
            prev[1] = min(prev[1], x0)
            prev[2] = max(prev[2], x1)
        else:
            out.append([p, x0, x1])
    return [(p, x0, x1) for p, x0, x1 in out]


def merge_in_strip(items):
    """栏内顺序合并续写片段（不受几何间距限制）。

    PDF 抽取常把一段切成 "…Ben Sh" + "nd Sack,"，两者在**几何上不相邻**
    （中间夹着别的块），所以按间距的合并会漏。这里改为：在栏内按 y 顺序，
    只要"前一段以字母结尾"且"后一段是短 ASCII 续写片段（或小写开头）"就合并。
    """
    if len(items) < 2:
        return items
    ordered = sorted(items, key=lambda p: (round((p.bbox[1] + p.bbox[3]) / 2.0, 1),
                                           p.bbox[0]))
    out = []
    for p in ordered:
        if not out:
            out.append(p)
            continue
        pp = out[-1]
        txt = (pp.body_text() or "").rstrip()
        nxt = (p.body_text() or "").lstrip()
        if not txt or not nxt:
            out.append(p)
            continue
        short_frag = len(nxt) <= 16 and nxt.isascii() and nxt[:1].islower()
        lower_cont = nxt[:1].islower() and txt[-1].isalpha()
        if (txt[-1].isalpha() and (short_frag or lower_cont)) or \
           (txt[-1] in ",;:" and nxt[:1].islower()):
            sep = "" if txt.endswith("-") else " "
            pp.text = (txt[:-1] if txt.endswith("-") else txt) + sep + nxt
            pt = (pp.translation or "").rstrip()
            nt = (p.translation or "").lstrip()
            if pt or nt:
                pp.translation = (pt + "　" + nt).strip() if (pt and nt) else (pt or nt)
            pp.bbox = (min(pp.bbox[0], p.bbox[0]), min(pp.bbox[1], p.bbox[1]),
                       max(pp.bbox[2], p.bbox[2]), max(pp.bbox[3], p.bbox[3]))
        else:
            out.append(p)
    return out


def drop_fig_captions(blocks, regions, pw):
    """剔除已经随"图页"绘制过的整宽图注。

    图注（如 "Figure 1: …"）横跨两栏，若留在正文流里会被同时放进左右两栏、
    在双栏正文之前横跨绘制，把正文挤到下面——看起来就像整页变成了单栏。
    figbuild 已经把图注连同图片一起翻译并绘制，所以这里直接去掉。
    判据：整宽（>0.72 页宽）且纵向落在某图表带内或其后 40pt 内。
    """
    bands = [r for r in regions if r.kind in ("figure", "table")]
    if not bands:
        return blocks
    out = []
    for p, x0, x1 in blocks:
        wide = (x1 - x0) > pw * 0.72
        if not wide:
            out.append((p, x0, x1))
            continue
        r = pymupdf.Rect(p.bbox)
        skip = False
        for b in bands:
            if r.y0 >= b.y0 - 6 and r.y0 <= b.y1 + 40:
                skip = True
                break
        if not skip:
            out.append((p, x0, x1))
    return out


def place_items(np, strips, zh_font, zh_path, ph, margin, taken, fig_bands,
                foot, defer, spans, span_x0=None, span_x1=None, layer=None,
                flow_mode=False, start_y=None, log=None):
    """**单一时间轴**放置：跨栏块只画一次。

    做法：把每条 strip 的块合成一个列表（带所属栏），按原文 y 排序；
    跨栏块用 [span_x0, span_x1] 的宽度画，其余用所属 strip 的宽度；
    每条 strip 各自维护"本栏游标"，保证同栏内不重叠、只向后推挤。
    """
    rows = []
    for si, strip in enumerate(strips):
        for p in merge_in_strip(strip.items):
            spanning = (span_x1 is not None and
                        (id(p) in spans or getattr(p, "_full_span", False)))
            rows.append((round((p.bbox[1] + p.bbox[3]) / 2.0, 1), p.bbox[0], si,
                         p, spanning))
    rows.sort(key=lambda r: (r[0], r[1], 0 if r[4] else 1))
    done = set()                     # 已画过的块 id（跨栏块只画一次）
    cursors = [(start_y if start_y is not None else margin)] * len(strips)
    max_y = margin

    for _cy, _x0, si, p, spanning in rows:
        if id(p) in done:
            continue
        text = p.body_text()
        if not text or not text.strip():
            done.add(id(p))
            continue
        strip = strips[si]
        x_left = span_x0 if (spanning and span_x0 is not None) else strip.x0
        x_right = span_x1 if (spanning and span_x1 is not None) else strip.x1
        eff_w = max(60.0, x_right - x_left)

        st = p.start_line or 0
        if st:
            base_fs = p.size or 9.5
            all_lines = wrap_text(text, zh_font, base_fs, eff_w)
            text = "".join(all_lines[st:]) if st < len(all_lines) else ""
            if not text.strip():
                done.add(id(p))
                continue

        # 基准 y：本栏游标与原文位置取大（只向后推挤，不向前）
        # flow_mode（参考文献页等）：完全按游标连续排，不做逐段对齐，
        # 这样字号不会被压到下限，也不会出现单词被切断。
        base = cursors[si] if not spanning else max(cursors)
        _floor = start_y if start_y is not None else margin
        y = max(base, _floor) if flow_mode else max(base, p.bbox[1], _floor)
        limit = ph - margin
        for band in fig_bands:
            fx0, fx1 = band.cols[0]
            if band.y0 >= y + 2 and not (fx1 < x_left or fx0 > x_right):
                limit = min(limit, band.y0 - 6)
        # 视觉层（图片/表格所占矩形）必须整块避开
        for lr in (layer or []):
            if lr.y0 >= y + 2 and not (lr.x1 < x_left or lr.x0 > x_right):
                limit = min(limit, lr.y0 - 6)
        for (ty0, _ty1, tx0, tx1) in taken:
            if ty0 >= y + 2 and not (tx1 < x_left or tx0 > x_right):
                limit = min(limit, ty0 - 4)
        room = max(0.0, limit - y)

        want = min(p.size or 9.5, 10.5)

        def _try(fs, lines, lead):
            """在 y 处试画；与已有内容重叠就整体下移到空隙。返回结束 y 或 None。"""
            yy = y
            h = len(lines) * lead
            guard = 0
            while _overlaps(yy, yy + h, x_left, x_right, taken) and guard < 40:
                guard += 1
                nxt = yy
                for (ty0, ty1, tx0, tx1) in taken:
                    if (yy < ty1 - 1 and yy + h > ty0 + 1
                            and x_left < tx1 - 1 and x_right > tx0 + 1):
                        nxt = max(nxt, ty1 + 2)
                if nxt <= yy:
                    break
                yy = nxt
            if yy + h > ph - margin:
                return None
            end = _draw_lines(np, lines, x_left, yy, fs, zh_path, lead=lead)
            taken.append((yy, end, x_left, x_right))
            return end

        y_end = None
        got = _fit(zh_font, text, eff_w, room, want)
        if got is not None:
            y_end = _try(got[0], got[1], got[0] * LEAD)
        if y_end is None:
            got2 = _fit_tight(zh_font, text, eff_w, room, want)
            if got2 is not None:
                y_end = _try(got2[0], got2[1], got2[2])
        if y_end is None:
            fs = max(MIN_FS, min(want, 8.6))
            lines = wrap_text(text, zh_font, fs, eff_w)
            lead = fs * LEAD
            n = max(0, int(room // lead))
            if n <= 0:
                defer.append((strip.name, p, st))
                foot.append("%s 顺延" % strip.name)
                done.add(id(p))
                continue
            y_end = _try(fs, lines[:n], lead)
            if y_end is None:
                defer.append((strip.name, p, st))
                foot.append("%s 顺延" % strip.name)
                done.add(id(p))
                continue
            if n < len(lines):
                defer.append((strip.name, p, st + n))
                foot.append("%s 接上页" % strip.name)

        # 占用区间影响所有与其 x 重叠的栏的游标
        newcur = y_end + (2.0 if p.kind == KIND_BODY else 4.5)
        for i, s in enumerate(strips):
            if not (x_right < s.x0 or x_left > s.x1):
                cursors[i] = max(cursors[i], newcur)
        max_y = max(max_y, y_end)
        done.add(id(p))
    return max_y


# ------------------------------------------------------------------ 图表放置

def place_figures(np, page, pno, figs, zh_path, cache, taken, log=None,
                  figpages=None):
    """把该页图表（翻译后）放到右半页的同一位置、同一尺寸。

    优先用主流程已经建好的 `figpages`（FigurePages，含 OCR/图注译文），
    没有时才现场构建。右半页与源页同尺寸，所以"源页坐标 + 右半页原点"
    就是目标位置，天然满足"与左栏图片大小排版一致"。
    """
    if not figs:
        return 0
    half = np.rect.width / 2.0 + GAP / 2.0
    n = 0

    # ---------- 方案甲：优先用 figpages 的条目（一图一条） ----------
    # 这才是"一页多图"的正确表示：每张图 + 它的图注各占一条，
    # 不会像 structlab 的合并带那样把并排图圈在一起、也不会漏画。
    if figpages is not None and figpages.has(pno):
        entries = list(figpages.by_source.get(pno) or [])
        for (pg_no, clip, kind, _pw, _ph) in entries:
            clip = pymupdf.Rect(clip)
            if clip.width < 16 or clip.height < 16:
                continue
            try:
                target = pymupdf.Rect(half + clip.x0, clip.y0,
                                      half + clip.x1, clip.y1)
                np.show_pdf_page(target, figpages.doc, pg_no, clip=clip)
                taken.append((target.y0 - 2, target.y1 + 2, target.x0, target.x1))
                n += 1
                if os.environ.get("PDFTRANS_DEBUG"):
                    print("      [fig] 条目 p%d#%d kind=%s clip=%.0f,%.0f,%.0f,%.0f"
                          % (pno + 1, n, kind, clip.x0, clip.y0, clip.x1, clip.y1),
                          flush=True)
            except Exception as e:
                if os.environ.get("PDFTRANS_DEBUG"):
                    print("      [fig] 条目失败: %s" % str(e)[:70], flush=True)
        if n:
            return n
        # 翻译页没有可用条目 → 落到下面的原图回退

    src_doc = None
    page_no = 0
    src_is_translated = False
    if figpages is not None and figpages.has(pno):
        src_doc = figpages.doc
        page_no = figpages.by_source[pno][0][0]
        src_is_translated = True
    else:
        try:
            import figbuild as FB
            src_doc, _st = FB.build_translated_page_all(page.parent, pno, zh_path,
                                                        cache=cache)
            page_no = 0
            src_is_translated = True
        except Exception as e:
            if log:
                log("      图表翻译失败（回退原图）：%s" % str(e)[:80])
            src_doc = None
    if src_doc is None or not src_is_translated:
        # **回退**：翻译流程没产出内容（例如扫描图/纯截图页）时，
        # 直接嵌原页对应区域——图片绝不能因为"没译文"就消失。
        src_doc = page.parent
        page_no = pno
    half = np.rect.width / 2.0 + GAP / 2.0
    n = 0
    for rg in figs:
        fx0, fx1 = rg.cols[0]
        clip = pymupdf.Rect(fx0, rg.y0, fx1, rg.y1)
        if clip.width < 16 or clip.height < 16:
            continue
        src = src_doc
        pno_draw = page_no
        # 若翻译页里这段区域是空白（扫描图/纯截图，没有可替换的文字），
        # 就把来源换成**原页**——图绝不能因为"没译文"就消失。
        if src_is_translated and rg.kind != "table":
            try:
                blank = True
                if src.get_text(clip=clip).strip():
                    blank = False
                else:
                    for im in src.get_images(full=True):
                        for ir in src.get_image_rects(im[0]):
                            if (ir & clip).get_area() > 0.25 * clip.get_area():
                                blank = False
                                break
                        if not blank:
                            break
                if blank:
                    src = page.parent
                    pno_draw = pno
                    if os.environ.get("PDFTRANS_DEBUG"):
                        print("      [fig] p%d 翻译页该区域空白 → 回退原图" % (pno + 1),
                              flush=True)
            except Exception:
                pass
        if log and os.environ.get("PDFTRANS_DEBUG"):
            log("      [fig] p%d kind=%s clip=%.0f,%.0f,%.0f,%.0f"
                % (pno + 1, rg.kind, clip.x0, clip.y0, clip.x1, clip.y1))
        if rg.kind == "table":
            # 表格必须用"按行列重建"的表页，不能用图页：
            # 图页只含图注与图内标签，表格正文会整段缺失。
            try:
                import tablelab as TL
                tabs = TL.table_cells(page.parent, pno)
            except Exception:
                tabs = []
            tab = None
            for cap, ct, band, rows in tabs:
                # 用"表格带与图区是否有实质纵向交集"判定，不要比 y0：
                # table_bands 给的是 (0,375.9) 而图区是 (40,376)，起点不同。
                ov = min(band.y1, rg.y1) - max(band.y0, rg.y0)
                if ov > 0.4 * max(1.0, min(band.y1 - band.y0, rg.y1 - rg.y0)):
                    tab = (cap, ct, band, rows)
                    break
            if tab is None and len(tabs) == 1:
                # 只有一张表时直接采用（避免因坐标细节漏掉）
                tab = tabs[0]
            if os.environ.get("PDFTRANS_DEBUG"):
                print("      [tab] p%d 候选表%d 命中=%s" % (pno + 1, len(tabs),
                                                          tab is not None), flush=True)
            if tab is not None:
                try:
                    import figbuild as FB
                    tpage, _st = FB.build_translated_table(page.parent, pno, zh_path,
                                                           cache=cache, tab=tab)
                    if tpage is not None:
                        src = tpage.parent
                        pno_draw = 0
                        # 用**单元格实际范围**裁切，而不是表格带的整页宽：
                        # 表格带是 0~612，但表格只占其中一段，多裁会把大片空白带进来。
                        cap, _ct, band, rows = tab
                        xs0 = [r.x0 for row in rows for r, _t in row]
                        xs1 = [r.x1 for row in rows for r, _t in row]
                        ys0 = [r.y0 for row in rows for r, _t in row]
                        ys1 = [r.y1 for row in rows for r, _t in row]
                        if xs0 and xs1:
                            clip = pymupdf.Rect(min(xs0) - 6, min(ys0) - 8,
                                                max(xs1) + 6, max(ys1) + 8)
                        else:
                            clip = pymupdf.Rect(0, rg.y0, page.rect.width, rg.y1)
                        if os.environ.get("PDFTRANS_DEBUG"):
                            _tp = tpage.get_text(clip=clip)
                            print("      [tab] 新clip=%.0f,%.0f,%.0f,%.0f 行%d 表页总行%d"
                                  % (clip.x0, clip.y0, clip.x1, clip.y1,
                                     len([x for x in _tp.splitlines() if x.strip()]),
                                     len([x for x in tpage.get_text().splitlines() if x.strip()])),
                                  flush=True)
                except Exception as e:
                    if log:
                        log("      表格重建失败：%s" % str(e)[:80])
        try:
            target = pymupdf.Rect(half + clip.x0, clip.y0, half + clip.x1, clip.y1)
            np.show_pdf_page(target, src, pno_draw, clip=clip)
            taken.append((target.y0 - 2, target.y1 + 2, target.x0, target.x1))
            n += 1
            if os.environ.get("PDFTRANS_DEBUG"):
                try:
                    _tt = np.get_text(clip=target)
                except Exception:
                    _tt = ""
                print("      [fig] 画入 kind=%s target=%.0f,%.0f,%.0f,%.0f 文本行=%d"
                      % (rg.kind, target.x0, target.y0, target.x1, target.y1,
                         len([x for x in _tt.splitlines() if x.strip()])), flush=True)
        except Exception as _e:
            if os.environ.get("PDFTRANS_DEBUG"):
                print("      [fig] 画失败 kind=%s: %s" % (rg.kind, str(_e)[:80]), flush=True)
    return n


# ------------------------------------------------------------------ 顺延续排

def render_deferred(np, pending, ox, ph, margin, zh_font, zh_path, taken, foot,
                    strip_specs=None, pw=None, flow=True):
    """把顺延内容排到这一页（续页）。

    flow=True（默认）：**从页顶连续排**（上对齐）。
    顺延页的左半页是空白的，所以不需要对齐任何原文 y——直接排满即可。
    """
    done = 0
    still = []
    by_name = {}
    for name, piece, start in pending:
        by_name.setdefault(name, []).append((piece, start))

    if not strip_specs:
        if pw is None:
            pw = np.rect.width / 2.0
        strip_specs = [(margin, pw - margin, "整宽")]

    for name, items in by_name.items():
        spec = None
        for (x0, x1, sname) in strip_specs:
            if ("左" in name) == ("左" in sname):
                spec = (x0, x1, sname)
                break
        if spec is None:
            spec = strip_specs[0]
        strip = Strip(ox + spec[0], ox + spec[1], spec[2])
        # **一次性把该栏所有块交给 place_items**：这样游标连续推进、整页填满。
        # 逐块调用会让每块都从页顶开始，导致每页只放一块、续页数量爆炸。
        for piece, start in items:
            piece.start_line = start
        strip.items = [pc for pc, _st in items]
        inner = []
        place_items(np, [strip], zh_font, zh_path, ph, margin, taken, [],
                    foot, inner, set(), flow_mode=flow)
        for (_n, pp, ss) in inner:
            still.append((name, pp, ss))
        done += len(items)
        strip.items = []
    return done, still


# ------------------------------------------------------------------ 保存

def save_doc(doc, out_path):
    d = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(d, exist_ok=True)
    try:
        doc.subset_fonts()
    except Exception:
        pass
    tmp = out_path + ".tmp"
    for attempt in (True, False):
        try:
            if attempt:
                doc.save(tmp, garbage=4, deflate=True, clean=True)
                os.replace(tmp, out_path)
                return out_path
            raise RuntimeError("target locked")
        except Exception:
            if attempt:
                continue
            alt = "%s-%s.pdf" % (os.path.splitext(out_path)[0],
                                 time.strftime("%Y%m%d-%H%M%S"))
            doc.save(alt, garbage=4, deflate=True, clean=True)
            return alt


# ------------------------------------------------------------------ 主入口

def render_regions(doc, per_page, pages, out_path, zh_font_path,
                   font_size=9.5, raster=False, zoom=2.0, log=print,
                   figpages=None, cache=None, on_page=None):
    """区域化排版主入口。返回 (输出路径, 统计)。"""
    out = pymupdf.open()
    zh_font = pymupdf.Font(fontfile=zh_font_path)
    cache = cache or {}
    if not cache:
        log("   [警告] 未传入译文缓存 → 图内文字与正文都不会有中文")
    margin = 46
    drop = SL.repeated_margin_texts(doc, range(doc.page_count))
    mbands = SL.margin_bands(doc, range(doc.page_count))
    src_pages = list(pages)

    stats = {"pages": 0, "two_band_pages": 0, "one_pages": 0, "figures": 0,
             "deferred_pieces": 0, "cont_pages": 0}
    maps = []
    for i, pno in enumerate(src_pages):
        page = doc[pno]
        regions = SL.page_layout(page, drop_keys=drop, margin_bands_=mbands)        # 表格块只保留**确实落在真实表格带里**的（extract_document 的分类不可靠）
        tbands = []
        try:
            import tablelab as TL
            tbands = TL.table_bands(page)
        except Exception:
            tbands = []

        def _keep(p):
            if p.kind in (KIND_BODY, KIND_CAPTION, KIND_NOTE):
                return True
            if p.kind == KIND_TABLE:
                if not tbands:
                    return False
                return any(t0 - 2 <= p.bbox[1] and p.bbox[3] <= t1 + 2
                           for (t0, t1) in tbands)
            return False

        blocks = [(p, p.bbox[0], p.bbox[2]) for p in per_page.get(pno, []) if _keep(p)]
        # **图层优先**：图表/表格所占矩形是唯一真相。
        # 表格带按栏切开（否则会把两栏都堵死 → 顺延暴涨、页面留白）。
        col_ranges = _two_cols(regions, page.rect.width) if any(
            r.kind == "two" for r in regions) else [(page.rect.width * 0.08,
                                                     page.rect.width * 0.92)]
        layer = visual_layer(page, regions, col_ranges)
        flow, inside = split_blocks(blocks, layer)
        blocks = flow
        # 图注已被"图页"带着翻译绘制，正文流里不能再画一遍（否则第 29 页那种重复）
        cap_rects = []
        try:
            for clip, cap_rect, _ct, _k in SL.figure_regions_for(
                    page, drop_keys=drop, margin_bands_=mbands):
                if cap_rect is not None and cap_rect.height > 2:
                    cap_rects.append(pymupdf.Rect(cap_rect))
        except Exception:
            cap_rects = []
        if cap_rects:
            def _is_dup_cap(p):
                r = pymupdf.Rect(p.bbox)
                for cr in cap_rects:
                    if (r & cr).get_area() > 0.5 * max(1.0, r.get_area()):
                        return True
                return False
            blocks = [(p, x0, x1) for (p, x0, x1) in blocks if not _is_dup_cap(p)]
        # 合并被 PDF 切断的片段（否则参考文献页会出现 "nd"/"ation" 这类孤立译文）
        blocks = merge_continuations(blocks)
        # 剔除已随图页绘制的整宽图注（否则会被放进左右两栏、挤掉双栏正文）
        blocks = drop_fig_captions(blocks, regions, page.rect.width)
        mp = MapPage(pno, page.rect.width, page.rect.height, regions, blocks)
        mp.layer = layer
        mp.inside = inside
        mp.cap_rects = cap_rects
        maps.append(mp)
        if on_page:
            try:
                on_page(i + 1, len(src_pages), "原第 %d 页 · %s" % (pno + 1, mp.kind))
            except Exception:
                pass

    pending = []
    for mp in maps:
        pno = mp.pno
        page = doc[pno]
        pw, ph = mp.pw, mp.ph
        np = out.new_page(width=pw * 2 + GAP, height=ph)
        np.show_pdf_page(pymupdf.Rect(0, 0, pw, ph), doc, pno)
        np.draw_line(pymupdf.Point(pw + GAP / 2, 0), pymupdf.Point(pw + GAP / 2, ph),
                     color=(0.78, 0.78, 0.78), width=0.7)
        ox = pw + GAP

        strips, mode, spans = build_strips(page, mp.regions, mp.blocks, pw)
        stats["pages"] += 1
        stats["two_band_pages" if mode == "two" else "one_pages"] += 1

        taken = []
        foot = []
        defer = []
        full_w = pw - margin                    # 译文横跨时的右边界（右半页内）
        span_x0 = strips[0].x0 if strips else margin

        stats["figures"] += place_figures(np, page, pno, mp.figs, zh_font_path,
                                          cache, taken, log=log, figpages=figpages)

        np.insert_text(pymupdf.Point(ox + margin, margin - 24),
                       "第 %d 页 · 译文%s" % (pno + 1, "（双栏对应）" if mode == "two" else ""),
                       fontname="zhcn", fontfile=zh_font_path, fontsize=8.5,
                       color=(0.45, 0.45, 0.45))

        # 本页只排**本页自己的译文**（上对齐）。
        # 上一页放不下的内容不再塞进本页——而是另起一页（左栏空白），见收尾循环。
        cont_still = []
        if pending:
            rest = list(pending)
            pending = []
        else:
            rest = []
        y_after_cont = margin

        all_strips = []
        for st in strips:
            s2 = Strip(ox + st.x0, ox + st.x1, st.name)
            s2.items = st.items
            all_strips.append(s2)
        place_items(np, all_strips, zh_font, zh_font_path, ph, margin, taken,
                    mp.figs, foot, defer, spans,
                    span_x0=ox + span_x0, span_x1=ox + full_w,
                    layer=[pymupdf.Rect(lr.x0 + ox, lr.y0, lr.x1 + ox, lr.y1)
                           for lr in mp.layer],
                    flow_mode=is_flow_page(mp.blocks, ph),
                    start_y=y_after_cont)
        # 本页顺延的处理：**紧跟在本页之后**插入续页（而不是统一堆到文末）。
        # 续页左栏放原页（保持"左原文/右译文"的形态），右栏从页顶连续排译文。
        pending = [(n, p, k) for (n, p, k) in defer] + rest + list(cont_still)
        if foot:
            np.insert_text(pymupdf.Point(ox + margin, ph - 18),
                           "→ " + "；".join(sorted(set(foot))[:3]),
                           fontname="zhcn", fontfile=zh_font_path, fontsize=7.5,
                           color=(0.35, 0.4, 0.55))
        guard_c = 0
        while pending and guard_c < 20:
            guard_c += 1
            stats["cont_pages"] += 1
            # **续页左栏保持空白**（不放任何原文画面）：用户在 PDF 阅读器里
            # 一眼就能看出"这是译文续页"，不会被误认成源页。
            npc = out.new_page(width=pw * 2 + GAP, height=ph)
            npc.draw_line(pymupdf.Point(pw + GAP / 2, 0), pymupdf.Point(pw + GAP / 2, ph),
                          color=(0.78, 0.78, 0.78), width=0.7)
            npc.insert_text(pymupdf.Point(ox + margin, margin - 24),
                            "译文续页 · 接第 %d 页译文（左栏留空）" % (pno + 1),
                            fontname="zhcn", fontfile=zh_font_path, fontsize=8.5,
                            color=(0.45, 0.45, 0.45))
            foot_c = []
            taken_c = []
            _n, pending = render_deferred(
                npc, pending, ox, ph, margin, zh_font, zh_font_path, taken_c, foot_c,
                strip_specs=[(st.x0, st.x1, st.name) for st in strips], flow=True)
            stats["deferred_pieces"] += _n
            if foot_c:
                npc.insert_text(pymupdf.Point(ox + margin, ph - 18),
                                "→ " + "；".join(sorted(set(foot_c))[:3]),
                                fontname="zhcn", fontfile=zh_font_path, fontsize=7.5,
                                color=(0.35, 0.4, 0.55))

    # 顺延内容已在每页之后就地插入续页，这里只记录是否还有残留（应为 0）
    stats["leftover"] = len(pending)

    saved = save_doc(out, out_path)
    return saved, stats
