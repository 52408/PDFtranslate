# -*- coding: utf-8 -*-
"""ocrlab.py - Windows OCR bridge for simple raster diagrams.

Vector figures are still handled by figlab/figbuild.  This module only handles
raster regions that look like line-art diagrams / flowcharts: white background,
few colours, moderate dark-pixel ratio.  Complex screenshots and photos are
left untouched by design.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

import pymupdf

try:
    import numpy as np
    from PIL import Image
    _IMAGE_AVAILABLE = True
except Exception:
    np = None
    Image = None
    _IMAGE_AVAILABLE = False

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
OCR_PS1 = os.path.join(TOOL_DIR, "ocr_win.ps1")
NUMISH = re.compile(r"^[\s\d\.\,\-\+%\(\)\[\]/*=<>~±:;'\"°×−–—|\\]+$")

_CACHE = {}
_TMPDIR = None
_SEQ = iter(range(1, 10 ** 6))
_ENABLED = True                 # 可由 CLI 关闭（--no-ocr）
_DENIED = 0                     # 因权限被拒的次数（用于显式告警，避免静默降级）
_WARN = True                    # --no-ocr 时关掉告警，避免误导


def set_warn(v):
    global _WARN
    _WARN = bool(v)


def set_enabled(v):
    global _ENABLED
    _ENABLED = bool(v)


def enabled():
    return _ENABLED


def reset_denied():
    global _DENIED
    _DENIED = 0


def denied_count():
    return _DENIED


def _clean(t):
    return re.sub(r"\s+", " ", str(t)).strip()


def work_tmpdir():
    """OCR 中转目录。

    系统 %TEMP% 在受限沙箱里不可写（PyMuPDF 连 PNG 都存不下），
    所以优先用环境变量 PDFTRANS_TMP，其次放在工具目录旁的 cache\\ocr_tmp，
    保证任何沙箱模式下 OCR 都能工作。
    """
    global _TMPDIR
    if _TMPDIR:
        return _TMPDIR
    cand = []
    env = os.environ.get("PDFTRANS_TMP")
    if env:
        cand.append(env)
    try:
        import settings_store as SS
        root = SS.root_dir()
        cand.append(os.path.join(root, "cache", "ocr_tmp"))
    except Exception:
        pass
    cand.append(os.path.join(TOOL_DIR, "..", "..", "cache", "ocr_tmp"))
    cand.append(os.path.join(tempfile.gettempdir(), "pdftrans_ocr_tmp"))
    for d in cand:
        try:
            d = os.path.abspath(d)
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".w")
            with open(probe, "wb") as f:
                f.write(b"1")
            os.remove(probe)
            _TMPDIR = d
            return d
        except Exception:
            continue
    _TMPDIR = tempfile.mkdtemp(prefix="pdftrans_ocr_")
    return _TMPDIR


def looks_translatable(t):
    t = _clean(t)
    if len(t) < 2 or len(t) > 120:
        return False
    if NUMISH.match(t):
        return False
    return sum(1 for c in t if c.isalpha()) >= 2


def simple_diagram_metrics(page, clip, zoom=1.5):
    """Return (is_simple, metrics) for a raster region."""
    if not _IMAGE_AVAILABLE:
        return False, {"error": "PIL/numpy unavailable"}
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    arr = np.asarray(img, dtype=np.uint8)
    if arr.size == 0:
        return False, {}
    # Sample down for speed; metrics remain stable enough for this decision.
    arr = arr[::2, ::2]
    white = float(np.mean(np.all(arr > 235, axis=2)))
    dark = float(np.mean(np.all(arr < 60, axis=2)))
    mx = arr.max(axis=2)
    mn = arr.min(axis=2)
    colorful = float(np.mean((mx - mn) > 30))
    q = (arr // 16).astype(np.uint8)
    colors = int(len(np.unique(q.reshape(-1, 3), axis=0)))
    # White-background diagrams/forms qualify; dark or busy screenshots do not.
    is_simple = ((white > 0.90 and dark < 0.02 and colors < 150)
                 or (white > 0.80 and dark < 0.05 and colors < 100))
    return is_simple, {"white": white, "dark": dark, "colorful": colorful,
                       "colors": colors, "size": (pix.width, pix.height)}


def has_real_image(page, clip):
    for img in page.get_images(full=True):
        try:
            rects = page.get_image_rects(img[0])
        except Exception:
            continue
        for r in rects:
            if (r & clip).get_area() > 0.3 * r.get_area():
                return True
    return False


def _run_windows_ocr(png_path, json_path, lang="zh-Hans-CN"):
    ps = shutil.which("powershell.exe") or "powershell.exe"
    if not os.path.exists(OCR_PS1):
        raise RuntimeError("ocr_win.ps1 not found")
    cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", OCR_PS1,
           "-ImagePath", png_path, "-OutputJson", json_path, "-Language", lang]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Windows OCR failed")[:300])
    with open(json_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = [data]
    return data or []


def _merge_boxes(boxes):
    """Merge OCR lines that are vertically adjacent in the same column."""
    groups = []
    for rect, text, size in sorted(boxes, key=lambda x: (x[0].y0, x[0].x0)):
        text = _clean(text)
        if not text:
            continue
        placed = False
        for g in groups:
            last = g["rect"]
            min_w = max(6.0, 0.35 * min(rect.width, last.width))
            same_col = abs(rect.x0 - last.x0) <= min_w
            gap = rect.y0 - last.y1
            if same_col and -2.0 <= gap <= 0.85 * max(rect.height, last.height):
                g["rect"] |= rect
                g["text"] = (g["text"].rstrip() + " " + text).strip()
                g["size"] = max(g["size"], size)
                placed = True
                break
        if not placed:
            groups.append({"rect": pymupdf.Rect(rect), "text": text, "size": size})
    return [(g["rect"], g["text"], g["size"]) for g in groups]


def ocr_region(page, clip, zoom=3.0, lang="zh-Hans-CN"):
    """OCR a PDF rectangle and return merged [(pdf_rect, text, est_size), ...].

    Results are cached for the current process, so figterms and figbuild do not
    OCR the same region twice.
    """
    key = (id(page.parent), page.number, tuple(round(v, 1) for v in clip),
           round(zoom, 2), lang)
    if not _ENABLED:                 # --no-ocr 时一次都不该走到这里
        return []
    if key in _CACHE:
        return _CACHE[key]
    tmpdir = None
    try:
        if not _IMAGE_AVAILABLE:
            return []
        base = work_tmpdir()
        # 注意：不要用 tempfile.mkdtemp(dir=base)——Windows 上它仍可能落到系统
        # 临时区（被沙箱封锁），导致 pix.save 报 Permission denied。这里自己建。
        tmpdir = os.path.join(base, "r_%d_%d" % (os.getpid(), next(_SEQ)))
        os.makedirs(tmpdir, exist_ok=True)
        png = os.path.join(tmpdir, "region.png")
        js = os.path.join(tmpdir, "region.json")
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
        pix.save(png)
        raw = _run_windows_ocr(png, js, lang)
        boxes = []
        for it in raw:
            try:
                r = pymupdf.Rect(clip.x0 + float(it["x"]) / zoom,
                                 clip.y0 + float(it["y"]) / zoom,
                                 clip.x0 + (float(it["x"]) + float(it["w"])) / zoom,
                                 clip.y0 + (float(it["y"]) + float(it["h"])) / zoom)
                boxes.append((r, it.get("text", ""), 10.0))
            except Exception:
                continue
        merged = [b for b in _merge_boxes(boxes) if looks_translatable(b[1])]
        _CACHE[key] = merged
        return merged
    except Exception as e:
        # 失败原因要看得见：OCR 是"兜底增强"，不该让整篇翻译失败，
        # 但也不能静默——否则成品里会悄悄少掉图内文字译文。
        global _DENIED
        msg = str(e)
        low = msg.lower()
        if ("access-denied" in low or "denied" in low or "拒绝访问" in msg
                or "UnauthorizedAccess" in msg):
            _DENIED += 1
            if _DENIED <= 2 and _WARN:
                print("   [OCR 不可用] WinRT 无法读取工作区文件（受限进程令牌下 "
                      "GetFileFromPathAsync 报 0x80070005）。", flush=True)
        else:
            print("   [OCR 跳过] %s: %s" % (type(e).__name__, msg[:200]), flush=True)
        _CACHE[key] = []
        return []
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def is_ocr_candidate(page, clip):
    """True only for simple, label-like diagrams (not dense text screenshots)."""
    if not _ENABLED:
        return False
    if not has_real_image(page, clip):
        return False
    simple, _metrics = simple_diagram_metrics(page, clip)
    if not simple:
        return False
    boxes = ocr_region(page, clip)
    if not boxes or len(boxes) > 24:
        return False
    avg_len = sum(len(t) for _r, t, _s in boxes) / float(len(boxes))
    return avg_len <= 35.0


def collect_ocr_labels(doc, pages=None):
    """Collect translatable OCR labels from simple raster figure regions."""
    import figlab as FL
    pages = pages or range(doc.page_count)
    seen = {}
    for pno in pages:
        page = doc[pno]
        for clip, _cap_rect, _cap_text, _kind in FL.figure_regions(page):
            if not has_real_image(page, clip):
                continue
            simple, _metrics = simple_diagram_metrics(page, clip)
            if not simple:
                continue
            for _r, text, _s in ocr_region(page, clip):
                if looks_translatable(text):
                    t = _clean(text)
                    seen[t] = seen.get(t, 0) + 1
    return seen
