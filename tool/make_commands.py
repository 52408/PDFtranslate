# -*- coding: utf-8 -*-
"""make_commands.py —— 自动检测 input\\ 里的 PDF，按 README「路线 A」生成可复制的命令。

界面里做了三件事：
  1. 列出 input\\ 下所有 PDF（可浏览其它目录）
  2. 为选中的文件生成路线 A 的四条命令，一键复制
  3. 可直接在界面里执行：整篇 / 前 N 页 / 预览排版（不花钱）/ 换引擎

命令模板与 README「路线 A」保持一致：
  run.cmd ..\\input\\<文件名>                  :: 整篇
  run.cmd ..\\input\\<文件名> --pages 1-5      :: 先翻 5 页看效果
  run.cmd ..\\input\\<文件名> --dry-run        :: 不花钱，只验证排版
  run.cmd --list-providers                     :: 看引擎列表和 Key 状态
  run.cmd --settings                           :: 打开设置界面
"""

import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import settings_store as S

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INPUT = os.path.join(ROOT, "input")
OUTPUT = os.path.join(ROOT, "output")
INBOX = os.path.join(ROOT, "inbox")


def scan_pdfs(folder):
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(".pdf"))


class CommandMaker:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.proc = None
        self.folder = INPUT
        self.files = []

        root.title("PDFtrans 命令生成器")
        root.geometry("900x660")
        root.minsize(820, 600)

        self._build()
        self.reload()
        self.root.after(150, self._drain)

    # ------------------------------------------------------------------ 布局

    def _build(self):
        top = ttk.Frame(self.root, padding=(14, 12, 14, 4))
        top.pack(fill="x")
        ttk.Label(top, text="检测到的 PDF", font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self.lbl_count = ttk.Label(top, text="", foreground="#0a6")
        self.lbl_count.pack(side="left", padx=10)
        ttk.Button(top, text="重新扫描", command=self.reload).pack(side="right")
        ttk.Button(top, text="浏览其它目录…", command=self.browse).pack(side="right", padx=8)
        self.lbl_engine = ttk.Label(top, text="", foreground="#666")
        self.lbl_engine.pack(side="right", padx=12)

        body = ttk.Frame(self.root, padding=(14, 6, 14, 6))
        body.pack(fill="both", expand=True)

        left = ttk.LabelFrame(body, text="PDF 文件", padding=6)
        left.pack(side="left", fill="both")
        self.lst = tk.Listbox(left, width=32, height=14, exportselection=False,
                              font=("Microsoft YaHei UI", 9))
        self.lst.pack(side="left", fill="y")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.lst.yview)
        sb.pack(side="left", fill="y")
        self.lst.configure(yscrollcommand=sb.set)
        self.lst.bind("<<ListboxSelect>>", lambda e: self.render())

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        opt = ttk.LabelFrame(right, text="生成选项", padding=10)
        opt.pack(fill="x")
        opt.columnconfigure(1, weight=1)

        ttk.Label(opt, text="试翻页数").grid(row=0, column=0, sticky="w", pady=3)
        self.var_pages = tk.StringVar(value="1-5")
        ttk.Entry(opt, textvariable=self.var_pages, width=12).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(opt, text="（用于「先翻几页」那条命令）", foreground="#888").grid(
            row=0, column=2, sticky="w", padx=8)

        ttk.Label(opt, text="引擎覆盖").grid(row=1, column=0, sticky="w", pady=3)
        self.var_provider = tk.StringVar(value="（用当前引擎）")
        self.cmb = ttk.Combobox(opt, textvariable=self.var_provider, state="readonly", width=28)
        self.cmb.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(opt, text="（可选，只在生成的命令里加 --provider）",
                  foreground="#888").grid(row=1, column=2, sticky="w", padx=8)

        self.var_abs = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="用绝对路径（省得切目录）", variable=self.var_abs,
                        command=self.render).grid(row=2, column=0, columnspan=3, sticky="w", pady=3)

        cf = ttk.LabelFrame(right, text="路线 A 命令（可直接复制）", padding=6)
        cf.pack(fill="both", expand=True, pady=(10, 0))
        self.txt = tk.Text(cf, height=11, wrap="none", font=("Consolas", 9))
        self.txt.pack(fill="both", expand=True)
        self.txt.configure(state="disabled")
        self.txt.tag_configure("comment", foreground="#888")
        self.txt.tag_configure("cmd", foreground="#000")

        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="复制全部命令", command=self.copy_all).pack(side="left")
        ttk.Button(bar, text="复制选中那条", command=self.copy_one).pack(side="left", padx=6)

        self.btns = ttk.Frame(right)
        self.btns.pack(fill="x", pady=(8, 0))
        ttk.Button(self.btns, text="① 预览排版（不花钱）", command=self.run_dry).pack(side="left")
        ttk.Button(self.btns, text="② 先翻几页", command=self.run_pages).pack(side="left", padx=6)
        ttk.Button(self.btns, text="③ 翻译整篇", command=self.run_full).pack(side="left", padx=6)
        ttk.Button(self.btns, text="打开 output 目录",
                   command=lambda: self.open_dir(OUTPUT)).pack(side="right")

        lf = ttk.LabelFrame(self.root, text="执行输出", padding=6)
        lf.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.log = tk.Text(lf, height=8, wrap="word", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ 数据

    def reload(self):
        self.files = scan_pdfs(self.folder)
        self.lst.delete(0, "end")
        for f in self.files:
            self.lst.insert("end", f)
        where = "input\\" if self.folder == INPUT else self.folder
        self.lbl_count.configure(text="%s  （%d 个）" % (where, len(self.files)))
        try:
            cat = S.load_catalog()
            st = S.load_settings()
            self.cmb.configure(values=["（用当前引擎）"] + [p["id"] for p in cat.get("providers", [])])
            self.lbl_engine.configure(text="当前引擎：%s" % S.active_label(st, cat))
        except Exception as e:
            self.lbl_engine.configure(text="读配置失败：%s" % e)
        if self.files:
            self.lst.selection_set(0)
            self.render()
        else:
            self.set_text([("（这个目录里没有 PDF）", "comment")])

    def browse(self):
        d = filedialog.askdirectory(title="选择存放 PDF 的目录", initialdir=self.folder)
        if d:
            self.folder = d
            self.reload()

    def selected(self):
        sel = self.lst.curselection()
        if not sel:
            return None
        return self.files[sel[0]]

    # ------------------------------------------------------------------ 生成命令

    def target_token(self, name):
        if self.var_abs.get():
            return os.path.join(self.folder, name)
        if self.folder == INPUT:
            return r"..\input" + "\\" + name
        return os.path.join(self.folder, name)

    def commands(self):
        """返回 [(命令行, 注释), ...]，与 README 路线 A 对应。"""
        name = self.selected()
        if not name:
            return []
        t = self.target_token(name)
        prov = self.var_provider.get()
        extra = "" if prov.startswith("（") else " --provider %s" % prov
        pages = self.var_pages.get().strip() or "1-5"
        stem = os.path.splitext(name)[0]
        tool_cd = "cd /d " + HERE
        cmds = [
            (tool_cd, "先切到 tool 目录（用绝对路径的命令可以跳过这步）"),
            ("run.cmd %s%s" % (t, extra), "整篇翻译 → ..\\output\\%s.bilingual.pdf" % stem),
            ("run.cmd %s --pages %s%s" % (t, pages, extra), "先翻 %s 页看效果（最省钱）" % pages),
            ("run.cmd %s --dry-run" % t, "不花钱，只验证抽取与排版"),
            ("run.cmd %s --raster" % t, "可选：左栏改位图渲染（默认矢量嵌入）"),
            ("run.cmd --list-providers", "看引擎列表和 Key 状态"),
            ("run.cmd --settings", "打开设置界面"),
        ]
        return cmds

    def set_text(self, items):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        for text, tag in items:
            self.txt.insert("end", text + "\n", tag)
        self.txt.configure(state="disabled")
        self.plain = [t for t, g in items if g == "cmd"]

    def render(self):
        cmds = self.commands()
        if not cmds:
            self.set_text([("请先在左边选一个 PDF", "comment")])
            return
        items = []
        self.command_lines = []
        for c, note in cmds:
            items.append((":: " + note, "comment"))
            items.append((c, "cmd"))
            items.append(("", "comment"))
            if note.startswith(("整篇", "先翻", "不花钱", "可选：左栏")):
                self.command_lines.append(c)
        self.set_text(items)

    def copy_all(self):
        cmds = getattr(self, "command_lines", [])
        if not cmds:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(cmds))
        self.echo("已复制 %d 条命令到剪贴板：" % len(cmds))
        for c in cmds:
            self.echo("   " + c)

    def copy_one(self):
        try:
            sel = self.txt.get("sel.first", "sel.last").strip()
        except tk.TclError:
            messagebox.showinfo("先选中", "请先用鼠标选中要复制的那一行。")
            return
        if not sel:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(sel)
        self.echo("已复制：" + sel)

    # ------------------------------------------------------------------ 执行

    def echo(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def open_dir(self, path):
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)  # noqa
        except Exception as e:
            messagebox.showwarning("打不开", str(e))

    def _set_buttons(self, state):
        for b in self.btns.winfo_children():
            try:
                b.configure(state=state)
            except Exception:
                pass

    def run_cmd(self, extra):
        name = self.selected()
        if not name:
            messagebox.showinfo("先选文件", "请先在左边选一个 PDF。")
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("正在忙", "上一条命令还在跑，等它结束。")
            return
        prov = self.var_provider.get()
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", os.path.join(HERE, "run.ps1"),
               os.path.join(self.folder, name)]
        if not prov.startswith("（"):
            cmd += ["--provider", prov]
        cmd += extra
        self.echo("")
        self.echo("$ " + " ".join(cmd[4:]))
        self._set_buttons("disabled")
        self.proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace",
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def pump():
            for line in self.proc.stdout:
                self.q.put(("log", line.rstrip()))
            rc = self.proc.wait()
            self.q.put(("done", rc))

        threading.Thread(target=pump, daemon=True).start()

    def run_dry(self):
        self.run_cmd(["--dry-run"])

    def run_pages(self):
        self.run_cmd(["--pages", self.var_pages.get().strip() or "1-5"])

    def run_full(self):
        self.run_cmd([])

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.echo(payload)
                else:
                    self._set_buttons("normal")
                    self.echo("—— 结束（返回码 %s）——" % payload)
                    if payload == 0:
                        self.echo("成品目录：%s" % OUTPUT)
        except queue.Empty:
            pass
        self.root.after(150, self._drain)


def cli_report(path=None, pages="1-5", provider=None):
    """无界面输出：把生成结果打印到终端（也方便脚本/自测调用）。"""
    if path:
        folder = os.path.dirname(os.path.abspath(path))
        name = os.path.basename(path)
        token = os.path.abspath(path)
    else:
        folder, name = INPUT, None
        files = scan_pdfs(folder)
        if not files:
            print("input\\ 目录里没有 PDF。用法：make_commands.py [某个.pdf]")
            return 1
        name = files[0]
        token = r"..\input" + "\\" + name

    try:
        cat, st = S.load_catalog(), S.load_settings()
        engine = S.active_label(st, cat)
        miss = [p["id"] for p in cat.get("providers", [])
                if p.get("api") not in ("google_web", "ollama")
                and not (st.get("providers", {}).get(p["id"], {}).get("api_key"))]
    except Exception as e:
        engine, miss = "读取失败：%s" % e, []

    extra = " --provider %s" % provider if provider else ""
    print("检测到 %d 个 PDF（%s）" % (len(scan_pdfs(folder)), folder))
    print("当前引擎：%s" % engine)
    if miss and not provider:
        print("尚未配置 Key 的引擎：%s" % ", ".join(miss))
    print()
    print(":: 先切到 tool 目录（用绝对路径的命令可以跳过这步）")
    print("cd /d %s" % HERE)
    print()
    print(":: 整篇翻译 → ..\\output\\%s.bilingual.pdf" % os.path.splitext(name)[0])
    print("run.cmd %s%s" % (token, extra))
    print()
    print(":: 先翻 %s 页看效果（最省钱）" % pages)
    print("run.cmd %s --pages %s%s" % (token, pages, extra))
    print()
    print(":: 不花钱，只验证抽取与排版")
    print("run.cmd %s --dry-run" % token)
    print()
    print(":: 看引擎列表和 Key 状态")
    print("run.cmd --list-providers")
    return 0


def launch(selftest_seconds=0, snapshot_path=None):
    root = tk.Tk()
    app = CommandMaker(root)
    if snapshot_path:
        def snap():
            try:
                root.update_idletasks()
                root.lift()
                root.attributes("-topmost", True)
                root.update()
                time.sleep(0.7)
                from PIL import ImageGrab
                x, y = root.winfo_rootx(), root.winfo_rooty()
                ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(snapshot_path)
                print("SNAPSHOT=" + snapshot_path)
            except Exception as e:
                print("SNAPSHOT FAILED: %s: %s" % (type(e).__name__, e))
            finally:
                root.destroy()
        root.after(1000, snap)
    if selftest_seconds:
        root.after(int(selftest_seconds * 1000), root.destroy)
    root.mainloop()


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if "--report" in args or (args and args[0].lower().endswith(".pdf")):
        pdf = next((a for a in args if a.lower().endswith(".pdf")), None)
        pages = "1-5"
        prov = None
        for i, a in enumerate(args):
            if a == "--pages" and i + 1 < len(args):
                pages = args[i + 1]
            if a == "--provider" and i + 1 < len(args):
                prov = args[i + 1]
        sys.exit(cli_report(pdf, pages, prov))
    secs = 3 if "--selftest" in args else 0
    snap = None
    for i, a in enumerate(args):
        if a == "--snapshot" and i + 1 < len(args):
            snap = args[i + 1]
    launch(secs, snap)
