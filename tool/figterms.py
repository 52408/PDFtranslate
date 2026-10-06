# -*- coding: utf-8 -*-
"""figterms.py —— 把图内标签送去翻译并写入缓存（与正文共用同一条缓存）。

只翻译"看起来是词/短语"的标签：
  * 长度 ≤ 80 字符
  * 含至少 2 个字母
  * 不是纯数字/纯符号（如 "0.5"、"p<0.05"、"36.0"）
  * 不是纯公式片段

翻译后写入 cache（键与正文相同：sha1(清理后文本)），
这样正文里出现同词时会复用，图文术语自然一致。
"""

import hashlib
import json
import os
import re
import sys
import time

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import figlab as FL
import ocrlab as OL
import providers as P
import settings_store as S

NUMISH = re.compile(r"^[\s\d\.\,\-\+%\(\)\[\]/*=<>~±:;'\"°×−–—|\\]+$")


def clean(t):
    t = re.sub(r"[\u200b\u200e\u200f\ufeff]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def looks_translatable(t):
    t = clean(t)
    if len(t) < 2 or len(t) > 80:
        return False
    if NUMISH.match(t):
        return False
    letters = sum(1 for c in t if c.isalpha())
    return letters >= 2


def key_of(t):
    return hashlib.sha1(clean(t).encode("utf-8")).hexdigest()[:16]


def collect_labels(doc, pages=None, on_page=None):
    """收集所有图表区域内的可翻译文字（与 figbuild 用同一套合并规则，键才能对上）。

    on_page(已完成页数, 总页数)：供进度条使用，可为 None。
    """
    import figbuild as FB
    pages = list(pages) if pages is not None else list(range(doc.page_count))
    total = len(pages)
    seen = {}
    for i, pno in enumerate(pages):
        page = doc[pno]
        body = FL.page_body_size(page)
        for clip, cap_rect, cap_text, kind in FL.figure_regions(page):
            spans = FL.texts_in_region(page, clip, body)
            merged = FB.merge_label_spans([(r, t, s) for r, t, s, _ in spans], body)
            for r, t, size in merged:
                if looks_translatable(t):
                    seen[clean(t)] = seen.get(clean(t), 0) + 1
            if not merged and OL.enabled() and OL.is_ocr_candidate(page, clip):
                try:
                    for _r, ot, _s in OL.ocr_region(page, clip):
                        if looks_translatable(ot):
                            seen[clean(ot)] = seen.get(clean(ot), 0) + 1
                except Exception:
                    pass
            if cap_text and looks_translatable(cap_text):
                seen[clean(cap_text)] = seen.get(clean(cap_text), 0) + 1
        if on_page:
            try:
                on_page(i + 1, total, True)
            except TypeError:
                on_page()
    return seen


def main():
    pdf = sys.argv[1]
    cache_path = sys.argv[2]
    provider_id = sys.argv[3] if len(sys.argv) > 3 else None
    workers = int(sys.argv[4]) if len(sys.argv) > 4 else 4

    cache = {}
    if os.path.exists(cache_path):
        cache = json.load(open(cache_path, encoding="utf-8"))

    doc = pymupdf.open(pdf)
    labels = collect_labels(doc)
    todo = [(t, n) for t, n in labels.items() if key_of(t) not in cache]
    todo.sort(key=lambda x: -x[1])
    print("图内可翻译标签 %d 个，其中缺译文 %d 个" % (len(labels), len(todo)))

    if not todo:
        print("无需翻译")
        return
    settings = S.load_settings()
    catalog = S.load_catalog()
    cfg, prov = S.resolve(settings, catalog, provider_id)
    print("使用引擎: %s | 模型: %s" % (prov.label, prov.model))

    lang = settings.get("target_language") or P.DEFAULT_TARGET_LANG
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import pdf_bilingual as PB

    batches = [todo[i:i + 20] for i in range(0, len(todo), 20)]
    done = 0
    t0 = time.time()
    for bi, batch in enumerate(batches):
        texts = [t for t, _ in batch]
        try:
            res = PB.translate_blocks(prov, texts, lang)
        except Exception as e:
            print("  批次 %d 失败: %s" % (bi + 1, str(e)[:120]))
            continue
        for t, tr in zip(texts, res):
            if tr:
                cache[key_of(t)] = tr
        done += 1
        if done % 3 == 0 or done == len(batches):
            print("  [%d/%d 批] 累计 %.0fs" % (done, len(batches), time.time() - t0))
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    print("完成，缓存已更新: %s（共 %d 条）" % (cache_path, len(cache)))


if __name__ == "__main__":
    main()
