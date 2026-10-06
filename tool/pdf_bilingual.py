#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pdf_bilingual.py —— 把英文 PDF 论文/文档生成「左栏原文、右栏中文译文」的双语对照 PDF。

翻译引擎由设置界面（settings_ui.py）管理，也可用命令行参数临时覆盖：
    python pdf_bilingual.py INPUT.pdf [-o OUT.pdf] [--pages 1-5,8]
                            [--provider deepseek|siliconflow|dashscope|ollama|google|custom-xxx]
                            [--api-key ...] [--base-url ...] [--model ...]
                            [--lang 简体中文] [--font-size 9.5] [--workers 4] [--dry-run]

设计要点：
  * 左栏 = 原页面矢量嵌入（公式/图表/排版原样保留）
  * 右栏 = 对应原文段落的中文译文，按原文段落顺序编号对齐
  * 页眉/页脚等重复水印自动识别并跳过，不浪费翻译额度
  * 断点续跑：译文缓存到 <输出名>.cache.json，中断后重跑不用重新翻译
"""

import argparse
import concurrent.futures as futures
import hashlib
import json
import os
import re
import sys
import time

try:
    import pymupdf
except ImportError:  # 兼容旧名字
    import fitz as pymupdf

import providers as P
import settings_store as S
import progress as PG
from progress import progress, phase, Ticker

try:
    import layout2 as L
except ImportError:      # 允许单独运行本文件
    L = None

try:
    import figpipeline as FP
except ImportError:
    FP = None

SYSTEM_PROMPT = (
    "你是专业学术论文翻译引擎。把用户给出的英文论文段落翻译成{lang}，要求：\n"
    "1. 只输出译文，不要解释、不要加前后缀。\n"
    "2. 数学公式、变量符号、数字、单位、代码、URL 原样保留，不要翻译。\n"
    "3. 人名、机构名、模型名、数据集名保留英文原样（如 Transformer、BERT、ImageNet）。\n"
    "4. 文献引用标记保留原样，如 [1]、[12,13]、(Vaswani et al., 2017)。\n"
    "5. 专业术语使用学术界通行译法，首次出现可用「中文（English）」形式。\n"
    "6. 保持原文段落结构，忠实、通顺、不增删内容。"
)


# ----------------------------------------------------------------------------- utils

def utf8_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg):
    print(msg, flush=True)


def norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().lower()


LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
    "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "--", "\u2212": "-", "\u00ad": "",
}


def clean_text(t: str) -> str:
    """修复连字/特殊符号提取坏掉的问题（there by -> thereby 等）。"""
    for k, v in LIGATURES.items():
        t = t.replace(k, v)
    t = t.replace("\ufffd", "").replace("\x00", "")
    t = re.sub(r"[\u200b\u200e\u200f\ufeff]", "", t)
    lines = []
    for ln in t.split("\n"):
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------- translate

BATCH_INSTRUCTION = (
    "下面是 %d 个编号段落，请逐段翻译成%s。\n"
    '只返回 JSON 对象，格式为 {"translations": ["第一段译文", "第二段译文", ...]}，'
    "数组长度必须等于 %d，顺序与输入编号一致。\n\n%s"
)


def parse_translations(raw, n):
    out = [None] * n
    txt = (raw or "").strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            arr = obj.get("translations") or obj.get("translation") or obj.get("result")
            if isinstance(arr, str):
                arr = [arr]
            if isinstance(arr, list):
                for i in range(min(n, len(arr))):
                    if isinstance(arr[i], str):
                        out[i] = arr[i].strip()
        except Exception:
            pass
    if all(x is None for x in out):  # 退化解析：[1] xxx
        parts = re.split(r"(?:^|\n)\s*\[(\d+)\]\s*", txt)
        if len(parts) >= 3:
            for i in range(1, len(parts) - 1, 2):
                idx = int(parts[i]) - 1
                if 0 <= idx < n:
                    out[idx] = parts[i + 1].strip()
    return out


def translate_blocks(prov, texts, lang):
    """把一批段落交给引擎翻译，返回等长译文列表。"""
    if prov.api == "google_web":
        target = {"简体中文": "zh-CN", "繁體中文": "zh-TW", "English": "en",
                  "日本語": "ja", "한국어": "ko"}.get(lang, "zh-CN")
        return prov.translate_units(texts, target)
    numbered = "\n\n".join("[%d] %s" % (i + 1, t) for i, t in enumerate(texts))
    prompt = BATCH_INSTRUCTION % (len(texts), lang, len(texts), numbered)
    system = SYSTEM_PROMPT.format(lang=lang)
    raw = prov.chat(system, prompt, max_tokens=min(8000, 400 + 8 * len(prompt)), json_mode=True)
    return parse_translations(raw, len(texts))


# ----------------------------------------------------------------------------- extract

class Block:
    __slots__ = ("page", "bbox", "text", "style", "translation", "is_figure")

    def __init__(self, page, bbox, text, style):
        self.page = page
        self.bbox = bbox
        self.text = text
        self.style = style
        self.translation = None
        self.is_figure = False


def extract_blocks(doc, pages):
    """按页抽取正文段落块，并去掉跨页重复的页眉/页脚。"""
    raw = {}
    for pno in pages:
        page = doc[pno]
        d = page.get_text("dict")
        items = []
        for blk in d.get("blocks", []):
            if blk.get("type") == 1:  # 图片块
                items.append(("figure", pymupdf.Rect(blk["bbox"]), None, None))
                continue
            lines, sizes = [], []
            for line in blk.get("lines", []):
                s = "".join(sp.get("text", "") for sp in line.get("spans", []))
                if s.strip():
                    lines.append(s)
                    for sp in line.get("spans", []):
                        sizes.append(round(sp.get("size", 0), 1))
                # 行内公式：把 type=1 的 span 还原成占位符
            text = clean_text("\n".join(lines))
            if not text:
                continue
            size = max(set(sizes), key=sizes.count) if sizes else 10.0
            items.append(("text", pymupdf.Rect(blk["bbox"]), text, size))
        raw[pno] = items

    # 重复页眉/页脚检测
    freq = {}
    for pno, items in raw.items():
        for kind, rect, text, size in items:
            if kind != "text":
                continue
            n = norm_text(text)
            if len(n) > 120:
                continue
            freq[n] = freq.get(n, 0) + 1
    threshold = max(2, int(len(pages) * 0.5))
    drop = {n for n, c in freq.items() if c >= threshold}

    result = {}
    for pno in pages:
        page = doc[pno]
        pw, ph = page.rect.width, page.rect.height
        out = []
        for kind, rect, text, size in raw[pno]:
            if kind == "figure":
                out.append(Block(pno, rect, "[图]", size or 0))
                out[-1].is_figure = True
                continue
            if norm_text(text) in drop:
                continue
            if len(text) < 2:
                continue
            # 旋转文字（竖排水印 / arXiv 侧边条）：又高又窄且无换行
            w, h = rect.width, rect.height
            if h > w * 2.5 and "\n" not in text:
                continue
            # 页边区域内的短文本：页眉页脚/水印/页码
            in_margin = (rect.y1 < ph * 0.055) or (rect.y0 > ph * 0.93) \
                        or (rect.x1 < pw * 0.06) or (rect.x0 > pw * 0.94)
            if in_margin and len(text) <= 90:
                continue
            out.append(Block(pno, rect, text, size))
        out.sort(key=lambda b: (round(b.bbox.y0 / 8), b.bbox.x0))
        result[pno] = out
    return result


# ----------------------------------------------------------------------------- render

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


def measure_page(blocks, font, font_size, text_width, with_figures=True):
    """估算一页右栏需要的总高度（行数 × 行距 + 段间距）。"""
    total = 0.0
    for b in blocks:
        if b.is_figure:
            if with_figures:
                total += 12.0
            continue
        body = b.translation or b.text
        fs = font_size if len(body) < 900 else font_size - 0.5
        lines = wrap_text(body, font, fs, text_width)
        total += len(lines) * fs * 1.42 + fs * 0.71
    return total


def autofit_font_size(blocks, font, text_width, avail_height, start,
                      with_figures=True, floor=6.2):
    """中文通常比英文长，逐档缩小字号直到本页能放下；返回 (字号, 是否仍溢出)。"""
    size = start
    while size > floor:
        if measure_page(blocks, font, size, text_width, with_figures) <= avail_height:
            return size, False
        size = round(size - 0.3, 1)
    return floor, measure_page(blocks, font, floor, text_width, with_figures) > avail_height


def render_col(newpage, right_x0, margin, ph, blocks,
               start_y, bottom_limit, font, cjk_fontname, zh_font_path, font_size,
               with_figures, header):
    """在右栏排 blocks，返回 (结束 y, 放不下的 (块, 起始行号) 列表)。"""
    newpage.draw_line(pymupdf.Point(right_x0 - 8, 0), pymupdf.Point(right_x0 - 8, ph),
                      color=(0.78, 0.78, 0.78), width=0.7)
    newpage.insert_text(pymupdf.Point(right_x0 + margin, margin - 22), header,
                        fontname="helv", fontsize=8.5, color=(0.45, 0.45, 0.45))
    text_width = newpage.rect.width - right_x0 - margin * 2

    y = start_y
    leftover = []
    idx = 0
    for b in blocks:
        idx += 1
        if b.is_figure:
            if not with_figures:
                continue
            label = "[图/表 %d：见左栏对应位置]" % idx
            if y + 12 > bottom_limit:
                leftover.append((b, 0))
                continue
            newpage.insert_text(pymupdf.Point(right_x0 + margin, y), label,
                                fontname="helv", fontsize=8.5, color=(0.55, 0.55, 0.55))
            y += 12
            continue

        body = b.translation if b.translation else b.text
        prefix = "" if b.translation else "[原文占位] "
        fs = font_size if len(body) < 900 else font_size - 0.5
        lines = wrap_text(body, font, fs, text_width)
        start_line = getattr(b, "start_line", 0)
        lead = fs * 1.42
        drawn = 0
        for i in range(start_line, len(lines)):
            if y + lead > bottom_limit:
                leftover.append((b, i))
                break
            newpage.insert_text(pymupdf.Point(right_x0 + margin, y),
                                (prefix + lines[i]) if i == start_line else lines[i],
                                fontname=cjk_fontname, fontfile=zh_font_path,
                                fontsize=fs, color=(0, 0, 0))
            y += lead
            drawn += 1
        y += lead * 0.5
        if start_line + drawn < len(lines):
            break
    return y, leftover


def render_bilingual(doc, blocks_by_page, pages, out_path, zh_font_path,
                     font_size=9.5, zoom=2.0, with_figures=True, stats=None,
                     raster=False):
    out = pymupdf.open()
    font = pymupdf.Font(fontfile=zh_font_path)
    cjk_fontname = "zhcn"
    margin = 46
    gap = 16
    continuations = {}          # 原页码 -> [(块, 起始行号), ...]
    overflow_pages = []

    for pno in pages:
        page = doc[pno]
        pw, ph = page.rect.width, page.rect.height
        W = pw * 2 + gap
        H = ph
        left_rect = pymupdf.Rect(0, 0, pw, ph)
        right_x0 = pw + gap

        newpage = out.new_page(width=W, height=H)
        if raster:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            if pix.n > 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            try:
                pix.set_compression("jpeg", jpg_quality=88)
            except Exception:
                pass
            newpage.insert_image(left_rect, pixmap=pix)
        else:
            newpage.show_pdf_page(left_rect, doc, pno)

        blocks = blocks_by_page[pno]
        text_width = pw - margin * 2
        bottom_limit = ph - margin + 6
        avail = bottom_limit - margin

        # 1) 先试用户字号，不够就逐档缩小（缩到 7.2pt 为止，保持可读）
        page_fs, fits = autofit_font_size(blocks, font, text_width, avail,
                                          font_size, with_figures=with_figures, floor=7.2)
        y, left = render_col(newpage, right_x0, margin, ph, blocks,
                             margin, bottom_limit, font, cjk_fontname, zh_font_path,
                             page_fs, with_figures, "第 %d 页 中文译文" % (pno + 1))
        if not left:
            continue

        # 2) 仍放不下 -> 缩到 6.2pt 再排一次（尽量在本页挤下）
        page_fs = 6.2
        newpage2 = out.new_page(width=W, height=H)
        if raster:
            newpage2.insert_image(left_rect, pixmap=pix)
        else:
            newpage2.show_pdf_page(left_rect, doc, pno)
        y, left = render_col(newpage2, right_x0, margin, ph, blocks,
                             margin, bottom_limit, font, cjk_fontname, zh_font_path,
                             page_fs, with_figures, "第 %d 页 中文译文（自动缩小）" % (pno + 1))
        if left:
            continuations[pno] = left
            overflow_pages.append(pno + 1)

    # 3) 续页：把放不下的段落接着排，左栏保留原页缩略图，不丢任何内容
    for pno in list(continuations.keys()):
        parent = doc[pno]
        pw, ph = parent.rect.width, parent.rect.height
        W = pw * 2 + gap
        left_rect = pymupdf.Rect(0, 0, pw, ph)
        right_x0 = pw + gap
        todo = continuations[pno]
        round_no = 0
        while todo:
            round_no += 1
            if round_no > 6:
                break
            newpage = out.new_page(width=W, height=H)
            newpage.show_pdf_page(left_rect, doc, pno)
            newpage.insert_text(pymupdf.Point(pw * 0.5 - 120, ph - 14),
                                "（第 %d 页原文缩略图 · 译文续 %d）" % (pno + 1, round_no),
                                fontname="helv", fontsize=8, color=(0.6, 0.6, 0.6))
            blocks = []
            for b, line in todo:
                blocks.append(ContinuationBlock(b, line))
            text_width = pw - margin * 2
            bottom_limit = ph - margin + 6
            y, left = render_col(newpage, right_x0, margin, ph, blocks,
                                 margin, bottom_limit, font, cjk_fontname, zh_font_path,
                                 7.5, with_figures,
                                 "第 %d 页 译文续（%d）" % (pno + 1, round_no))
            todo = left

    try:
        out.subset_fonts(verbose=False)
    except Exception as e:
        log("   [提示] 字体子集化失败，文件会偏大：%s" % e)

    # 原子写入：先写临时文件再替换。直接覆盖时某些环境会因“无法删除旧文件”而整次失败。
    tmp_out = out_path + ".tmp.pdf"
    saved_to = out_path
    try:
        out.save(tmp_out, garbage=4, deflate=True)
        out.close()
        try:
            os.replace(tmp_out, out_path)
        except OSError:
            # 目标被阅读器占用 / 无删除权限：另存一个带时间戳的新文件，绝不丢结果
            stamp = time.strftime("%Y%m%d-%H%M%S")
            alt = "%s-%s.pdf" % (os.path.splitext(out_path)[0], stamp)
            os.replace(tmp_out, alt)
            saved_to = alt
            log("   [提示] 原文件被占用（多半是 PDF 阅读器正开着它），已另存为：%s" % alt)
    except Exception:
        out.close()
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except Exception:
            pass
        raise

    if overflow_pages:
        stats = stats if stats is not None else {}
        stats["overflow_pages"] = overflow_pages
    return saved_to, overflow_pages


class ContinuationBlock:
    """续页里用的块：从原文块的某一行开始继续排。"""

    def __init__(self, block, start_line):
        self.page = block.page
        self.bbox = block.bbox
        self.text = block.text
        self.style = block.style
        self.translation = block.translation
        self.is_figure = block.is_figure
        self.start_line = start_line


# ----------------------------------------------------------------------------- cjk font

CJK_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\msyhl.ttc",
]


def find_cjk_font(explicit=None):
    if explicit:
        if os.path.exists(explicit):
            return explicit
        raise SystemExit("指定的字体文件不存在：%s" % explicit)
    for p in CJK_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("未找到中文字体，请用 --font 指定一个 .ttf/.ttc 字体文件")


# ----------------------------------------------------------------------------- pipeline

def parse_pages(spec, total):
    if not spec:
        return list(range(total))
    pages = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else total
            pages.extend(range(a - 1, min(b, total)))
        else:
            pages.append(int(part) - 1)
    return [p for p in pages if 0 <= p < total]


def chunk_blocks(blocks, max_chars=2600, max_items=14):
    chunks, cur, size = [], [], 0
    for b in blocks:
        if getattr(b, "is_figure", False):      # 兼容旧 Block 与新 layout2.Piece
            continue
        weight = len(b.text) + 12
        if cur and (size + weight > max_chars or len(cur) >= max_items):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += weight
    if cur:
        chunks.append(cur)
    return chunks


def cache_path_for(out_path, cache_dir=None, in_path=None):
    """译文缓存文件名。

    **按输入文件名派生**（而不是输出文件名）：否则换个输出名就会全部重新翻译，
    白白花钱。同一篇论文无论输出叫什么、放哪里，都共用同一份缓存。
    """
    stem = os.path.splitext(os.path.basename(in_path or out_path))[0]
    base = stem + ".cache.json"
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, base)
    return os.path.join(os.path.dirname(os.path.abspath(out_path)), base)


def load_cache(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(path, cache):
    """写缓存。失败（磁盘/权限/只读）只返回 False，绝不打断翻译流程。"""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def choose_provider_interactive(settings, catalog):
    """备用文本向导：不想开图形界面时，用 --wizard 走这里。"""
    items = catalog.get("providers", [])
    print("\n请选择翻译引擎：")
    for i, p in enumerate(items, 1):
        print("  %2d) %s" % (i, p.get("label", p["id"])))
        if p.get("note"):
            print("      %s" % p["note"])
    print()
    while True:
        raw = input("输入编号（直接回车 = %s）: " % "1").strip()
        if not raw:
            idx = 0
            break
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            idx = int(raw) - 1
            break
        print("请输入 1-%d 之间的数字。" % len(items))
    entry = items[idx]
    pid = entry["id"]

    key = ""
    if entry.get("api") != "google_web" and entry.get("api") != "ollama":
        key = input("粘贴 %s 的 API Key: " % entry.get("label", pid)).strip()
    model = input("模型名（回车用默认 %s）: " % entry.get("default_model", "")).strip() \
        or entry.get("default_model", "")

    settings["active_provider"] = pid
    settings.setdefault("providers", {})[pid] = {
        "api_key": key, "model": model, "base_url": entry.get("base_url", ""),
    }
    try:
        S.save_catalog(catalog)
        S.save_settings(settings)
        print("已保存到 %s\n" % S.settings_path())
    except Exception as e:
        print("（保存配置失败，本次仍继续：%s）\n" % e)
    return pid


def run_translation(pieces, prov, cache, cache_file, lang, workers, t0,
                    skip_api=False, label="段", allow_empty=False):
    """统一的翻译流程：查缓存 -> 分批并发翻译 -> 写缓存。

    pieces: 任何带 .text / .translation 属性的对象列表（Block 或 layout2.Piece 都行）
    返回 (已翻译数, 总数)
    """
    fresh = 0
    for b in pieces:
        key = sha1(b.text)
        if key in cache:
            b.translation = cache[key]
        else:
            fresh += 1
    log("== 缓存命中 %d %s，待翻译 %d %s（%.0f 字符）"
        % (len(pieces) - fresh, label, fresh, label,
           sum(len(b.text) for b in pieces if b.translation is None)))

    if skip_api:
        n_ok = sum(1 for b in pieces if b.translation)
        if n_ok == 0 and not allow_empty:
            raise SystemExit(
                "没有可用的译文缓存：%s\n"
                "  * 想真正翻译一次：去掉 --render-only\n"
                "  * 只想看排版效果：加 --dry-run（右栏显示原文占位）" % cache_file)
        return n_ok, len(pieces)

    chunks = chunk_blocks([b for b in pieces if b.translation is None])
    if chunks:
        log("== 分成 %d 批，并发 %d 路请求" % (len(chunks), workers))
        done = [0]
        lock = __import__("threading").Lock()
        fatal = []
        cache_warned = [False]
        bar = progress("翻译%s" % label, total=len(chunks), unit="批")

        def persist():
            if save_cache(cache_file, cache):
                return
            if not cache_warned[0]:
                cache_warned[0] = True
                bar.note("   [警告] 缓存写不进去（%s），本次译文只保存在内存里；"
                         "翻译本身不受影响，但中断后无法续跑。" % cache_file)

        def run(chunk, ci):
            for attempt in range(3):
                try:
                    res = translate_blocks(prov, [b.text for b in chunk], lang)
                    with lock:
                        for b, t in zip(chunk, res):
                            if t:
                                b.translation = t
                                cache[sha1(b.text)] = t
                        done[0] += 1
                        n_new = sum(1 for b in chunk if b.translation)
                        bar.step(extra="第 %d 批 · %d 条新译文" % (ci + 1, n_new))
                        persist()
                    return True
                except P.TranslationError as e:
                    msg = str(e)
                    bar.note("   批次 %d 失败：%s" % (ci + 1, msg[:200]))
                    if "拒绝访问" in msg or "没有填 API Key" in msg or "404" in msg:
                        with lock:
                            fatal.append(msg)
                        return False
                except Exception as e:
                    bar.note("   批次 %d 第 %d 次失败：%s" % (ci + 1, attempt + 1, str(e)[:120]))
                time.sleep(3 * (attempt + 1))
            return False

        try:
            with futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                list(ex.map(lambda cb: run(*cb), [(c, i) for i, c in enumerate(chunks)]))
        finally:
            bar.close()
        persist()
        if fatal and done[0] == 0:
            raise SystemExit("翻译全部失败：%s" % fatal[0])

    return sum(1 for b in pieces if b.translation), len(pieces)


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description="英文 PDF -> 左原文/右中文 双语对照 PDF")
    ap.add_argument("input", nargs="?", help="要翻译的英文 PDF")
    ap.add_argument("-o", "--output")
    ap.add_argument("--pages", help="只翻译这些页，如 1-5,8")
    ap.add_argument("--provider", help="用哪个引擎（deepseek/siliconflow/dashscope/ollama/google/custom-xxx）")
    ap.add_argument("--api-key", help="临时覆盖该引擎的 API Key")
    ap.add_argument("--base-url", help="临时覆盖接口地址")
    ap.add_argument("--model", help="临时覆盖模型名")
    ap.add_argument("--lang", help="目标语言，默认取设置里的值")
    ap.add_argument("--font", help="中文字体文件 .ttf/.ttc")
    ap.add_argument("--font-size", type=float, help="右栏中文字号（默认取设置里的值）")
    ap.add_argument("--zoom", type=float, default=2.0, help="--raster 时左栏原页面渲染倍率")
    ap.add_argument("--raster", action="store_true",
                    help="左栏用位图渲染（默认矢量嵌入：更小、文字可选）")
    ap.add_argument("--workers", type=int, help="并发翻译请求数（默认取设置里的值）")
    ap.add_argument("--no-figures", action="store_true", help="不输出图表占位说明")
    ap.add_argument("--no-ocr", action="store_true",
                    help="跳过位图图表的 OCR（受限沙箱里 OCR 不可用时用这个明确跳过）")
    ap.add_argument("--no-progress", action="store_true",
                    help="关闭进度条/计时（日志更干净，适合重定向到文件）")
    ap.add_argument("--layout", choices=["smart", "region"], default="smart",
                    help="排版模式：smart=右半页整宽正文流（默认）；"
                         "region=译文按原文栏结构分左右两栏（2×2 对照）")
    ap.add_argument("--workers-progress", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--cache-dir", help="译文缓存目录（默认放在输出文件旁边）")
    ap.add_argument("--settings", action="store_true", help="打开图形设置界面后退出")
    ap.add_argument("--list-providers", action="store_true", help="列出所有可用引擎后退出")
    ap.add_argument("--no-interactive", action="store_true", help="已废弃，保留兼容")
    ap.add_argument("--wizard", action="store_true",
                    help="配置不全时进入文本向导（默认改为提示打开图形设置界面）")
    ap.add_argument("--render-only", action="store_true",
                    help="不调用翻译 API，直接用 cache\\ 里的现有译文重新排版出 PDF（改字号/版式时用）")
    ap.add_argument("--legacy-layout", action="store_true",
                    help="用旧版排版（不区分正文/注释/图表）")
    ap.add_argument("--no-notes", action="store_true", help="不输出页脚注释译文")
    ap.add_argument("--no-captions", action="store_true", help="不输出图表标题译文")
    ap.add_argument("--translate-figures", action="store_true",
                    help="把图表本身也翻译好嵌进右栏（白色覆盖原文位置后回贴中文；纯图片不翻译）")
    ap.add_argument("--no-figure-captions", action="store_true",
                    help="图表已翻译时，不再额外输出 [图/表] 文字区")
    ap.add_argument("--dry-run", action="store_true",
                    help="不调用翻译 API，只验证抽取与排版（右栏显示原文占位）")
    args = ap.parse_args()

    # 运行时目录自愈：仓库里不带 input/output/cache/inbox（不入版本库），
    # 新克隆下来第一次跑要能自己补齐，否则会直接报错找不到目录。
    try:
        S.ensure_dirs()
    except Exception:
        pass

    # 进度条：默认开；--no-progress 关闭（重定向日志时更干净）
    PG.set_enabled(not getattr(args, "no_progress", False))

    # --- 纯设置类操作 ---
    if args.settings:
        import settings_ui
        settings_ui.launch()
        return
    if args.list_providers:
        cat = S.load_catalog()
        s = S.load_settings()
        print("当前引擎：%s\n" % S.active_label(s, cat))
        for p in cat.get("providers", []):
            ov = (s.get("providers") or {}).get(p["id"], {})
            has = "已配置 Key" if ov.get("api_key") else ("无需 Key" if p.get("api") in
                                                          ("google_web", "ollama") else "缺 Key")
            print("  %-14s %-26s %-10s %s" % (p["id"], p.get("label", ""), has,
                                              p.get("base_url", "")))
        return

    if not args.input:
        ap.error("缺少输入 PDF（或加 --settings 打开设置界面）")
    if not os.path.exists(args.input):
        raise SystemExit("找不到输入文件：%s" % args.input)

    # --- 设置与引擎 ---
    settings = S.load_settings()
    catalog = S.load_catalog()
    pdf_cfg = settings.get("pdf", {})
    font_size = args.font_size if args.font_size else float(pdf_cfg.get("font_size", 9.5))
    workers = args.workers if args.workers else int(pdf_cfg.get("workers", 4))
    raster = args.raster or bool(pdf_cfg.get("raster", False))
    with_figures = (not args.no_figures) and bool(pdf_cfg.get("figures", True))
    lang = args.lang or settings.get("target_language") or P.DEFAULT_TARGET_LANG
    translate_figures = args.translate_figures or bool(pdf_cfg.get("translate_figures", False))

    pid = args.provider or settings.get("active_provider")
    prov = None
    if not args.dry_run and not args.render_only:
        entry = S.provider_entry(catalog, pid)
        if pid == "auto" or entry is None or (entry.get("api") not in ("google_web", "ollama")
                                              and not (settings.get("providers", {})
                                                       .get(pid, {}).get("api_key"))
                                              and not args.api_key):
            if not args.wizard:
                # 不在无交互环境里等待输入：直接告诉用户去图形界面配置
                raise SystemExit(
                    "当前引擎「%s」还没有配置好（缺 API Key 或引擎不存在）。\n\n"
                    "请双击  tool\\设置翻译引擎.cmd  打开设置界面：\n"
                    "  选择引擎 → 填 API Key → 点「测试连接」→ 点「保存并设为当前引擎」\n\n"
                    "也可以临时指定：  run.cmd 论文.pdf --api-key sk-xxx --provider deepseek\n"
                    "只想验证排版不花钱：  run.cmd 论文.pdf --dry-run\n"
                    "想用文本向导配置：    run.cmd 论文.pdf --wizard" % pid)
            pid = choose_provider_interactive(settings, catalog)
            settings = S.load_settings()
            entry = S.provider_entry(catalog, pid)

        cfg = P.merge_settings(entry, settings)
        if args.api_key:
            cfg["api_key"] = args.api_key
        if args.base_url:
            cfg["base_url"] = args.base_url
        if args.model:
            cfg["model"] = args.model
        prov = P.build(cfg)
        if args.base_url and args.base_url.rstrip("/") != (entry.get("base_url") or "").rstrip("/"):
            prov.label = "%s（本次使用自定义地址）" % prov.label
        try:
            prov.check_ready()
        except P.TranslationError as e:
            raise SystemExit("%s\n提示：运行 run.cmd --settings 打开设置界面。" % e)

    out_path = args.output or (os.path.splitext(args.input)[0] + ".bilingual.pdf")
    zh_font = find_cjk_font(args.font)

    doc = pymupdf.open(args.input)
    pages = parse_pages(args.pages, doc.page_count)
    if not pages:
        raise SystemExit("没有可翻译的页面")

    t0 = time.time()
    log("== 输入: %s（%d 页，翻译 %d 页）" % (os.path.basename(args.input), doc.page_count, len(pages)))
    if prov is not None:
        log("== 引擎: %s | 模型: %s | 目标语言: %s" % (prov.label, prov.model, lang))

    cache_file = cache_path_for(out_path, args.cache_dir, in_path=args.input)
    cache = load_cache(cache_file)

    use_new = (L is not None) and (not args.legacy_layout) \
        and (pdf_cfg.get("layout", "smart") != "legacy")
    if args.legacy_layout:
        log("== 排版模式：legacy（旧版混排）")
    elif use_new:
        log("== 排版模式：smart（正文/注释/图表分流，正文跨页连续）")
    show_notes = (not args.no_notes) and bool(pdf_cfg.get("notes", True))
    show_captions = (not args.no_captions) and bool(pdf_cfg.get("captions", True))
    saved_to, over = None, []
    n_ok = n_total = 0
    stats = {}

    if use_new:
        # ---------------- 新流程：正文 / 注释 / 图表 分流 ----------------
        _an = [0]
        _an = [0]
        with Ticker("分析页面（正文/注释/图表分流）") as _tk:
            def _on_pg(*a):
                _an[0] += 1
                _tk.tick("已处理 %d 页" % _an[0])
            per_page, cstats = L.extract_document(doc, pages, on_page=_on_pg)
            _tk.final = "共 %d 页" % _an[0]
        body_pieces = [b for p in pages for b in per_page[p] if b.kind == L.KIND_BODY]
        note_pieces = [b for p in pages for b in per_page[p] if b.kind == L.KIND_NOTE]
        cap_pieces = [b for p in pages for b in per_page[p] if b.kind == L.KIND_CAPTION]
        table_pieces = [b for p in pages for b in per_page[p] if b.kind == L.KIND_TABLE]
        # tablelab rebuilds tables cell-by-cell, while extract_document groups a
        # few neighbouring cells into one PDF text block.  Add the individual
        # cell strings to the translation queue so the rebuilt table can find
        # per-cell translations under the same hash key later.
        table_cell_pieces = []
        try:
            import tablelab as TL
            seen_cells = set()
            for pno in pages:
                for _cap_rect, _cap_text, _band, rows in TL.table_cells(doc, pno):
                    for row in rows:
                        for _rect, cell_text in row:
                            txt = " ".join(str(cell_text).split())
                            if len(txt) < 3:
                                continue
                            if L.NUMISH.match(txt) or L.SYMBOLIC.match(txt):
                                continue
                            key = sha1(txt)
                            if key in cache or key in seen_cells:
                                continue
                            seen_cells.add(key)
                            table_cell_pieces.append(
                                L.Piece(pno, tuple(_rect), txt, 9.0, L.KIND_TABLE))
        except Exception as e:
            log("   表格逐格翻译准备跳过：%s" % str(e)[:100])
        # figlab may merge a caption that was split across adjacent text blocks.
        # The original caption pieces are already in cap_pieces, but the merged
        # full text needs its own translation key for figbuild to use it.
        extra_caption_pieces = []
        try:
            import figlab as FL
            seen_caps = set()
            for pno in pages:
                for _r, _text, _kind in FL.find_captions(doc[pno]):
                    txt_norm = " ".join(str(_text).split())
                    if len(txt_norm) < 8 or txt_norm in seen_caps:
                        continue
                    if any(" ".join(b.text.split()) == txt_norm for b in cap_pieces):
                        continue
                    key_raw = sha1(str(_text).strip())
                    key_norm = sha1(txt_norm)
                    if key_raw in cache or key_norm in cache:
                        continue
                    seen_caps.add(txt_norm)
                    extra_caption_pieces.append(
                        L.Piece(pno, tuple(_r), str(_text), 9.0, L.KIND_CAPTION))
        except Exception as e:
            log("   合并图注翻译准备跳过：%s" % str(e)[:100])
        todo = (body_pieces + note_pieces + cap_pieces + table_pieces
                + table_cell_pieces + extra_caption_pieces)
        log("== 分流完成：正文 %d 块 / 注释 %d 块 / 图表标题 %d 块 / 表格块 %d + 逐格 %d + 合并图注 %d；"
            "剔除图表噪音 %d 块、页眉页脚 %d 块"
            % (len(body_pieces), len(note_pieces), len(cap_pieces),
               len(table_pieces), len(table_cell_pieces), len(extra_caption_pieces),
               cstats["fig_dropped"], cstats["junk_dropped"]))
        if note_pieces:
            log("   注释示例：%s" % note_pieces[0].text.replace("\n", " ")[:60])

        n_ok, n_total = run_translation(todo, prov, cache, cache_file, lang, workers, t0,
                                        skip_api=(args.dry_run or args.render_only),
                                        label="块", allow_empty=args.dry_run)
        calls = getattr(prov, "calls", 0)
        log("== 翻译完成 %d/%d 块（API 调用 %d 次，约 %.0f 输入字符）"
            % (n_ok, n_total, calls, getattr(prov, "chars", 0)))
        if n_ok < n_total:
            log("== 注意：有 %d 块没拿到译文，重跑同一命令会自动补翻。" % (n_total - n_ok))

        log("== 生成对照 PDF（左＝原文；右栏＝正文流 → [注] → [图]）…")
        figpages = None
        if translate_figures and FP is not None:
            log("== 翻译图表并嵌回右栏（白底覆盖原文位置）…")
            try:
                import ocrlab as _OL0
                _OL0.set_enabled(not args.no_ocr)
                _OL0.set_warn(not args.no_ocr)      # 明确跳过时不再告警
                _OL0.reset_denied()
                if args.no_ocr:
                    log("   （已按要求跳过 OCR：位图图表的图内文字不翻译）")
            except Exception:
                pass
            fp_cache = dict(cache)
            # 图内文字先补翻（复用同一条缓存，术语与正文一致）
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import figterms as FT
                with progress("扫描图内文字", total=len(pages), unit="页") as _pb:
                    labels = FT.collect_labels(doc, pages, on_page=_pb.step)
                miss = [t for t in labels if FT.key_of(t) not in fp_cache]
                if miss and prov is not None:
                    log("   图内新词 %d 个，送翻译…" % len(miss))
                    n_batch = (len(miss) + 19) // 20
                    with progress("翻译图内文字", total=n_batch, unit="批") as _pb2:
                        for i in range(0, len(miss), 20):
                            batch = miss[i:i + 20]
                            try:
                                res = translate_blocks(prov, batch, lang)
                            except Exception as e:
                                _pb2.note("   图内词批次失败：%s" % str(e)[:80])
                                _pb2.step()
                                continue
                            n_new = 0
                            for t, tr in zip(batch, res):
                                if tr:
                                    fp_cache[FT.key_of(t)] = tr
                                    cache[FT.key_of(t)] = tr
                                    n_new += 1
                            _pb2.step(extra="第 %d 批 · 新增 %d 条" % (i // 20 + 1, n_new))
                    save_cache(cache_file, cache)
            except Exception as e:
                log("   图内文字翻译跳过：%s" % str(e)[:100])
            _figbar = progress("生成中文图表页", total=len(pages), unit="页")
            figpages = FP.build_all(doc, pages, zh_font, fp_cache, log=log,
                                    on_page=lambda *a: _figbar.step())
            _figbar.close()
            log("   图表翻译完成：%d 页（矢量文字 %d 处，OCR 位图 %d/%d 处，纯图片 %d 处）"
                % (figpages.stats["figures"], figpages.stats["labels"],
                   figpages.stats.get("ocr_translated", 0), figpages.stats.get("ocr", 0),
                   figpages.stats["raster"]))
            # OCR 被限制时必须明说，否则成品会悄悄少掉位图图内文字
            try:
                import ocrlab as _OL
                if _OL.denied_count():
                    log("   ⚠ 本次有 %d 张位图图表未能 OCR：图内英文未翻译（其余部分正常）。"
                        % _OL.denied_count())
                    log("     原因：Windows OCR 的文件代理拒绝受限进程令牌读取工作区文件（0x80070005）。")
                    log("     解决：在非沙箱/提权环境重跑同一条命令即可补齐（译文已缓存，很快）；"
                        "不想跑 OCR 可加 --no-ocr。")
            except Exception:
                pass

        _laybar = progress("排版右栏译文", total=len(pages), unit="页")

        def _lay_cb(done, total, note=""):
            _laybar.set(done, extra=note)

        if getattr(args, "layout", "smart") == "region":
            # ---------------- 区域化排版（2×2：原文左|右 / 译文左|右） ----------------
            try:
                import regionlayout as RL
            except ImportError as e:
                raise SystemExit("无法加载区域化排版模块：%s" % e)
            _laybar.note("   排版模式：region（译文按原文栏结构分左右两栏）")
            saved_to, rst = RL.render_regions(
                doc, per_page, pages, out_path, zh_font,
                font_size=font_size, raster=raster, zoom=args.zoom, log=log,
                figpages=figpages, cache=cache, on_page=_lay_cb)
            _laybar.close()
            # 归一化统计字段，便于与旧渲染器统一汇报
            stats = {
                "body_pieces": sum(1 for p in pages for b in per_page[p]
                                   if b.kind == L.KIND_BODY),
                "notes": rst.get("one_pages", 0),
                "captions": rst.get("two_band_pages", 0),
                "continuation_pages": rst.get("cont_pages", 0),
            }
            log("== 排版：区域化 %d 页（双栏对应 %d / 单栏 %d）图表 %d 张，"
                "顺延 %d 块，续页 %d 页，仍未放下 %d 块"
                % (rst.get("pages", 0), rst.get("two_band_pages", 0),
                   rst.get("one_pages", 0), rst.get("figures", 0),
                   rst.get("deferred_pieces", 0), rst.get("cont_pages", 0),
                   rst.get("leftover", 0)))
        else:
            saved_to, stats = L.render_bilingual2(
                doc, per_page, pages, out_path, zh_font,
                font_size=font_size, zoom=args.zoom, raster=raster,
                show_caption=show_captions, show_note=show_notes,
                figpages=figpages, on_page=_lay_cb,
                caption_zone=(show_captions and not translate_figures)
                if not args.no_captions else False)
            _laybar.close()
            log("== 排版：正文 %d 块连续排版，注释 %d 条、图表标题 %d 条单独成段，续页 %d 页"
                % (stats["body_pieces"], stats["notes"], stats["captions"],
                   stats["continuation_pages"]))
    else:
        # ---------------- 旧流程（--legacy-layout） ----------------
        blocks_by_page = extract_blocks(doc, pages)
        all_blocks = [b for p in pages for b in blocks_by_page[p]]
        todo = [b for b in all_blocks if not b.is_figure]
        log("== 抽取到 %d 个段落块，其中图片/图表块 %d 个"
            % (len(todo), len(all_blocks) - len(todo)))
        n_ok, n_total = run_translation(todo, prov, cache, cache_file, lang, workers, t0,
                                        skip_api=(args.dry_run or args.render_only),
                                        label="段", allow_empty=args.dry_run)
        log("== 翻译完成 %d/%d 段（API 调用 %d 次）"
            % (n_ok, n_total, getattr(prov, "calls", 0)))
        log("== 生成对照 PDF（旧版排版）…")
        saved_to, over = render_bilingual(doc, blocks_by_page, pages, out_path, zh_font,
                                          font_size=font_size, zoom=args.zoom,
                                          with_figures=with_figures, raster=raster)

    doc.close()
    size_mb = os.path.getsize(saved_to) / 1048576.0
    if over:
        log("== 提示：第 %s 页中文较长，已用「续页」接在后面。" % ", ".join(str(p) for p in over[:12]))
    log("== 完成: %s（%.1f MB，用时 %.0fs）" % (saved_to, size_mb, time.time() - t0))
    print("OUTPUT=" + os.path.abspath(saved_to))


if __name__ == "__main__":
    main()
