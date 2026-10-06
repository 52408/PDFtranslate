# -*- coding: utf-8 -*-
"""tablelab.py —— 表格结构识别（行列还原）。

论文里的表格通常是"矢量文字格"，没有边框可依赖，所以按几何聚类：
  1) 由"Table N:" 图注定位表格带（图注上方的横带）
  2) 带内文字块按 y 聚类成**行**（容差 = 行高的一半）
  3) 每行内按 x 排序成**列**，同时记录每格的框（用于红删 + 中文回贴）
"""

import re

import pymupdf

TAB_CAP = re.compile(r"^(tab(?:le)?\.?|表)\s*\.?\s*\d+\s*[:：]", re.I)
FIG_CAP = re.compile(r"^(fig(?:ure)?\.?|图)\s*\.?\s*\d+\s*[:：]", re.I)


def table_captions(page):
    """返回本页的表格标题块 [(rect, text)]。"""
    out = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        lines = []
        for ln in blk.get("lines", []):
            s = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            if s.strip():
                lines.append(s)
        t = "\n".join(lines).strip()
        if t and TAB_CAP.match(t):
            out.append((pymupdf.Rect(blk["bbox"]), t))
    return out


def cells_in_band(page, band):
    """取出带内的文字块（每块算一个"格"）。返回 [(rect, text, size)]。"""
    out = []
    for blk in page.get_text("dict", clip=band)["blocks"]:
        if blk.get("type") != 0:
            continue
        lines, sizes = [], []
        for ln in blk.get("lines", []):
            s = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            if s.strip():
                lines.append(s)
                sizes.extend(round(sp.get("size", 0), 1) for sp in ln.get("spans", []))
        t = "\n".join(lines).strip()
        if not t:
            continue
        size = max(set(sizes), key=sizes.count) if sizes else 10.0
        out.append((pymupdf.Rect(blk["bbox"]), t, size))
    return out


def group_rows(cells, tol_ratio=0.6):
    """按 y 把格聚成行；返回 [[(rect, text, size), ...], ...]（行内按 x 排序）。"""
    if not cells:
        return []
    items = sorted(cells, key=lambda c: (c[0].y0, c[0].x0))
    rows = []
    for rect, text, size in items:
        placed = False
        for row in rows:
            ref = row["ref"]
            tol = max(2.5, min(rect.height, ref.height) * tol_ratio)
            if abs(rect.y0 - ref.y0) <= tol:
                row["cells"].append((rect, text, size))
                # 行的参考 y 取平均，避免逐格漂移
                row["ref_y"] = (row["ref_y"] * (len(row["cells"]) - 1) + rect.y0) / len(row["cells"])
                placed = True
                break
        if not placed:
            rows.append({"ref": rect, "ref_y": rect.y0, "cells": [(rect, text, size)]})
    for row in rows:
        row["cells"].sort(key=lambda c: c[0].x0)
    rows.sort(key=lambda r: r["ref_y"])
    return [r["cells"] for r in rows]


def column_bounds(page, band, min_gap=10.0):
    """推断表格的列边界。

    做法：收集带内所有**行内 span** 的横向区间，合并成若干"列区间"，
    相邻区间之间的空隙中点即为列边界。返回 [边界值, ...]（升序，不含首尾）。
    """
    intervals = []
    for blk in page.get_text("dict", clip=band)["blocks"]:
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                if sp.get("text", "").strip():
                    r = pymupdf.Rect(sp["bbox"])
                    intervals.append((r.x0, r.x1))
    if not intervals:
        return []
    intervals.sort()
    merged = [list(intervals[0])]
    for x0, x1 in intervals[1:]:
        if x0 - merged[-1][1] <= 1.0:          # 相邻/重叠 → 同一列
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    bounds = []
    for a, b in zip(merged, merged[1:]):
        gap = b[0] - a[1]
        if gap >= min_gap:
            bounds.append((a[1] + b[0]) / 2)
    return bounds


def split_row(row, bounds):
    """把一行里的文本按列边界切分：在边界处空格最少的位置断开。

    返回 [(rect, text)] —— 每个格一个框（框按该段文字在行内的比例估算）。
    """
    if not bounds:
        return [(r, t) for r, t, _s in row]
    out = []
    for rect, text, _size in row:
        if rect.width <= 0 or not text.strip():
            continue
        pieces = []
        cuts = [b for b in bounds if rect.x0 + 4 < b < rect.x1 - 4]
        if not cuts:
            out.append((rect, text))
            continue
        # 按字符位置估算每个字符的横向位置，在最近的边界处断开
        n = len(text)
        last = 0
        for b in cuts:
            frac = (b - rect.x0) / rect.width
            idx = max(last + 1, min(n, int(round(frac * n))))
            # 优先在空格处断开
            sp_idx = text.rfind(" ", last, min(n, idx + 3))
            if sp_idx > last:
                idx = sp_idx
            pieces.append((last, idx))
            last = idx
        if last < n:
            pieces.append((last, n))
        for a, b2 in pieces:
            seg = text[a:b2].strip()
            if not seg:
                continue
            # 估算该段在行内的框
            x0 = rect.x0 + rect.width * (a / n)
            x1 = rect.x0 + rect.width * (b2 / n)
            out.append((pymupdf.Rect(x0, rect.y0, x1, rect.y1), seg))
    return out



def figure_caption_bottoms(page):
    """Return bottom y of figure captions on the page.

    A table band often extends upward far enough to swallow the figure caption
    immediately above it.  Keep the table band below that caption.
    """
    out = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        lines = []
        for ln in blk.get("lines", []):
            t = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            if t.strip():
                lines.append(t)
        text = "\n".join(lines).strip()
        if text and FIG_CAP.match(text):
            out.append(pymupdf.Rect(blk["bbox"]).y1)
    return out


def _span_lines(page, band):
    """Collect text spans and cluster them into physical lines."""
    spans = []
    for blk in page.get_text("dict", clip=band)["blocks"]:
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                t = sp.get("text", "").strip()
                if t:
                    spans.append((pymupdf.Rect(sp["bbox"]), t, sp.get("size", 10.0)))
    lines = []
    for r, t, size in sorted(spans, key=lambda x: (x[0].y0, x[0].x0)):
        placed = False
        for line in lines:
            ref = line["rect"]
            tol = max(1.5, min(r.height, ref.height) * 0.5)
            if abs(r.y0 - ref.y0) <= tol:
                line["items"].append((r, t, size))
                line["rect"] |= r
                placed = True
                break
        if not placed:
            lines.append({"rect": pymupdf.Rect(r), "items": [(r, t, size)]})
    for line in lines:
        line["items"].sort(key=lambda x: x[0].x0)
    lines.sort(key=lambda l: l["rect"].y0)
    return lines


def grid_rows(page, band):
    """Special extractor for the Pilot/Platform/.../Challenges table.

    This table has no vertical rules and PyMuPDF groups the whole body into
    one text block.  Reconstruct it from physical text lines instead.
    """
    lines = _span_lines(page, band)
    if not lines:
        return None
    header_text = " ".join(t for _r, t, _s in lines[0]["items"])
    if "Pilot" not in header_text or "Challenges" not in header_text:
        return None

    def find_x(label):
        for r, t, _s in lines[0]["items"]:
            if t.strip().lower().startswith(label):
                return r.x0
        return None

    starts = [find_x("pilot"), find_x("platform"), find_x("particip"),
              find_x("no."), find_x("challenges")]
    if any(x is None for x in starts):
        return None
    starts = sorted(starts)
    bounds = [(starts[i] + starts[i + 1]) / 2.0 for i in range(len(starts) - 1)]

    def col_of(x):
        for i, b in enumerate(bounds):
            if x < b:
                return i
        return len(starts) - 1

    rows = []
    current = None
    for line_no, line in enumerate(lines):
        items = line["items"]
        pilot_txt = " ".join(t for r, t, _s in items if col_of(r.x0) == 0).strip()
        new_row = (line_no == 0 or (len(pilot_txt) == 1 and pilot_txt.isalpha()))
        if new_row or current is None:
            current = {i: [] for i in range(len(starts))}
            rows.append(current)
        for r, t, size in items:
            current[col_of(r.x0)].append((r, t, size))

    out = []
    for row in rows:
        cells = []
        for i in range(len(starts)):
            items = row.get(i) or []
            if not items:
                continue
            rect = pymupdf.Rect(items[0][0])
            for r, _t, _s in items[1:]:
                rect |= r
            lines = []
            for r, t, size in items:
                placed_line = False
                for ln in lines:
                    if abs(ln["y"] - r.y0) <= 2.0:
                        ln["items"].append((r, t, size))
                        placed_line = True
                        break
                if not placed_line:
                    lines.append({"y": r.y0, "items": [(r, t, size)]})
            lines.sort(key=lambda l: l["y"])
            parts = []
            for ln in lines:
                parts.append(" ".join(t for _r, t, _s in
                                      sorted(ln["items"], key=lambda x: x[0].x0)))
            text = " ".join(parts).strip()
            if text:
                cells.append((rect, text))
        if cells:
            out.append(cells)
    return out if len(out) >= 2 else None


def grid_rows_numeric(page, band):
    """Extractor for the numeric balance table (Treatment/Control/Difference)."""
    lines = _span_lines(page, band)
    if not lines:
        return None
    header_text = " ".join(t for _r, t, _s in lines[0]["items"])
    if not all(k in header_text for k in ("Treatment", "Control", "Difference")):
        return None

    def start_for(label, default):
        for line in lines:
            for r, t, _s in line["items"]:
                if t.strip().startswith(label):
                    return r.x0
        return default

    tx = start_for("Treatment", 272.3)
    cx = start_for("Control", 349.1)
    dx = start_for("Difference", 413.3)

    def col_of(x):
        if x < tx - 5.0:
            return 0
        if x < cx - 5.0:
            return 1
        if x < dx - 5.0:
            return 2
        return 3

    out = []
    for line in lines:
        cells = {}
        for r, t, size in line["items"]:
            cells.setdefault(col_of(r.x0), []).append((r, t, size))
        row = []
        for i in range(4):
            items = cells.get(i) or []
            if not items:
                continue
            rect = pymupdf.Rect(items[0][0])
            for r, _t, _s in items[1:]:
                rect |= r
            text = " ".join(t for _r, t, _s in sorted(items, key=lambda x: x[0].x0)).strip()
            if text:
                row.append((rect, text))
        if row:
            out.append(row)
    return out if len(out) >= 2 else None


class GridRows(list):
    """Marker: rows produced by a structural span grid (safe to translate fully)."""
    pass


def _cell_from_items(items):
    rect = pymupdf.Rect(items[0][0])
    for r, _t, _s in items[1:]:
        rect |= r
    lines = []
    for r, t, size in items:
        placed = False
        for ln in lines:
            if abs(ln["y"] - r.y0) <= 2.0:
                ln["items"].append((r, t, size))
                placed = True
                break
        if not placed:
            lines.append({"y": r.y0, "items": [(r, t, size)]})
    lines.sort(key=lambda l: l["y"])
    parts = []
    for ln in lines:
        parts.append(" ".join(t for _r, t, _s in
                              sorted(ln["items"], key=lambda x: x[0].x0)))
    return rect, " ".join(parts).strip()


def grid_rows_generic(page, band):
    """Generic span grid fallback for tables not covered by special schemas.

    Column intervals are inferred from all span x-ranges; rows start when a
    line has content in the first column and the current row already has one,
    or when the vertical gap is clearly larger than one line.
    """
    lines = _span_lines(page, band)
    if len(lines) < 2:
        return None
    intervals = []
    for line in lines:
        for r, _t, _s in line["items"]:
            intervals.append((r.x0, r.x1))
    if not intervals:
        return None
    intervals.sort()
    merged = [list(intervals[0])]
    for x0, x1 in intervals[1:]:
        if x0 - merged[-1][1] <= 3.0:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    if len(merged) < 2 or len(merged) > 12:
        return None
    bounds = [(merged[i][1] + merged[i + 1][0]) / 2.0
              for i in range(len(merged) - 1)]

    def col_of(x):
        for i, b in enumerate(bounds):
            if x < b:
                return i
        return len(merged) - 1

    rows = []
    current = None
    prev = None
    for line in lines:
        col_items = {}
        for r, t, size in line["items"]:
            col_items.setdefault(col_of(r.x0), []).append((r, t, size))
        has_col0 = bool(col_items.get(0))
        row_has_col0 = bool(current and any(i == 0 for i, _items in current))
        new_row = current is None
        if not new_row and prev is not None:
            gap = line["rect"].y0 - prev["rect"].y1
            if gap > max(4.0, 0.75 * prev["rect"].height) or (has_col0 and row_has_col0):
                new_row = True
        if new_row:
            current = []
            rows.append(current)
        for i, items in col_items.items():
            current.append((i, items))
        prev = line

    out = []
    for row in rows:
        by_col = {}
        for i, items in row:
            by_col.setdefault(i, []).extend(items)
        cells = []
        for i in range(len(merged)):
            items = by_col.get(i) or []
            if not items:
                continue
            rect, text = _cell_from_items(items)
            if text:
                cells.append((rect, text))
        if cells:
            out.append(cells)
    return out if len(out) >= 2 else None


def table_band_top(page, cap_rect, default):
    """Extend a table band upward to its outer top horizontal rule."""
    pw = page.rect.width
    horiz = []
    for dr in page.get_drawings():
        for it in dr.get("items", []):
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.y - b.y) < 0.5:
                    x0, x1 = sorted([a.x, b.x])
                    if x1 - x0 > pw * 0.25:
                        horiz.append((min(a.y, b.y), x0, x1))
            elif it[0] == "re":
                r = pymupdf.Rect(it[1])
                if r.height < 3 and r.width > pw * 0.25:
                    horiz.append((r.y0, r.x0, r.x1))
    if not horiz:
        return default
    bottoms = [h for h in horiz if h[0] <= cap_rect.y0 - 5]
    if not bottoms:
        return default
    by, bx0, bx1 = max(bottoms, key=lambda h: h[0])
    same = [h for h in bottoms if abs(h[1] - bx0) < 8 and abs(h[2] - bx1) < 8]
    top = min(same, key=lambda h: h[0]) if same else (by, bx0, bx1)
    return max(0.0, min(default, top[0] - 2))


def _looks_garbled(text):
    toks = re.findall(r"[A-Za-z]+|\S+", text)
    if len(toks) < 6:
        return False
    short = 0
    for t in toks[:6]:
        letters = len(re.sub(r"[^A-Za-z]", "", t))
        if letters <= 2:
            short += 1
    return short >= 4


def fix_garbled_rows(page, band, rows):
    """OCR-correct cells whose PDF text layer is broken (common in some fonts)."""
    try:
        import ocrlab as OL
    except Exception:
        return rows
    for i, row in enumerate(rows):
        if not row:
            continue
        row_rect = pymupdf.Rect(row[0][0])
        for _r, _t in row[1:]:
            row_rect |= _r
        for j, (r, text) in enumerate(row):
            if not _looks_garbled(text):
                continue
            top = band.y0 if i == 0 else max(band.y0, row_rect.y0 - 2)
            clip = pymupdf.Rect(max(0.0, r.x0 - 2), top,
                                min(page.rect.width, r.x1 + 2), row_rect.y1 + 2)
            try:
                ocr = OL.ocr_region(page, clip, zoom=3.0)
            except Exception:
                continue
            parts = []
            for orr, ot, _os in ocr:
                if orr.x1 < r.x0 - 4 or orr.x0 > r.x1 + 4:
                    continue
                parts.append(ot)
            new_text = " ".join(parts).strip()
            if len(new_text) > len(text) + 10:
                row[j] = (r, new_text)
    return rows


def table_cells(doc, pno):
    """返回某页所有表格的 [(cap_rect, cap_text, band, rows)]，rows 为逐格列表。"""
    page = doc[pno]
    fig_bottoms = figure_caption_bottoms(page)
    res = []
    for cap_rect, cap_text in table_captions(page):
        band = pymupdf.Rect(0, max(0, cap_rect.y0 - page.rect.height * 0.55),
                            page.rect.width, cap_rect.y0 - 1)
        above = [y for y in fig_bottoms if y < cap_rect.y0]
        if above:
            band.y0 = max(band.y0, max(above) + 2)
        band.y0 = table_band_top(page, cap_rect, band.y0)
        cells = cells_in_band(page, band)
        if not cells:
            continue
        rows = group_rows(cells)
        old_max_len = max((len(cell[1]) for row in rows for cell in row), default=0)
        numeric_grid = grid_rows_numeric(page, band)
        grid = grid_rows(page, band)
        generic = None
        if numeric_grid is None and grid is None:
            # Only take the generic route when the old block-based result is
            # clearly unreliable (too few rows or a giant merged cell).
            if len(rows) <= 3 or old_max_len > 160:
                generic = grid_rows_generic(page, band)
        if numeric_grid is not None:
            split = GridRows(numeric_grid)
        elif grid is not None:
            split = GridRows(grid)
        elif generic is not None:
            split = GridRows(generic)
        else:
            bounds = column_bounds(page, band)
            split = [[(r, t) for r, t in split_row(row, bounds)] for row in rows]
        if isinstance(split, GridRows):
            fix_garbled_rows(page, band, split)
        res.append((cap_rect, cap_text, band, split))
    return res


def table_bands(page):
    """本页所有**真实表格**的纵向范围 [(y0, y1), ...]。

    用途：extract_document 的 kind 分类会把标题/图内文字误标成 table，
    排版时需要用它来判定"这个块是否真的在表格里"。
    """
    out = []
    try:
        fig_bottoms = figure_caption_bottoms(page)
        for cap_rect, _cap_text in table_captions(page):
            y0 = max(0.0, cap_rect.y0 - page.rect.height * 0.55)
            above = [y for y in fig_bottoms if y < cap_rect.y0]
            if above:
                y0 = max(y0, max(above) + 2)
            out.append((y0, cap_rect.y1))
    except Exception:
        pass
    return out


def in_table_band(rect, bands):
    for (y0, y1) in bands:
        if rect.y0 >= y0 - 2 and rect.y1 <= y1 + 2:
            return True
    return False


def analyze(doc, pno):
    """输出某页的表格结构报告。"""
    page = doc[pno]
    tabs = table_cells(doc, pno)
    print("原第 %d 页：%d 个表格" % (pno + 1, len(tabs)))
    for cap_rect, cap_text, band, rows in tabs:
        print("  标题: %s" % cap_text[:50].replace("\n", " "))
        print("  表格带 y=%.0f~%.0f | 还原 %d 行" % (band.y0, band.y1, len(rows)))
        for i, row in enumerate(rows[:12]):
            txt = " | ".join((" ".join(t.split()))[:18] for _r, t in row)
            print("    行%2d(%d格): %s" % (i, len(row), txt[:115]))


if __name__ == "__main__":
    import sys
    d = pymupdf.open(sys.argv[1])
    for a in sys.argv[2:] or ["1"]:
        analyze(d, int(a) - 1)
