# -*- coding: utf-8 -*-
"""settings_ui.py —— 翻译引擎设置界面（tkinter，无需额外安装）。

功能：
  * 下拉选择翻译引擎（内置 DeepSeek / 硅基流动 / 通义千问 / Kimi / 智谱 / OpenAI / 本地 Ollama / 免费引擎）
  * 填 API Key、模型名、接口地址；「测试连接」当场验证 Key 是否可用
  * 「拉取模型列表」把服务端可用模型列出来，直接选
  * 「新增引擎」可接入任何 OpenAI 兼容服务（中转站、公司内网、自建 vLLM…），「删除引擎」移除自建项
  * 统一设置：目标语言、右栏字号、并发数等
  * 保存到 settings.json，主程序下次运行自动生效
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import providers as P
import settings_store as S

TEST_SENTENCE = ("The dominant sequence transduction models are based on complex recurrent or "
                 "convolutional neural networks that include an encoder and a decoder.")


class SettingsApp:
    def __init__(self, root):
        self.root = root
        self.catalog = S.load_catalog()
        self.settings = S.load_settings()
        self.q = queue.Queue()
        self.current_ids = []

        root.title("PDFtrans 翻译引擎设置")
        root.geometry("1000x840")
        root.minsize(920, 700)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        self._build_header()
        self._build_body()
        self._build_footer()
        self.refresh_provider_list()
        self.load_active_into_form()
        self.root.after(120, self._drain_queue)

    # ------------------------------------------------------------------ 布局

    def _build_header(self):
        bar = ttk.Frame(self.root, padding=(14, 12, 14, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="翻译引擎", font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self.lbl_active = ttk.Label(bar, text="", foreground="#0a6")
        self.lbl_active.pack(side="left", padx=12)
        ttk.Label(bar, text="（当前生效的引擎会用于 PDF 翻译）",
                  foreground="#666").pack(side="left")

    def _build_body(self):
        body = ttk.Frame(self.root, padding=(14, 0, 14, 0))
        body.pack(fill="both", expand=True)

        left = ttk.LabelFrame(body, text="可用引擎", padding=8)
        left.pack(side="left", fill="y")

        self.lst = tk.Listbox(left, width=26, height=19, exportselection=False,
                              font=("Microsoft YaHei UI", 9))
        self.lst.pack(side="left", fill="y")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.lst.yview)
        sb.pack(side="left", fill="y")
        self.lst.configure(yscrollcommand=sb.set)
        self.lst.bind("<<ListboxSelect>>", lambda e: self.on_pick())

        btns = ttk.Frame(left)
        btns.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(btns, text="使用此引擎", command=self.on_pick, width=11).pack(pady=2)
        ttk.Button(btns, text="新增引擎", command=self.on_add, width=11).pack(pady=2)
        ttk.Button(btns, text="删除引擎", command=self.on_delete, width=11).pack(pady=2)
        ttk.Separator(btns, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(btns, text="打开官网", command=self.on_homepage, width=11).pack(pady=2)
        ttk.Button(btns, text="重新载入", command=self.reload, width=11).pack(pady=2)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        # --- 引擎参数 ---
        pf = ttk.LabelFrame(right, text="这个引擎怎么连", padding=10)
        pf.pack(fill="x")
        pf.columnconfigure(1, weight=1)

        ttk.Label(pf, text="名称").grid(row=0, column=0, sticky="w", pady=3)
        self.var_label = tk.StringVar()
        ttk.Entry(pf, textvariable=self.var_label).grid(row=0, column=1, sticky="ew", pady=3)

        ttk.Label(pf, text="协议类型").grid(row=1, column=0, sticky="w", pady=3)
        self.var_api = tk.StringVar()
        self.cmb_api = ttk.Combobox(pf, textvariable=self.var_api, state="readonly",
                                    values=[t[1] for t in P.API_TYPES])
        self.cmb_api.grid(row=1, column=1, sticky="ew", pady=3)
        self.cmb_api.bind("<<ComboboxSelected>>", lambda e: self.on_api_type_change())

        ttk.Label(pf, text="API Key").grid(row=2, column=0, sticky="w", pady=3)
        keybox = ttk.Frame(pf)
        keybox.grid(row=2, column=1, sticky="ew", pady=3)
        keybox.columnconfigure(0, weight=1)
        self.var_key = tk.StringVar()
        self.ent_key = ttk.Entry(keybox, textvariable=self.var_key, show="*")
        self.ent_key.grid(row=0, column=0, sticky="ew")
        self.var_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(keybox, text="显示", variable=self.var_show,
                        command=self.toggle_key).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(keybox, text="粘贴", width=6,
                   command=self.paste_key).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(pf, text="接口地址").grid(row=3, column=0, sticky="w", pady=3)
        self.var_base = tk.StringVar()
        ttk.Entry(pf, textvariable=self.var_base).grid(row=3, column=1, sticky="ew", pady=3)

        ttk.Label(pf, text="模型名").grid(row=4, column=0, sticky="w", pady=3)
        mbox = ttk.Frame(pf)
        mbox.grid(row=4, column=1, sticky="ew", pady=3)
        mbox.columnconfigure(0, weight=1)
        self.var_model = tk.StringVar()
        self.cmb_model = ttk.Combobox(mbox, textvariable=self.var_model)
        self.cmb_model.grid(row=0, column=0, sticky="ew")
        self.btn_models = ttk.Button(mbox, text="拉取模型", width=9, command=self.on_list_models)
        self.btn_models.grid(row=0, column=1, padx=(6, 0))

        self.lbl_note = ttk.Label(pf, text="", foreground="#666", wraplength=380, justify="left")
        self.lbl_note.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.btn_test = ttk.Button(pf, text="测试连接（翻译一句样例）", command=self.on_test)
        self.btn_test.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # --- 统一选项 ---
        of = ttk.LabelFrame(right, text="统一选项", padding=10)
        of.pack(fill="x", pady=(10, 0))
        of.columnconfigure(1, weight=1)

        ttk.Label(of, text="目标语言").grid(row=0, column=0, sticky="w", pady=3)
        self.var_lang = tk.StringVar()
        ttk.Combobox(of, textvariable=self.var_lang, values=[
            "简体中文", "繁體中文", "English", "日本語", "한국어",
        ]).grid(row=0, column=1, sticky="ew", pady=3)

        ttk.Label(of, text="右栏字号").grid(row=1, column=0, sticky="w", pady=3)
        self.var_font = tk.DoubleVar(value=9.5)
        ttk.Spinbox(of, from_=7.0, to=14.0, increment=0.5, textvariable=self.var_font,
                    width=8).grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(of, text="并发请求数").grid(row=2, column=0, sticky="w", pady=3)
        self.var_workers = tk.IntVar(value=4)
        ttk.Spinbox(of, from_=1, to=16, increment=1, textvariable=self.var_workers,
                    width=8).grid(row=2, column=1, sticky="w", pady=3)

        self.var_raster = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="左栏用位图渲染（默认矢量嵌入，更小更清晰）",
                        variable=self.var_raster).grid(row=3, column=0, columnspan=2, sticky="w", pady=3)

        self.var_figures = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="遇图表输出占位说明",
                        variable=self.var_figures).grid(row=4, column=0, columnspan=2, sticky="w", pady=3)

        ttk.Label(of, text="排版模式").grid(row=5, column=0, sticky="w", pady=3)
        self.var_layout = tk.StringVar(value="smart")
        ttk.Combobox(of, textvariable=self.var_layout, state="readonly", values=[
            "smart", "legacy",
        ]).grid(row=5, column=1, sticky="ew", pady=3)
        ttk.Label(of, text="smart＝正文/注释/图表分流，正文跨页连续（推荐）；legacy＝旧版混排",
                  foreground="#888", wraplength=380, justify="left").grid(
            row=6, column=0, columnspan=2, sticky="w")

        self.var_notes = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="输出页脚注释译文（[注] 区）",
                        variable=self.var_notes).grid(row=7, column=0, columnspan=2, sticky="w", pady=3)
        self.var_caps = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="输出图表标题译文（[图] 区）",
                        variable=self.var_caps).grid(row=8, column=0, columnspan=2, sticky="w", pady=3)

        self.var_figtrans = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="翻译图表本身并嵌回右栏（白底覆盖原文位置，纯图片不动）",
                        variable=self.var_figtrans).grid(row=9, column=0, columnspan=2,
                                                         sticky="w", pady=3)

        # --- 日志 ---
        lf = ttk.LabelFrame(right, text="状态", padding=6)
        lf.pack(fill="both", expand=True, pady=(10, 0))
        self.txt = tk.Text(lf, height=6, wrap="word", font=("Consolas", 9))
        self.txt.pack(fill="both", expand=True)
        self.txt.configure(state="disabled")

    def _build_footer(self):
        f = ttk.Frame(self.root, padding=(14, 8, 14, 12))
        f.pack(fill="x")
        ttk.Button(f, text="保存设置", command=self.on_save).pack(side="right")
        ttk.Button(f, text="关闭", command=self.root.destroy).pack(side="right", padx=8)
        ttk.Button(f, text="保存并设为当前引擎", command=self.on_save_and_use).pack(side="right", padx=8)
        self.lbl_path = ttk.Label(f, text="配置文件：" + S.settings_path(), foreground="#888")
        self.lbl_path.pack(side="left")

    # ------------------------------------------------------------------ 工具

    def log(self, msg):
        self.txt.configure(state="normal")
        self.txt.insert("end", msg.rstrip() + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _drain_queue(self):
        try:
            while True:
                fn = self.q.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.root.after(120, self._drain_queue)

    def _busy(self, on, text=None):
        state = "disabled" if on else "normal"
        self.btn_test.configure(state=state)
        self.btn_models.configure(state=state)
        if text is not None:
            self.lbl_active.configure(text=text)

    def toggle_key(self):
        self.ent_key.configure(show="" if self.var_show.get() else "*")

    def paste_key(self):
        try:
            self.var_key.set(self.root.clipboard_get().strip())
            self.log("已从剪贴板粘贴密钥。")
        except Exception as e:
            messagebox.showwarning("粘贴失败", "读取剪贴板失败：%s" % e)

    def api_label_to_type(self, label):
        for tid, tlabel in P.API_TYPES:
            if tlabel == label:
                return tid
        return "openai"

    def api_type_to_label(self, tid):
        for t, l in P.API_TYPES:
            if t == tid:
                return l
        return P.API_TYPES[0][1]

    # ------------------------------------------------------------------ 引擎列表

    def refresh_provider_list(self):
        self.lst.delete(0, "end")
        self.current_ids = []
        for p in self.catalog.get("providers", []):
            mark = "★ " if p["id"] == self.settings.get("active_provider") else "   "
            self.lst.insert("end", mark + p.get("label", p["id"]))
            self.current_ids.append(p["id"])
        self.lbl_active.configure(text="当前：%s" % S.active_label(self.settings, self.catalog))

    def selected_id(self):
        sel = self.lst.curselection()
        if not sel:
            return None
        return self.current_ids[sel[0]]

    def select_id(self, pid):
        if pid in self.current_ids:
            i = self.current_ids.index(pid)
            self.lst.selection_clear(0, "end")
            self.lst.selection_set(i)
            self.lst.see(i)

    def load_active_into_form(self):
        pid = self.settings.get("active_provider")
        self.select_id(pid)
        self.load_form(pid)

    def load_form(self, pid):
        entry = S.provider_entry(self.catalog, pid)
        if not entry:
            return
        ov = (self.settings.get("providers") or {}).get(pid, {})
        self.var_label.set(entry.get("label", pid))
        self.var_api.set(self.api_type_to_label(entry.get("api", "openai")))
        self.var_key.set(ov.get("api_key", ""))
        self.var_base.set(ov.get("base_url") or entry.get("base_url", ""))
        self.var_model.set(ov.get("model") or entry.get("default_model", ""))
        note = entry.get("note", "")
        if entry.get("id", "").startswith("custom"):
            note = (note + "  " if note else "") + "这是你自建的引擎，可随时删除。"
        self.lbl_note.configure(text=note)

    def on_pick(self):
        pid = self.selected_id()
        if not pid:
            return
        self.settings["active_provider"] = pid
        self.load_form(pid)
        self.refresh_provider_list()
        self.select_id(pid)
        self.log("已选择引擎：%s（记得点「保存设置」）" % pid)

    def on_api_type_change(self):
        api = self.api_label_to_type(self.var_api.get())
        if api == "google_web":
            self.var_base.set("https://translate.googleapis.com")
            self.var_model.set("google-web")
            self.ent_key.configure(state="disabled")
        else:
            self.ent_key.configure(state="normal")
            if api == "ollama" and not self.var_base.get():
                self.var_base.set("http://127.0.0.1:11434")

    def on_homepage(self):
        pid = self.selected_id()
        entry = S.provider_entry(self.catalog, pid) if pid else None
        url = (entry or {}).get("homepage")
        if not url:
            messagebox.showinfo("没有官网", "这个引擎没有登记申请地址。")
            return
        try:
            os.startfile(url)  # noqa
        except Exception as e:
            messagebox.showwarning("打不开", "%s\n%s" % (url, e))

    def reload(self):
        self.catalog = S.load_catalog()
        self.settings = S.load_settings()
        self.refresh_provider_list()
        self.load_active_into_form()
        self.log("已重新载入配置文件。")

    # ------------------------------------------------------------------ 增删引擎

    def on_add(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("新增翻译引擎")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("520x330")
        dlg.columnconfigure(1, weight=1)

        ttk.Label(dlg, text="适用于任何 OpenAI 兼容服务：中转站、公司内网网关、自建 vLLM / one-api 等。",
                  wraplength=480, foreground="#555").grid(row=0, column=0, columnspan=2,
                                                          sticky="w", padx=12, pady=(12, 8))
        fields = {}

        def row(r, key, label, default="", hint=""):
            ttk.Label(dlg, text=label).grid(row=r, column=0, sticky="w", padx=12, pady=4)
            var = tk.StringVar(value=default)
            ttk.Entry(dlg, textvariable=var).grid(row=r, column=1, sticky="ew", padx=(0, 12), pady=4)
            fields[key] = var
            if hint:
                ttk.Label(dlg, text=hint, foreground="#888").grid(row=r, column=2, sticky="w", pady=4)

        row(1, "label", "名称", "我的中转站")
        row(2, "base_url", "接口地址", "https://example.com/v1",
            "到 /v1 为止，不要带 /chat/completions")
        row(3, "default_model", "默认模型", "gpt-4o-mini")
        row(4, "api_key", "API Key", "")
        row(5, "homepage", "官网/申请地址", "")

        api_var = tk.StringVar(value=P.API_TYPES[0][1])
        ttk.Label(dlg, text="协议类型").grid(row=6, column=0, sticky="w", padx=12, pady=4)
        ttk.Combobox(dlg, textvariable=api_var, state="readonly",
                     values=[t[1] for t in P.API_TYPES]).grid(row=6, column=1, sticky="ew",
                                                              padx=(0, 12), pady=4)

        def ok():
            label = fields["label"].get().strip()
            base = fields["base_url"].get().strip()
            model = fields["default_model"].get().strip()
            if not label or not base:
                messagebox.showwarning("信息不全", "名称和接口地址必须填。", parent=dlg)
                return
            pid = "custom-" + "".join(ch if ch.isalnum() else "-" for ch in label.lower())[:24]
            entry = {
                "id": pid,
                "label": label + "（自建）",
                "api": self.api_label_to_type(api_var.get()),
                "base_url": base.rstrip("/"),
                "default_model": model,
                "note": "用户自建引擎。",
                "homepage": fields["homepage"].get().strip(),
                "custom": True,
            }
            S.upsert_provider(self.catalog, entry)
            self.settings.setdefault("providers", {})[pid] = {
                "api_key": fields["api_key"].get().strip(),
                "model": model,
                "base_url": entry["base_url"],
            }
            self.settings["active_provider"] = pid
            S.save_catalog(self.catalog)
            S.save_settings(self.settings)
            self.refresh_provider_list()
            self.select_id(pid)
            self.load_form(pid)
            self.log("已新增引擎：%s" % entry["label"])
            dlg.destroy()

        bar = ttk.Frame(dlg)
        bar.grid(row=7, column=0, columnspan=3, sticky="e", padx=12, pady=14)
        ttk.Button(bar, text="添加", command=ok).pack(side="right")
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="right", padx=8)

    def on_delete(self):
        pid = self.selected_id()
        if not pid:
            return
        entry = S.provider_entry(self.catalog, pid)
        if not entry:
            return
        if not entry.get("custom"):
            messagebox.showinfo("内置引擎不可删除",
                                "「%s」是内置引擎。可以保留不用，或换成别的引擎。" % entry.get("label"))
            return
        if not messagebox.askyesno("确认删除", "删除自建引擎「%s」？" % entry.get("label")):
            return
        self.catalog = S.delete_provider(self.catalog, pid)
        self.settings.get("providers", {}).pop(pid, None)
        if self.settings.get("active_provider") == pid:
            self.settings["active_provider"] = "deepseek"
        S.save_catalog(self.catalog)
        S.save_settings(self.settings)
        self.refresh_provider_list()
        self.load_active_into_form()
        self.log("已删除引擎：%s" % pid)

    # ------------------------------------------------------------------ 表单 -> 配置

    def form_to_entry(self, pid):
        entry = dict(S.provider_entry(self.catalog, pid) or {"id": pid})
        entry["label"] = self.var_label.get().strip() or entry.get("label", pid)
        entry["api"] = self.api_label_to_type(self.var_api.get())
        entry["base_url"] = self.var_base.get().strip().rstrip("/")
        entry["default_model"] = self.var_model.get().strip()
        return entry

    def collect(self):
        pid = self.selected_id() or self.settings.get("active_provider")
        entry = self.form_to_entry(pid)
        S.upsert_provider(self.catalog, entry)
        self.settings.setdefault("providers", {})[pid] = {
            "api_key": self.var_key.get().strip(),
            "model": self.var_model.get().strip(),
            "base_url": entry["base_url"],
        }
        self.settings["target_language"] = self.var_lang.get().strip() or P.DEFAULT_TARGET_LANG
        try:
            font = float(self.var_font.get())
        except Exception:
            font = 9.5
        try:
            workers = int(self.var_workers.get())
        except Exception:
            workers = 4
        self.settings["pdf"] = {
            "font_size": max(6.0, min(18.0, font)),
            "workers": max(1, min(16, workers)),
            "raster": bool(self.var_raster.get()),
            "figures": bool(self.var_figures.get()),
            "layout": self.var_layout.get() or "smart",
            "notes": bool(self.var_notes.get()),
            "captions": bool(self.var_caps.get()),
            "translate_figures": bool(self.var_figtrans.get()),
        }
        return pid

    # ------------------------------------------------------------------ 保存 / 测试

    def on_save(self):
        try:
            pid = self.collect()
        except P.TranslationError as e:
            messagebox.showerror("保存失败", str(e))
            return
        S.save_catalog(self.catalog)
        S.save_settings(self.settings)
        self.refresh_provider_list()
        self.select_id(pid)
        self.log("已保存：当前引擎 = %s，配置写入 %s" % (pid, S.settings_path()))
        messagebox.showinfo("已保存", "设置已保存。\n当前引擎：%s" % pid)

    def on_save_and_use(self):
        pid = self.selected_id()
        if pid:
            self.settings["active_provider"] = pid
        self.on_save()

    def _make_provider(self):
        pid = self.collect()
        self.settings["active_provider"] = pid
        cfg = P.merge_settings(S.provider_entry(self.catalog, pid), self.settings)
        return pid, P.build(cfg)

    def on_test(self):
        try:
            pid, prov = self._make_provider()
        except Exception as e:
            messagebox.showerror("配置有误", str(e))
            return
        self._busy(True, "正在测试 %s …" % pid)
        self.log("— 测试 %s（模型 %s）—" % (prov.label, prov.model))
        lang = self.settings.get("target_language", P.DEFAULT_TARGET_LANG)

        def work():
            try:
                if prov.api == "google_web":
                    out = prov.translate_units([TEST_SENTENCE], "zh-CN")[0]
                    info = "免费引擎可用"
                else:
                    txt = prov.chat(
                        "你是专业学术翻译。只输出译文，保留公式与引用标记。",
                        "把下面这句话翻译成%s，只输出译文：\n\n%s" % (lang, TEST_SENTENCE),
                        max_tokens=400, json_mode=False)
                    out = txt
                    info = "连接成功"
                self.q.put(lambda: self._test_ok(pid, info, out))
            except Exception as e:
                msg = str(e)
                self.q.put(lambda: self._test_fail(pid, msg))

        threading.Thread(target=work, daemon=True).start()

    def _test_ok(self, pid, info, out):
        self._busy(False)
        S.save_catalog(self.catalog)
        S.save_settings(self.settings)
        self.refresh_provider_list()
        self.select_id(pid)
        self.log("%s：%s" % (info, pid))
        self.log("样例译文：" + (out or "").strip()[:300])
        messagebox.showinfo("测试通过", "%s\n\n样例译文：\n%s" % (info, (out or "").strip()[:300]))

    def _test_fail(self, pid, msg):
        self._busy(False)
        self.log("测试失败（%s）：%s" % (pid, msg))
        messagebox.showerror("测试失败", msg)

    def on_list_models(self):
        try:
            pid, prov = self._make_provider()
        except Exception as e:
            messagebox.showerror("配置有误", str(e))
            return
        self._busy(True, "正在拉取模型列表 …")
        self.log("— 拉取 %s 的模型列表 —" % prov.label)

        def work():
            try:
                models = prov.models()
                self.q.put(lambda: self._models_ok(models))
            except Exception as e:
                msg = str(e)
                self.q.put(lambda: self._models_fail(msg))

        threading.Thread(target=work, daemon=True).start()

    def _models_ok(self, models):
        self._busy(False)
        if not models:
            self.log("没有取到模型列表；可以直接手动填写模型名。")
            messagebox.showinfo("没有结果", "服务端没返回模型列表，请手动填写模型名。")
            return
        self.cmb_model.configure(values=models)
        self.log("取到 %d 个模型，已填入下拉框。例如：%s" % (len(models), ", ".join(models[:8])))
        if not self.var_model.get():
            self.var_model.set(models[0])
        messagebox.showinfo("已获取", "取到 %d 个模型。\n在「模型名」下拉框里选择即可。" % len(models))

    def _models_fail(self, msg):
        self._busy(False)
        self.log("拉取模型失败：%s" % msg)
        messagebox.showerror("拉取失败", msg)

    # ------------------------------------------------------------------ 启动时填充

    def prefill(self):
        s = self.settings
        self.var_lang.set(s.get("target_language", P.DEFAULT_TARGET_LANG))
        pdf = s.get("pdf", {})
        self.var_font.set(pdf.get("font_size", 9.5))
        self.var_workers.set(pdf.get("workers", 4))
        self.var_raster.set(bool(pdf.get("raster", False)))
        self.var_figures.set(bool(pdf.get("figures", True)))
        self.var_layout.set(pdf.get("layout", "smart"))
        self.var_notes.set(bool(pdf.get("notes", True)))
        self.var_caps.set(bool(pdf.get("captions", True)))
        self.var_figtrans.set(bool(pdf.get("translate_figures", False)))


def launch(selftest_seconds=0, snapshot_path=None):
    root = tk.Tk()
    app = SettingsApp(root)
    app.prefill()

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
                box = (x, y, x + root.winfo_width(), y + root.winfo_height())
                ImageGrab.grab(bbox=box).save(snapshot_path)
                print("SNAPSHOT=" + snapshot_path)
            except Exception as e:
                print("SNAPSHOT FAILED: %s: %s" % (type(e).__name__, e))
            finally:
                root.destroy()
        root.after(900, snap)

    if selftest_seconds:
        root.after(int(selftest_seconds * 1000), root.destroy)
    root.mainloop()


if __name__ == "__main__":
    import sys
    secs = 3 if "--selftest" in sys.argv else 0
    snap = None
    for i, a in enumerate(sys.argv):
        if a == "--snapshot" and i + 1 < len(sys.argv):
            snap = sys.argv[i + 1]
    launch(secs, snap)
