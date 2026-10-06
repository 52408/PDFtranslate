# -*- coding: utf-8 -*-
"""structlab.py —— 页面的"栏结构"检测：单栏 / 双栏 / 混排（按纵向带切分）。

设计要点
--------
双栏论文里出现整页宽元素（标题、作者块、摘要、跨栏大图、宽表）时，
原文是"该带横跨两栏，之后恢复双栏"。所以检测的粒度不是"整页一种结构"，
而是**按纵向带（band）切分**：每个带自己声明是 单栏 / 双栏 / 整页宽。

产出（Region）：每个带给出
    kind      : "full"（整页宽）| "one"（单栏）| "two"（双栏）
    y0, y1    : 带的纵向范围
    cols      : 栏区间 [(x0, x1), ...]（full 就是整页宽一条）
    cols_est  : 从文字块统计出的栏边界，供更精细的放置

用法
----
    import structlab as SL
    regions = SL.page_layout(page)
    print(SL.describe(regions))
"""

import re

import pymupdf

# 整页宽判定：块宽超过页宽的该比例，即认为横跨
FULL_W_RATIO = 0.72
# 栏内文本宽度占半页的合理上限（超过说明不是半栏块）
COL_MAX_RATIO = 0.62
# 栏起始 x 的聚类容差
TOL = 16.0
# 一个带里至少这么多块才认为它"是"该结构（否则并入相邻带）
MIN_BLOCKS = 3
# 页眉/页脚判定：落在页面上下该比例的边距里，且在多页重复出现
MARGIN_RATIO = 0.085


def repeated_margin_texts(doc, pages=None, min_hits=2):
    """找出重复出现的页眉/页脚文本（书名、会议名、页码行）。

    不先剔除它们的话，它们会被当成"整页宽元素"从而把每一页都误判成混排。
    """
    pages = list(pages) if pages is not None else list(range(doc.page_count))
    seen = {}
    for pno in pages:
        page = doc[pno]
        h = page.rect.height
        for blk in page.get_text("dict")["blocks"]:
            if blk.get("type") != 0:
                continue
            r = pymupdf.Rect(blk["bbox"])
            # 用"块中心贴近页边"判定，而不是整块落在边距内：
            # 页眉块常跨在边距边界上（如 y=62~69，阈值 67.3），整块判定会漏掉。
            cy = (r.y0 + r.y1) / 2.0
            near_top = cy < h * (MARGIN_RATIO + 0.02)
            near_bottom = cy > h * (1 - MARGIN_RATIO - 0.02)
            if not (near_top or near_bottom):
                continue
            txt = " ".join(sp.get("text", "") for ln in blk.get("lines", [])
                           for sp in ln.get("spans", [])).strip()
            if not txt:
                continue
            key = re.sub(r"\d+", "#", txt)[:80]
            seen.setdefault(key, set()).add(pno)
    return {k for k, v in seen.items() if len(v) >= min_hits}


def _norm_key(txt):
    return re.sub(r"\d+", "#", txt)[:80]


def margin_bands(doc, pages=None, min_hits=2, tol=6.0):
    """找出页眉/页脚所在的**纵向区间**并集。

    比"按文本匹配重复"更稳：奇数页/偶数页的页眉文本往往不同
    （书名 vs 会议名），但位置一致。返回 [(y0, y1), ...]。
    """
    pages = list(pages) if pages is not None else list(range(doc.page_count))
    seen = {}
    for pno in pages:
        page = doc[pno]
        h = page.rect.height
        for blk in page.get_text("dict")["blocks"]:
            if blk.get("type") != 0:
                continue
            r = pymupdf.Rect(blk["bbox"])
            cy = (r.y0 + r.y1) / 2.0
            near_top = cy < h * (MARGIN_RATIO + 0.02)
            near_bottom = cy > h * (1 - MARGIN_RATIO - 0.02)
            if not (near_top or near_bottom):
                continue
            key = (round(r.y0 / tol), round(r.y1 / tol))
            seen.setdefault(key, []).append((pno, r.y0, r.y1))
    bands = []
    for key, hits in seen.items():
        if len(hits) < min_hits:
            continue
        bands.append((min(h[1] for h in hits) - 2, max(h[2] for h in hits) + 2))
    return bands


def in_bands(r, bands, slack=1.0):
    for (b0, b1) in bands:
        if r.y0 >= b0 - slack and r.y1 <= b1 + slack:
            return True
    return False


def _blocks(page, y_lo, y_hi, drop_keys=None, exclude=None, margin_bands_=None):
    """取页面正文区域内的文字块（含字号），排除页眉页脚与图表区域。"""
    h = page.rect.height
    exclude = exclude or []
    out = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        r = pymupdf.Rect(blk["bbox"])
        if r.y0 < y_lo or r.y1 > y_hi:
            continue
        # 页眉/页脚：按位置区间剔除（比按文本匹配更稳）
        if margin_bands_ and in_bands(r, margin_bands_):
            continue
        # 落在图表区域内的块 = 图内标签，交给图表流程处理，不参与栏判断
        if any((r & fr).get_area() > 0.5 * r.get_area() for fr in exclude):
            continue
        sizes = []
        txt = []
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                if sp.get("text", "").strip():
                    txt.append(sp["text"])
                    sizes.append(round(sp.get("size", 10), 1))
        if not txt:
            continue
        joined = " ".join(txt).strip()
        if drop_keys and _norm_key(joined) in drop_keys:
            continue                    # 页眉/页脚
        out.append({"rect": r, "text": joined,
                    "size": max(set(sizes), key=sizes.count) if sizes else 10.0})
    return out


def _cluster_cols(xs, tol=TOL):
    """把 x 起点聚成栏区间，返回 [(x0, x1, 块数), ...]（按块数降序）。"""
    clusters = []
    for x0, x1 in sorted(xs):
        hit = None
        for c in clusters:
            if abs(x0 - c[0]) <= tol:
                hit = c
                break
        if hit is None:
            clusters.append([x0, x1, 1])
        else:
            hit[1] = max(hit[1], x1)
            hit[2] += 1
    clusters.sort(key=lambda c: -c[2])
    return [tuple(c) for c in clusters]


def col_start_vocab(blocks, w, min_hits=3):
    """整页的"栏起点词表"：某个 x 起点出现 ≥ min_hits 次，就认为那里是一条真栏。

    用途：某一段里右栏可能只有 1 个块，只按局部计数会被滤掉，
    于是整段被误判成单栏。跨段落累计起点次数就能认出它是真栏。
    """
    hits = {}
    for b in blocks:
        if b["rect"].width > w * FULL_W_RATIO:
            continue
        x0 = round(b["rect"].x0, 1)
        key = None
        for k in hits:
            if abs(k - x0) <= TOL:
                key = k
                break
        if key is None:
            hits[x0] = 1
        else:
            hits[key] += 1
    return {k for k, v in hits.items() if v >= min_hits}


def _judge(blocks, w, vocab=None):
    """判断一组块的栏结构，返回 ("two", cols) | ("one", cols) | ("full", cols)。

    判据只用**栏起点（x0）的分布**，并且排除"整宽块"（宽度 > 0.72 页宽）：
      * 整宽块（图注、小标题）横跨两栏，它的 x1 会把左栏拉宽，
        导致左/右两栏宽度比失衡，从而把双栏误判成单栏。
      * 两个起点簇足够分开 → 双栏
      * 只有一个起点簇     → 单栏
    """
    if not blocks:
        return None, []
    narrow = [b for b in blocks if b["rect"].width <= w * FULL_W_RATIO] or blocks
    cols = _cluster_cols([(round(b["rect"].x0, 1), round(b["rect"].x1, 1)) for b in narrow])
    # 双栏判据（不依赖块数）：两个起点簇的**典型栏宽相近** 且 起点足够分开。
    # 有些段落右栏只出现 1 个块（如"大图下方的第一页"），按块数会被滤掉。
    # 单栏论文里"标题缩进 + 正文"两簇的宽度差通常很大，因此不会被误判。
    # 主判据：**两个起点簇各 ≥2 块**且栏宽相近 → 双栏。
    # 之所以要求 ≥2 块：单栏论文里"行内缩进"也会形成一个孤立的起点簇，
    # 放宽到 1 块会把单栏页误判成双栏。
    strong = [c for c in cols if c[2] >= 2]
    if len(strong) >= 2:
        two = sorted(strong[:2], key=lambda c: c[0])
        lw = _cluster_typical_width(narrow, two[0], w)
        rw = _cluster_typical_width(narrow, two[1], w)
        ratio = max(lw, rw) / max(1.0, min(lw, rw))
        if two[1][0] - two[0][0] > w * 0.20 and ratio <= 1.8:
            return "two", [(two[0][0], two[0][0] + lw), (two[1][0], two[1][0] + rw)]
    # 补充判据：主簇（≥3 块）之外还有一个**孤立的、延伸到右边界的**起点簇。
    # 处理"大图下方那一页正文右栏只剩一块"的情况（如 06328 第 5 页）。
    if len(cols) >= 2 and cols[0][2] >= 3:
        main, extra = cols[0], None
        for c in sorted(cols[1:], key=lambda c: c[0]):
            if abs(c[0] - main[0]) > w * 0.20:
                extra = c
                break
        if extra is not None:
            reach = max([b["rect"].x1 for b in narrow
                         if abs(b["rect"].x0 - extra[0]) <= TOL] or [0])
            mw = _cluster_typical_width(narrow, main, w)
            ew = _cluster_typical_width(narrow, extra, w)
            ratio = max(mw, ew) / max(1.0, min(mw, ew))
            if reach >= w * 0.80 and ratio <= 1.8:
                a, b = sorted([main, extra], key=lambda c: c[0])
                return "two", [(a[0], a[0] + _cluster_typical_width(narrow, a, w)),
                               (b[0], b[0] + _cluster_typical_width(narrow, b, w))]
    if len(strong) >= 2:
        two = sorted(strong[:2], key=lambda c: c[0])
        left_w = _cluster_typical_width(narrow, two[0], w)
        right_w = _cluster_typical_width(narrow, two[1], w)
        ratio = max(left_w, right_w) / max(1.0, min(left_w, right_w))
        if two[1][0] - two[0][0] > w * 0.22 and ratio <= 2.6:
            return "two", [(two[0][0], two[0][0] + left_w),
                           (two[1][0], two[1][0] + right_w)]
    if strong:
        c = strong[0]
        return "one", [(c[0], c[1])]
    xs0 = min(b["rect"].x0 for b in blocks)
    xs1 = max(b["rect"].x1 for b in blocks)
    return "one", [(xs0, xs1)]


def _cluster_typical_width(blocks, cluster, w):
    """某个起点簇的"典型栏宽"。

    取该簇内块宽度的**中位数**（而不是最窄的那些）：
    标题页的作者姓名是短行（约 90pt），若取最窄会得到 86pt 的"栏宽"，
    译文就会被塞进极窄的条里。中位数能反映该栏的常见宽度。
    同时对异常宽的跨栏块做上限约束。
    """
    ws = sorted(b["rect"].width for b in blocks
                if abs(b["rect"].x0 - cluster[0]) <= TOL
                and b["rect"].width <= w * FULL_W_RATIO)
    if not ws:
        return min(cluster[1] - cluster[0], w * 0.5)
    mid = ws[len(ws) // 2]
    # 中位数可能仍偏小（短行多）：取"中位数与 0.75 分位"的较大者
    q3 = ws[min(len(ws) - 1, int(len(ws) * 0.75))]
    return max(mid, q3)


def page_layout(page, y_lo_ratio=0.05, y_hi_ratio=0.95, split_bands=True,
                drop_keys=None, fig_regions=None, margin_bands_=None):
    """检测一页的栏结构，返回 [Region, ...]（按 y 排序）。

    流程（与"先查图片位置"一致）：
      0) 先拿到图表区域；落在图表区域里的文字是图内标签，不能参与栏判断
      1) 整页先判结构：只看"窄块"（宽度 ≤ 0.72 页宽，即真正的栏内文本）的 x 起点分布；
         单栏 → 整页一个 one；双栏 → 再按纵向带切分（处理"标题横跨 + 下方双栏"）
      2) 双栏页里，明显横跨的块各自成一条 full 带
    """
    w, h = page.rect.width, page.rect.height
    y_lo, y_hi = h * y_lo_ratio, h * y_hi_ratio

    # 0) 图表区域：里面的文字不参与栏检测
    figs = []
    if fig_regions is None:
        try:
            import figlab as _FL
            figs = [r[0] for r in _FL.figure_regions(page)]
        except Exception:
            figs = []
    else:
        figs = [pymupdf.Rect(r) for r in fig_regions]
    # 图表自身作为一条 full 带（译文里图片放在这里，横跨整页宽）
    fig_bands = []
    for fr in figs:
        px0 = max(0.0, fr.x0)
        px1 = min(w, fr.x1)
        py0 = max(y_lo, fr.y0)
        py1 = min(y_hi, fr.y1)
        if py1 - py0 > 12 and px1 - px0 > 12:
            fig_bands.append(Region("figure", py0, py1, [(px0, px1)], "图表区域"))
    # **表格带也登记为"图层"**：否则表格块会被排除出正文流，
    # 而渲染器若只画图不画表，表格内容就会彻底消失。
    try:
        import tablelab as _TL
        for (t0, t1) in _TL.table_bands(page):
            ty0, ty1 = max(y_lo, t0), min(y_hi, t1)
            if ty1 - ty0 > 12:
                fig_bands.append(Region("table", ty0, ty1, [(0.0, w)], "表格区域"))
    except Exception:
        pass

    blocks = _blocks(page, y_lo, y_hi, drop_keys=drop_keys, exclude=figs,
                     margin_bands_=margin_bands_)
    if not blocks and not fig_bands:
        return []
    if not blocks:
        return sorted(fig_bands, key=lambda r: r.y0)

    narrow = [b for b in blocks if b["rect"].width <= w * FULL_W_RATIO]
    vocab = col_start_vocab(blocks, w)
    page_kind_est, page_cols = _judge(narrow or blocks, w, vocab)

    # ---- 关键：**图表带参与分区** ----
    # 一个图表带把页面切成上下两段，每段独立判栏。
    # 否则"上面单栏图 + 下面双栏正文"这种页会被顶部块带偏，整页误判成单栏。
    if fig_bands and split_bands:
        cuts = sorted((fb.y0, fb.y1) for fb in fig_bands)
        segs = []
        cur = y_lo
        for (a, b) in cuts:
            if a - cur > 24:
                segs.append((cur, a))
            cur = max(cur, b)
        if y_hi - cur > 24:
            segs.append((cur, y_hi))
        regions = []
        for (a, b) in segs:
            inside = [bl for bl in blocks if bl["rect"].y0 >= a - 3 and bl["rect"].y1 <= b + 3]
            if not inside:
                continue
            # 判栏时**排除整宽块**（图注、小标题等）：它们横跨两栏，
            # 混进来会把左栏的 x 区间算得过宽，进而把双栏误判成单栏。
            narrow_inside = [bl for bl in inside
                             if bl["rect"].width <= w * FULL_W_RATIO]
            kind, cols = _judge(narrow_inside or inside, w, vocab)
            if kind == "two":
                mid = (cols[0][1] + cols[1][0]) / 2.0
                nl = sum(1 for bl in inside if bl["rect"].x0 < mid)
                nr = len(inside) - nl
                regions.append(Region("two", a, b, cols, "双栏带（左%d/右%d块）" % (nl, nr)))
                # 整宽块（图注等）单独作为一条带，译文横跨两栏
                wide_inside = [bl for bl in inside
                               if bl["rect"].width > w * FULL_W_RATIO]
                for bl in wide_inside:
                    r = bl["rect"]
                    regions.append(Region("full", r.y0, r.y1, [(0.0, w)],
                                          "整页宽：%s" % bl["text"][:26]))
            else:
                regions.append(Region("one", a, b, cols, "单栏带（%d 块）" % len(inside)))
        out = sorted(regions + fig_bands, key=lambda r: r.y0)
        return _merge_bands(out)

    if not split_bands or page_kind_est != "two":
        xs0 = min(b["rect"].x0 for b in blocks)
        xs1 = max(b["rect"].x1 for b in blocks)
        cols = page_cols if page_kind_est == "two" else [(xs0, xs1)]
        note = "整页双栏" if page_kind_est == "two" else "整页单栏"
        out = [Region(page_kind_est or "one", y_lo, y_hi, cols, note)]
        return _merge_bands(sorted(out + fig_bands, key=lambda r: r.y0))

    # ---- 双栏页：切纵向带 ----
    col_w = max(page_cols[0][1] - page_cols[0][0], page_cols[1][1] - page_cols[1][0])
    wide = [b for b in blocks
            if b["rect"].width > min(w * FULL_W_RATIO, col_w * 1.35)]
    marks = sorted(wide, key=lambda b: b["rect"].y0)
    regions = []
    cursor = y_lo

    def emit_gap(g0, g1):
        if g1 - g0 < 10:
            return
        inside = [b for b in blocks
                  if b["rect"].y0 >= g0 - 3 and b["rect"].y1 <= g1 + 3 and b not in wide]
        if not inside:
            return
        kind, cols = _judge(inside, w, vocab)
        mid = (cols[0][1] + cols[1][0]) / 2 if (kind == "two" and len(cols) == 2) else None
        if kind == "two" and mid is not None:
            nl = sum(1 for b in inside if b["rect"].x0 < mid)
            nr = len(inside) - nl
            regions.append(Region("two", g0, g1, cols, "双栏带（左%d/右%d块）" % (nl, nr)))
        else:
            regions.append(Region("one", g0, g1, cols, "单栏带（%d 块）" % len(inside)))

    # 按 y 排序时用 key（不能直接比较 dict；None 与 dict 也无法比较）
    timeline = sorted([(b["rect"].y0, b["rect"].y1, b) for b in marks]
                      + [(fb.y0, fb.y1, None) for fb in fig_bands],
                      key=lambda t: (t[0], t[1]))
    for y0, y1, b in timeline:
        emit_gap(cursor, y0)
        if b is not None:
            regions.append(Region("full", y0, y1, [(0.0, w)],
                                  "整页宽：%s" % b["text"][:30]))
        cursor = max(cursor, y1)
    emit_gap(cursor, y_hi)

    out = sorted(regions + fig_bands, key=lambda r: r.y0)
    return _merge_bands(out)


def _merge_bands(regions):
    """合并相邻同类带；图表带优先保留（不参与合并）。"""
    merged = []
    for rg in regions:
        prev = merged[-1] if merged else None
        if (prev is not None and prev.kind == rg.kind and rg.kind != "figure"
                and rg.y0 - prev.y1 < 16):
            prev.y1 = rg.y1
            if rg.kind == "two" and len(rg.cols) == 2 and len(prev.cols) == 2:
                prev.cols = [(min(prev.cols[0][0], rg.cols[0][0]),
                              max(prev.cols[0][1], rg.cols[0][1])),
                             (min(prev.cols[1][0], rg.cols[1][0]),
                              max(prev.cols[1][1], rg.cols[1][1]))]
            prev.note += " + " + rg.note
        else:
            merged.append(rg)
    return merged


class Region:
    __slots__ = ("kind", "y0", "y1", "cols", "note")

    def __init__(self, kind, y0, y1, cols, note=""):
        self.kind = kind          # full | one | two
        self.y0 = float(y0)
        self.y1 = float(y1)
        self.cols = [(float(a), float(b)) for a, b in cols]
        self.note = note

    @property
    def height(self):
        return self.y1 - self.y0

    def col_of(self, x):
        """给定 x，返回它属于第几栏（0/1）；单栏或整页宽返回 0 或 -1。"""
        if self.kind != "two" or len(self.cols) < 2:
            return 0
        mid = (self.cols[0][1] + self.cols[1][0]) / 2.0
        return 0 if x < mid else 1

    def __repr__(self):
        cs = " ".join("%.0f~%.0f" % c for c in self.cols)
        return "<%s y%.0f~%.0f [%s] %s>" % (self.kind, self.y0, self.y1, cs, self.note)


def figure_regions_for(page, drop_keys=None, margin_bands_=None):
    """用"栏结构带"产出图表区域，替代 figlab 的"图注向上取带"。

    为什么换：figlab 是按图注位置**向上**圈带的，一页里有多个图注时
    （例如一页 4 张承诺书截图）就会串位——图注配错图、甚至重复。
    structlab 的带是按"整页宽块 / 栏分布"推出来的，构造上不会串位。

    返回 [(clip, cap_rect, cap_text, kind), ...]，字段与 figlab 保持一致。
    """
    import tablelab as TL
    w, h = page.rect.width, page.rect.height
    regions = page_layout(page, drop_keys=drop_keys, margin_bands_=margin_bands_)
    # 图注（含表注）：按其 y 位置与 x 范围配对
    caps = []
    try:
        caps = list(TL.table_captions(page))
    except Exception:
        caps = []
    try:
        import figlab as FL
        for cr, ct, k in FL.find_captions(page):
            caps.append((cr, ct))
    except Exception:
        pass

    out = []
    for r in regions:
        if r.kind not in ("figure", "table"):
            continue
        clip = pymupdf.Rect(r.cols[0][0], r.y0, r.cols[0][1], r.y1)
        if clip.width < 20 or clip.height < 20:
            continue
        # 配对图注：紧贴在带下方、且横向与带重叠最多的那一条
        best, best_ov = None, 0.0
        for cr, ct in caps:
            if cr.y0 < r.y1 - 6 or cr.y0 > r.y1 + 40:
                continue
            ov = min(clip.x1, cr.x1) - max(clip.x0, cr.x0)
            if ov > best_ov:
                best, best_ov = (cr, ct), ov
        if best is None:
            # 退一步：找带内最靠下的一段文字当图注
            best = (pymupdf.Rect(clip.x0, r.y1, clip.x1, r.y1 + 10), "")
        out.append((clip, best[0], best[1], "table" if r.kind == "table" else "fig"))
    return out


def page_kind(regions):
    """整页归类：单栏 / 双栏 / 混排。"""
    kinds = [r.kind for r in regions]
    if not kinds:
        return "空"
    n_two = kinds.count("two")
    n_full = kinds.count("full")
    if n_two == 0:
        return "单栏"
    if n_full == 0 and n_two >= 1 and kinds.count("one") == 0:
        return "双栏"
    return "混排"


def document_profile(doc, max_pages=None):
    """整篇论文的栏结构画像。"""
    n = min(doc.page_count, max_pages or doc.page_count)
    pages = list(range(n))
    drop = repeated_margin_texts(doc, range(doc.page_count))
    counts = {}
    per_page = []
    for pno in pages:
        regs = page_layout(doc[pno], drop_keys=drop)
        k = page_kind(regs)
        counts[k] = counts.get(k, 0) + 1
        per_page.append((pno + 1, k, regs))
    return counts, per_page


def describe(regions):
    if not regions:
        return "（无正文）"
    return " | ".join("%s:y%.0f~%.0f:%s" % (r.kind, r.y0, r.y1, r.note)
                      for r in regions)


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:]:
        d = pymupdf.open(path)
        print("=" * 90)
        print(path)
        counts, per_page = document_profile(d, max_pages=12)
        print("  前 12 页构成: %s" % counts)
        for pno, k, regs in per_page[:6]:
            print("  第%2d页 [%s] %s" % (pno, k, describe(regs)))
