# -*- coding: utf-8 -*-
"""translate_dropped.py —— 收货台：把 PDF 拖到「翻译拖进来的PDF.cmd」上，
或把 PDF 放进 ..\\inbox 后双击该 cmd，就会依次翻译，成品进 ..\\output。

引擎与 Key 取自 settings.json（用「设置翻译引擎.cmd」配置一次即可）。
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INBOX = os.path.join(ROOT, "inbox")
INPUT = os.path.join(ROOT, "input")
RUN_PS1 = os.path.join(HERE, "run.ps1")


def collect():
    args = [a for a in sys.argv[1:] if a.lower().endswith(".pdf")]
    if args:
        return args
    if not os.path.isdir(INBOX):
        return []
    return [os.path.join(INBOX, f) for f in sorted(os.listdir(INBOX))
            if f.lower().endswith(".pdf")]


def main():
    os.makedirs(INPUT, exist_ok=True)
    os.makedirs(INBOX, exist_ok=True)
    files = collect()
    if not files:
        print("没有找到 PDF。")
        print("用法一：把 PDF 拖到「翻译拖进来的PDF.cmd」文件图标上。")
        print("用法二：把 PDF 放进 %s 后双击该 cmd。" % INBOX)
        input("按回车退出…")
        return 1

    for src in files:
        if not os.path.exists(src):
            print("跳过（不存在）：%s" % src)
            continue
        dst = os.path.join(INPUT, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(dst):
            import shutil
            shutil.copy2(src, dst)
        print("已就位：%s" % dst)
        print("===== 开始翻译：%s =====" % os.path.basename(dst))
        rc = subprocess.call([
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", RUN_PS1, dst,
        ])
        print("===== 结束（返回码 %d）=====\n" % rc)

    print("成品目录：%s" % os.path.join(ROOT, "output"))
    input("按回车退出…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
