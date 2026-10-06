# -*- coding: utf-8 -*-
"""layout2.py —— 新版双语排版：正文 / 注释 / 图表 分流，再分别翻译、分别排版。

解决的问题
  1) 图表坐标轴数字与标签（4~7pt 矢量文本）原本被当正文塞进右栏，读起来像乱码
  2) 页脚注释（7~8.5pt，∗/†/1/2 开头）原本插在正文中间，割断跨页段落的接续

做法
  * 分块：正文 body / 注释 note / 图表 fig / 图表标题 caption（按字号 + 位置 + 形态判定）
  * 分译：只有 body / caption / note 送翻译；纯数字和图内短标签直接丢弃（不送、不显示）
  * 排版：右栏固定三段顺序 —— 正文流（跨页连续）→ [注] 注释流 → [图] 图表标题
  * 正文跨页不断：一页装不下的正文自动接到下一页右栏顶部，并标注「接上页 / 接下页」
"""

import re
from dataclasses import dataclass, field

import pymupdf

import os as _os

_DEBUG = bool(_os.environ.get("PDFTRANS_DEBUG"))


def log(msg):
    print(msg, flush=True)

# ---------------------------------------------------------------- 分类常量

KIND_BODY = "body"
KIND_NOTE = "note"
KIND_CAPTION = "caption"
KIND_TABLE = "table"      # 表格单元格（随表格标题一起排进 [表] 区）
KIND_FIG = "fig"          # 图内文字（不翻译、不单独显示）

NUMISH = re.compile(r"^[\s\d\.\,\-\+%\(\)\[\]/*=<>~±:;'\"°×−–—|\\]+$")
SYMBOLIC = re.compile(r"^[^\w\u4e00-\u9fff]{1,10}$")
NOTE_MARK = re.compile(r"^[\*\u2217\u2020\u2021\u00a7\u00b6\d]{1,3}(?=[A-Z\u4e00-\u9fff(])")
# 真标题必须带冒号（"Figure 5: ..."），否则正文里的 "Figure 5 presents ..." 会被误判
FIG_NUM_HEAD = re.compile(r"^(fig(?:ure)?\.?|tab(?:le)?\.?|图|表|算法|algorithm)\s*\.?\s*\d+\s*[:：]",
                          re.I)
NOTE_HEAD = re.compile(r"^(note|footnote|注|注释)[:：\s]", re.I)
CAPTION_HINT = re.compile(r"^(fig(?:ure)?\.?\s*\d|table\s*\d)", re.I)


@dataclass
class Piece:
    """一个内容块。"""
    page: int
    bbox: tuple
    text: str
    size: float
    kind: str = KIND_BODY
    translation: str = None
    start_line: int = 0
    table_rows: list = field(default_factory=list)   # 表格标题块带上按行还原的表格摘要
    table_cells: list = field(default_factory=list)  # 该表格包含的单元格块（排版时用译文拼行）
    table_owner: tuple = None                        # 该单元格属于哪个表格标题

    @property
    def is_translatable(self):
        return self.kind in (KIND_BODY, KIND_NOTE, KIND_CAPTION, KIND_TABLE)

    def body_text(self):
        return self.translation if self.translation else self.text


# ---------------------------------------------------------------- 抽取与分类

def analyze_page(page, pno):
    """返回该页的 (blocks, stats)。blocks 已按类别标注。"""
    ph = page.rect.height
    pw = page.rect.width
    raw = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        lines, sizes = [], []
        for ln in blk.get("lines", []):
            s = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            if s.strip():
                lines.append(s)
                sizes.extend(round(sp.get("size", 0), 1) for sp in ln.get("spans", []))
        text = "\n".join(lines).strip()
        if not text:
            continue
        size = max(set(sizes), key=sizes.count) if sizes else 10.0
        raw.append({"rect": pymupdf.Rect(blk["bbox"]), "text": text, "size": size})

    # 正文基准字号 = 页内出现最多的字号（按字符数加权）
    weight = {}
    for b in raw:
        weight[round(b["size"], 1)] = weight.get(round(b["size"], 1), 0) + len(b["text"])
    body_size = max(weight, key=weight.get) if weight else 10.0

    blocks, stats = [], {"body": 0, "note": 0, "caption": 0,
                         "fig_dropped": 0, "junk_dropped": 0, "table_cells": 0}
    # ---- 第一趟：先用几何方法找出「表格标题」，并圈出它上方的表格横带 ----
    # 表格单元格（"定期/经常"、"6.52 分钟"…）混进正文流会变成一行一个词的碎片，
    # 所以先把横带里的块整块划走，再统一按行还原成 [图/表] 区的表格摘要。
    table_bands = []
    for b in raw:
        t = b["text"].strip()
        if re.match(r"^(tab|表)", t, re.I) and FIG_NUM_HEAD.match(t):
            r = b["rect"]
            table_bands.append((pymupdf.Rect(0, max(0, r.y0 - ph * 0.55), pw, r.y0), r))
    absorbed = {}          # raw 块下标 -> 所属表格标题的 rect
    if table_bands:
        for i, b in enumerate(raw):
            t = b["text"].strip()
            if in_margin_of(b["rect"], ph, pw) and len(t) <= 90:
                continue
            for band, capr in table_bands:
                # 只按纵向范围判断：表格宽度通常横跨整页，横向包含关系不可靠
                if band.y0 <= b["rect"].y0 <= band.y1 and len(t) <= 1500:
                    if re.match(r"^(fig(?:ure)?\.?|图)\s*\.?\s*\d+\s*[:：]", t, re.I):
                        continue
                    absorbed[i] = capr
                    break

    for bi, b in enumerate(raw):
        text, size, rect = b["text"], b["size"], b["rect"]
        t = text.strip()
        in_bottom = rect.y0 > ph * 0.60
        in_margin = in_margin_of(rect, ph, pw)

        kind = KIND_BODY
        # 1) 图内文字：明显小于正文
        if size <= body_size - 2.6:
            kind = KIND_FIG
        # 2) 页脚注释：略小于正文 + 位于页面下部 + 有 ∗/†/数字 引导
        elif size <= body_size - 1.2 and in_bottom and NOTE_MARK.match(t):
            kind = KIND_NOTE
        # 3) 图表标题：以 Figure/Table 开头（要求带冒号，避免正文里的 "Figure 5 presents"）
        elif FIG_NUM_HEAD.match(t):
            kind = KIND_CAPTION
        elif size <= body_size - 1.2 and in_bottom and len(t) < 40:
            kind = KIND_FIG
        # 4) 页边短文本（页码、页眉）直接丢
        if in_margin and len(t) <= 90:
            kind = "junk"

        # 5) 图表坐标轴数字/符号：短且纯数字或纯符号 -> 图内噪音
        if kind == KIND_BODY and len(t) <= 12 and (NUMISH.match(t) or SYMBOLIC.match(t)):
            kind = KIND_FIG

        # 6) 短碎片且不成句：轴标签等零碎文字，不送正文
        #    注意排除小节标题（"2 Background"），它们是正文结构
        if kind == KIND_BODY and len(t) <= 60:
            has_cjk = any("\u4e00" <= c <= "\u9fff" for c in t)
            sentence = bool(re.search(r"[.。！？!?；;]", t)) or t.count(" ") >= 12
            is_heading = len(t) <= 25 and bool(re.match(r"^\d{1,2}(\.\d+)*\s+\S", t))
            if not has_cjk and not sentence and not is_heading:
                kind = KIND_FIG if not absorbed else KIND_TABLE

        # 7) 落在表格横带里的块：划为表格单元格
        if bi in absorbed and kind in (KIND_BODY, KIND_TABLE, KIND_FIG):
            kind = KIND_TABLE

        if kind == KIND_FIG:
            stats["fig_dropped"] = stats.get("fig_dropped", 0) + 1
            continue
        if kind == "junk":
            stats["junk_dropped"] = stats.get("junk_dropped", 0) + 1
            continue
        stats[kind] = stats.get(kind, 0) + 1
        piece = Piece(pno, tuple(rect), text, size, kind)
        if bi in absorbed:
            piece.table_owner = absorbed[bi]
        blocks.append(piece)

    # 表格摘要挂到对应的表格标题块上（保存单元格列表，排版时用译文按行拼）
    by_owner = {}
    for p in blocks:
        if p.kind == KIND_TABLE and getattr(p, "table_owner", None) is not None:
            by_owner.setdefault(tuple(p.table_owner), []).append(p)
    for p in blocks:
        if p.kind == KIND_CAPTION and re.match(r"^(tab|表)", p.text.strip(), re.I):
            cells = by_owner.get(tuple(p.bbox))
            if cells:
                p.table_cells = cells
                p.table_rows = group_rows(cells)
                stats["table_cells"] = stats.get("table_cells", 0) + len(cells)
    blocks.sort(key=lambda p: (round(p.bbox[1] / 8), p.bbox[0]))
    return blocks, stats


def in_margin_of(rect, ph, pw):
    return (rect.y1 < ph * 0.055 or rect.y0 > ph * 0.93
            or rect.x1 < pw * 0.06 or rect.x0 > pw * 0.94)


def group_rows(cells, tol=4.0, max_cols=8, use_translation=True):
    """把表格单元格按纵坐标归并成行，返回 ["治疗组(%) | 对照组(%)", ...]。

    use_translation=True 时优先使用译文（排版阶段调用）；否则用原文（预览用）。
    """
    rows = []
    for p in sorted(cells, key=lambda c: (c.bbox[1], c.bbox[0])):
        raw = (p.translation if (use_translation and p.translation) else p.text)
        txt = " ".join(str(raw).split())
        if not txt:
            continue
        placed = False
        for row in rows:
            if abs(row["y"] - p.bbox[1]) <= tol:
                row["items"].append((p.bbox[0], txt))
                placed = True
                break
        if not placed:
            rows.append({"y": p.bbox[1], "items": [(p.bbox[0], txt)]})
    out = []
    for row in sorted(rows, key=lambda r: r["y"]):
        items = [t for _, t in sorted(row["items"])][:max_cols]
        line = " | ".join(items).strip()
        if line:
            out.append(line)
    return out


def extract_document(doc, pages, on_page=None):
    """按页抽取并分类。on_page(已完成, 总数, 说明) 供进度条，可为 None。"""
    per_page = {}
    total = {"body": 0, "note": 0, "caption": 0, "table": 0,
             "fig_dropped": 0, "junk_dropped": 0, "table_cells": 0}
    _pages = list(pages)
    _n = len(_pages)
    for _i, pno in enumerate(_pages):
        blocks, stats = analyze_page(doc[pno], pno)
        per_page[pno] = blocks
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v
        if on_page:
            try:
                on_page(_i + 1, _n, "原第 %d 页" % (pno + 1))
            except TypeError:
                on_page(_i + 1)
            except Exception:
                pass
    return per_page, total


def streams(per_page, pages):
    """把各页的块接成各条流：正文 / 注释 / 图表标题 / 表格单元格。"""
    def chain(kind):
        out = []
        for pno in pages:
            out.extend([b for b in per_page[pno] if b.kind == kind])
        return out

    return {
        KIND_BODY: chain(KIND_BODY),
        KIND_NOTE: chain(KIND_NOTE),
        KIND_CAPTION: chain(KIND_CAPTION),
        KIND_TABLE: chain(KIND_TABLE),
    }


def page_of_piece(pieces, piece):
    """某个块属于原文档第几页（用于续页标注）。"""
    return piece.page


# ---------------------------------------------------------------- 排版

@dataclass
class Stream:
    """一条内容流，记录当前排到哪一块、哪一行。"""
    kind: str
    pieces: list = field(default_factory=list)
    idx: int = 0
    line: int = 0

    def done(self):
        return self.idx >= len(self.pieces)

    def current(self):
        return None if self.done() else self.pieces[self.idx]

    def advance_line(self, n=1):
        self.line += n

    def advance_piece(self):
        self.idx += 1
        self.line = 0


def wrap_text(text, font, size, width):
    lines = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        cur = ""
        for ch in para:
            if font.text_length(cur + ch, size) > width and cur:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
    return lines


def measure_page(pieces, font, font_size, text_width, with_figures=True):
    """估算这些块排下来需要多高（行数 × 行距 + 段间距）。"""
    total = 0.0
    for b in pieces:
        if getattr(b, "is_figure", False):
            if with_figures:
                total += 12.0
            continue
        body = b.translation if getattr(b, "translation", None) else b.text
        fs = font_size if len(body) < 900 else font_size - 0.4
        lines = wrap_text(body, font, fs, text_width)
        total += len(lines) * fs * 1.42 + fs * 0.71
    return total


def fit_block(page, piece, font, zh_font, cjk_name, cfg, y, limit, start=0, draw=True):
    """在 [y, limit) 内完整排一个正文块（可从第 start 行开始）。

    先按常规字号试；排不下就整体降字号（最低 7.0pt）；仍排不下则返回 False，
    由调用方把这一块挪到下一页。draw=False 时只做纯测量，不写入页面。
    """
    right_x0, margin = cfg["right_x0"], cfg["margin"]
    body = piece.body_text()
    if not body:
        return y
    best = None
    sizes = [cfg["font_size"], cfg["font_size"] - 0.4, cfg["font_size"] - 0.8,
             cfg["font_size"] - 1.2, 7.6, 7.0]
    for fs in sizes:
        if fs < 7.0:
            continue
        lines = wrap_text(body, font, fs, cfg["text_width"])
        if start:
            # 顺延部分按同一字号重新换行，起始行号可能略有偏移，取尽量接近的位置
            lines = lines[start:]
        lead = fs * 1.42
        if y + len(lines) * lead <= limit:
            best = (fs, lines, lead)
            break
    if best is None:
        return None
    fs, lines, lead = best
    if not draw:
        return y + len(lines) * lead + lead * 0.35
    for ln in lines:
        page.insert_text(pymupdf.Point(right_x0 + margin, y), ln,
                         fontname=cjk_name, fontfile=zh_font, fontsize=fs,
                         color=cfg["color"])
        y += lead
    return y + lead * 0.35


def render_stream(stream, page, font, zh_font, cjk_name, cfg, y, limit,
                  cjk_label, label=None, min_lines=1, placed=None,
                  font_sizes=None, top_free=False):
    """把一条流从 y 开始排到 limit。

    返回 (结束 y, 本页渲染的块数, 是否还有剩余)。
    top_free=True 时若整体放得下，会自动上移到紧贴 limit 的位置（避免底部留大片空白）。
    """
    right_x0, margin = cfg["right_x0"], cfg["margin"]
    if stream.done():
        return y, 0, False
    label_h = 13 if label else 0
    sizes = font_sizes or [cfg["font_size"]]

    if top_free:
        need_h = None
        for fs_try in sizes:
            h = measure_page(stream.pieces, font, fs_try, cfg["text_width"], False)
            if y + label_h + h <= limit:
                need_h = label_h + h
                break
        if need_h is not None:
            y = max(y, limit - need_h)

    if label:
        cjk_label(page, pymupdf.Point(right_x0 + margin, y), label, 8.2, (0.55, 0.45, 0.25))
        y += 13

    sizes = font_sizes or [cfg["font_size"]]
    # 探测：整条流若一条都排不下，逐档降字号；仍不行则只用最小字号排（剩余转附录）
    for fs_try in sizes:
        stream.line = 0
        if stream.idx >= len(stream.pieces):
            break
        p = stream.current()
        body = p.body_text()
        lines = wrap_text(body, font, fs_try, cfg["text_width"])
        if lines and y + len(lines) * fs_try * 1.42 <= limit:
            break

    used = 0
    while not stream.done():
        p = stream.current()
        fs = sizes[-1]
        lines = None
        for cand in sizes:
            body = p.body_text()
            trial = wrap_text(body, font, cand, cfg["text_width"])
            lead_try = cand * 1.42
            if (y + len(trial) * lead_try <= limit) or cand == sizes[-1]:
                fs, lines = cand, trial
                break
        body = p.body_text()
        lines = wrap_text(body, font, fs, cfg["text_width"])
        lead = fs * 1.42
        start = stream.line
        drawn = 0
        for i in range(start, len(lines)):
            if y + lead > limit:
                break
            page.insert_text(pymupdf.Point(right_x0 + margin, y), lines[i],
                             fontname=cjk_name, fontfile=zh_font, fontsize=fs,
                             color=cfg["color"])
            y += lead
            drawn += 1
        stream.advance_line(drawn)
        if start + drawn < len(lines):
            return y, used, True          # 本页排满
        if placed is not None:
            placed.add(id(p))
        y += lead * 0.45
        stream.advance_piece()
        used += 1
    return y, used, False


def render_bilingual2(doc, per_page, pages, out_path, zh_font_path,
                      font_size=9.5, zoom=2.0, raster=False,
                      show_caption=True, show_note=True, max_continuations=8,
                      figpages=None, caption_zone=None, on_page=None):
    """分流排版。返回 (saved_path, 统计信息)。on_page(已完成, 总数, 说明) 供进度条。"""
    out = pymupdf.open()
    font = pymupdf.Font(fontfile=zh_font_path)
    cjk_name = "zhcn"
    margin, gap = 46, 16

    def cjk_label(page, point, text, size=8.2, color=(0.5, 0.5, 0.5)):
        """用嵌入中文字体写标签，避免 helv 画中文变成空框。"""
        page.insert_text(point, text, fontname=cjk_name, fontfile=zh_font_path,
                         fontsize=size, color=color)

    st = streams(per_page, pages)
    body = Stream(KIND_BODY, st[KIND_BODY])
    notes = Stream(KIND_NOTE, st[KIND_NOTE])
    caps = Stream(KIND_CAPTION, st[KIND_CAPTION])
    tables = Stream(KIND_TABLE, st[KIND_TABLE])
    placed = set()          # 已成功排入文档的块 id

    def new_half(pno, raster_pix=None):
        page = doc[pno]
        pw, ph = page.rect.width, page.rect.height
        W = pw * 2 + gap
        np = out.new_page(width=W, height=ph)
        left = pymupdf.Rect(0, 0, pw, ph)
        if raster:
            pix = raster_pix or page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            if pix.n > 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            try:
                pix.set_compression("jpeg", jpg_quality=88)
            except Exception:
                pass
            np.insert_image(left, pixmap=pix)
        else:
            np.show_pdf_page(left, doc, pno)
        np.draw_line(pymupdf.Point(pw + gap / 2, 0), pymupdf.Point(pw + gap / 2, ph),
                     color=(0.78, 0.78, 0.78), width=0.7)
        cfg = {
            "right_x0": pw + gap, "margin": margin,
            "text_width": pw - margin * 2, "font_size": font_size,
            "color": (0, 0, 0),
        }
        return np, cfg, pw, ph

    def new_half_blank(pno):
        """Blank left half + right column, for an immediate continuation page."""
        page = doc[pno]
        pw, ph = page.rect.width, page.rect.height
        W = pw * 2 + gap
        np = out.new_page(width=W, height=ph)
        np.draw_line(pymupdf.Point(pw + gap / 2, 0), pymupdf.Point(pw + gap / 2, ph),
                     color=(0.78, 0.78, 0.78), width=0.7)
        cfg = {
            "right_x0": pw + gap, "margin": margin,
            "text_width": pw - margin * 2, "font_size": font_size,
            "color": (0, 0, 0),
        }
        return np, cfg, pw, ph

    stats = {"continuation_pages": 0, "notes": len(notes.pieces),
             "captions": len(caps.pieces), "body_pieces": len(body.pieces)}

    def render_overflow(src_page, items):
        """Render overflow pages immediately after the source page."""
        rest = items
        guard = 0
        while rest and stats["continuation_pages"] < max_continuations and guard < 20:
            guard += 1
            stats["continuation_pages"] += 1
            np2, cfg2, _, ph2 = new_half_blank(src_page)
            lim2 = ph2 - margin + 6
            cjk_label(np2, pymupdf.Point(cfg2["right_x0"] + margin, margin - 22),
                      "正文续（第 %d 页原文的后续译文）" % (src_page + 1), 8.5,
                      (0.45, 0.45, 0.45))
            y2 = margin
            new_rest = []
            for p, start in rest:
                got = fit_block(np2, p, font, zh_font_path, cjk_name, cfg2, y2, lim2,
                                start=start)
                if got is None:
                    new_rest.append((p, start))
                else:
                    y2 = got
                    placed.add(id(p))
            if len(new_rest) == len(rest):
                break
            rest = new_rest
        return rest

    # 正文按页取块：主页面只排本页自己的正文，确保与左栏逐页对齐；
    # 本页确实排不下的内容进入文末续页，不再挤占下一页主页面。
    body_by_page = {}
    for p in body.pieces:
        body_by_page.setdefault(p.page, []).append(p)
    body_overflow = []            # 本页未排下、移到文末续页的正文块
    leftover_figs = []            # 没能在本页插下的图表（原页码）
    placed = set()

    _total_pages = len(pages)
    for _pi, pno in enumerate(pages):
        page_no_now = pno + 1
        if on_page:
            try:
                on_page(_pi + 1, _total_pages, "原第 %d 页" % page_no_now)
            except Exception:
                pass
        np, cfg, pw, ph = new_half(pno)
        limit = ph - margin + 6
        head = "第 %d 页 · 译文" % page_no_now
        cjk_label(np, pymupdf.Point(cfg["right_x0"] + margin, margin - 22), head,
                  8.5, (0.45, 0.45, 0.45))

        # 本页只排自己的正文，确保第 N 页右栏对应原第 N 页（元素是 (块, 起始行号)）
        pending = [(p, 0) for p in body_by_page.get(pno, [])]
        if _DEBUG:
            _srcs = sorted(set(p.page + 1 for p, _s in pending))
            log('   [debug] out p%d pending sources=%s overflow=%d' % (pno + 1, _srcs, len(body_overflow)))

        # 先算出本页 [图/表] + [注] 到底需要多高（按最小字号估算），据此预留——
        # 固定比例预留要么撑爆正文、要么把图表挤走，都不可靠。
        zone = []            # 预留区要放的东西（先只用于量高度）
        page_caps, page_cells, zone_extra, page_notes = [], [], [], []
        if show_caption:
            page_caps = [p for p in caps.pieces if p.page == pno]
            page_cells = [p for p in tables.pieces if p.page == pno]
            tbl_text = ""
            for cap in page_caps:
                cells = getattr(cap, "table_cells", None)
                if cells:
                    rows = group_rows(cells, use_translation=True)
                    tbl_text = "表格内容：\n" + "\n".join(rows)
                    break
            if not tbl_text and page_cells:
                frag = "；".join((" ".join(p.body_text().split()))[:24] for p in page_cells[:14])
                tbl_text = "表格内容：" + frag
            if tbl_text:
                anchor = page_caps[0].bbox if page_caps else page_cells[0].bbox
                pseudo = Piece(pno, tuple(anchor), tbl_text, 9.0, KIND_CAPTION)
                pseudo.translation = tbl_text
                zone_extra.append(pseudo)
            zone += zone_extra + page_caps
        if show_note:
            page_notes = [p for p in notes.pieces if p.page == pno]
            zone += page_notes

        MIN_ZONE_FS = 7.0
        zone_h = measure_page(zone, font, MIN_ZONE_FS, cfg["text_width"], False) if zone else 0.0
        if zone_h:
            zone_h += 20 * (2 if (page_caps or page_cells) and page_notes else 1) + 8
        def fits(reserve_h):
            """预留 reserve_h 后，本页正文还能不能整块排下（决定要不要给图表留位置）。"""
            if not pending:
                return True
            body_limit = limit - reserve_h
            if body_limit - margin <= MIN_ZONE_FS * 1.42:
                return False
            # 逐个整块试：只要有一块放得下就算够用（放不下的会顺延到下一页）
            y_test = margin
            for p, start in pending:
                got = fit_block(np, p, font, zh_font_path, cjk_name, cfg, y_test,
                                body_limit, start=start, draw=False)
                if got is not None:
                    return True
                break
            return False

        reserve_h = 0.0
        if zone and fits(zone_h):
            reserve_h = zone_h
        reserve_start = limit - reserve_h

        # 分区从页面底部往上叠：[注] 贴底 → [图/表] 文字 → 翻译后的图表 → 正文用满剩余空间
        cap_pieces = zone_extra + page_caps
        cap_overflow = False
        cue = limit                        # 正文可用的下边界

        # ① [注] 贴底
        if show_note and page_notes:
            note_est = measure_page(page_notes, font, 7.2, cfg["text_width"], False) + 20
            note_est = min(note_est, (limit - margin) * 0.30)
            note_start = limit - note_est
            sub = Stream(KIND_NOTE, page_notes)
            render_stream(sub, np, font, zh_font_path, cjk_name, cfg,
                          note_start, limit, cjk_label, top_free=True,
                          label="[注] 本页页脚注释", placed=placed,
                          font_sizes=[9.5, 8.5, 7.6, 6.8, 6.2])
            cue = min(cue, note_start - 8)

        # ② 图表翻译启用时全局不输出 [图/表] 旁注（图表本身已带译文）；
        #    未启用图表翻译时才输出该文字区
        zone_on = show_caption if caption_zone is None else bool(caption_zone)
        emit_caption_zone = bool(cap_pieces) and zone_on
        if emit_caption_zone:
            cap_est = measure_page(cap_pieces, font, 8.5, cfg["text_width"], False) + 20
            cap_est = min(cap_est, max(0.0, cue - margin - 40))
            cap_start = cue - cap_est
            sub = Stream(KIND_CAPTION, cap_pieces)
            render_stream(sub, np, font, zh_font_path, cjk_name, cfg,
                          cap_start, cue - 2, cjk_label, top_free=True,
                          label="[图/表] 图表标题与表格内容", placed=placed,
                          font_sizes=[9.5, 8.5, 7.6, 6.8, 6.2])
            if not sub.done():
                cap_overflow = True
            for p in page_cells:
                placed.add(id(p))       # 已并入表格摘要，不再进附录
            cue = min(cue, cap_start - 10)

        # ③ 图表按锚点就地插入：轮到图上方最近正文块排完时立即放图，
        #    保持与原文阅读顺序一致；空间不够时整组等比缩小。
        pending_fig_id = {}
        fig_reserve = 0.0
        if figpages is not None and figpages.has(pno):
            akey = figpages.anchor_of(pno, per_page)
            pending_fig_id[pno] = akey if akey is not None else "before-body"

        # 正文：用满 cue 以上的空间；整块放不下就进入文末续页，
        # 不再顺延去挤占下一页主页面。用 (块, 起始行号) 记录进度，绝不重复画。
        # 图表按"文本流位置"插入：轮到它的锚点块时就地插入，与原文阅读顺序一致。
        y = margin
        carry = []
        body_full = False
        drawn_texts = set()
        fig_max_w = min(pw - 40, cfg["text_width"] * 1.12)   # 略宽于正文列，但不越过左栏

        def place_fig_here(y_cur, cue_cur, allow_shrink=True):
            """按"与左栏大小排版一致"放置整组图。

            优先原生尺寸（并排仍并排）；空间实在不够时整组等比缩小，
            只有连缩小后都无处可放才移到文末。
            """
            if figpages is None or not figpages.has(pno) or fig_placed_idx[0]:
                return y_cur, False
            room = cue_cur - y_cur
            if _DEBUG:
                log('   [debug] fig p%d call y=%.0f cue=%.0f room=%.0f' % (pno + 1, y_cur, cue_cur, room))
            # 原生尺寸能否放下？
            pl = figpages.plan_group(pno, fig_max_w, col_h=room, allow_shrink=False)
            natural_h = pl[4] + 6 if pl else 0.0
            if natural_h <= room:
                y_end, _h = figpages.place_group(np, pno, cfg, y_cur, cue_cur, max_w=fig_max_w)
                fig_placed_idx[0] = 1
                return y_end + 4, True
            if allow_shrink and room > 120:
                y_end, _h = figpages.place_group(np, pno, cfg, y_cur, cue_cur, max_w=fig_max_w)
                fig_placed_idx[0] = 1
                return y_end + 4, True
            return y_cur, False

        fig_placed_idx = [0]          # [已放置图数]（一页多图时递增）
        # 页顶图（在正文之前）
        if pending_fig_id.get(pno) == "before-body":
            ny, ok = place_fig_here(y, cue)
            if ok:
                y = ny

        for p, start in pending:
            if body_full:
                carry.append((p, start))
                continue
            if p.text in drawn_texts and start == 0:
                continue                     # 同一段已在上一页画过（跨页块被算两次）
            got = fit_block(np, p, font, zh_font_path, cjk_name, cfg, y, cue, start=start)
            if got is not None:
                y = got
                placed.add(id(p))
                drawn_texts.add(p.text)
                if pending_fig_id.get(pno) == id(p):
                    ny, ok = place_fig_here(y, cue)
                    if ok:
                        y = ny
                continue
            # 整块放不下：若这是本页的第一块，允许按行截断，剩余行顺延
            fs = cfg["font_size"]
            lines = wrap_text(p.body_text(), font, fs, cfg["text_width"])
            lead = fs * 1.42
            n = int((cue - y) // lead)
            if start == 0 and n > 0:
                for ln in lines[:n]:
                    np.insert_text(pymupdf.Point(cfg["right_x0"] + margin, y), ln,
                                   fontname=cjk_name, fontfile=zh_font_path,
                                   fontsize=fs, color=(0, 0, 0))
                    y += lead
                if n < len(lines):
                    carry.append((p, n))
                else:
                    placed.add(id(p))
            else:
                body_full = True
                carry.append((p, start))

        # 锚点块没排到（或本页正文排完后仍有空间）：把整组图放到正文之后
        if figpages is not None and figpages.has(pno) and not fig_placed_idx[0]:
            ny, ok = place_fig_here(y, cue)
            if ok:
                y = ny

        # 页脚只画一条提示，避免多条文字叠在一起
        notes_out = []
        if carry:
            notes_out.append("本页末尾正文续见下页")
        if cap_overflow:
            notes_out.append("图表标题/表格其余见文末附录")
        fig_left = (figpages is not None and figpages.has(pno)
                    and not fig_placed_idx[0])
        if fig_left:
            notes_out.append("本页图表移至文末")
        if _DEBUG:
            log("   [debug] p%d fig 预留=%.0f cue=%.0f 已放=%d 需要=%.0f 剩余图=%s"
                % (page_no_now, fig_reserve, cue, fig_placed_idx[0],
                   figpages.needed_height(pno, fig_max_w) if (figpages and figpages.has(pno)) else 0,
                   fig_left))
        if notes_out:
            cjk_label(np, pymupdf.Point(cfg["right_x0"] + margin, ph - 16),
                      "→ " + "；".join(notes_out) + "（第 %d 页）" % page_no_now,
                      7.5, (0.35, 0.4, 0.55))
        if fig_left:
            leftover_figs.append(pno)
        if carry:
            rest = render_overflow(pno, carry)
            if rest:
                body_overflow.extend(rest)

    # ---------------------------------------------------------------- 文末兜底
    # 按来源页分组生成续页，避免把第 9 页、第 21 页等不同来源的溢出
    # 混在同一张“正文续”上导致标题和内容对不上。
    overflow_by_page = {}
    for p, start in body_overflow:
        overflow_by_page.setdefault(p.page, []).append((p, start))
    for src_page in sorted(overflow_by_page):
        items = overflow_by_page[src_page]
        guard = 0
        while (items and stats["continuation_pages"] < max_continuations
               and guard < 20):
            guard += 1
            stats["continuation_pages"] += 1
            np2, cfg2, _, ph2 = new_half_blank(src_page)
            lim2 = ph2 - margin + 6
            cjk_label(np2, pymupdf.Point(cfg2["right_x0"] + margin, margin - 22),
                      "正文续（第 %d 页原文的后续译文）" % (src_page + 1), 8.5,
                      (0.45, 0.45, 0.45))
            y2 = margin
            rest = []
            for p, start in items:
                got = fit_block(np2, p, font, zh_font_path, cjk_name, cfg2, y2, lim2,
                                start=start)
                if got is None:
                    rest.append((p, start))
                else:
                    y2 = got
                    placed.add(id(p))
            if len(rest) == len(items):
                break
            items = rest

    # 兜底：本页排不下而没插进去的图表，放到文末补页（左栏仍是它的原文页）
    for fpno in leftover_figs:
        if figpages is None or not figpages.has(fpno):
            continue
        np3, cfg3, _, ph3 = new_half(fpno)
        lim3 = ph3 - margin + 6
        cjk_label(np3, pymupdf.Point(cfg3["right_x0"] + margin, margin - 22),
                  "图表（第 %d 页原文，正文中未能容纳，移至文末）" % (fpno + 1),
                  8.5, (0.5, 0.4, 0.3))
        figpages.place(np3, fpno, cfg3, margin, lim3, max_w=cfg3["text_width"] + 24)
        stats["continuation_pages"] = stats.get("continuation_pages", 0) + 1

    # ---------------------------------------------------------------- 兜底附录
    # 本页空间不足而没能排下的注释 / 图表标题，统一收集到文末，保证不丢内容。
    leftover = []
    if show_note:
        leftover += [(KIND_NOTE, p) for p in notes.pieces if id(p) not in placed]
    if show_caption and (caption_zone is None or caption_zone):
        leftover += [(KIND_CAPTION, p) for p in caps.pieces if id(p) not in placed]
    if leftover:
        stats["appendix_items"] = len(leftover)
        page_no_now = 0
        rounds = 0
        while leftover and rounds < 8:
            rounds += 1
            src_page = leftover[0][1].page
            np3, cfg3, pw3, ph3 = new_half(src_page)
            lim3 = ph3 - margin + 6
            cjk_label(np3, pymupdf.Point(cfg3["right_x0"] + margin, margin - 22),
                      "附录：本页空间不足而移至文末的内容（对应第 %d 页原文）" % (src_page + 1),
                      8.5, (0.5, 0.4, 0.3))
            y3 = margin
            still = []
            for kind, p in leftover:
                sub = Stream(kind, [p])
                y3, _, _ = render_stream(sub, np3, font, zh_font_path, cjk_name, cfg3,
                                         y3, lim3, cjk_label,
                                         label=("[注]" if kind == KIND_NOTE else "[图]"),
                                         placed=placed)
                if not sub.done():
                    still.append((kind, p))
            leftover = still
            stats["appendix_pages"] = stats.get("appendix_pages", 0) + 1

    saved = save_pdf(out, out_path)
    return saved, stats


def save_pdf(doc, out_path):
    """原子写入：先写临时文件再替换；目标被占用时另存带时间戳的新文件。"""
    try:
        doc.subset_fonts(verbose=False)
    except Exception:
        pass
    tmp = out_path + ".tmp.pdf"
    doc.save(tmp, garbage=4, deflate=True)
    doc.close()
    import os
    import time
    try:
        os.replace(tmp, out_path)
        return out_path
    except OSError:
        alt = "%s-%s.pdf" % (os.path.splitext(out_path)[0], time.strftime("%Y%m%d-%H%M%S"))
        os.replace(tmp, alt)
        return alt

