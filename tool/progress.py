# -*- coding: utf-8 -*-
"""progress.py —— 轻量进度条 / 计时器（零依赖，兼容重定向输出）。

为什么要这个东西：一份 30-40 页的论文要跑 1-3 分钟，中间只有零星几行日志，
看起来完全像卡死了。这里提供三种呈现：

  1) 真实终端：单行动态刷新进度条（\\r 覆盖），带耗时与 ETA
  2) 重定向到文件/管道：按百分比里程碑打印，每 10% 一行，不刷屏
  3) 关闭：只剩阶段标题（适合极简日志）

用法：
    with progress("翻译正文", total=12, unit="批") as bar:
        for batch in batches:
            bar.step(extra="第 %d 批" % i)
    # 退出时自动打印 "✔ 翻译正文 12 批 · 34.2s"

    阶段计时：with phase("生成对照 PDF"): ...
"""

import os
import shutil
import sys
import time

_ENABLED = True
_SHOW_TIME = True


def set_enabled(v):
    global _ENABLED
    _ENABLED = bool(v)


def enabled():
    return _ENABLED


def _is_tty():
    try:
        return sys.stdout is not None and sys.stdout.isatty()
    except Exception:
        return False


def fmt_dur(sec):
    sec = max(0.0, float(sec))
    if sec < 60:
        return "%.1fs" % sec
    m, s = divmod(int(sec), 60)
    if m < 60:
        return "%d分%02d秒" % (m, s)
    h, m = divmod(m, 60)
    return "%d时%02d分" % (h, m)


def term_width(default=100):
    try:
        return max(60, min(160, shutil.get_terminal_size().columns))
    except Exception:
        return default


def bar_text(label, done, total, extra="", elapsed=0.0, width=24):
    """拼出一条进度条文本（不含尾部换行）。"""
    if total and total > 0:
        frac = max(0.0, min(1.0, done / float(total)))
        filled = int(round(frac * width))
        bar = "█" * filled + "░" * (width - filled)
        pct = "%3d%%" % int(round(frac * 100))
        cnt = "%d/%d" % (done, total)
        eta = ""
        if done > 0 and elapsed > 0:
            left = elapsed / frac - elapsed
            eta = " 剩~%s" % fmt_dur(left)
        head = "%s %s %s %s" % (label, bar, pct, cnt)
    else:
        head = label
        eta = ""
    tail = ""
    if extra:
        tail = "  " + str(extra)
    if _SHOW_TIME:
        tail += "  [%s%s]" % (fmt_dur(elapsed), eta)
    line = head + tail
    w = term_width()
    if len(line) > w - 1:
        line = line[:w - 2] + "…"
    return line


class _Bar:
    def __init__(self, label, total=None, unit="", min_interval=0.12, stream=None):
        self.label = label
        self.total = total
        self.unit = unit
        self.stream = stream or sys.stdout
        self.tty = _is_tty()
        self.t0 = time.time()
        self.done = 0
        self.last_draw = 0.0
        self.last_mark = -1
        self.min_interval = min_interval
        self.closed = False

    # ---------------------------------------------------------------- 内部
    def _draw(self, force=False):
        if not _ENABLED or self.closed:
            return
        now = time.time()
        if not force and now - self.last_draw < self.min_interval:
            return
        self.last_draw = now
        text = bar_text(self.label, self.done, self.total, self.extra,
                        elapsed=now - self.t0)
        if self.tty:
            pad = max(0, term_width() - len(text) - 1)
            self.stream.write("\r" + text + " " * pad)
        else:
            # 重定向：只在跨过 10% 里程碑时打印一行，避免日志刷屏
            if self.total:
                pct = int(self.done / float(self.total) * 10)
                if pct == self.last_mark and not force:
                    return
                self.last_mark = pct
            else:
                if not force:
                    return
            self.stream.write(text + "\n")
        try:
            self.stream.flush()
        except Exception:
            pass

    # ---------------------------------------------------------------- 公开
    extra = ""

    def step(self, n=1, extra=None):
        self.done += n
        if extra is not None:
            self.extra = extra
        self._draw()

    def set(self, done, extra=None):
        self.done = done
        if extra is not None:
            self.extra = extra
        self._draw()

    def note(self, msg):
        """在中途插一行说明（会清掉当前进度行再打印，保持不串行）。"""
        if not _ENABLED:
            print(msg, flush=True)
            return
        if self.tty:
            self.stream.write("\r" + " " * (term_width() - 1) + "\r")
        self.stream.write(msg + "\n")
        try:
            self.stream.flush()
        except Exception:
            pass
        self._draw(force=True)

    def close(self, note=None):
        if note:
            self.closed = True
            el = time.time() - self.t0
            if _ENABLED:
                text = "✔ %s · %s  (%s)" % (self.label, fmt_dur(el), note)
                if self.tty:
                    self.stream.write("\r" + " " * (term_width() - 1) + "\r")
                self.stream.write(text + "\n")
                try:
                    self.stream.flush()
                except Exception:
                    pass
            return
        if self.closed:
            return
        self.closed = True
        el = time.time() - self.t0
        if not _ENABLED:
            return
        text = "✔ %s %s" % (self.label, ("%d %s" % (self.done, self.unit)).strip())
        text += " · %s" % fmt_dur(el)
        if self.extra:
            text += "  (%s)" % self.extra
        if self.tty:
            self.stream.write("\r" + " " * (term_width() - 1) + "\r")
        self.stream.write(text + "\n")
        try:
            self.stream.flush()
        except Exception:
            pass

    def __enter__(self):
        self._draw(force=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None and _ENABLED:
            self.note("✘ %s 中断：%s" % (self.label, str(exc)[:120]))
        self.close()
        return False


def progress(label, total=None, unit="", stream=None):
    return _Bar(label, total=total, unit=unit, stream=stream)


class _Phase:
    def __init__(self, label):
        self.label = label
        self.t0 = time.time()

    def __enter__(self):
        if _ENABLED:
            print("▶ %s …" % self.label, flush=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        el = time.time() - self.t0
        if _ENABLED:
            mark = "✔" if exc_type is None else "✘"
            print("%s %s · %s" % (mark, self.label, fmt_dur(el)), flush=True)
        return False


def phase(label):
    return _Phase(label)


class Ticker:
    """未知总量的阶段（例如"逐页排版"）：按固定间隔打点显示心跳。"""

    def __init__(self, label, every=1.0):
        self.bar = _Bar(label, total=None, unit="")
        self.every = every
        self.t_last = 0.0
        self.final = ""

    def tick(self, extra=None):
        now = time.time()
        if now - self.t_last < self.every:
            return
        self.t_last = now
        if extra is not None:
            self.bar.extra = extra
        self.bar._draw(force=True)

    def close(self, note=None):
        if note:
            self.bar.extra = note
        elif self.final:
            note = self.final
        self.bar.close(note=note)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
