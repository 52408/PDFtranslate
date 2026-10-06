# -*- coding: utf-8 -*-
"""settings_store.py —— 引擎目录（providers.json）与用户设置（settings.json）的读写。

providers.json  引擎目录：内置大模型/本地/免费引擎，可被用户通过设置界面追加
settings.json   用户选择：当前用哪个引擎、各引擎的 Key / 模型 / 地址、排版偏好
"""

import json
import os

import providers as P

DEFAULT_SETTINGS = {
    "active_provider": "deepseek",
    "providers": {},          # id -> {api_key, model, base_url, timeout}
    "target_language": P.DEFAULT_TARGET_LANG,
    "pdf": {
        "font_size": 9.5,
        "workers": 4,
        "raster": False,
        "figures": True,
        "layout": "smart",      # smart=正文/注释/图表分流；legacy=旧版混排
        "notes": True,          # 输出页脚注释译文
        "captions": True,       # 输出图表标题译文
        "translate_figures": False,   # 把图表本身也翻译好嵌进右栏
    },
}


def default_settings():
    """返回"不含任何密钥"的默认配置。

    密钥一律**不写进配置文件**：改用环境变量注入
    （PDFTRANS_API_KEY 或 <引擎ID>_API_KEY），见 load_settings()。
    """
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    s.setdefault("providers", {})
    return s


def write_default_settings():
    """把默认配置写入 settings.json（不覆盖已有文件）。返回写入的路径。"""
    path = settings_path()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_settings(), f, ensure_ascii=False, indent=2)
    return path


def root_dir():
    """定位项目根目录（存放 settings.json / input / output / deps 的那一级）。

    按目录特征自动识别，因此既支持 D:\\public\\PDFtrans\\tool\\ 的部署布局，
    也支持在工作区里直接跑（代码放在任意名字的子目录中）。
    """
    here = os.path.dirname(os.path.abspath(__file__))       # .../<tool>
    parent = os.path.dirname(here)                          # 项目根 or .../tools
    markers = ("deps", "input", "output", "cache", "settings.json",
               "providers.json", "inbox")
    if any(os.path.exists(os.path.join(parent, m)) for m in markers):
        return parent
    if any(os.path.exists(os.path.join(here, m)) for m in markers):
        return here
    return parent


def catalog_path():
    return os.path.join(root_dir(), "providers.json")


def settings_path():
    return os.path.join(root_dir(), "settings.json")


def ensure_dirs():
    """确保运行时目录存在。

    仓库里不带 input/output/cache/inbox（它们含论文与译文，不入版本库），
    所以新克隆下来第一次跑必须能自己补齐，否则会直接报错。
    返回创建/确认过的目录列表。
    """
    made = []
    for name in ("input", "output", "cache", "inbox"):
        p = os.path.join(root_dir(), name)
        try:
            os.makedirs(p, exist_ok=True)
            made.append(p)
        except Exception:
            pass
    return made


# --------------------------------------------------------------------------- 通用读写

def _read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            warn_broken(path, "顶层不是 JSON 对象")
        except UnicodeDecodeError as e:
            warn_broken(path, "编码不是 UTF-8（可能被别的程序按 GBK 改写过）：%s" % e)
        except json.JSONDecodeError as e:
            warn_broken(path, "JSON 语法错误：%s" % e)
        except Exception as e:
            warn_broken(path, "%s: %s" % (type(e).__name__, e))
    return json.loads(json.dumps(default))  # deep copy


def warn_broken(path, why):
    """配置文件损坏时必须吼一声，否则用户会莫名其妙“丢 Key”。"""
    msg = ("[配置警告] 无法解析 %s\n             原因：%s\n"
           "             本次将使用内置默认值（你已保存的 API Key 等设置不会被读取）。\n"
           "             修复：删掉该文件后重新在设置界面保存一次。\n" % (path, why))
    try:
        import sys
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception:
        pass


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_catalog():
    """引擎目录：内置引擎 + 用户在 providers.json 里的增删改。"""
    custom = _read_json(catalog_path(), {"providers": []})
    items = list(custom.get("providers") or [])
    have = {p.get("id") for p in items}
    for b in P.BUILTIN_PROVIDERS:
        if b["id"] not in have:
            items.append(dict(b))
    cat = dict(custom)
    cat["providers"] = items
    if not os.path.exists(catalog_path()):
        try:
            save_catalog(cat)
        except Exception:
            pass
    return cat


def save_catalog(catalog):
    _write_json(catalog_path(), catalog)


def load_settings():
    s = _read_json(settings_path(), DEFAULT_SETTINGS)
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, json.loads(json.dumps(v)))
    for k, v in DEFAULT_SETTINGS["pdf"].items():
        s["pdf"].setdefault(k, v)
    s.setdefault("providers", {})

    # 一次性迁移：老版本的 key.txt
    key_file = os.path.join(root_dir(), "key.txt")
    if os.path.exists(key_file):
        try:
            txt = open(key_file, "r", encoding="utf-8").read()
            for line in txt.splitlines():
                line = line.strip()
                if line.startswith("sk-"):
                    s["providers"].setdefault(s.get("active_provider", "deepseek"), {})["api_key"] = line
                    break
        except Exception:
            pass

    # 环境变量优先：把 Key 放在环境变量里，配置文件就不必存密钥，
    # 也就不会在分享/提交代码时泄露。支持：
    #   PDFTRANS_API_KEY（通用） / [引擎ID]_API_KEY（如 DEEPSEEK_API_KEY）
    pid = s.get("active_provider", "deepseek")
    env_key = None
    for name in ("PDFTRANS_API_KEY", "%s_API_KEY" % pid.upper().replace("-", "_")):
        v = os.environ.get(name)
        if v:
            env_key = v.strip()
            break
    if env_key:
        s["providers"].setdefault(pid, {})["api_key"] = env_key
    return s


def save_settings(settings):
    _write_json(settings_path(), settings)


# --------------------------------------------------------------------------- 组合

def resolve(settings=None, catalog=None, provider_id=None):
    """返回 (provider_cfg, provider_instance)，其中 Key/模型/地址已套用用户设置。"""
    settings = settings or load_settings()
    catalog = catalog or load_catalog()
    pid = provider_id or settings.get("active_provider") or "deepseek"
    base = P.provider_config(catalog, pid)
    if base is None:
        raise P.TranslationError("配置里找不到引擎「%s」，请在设置界面重新选择。" % pid)
    cfg = P.merge_settings(base, settings)
    cfg.setdefault("id", pid)
    return cfg, P.build(cfg)


def active_label(settings=None, catalog=None):
    settings = settings or load_settings()
    catalog = catalog or load_catalog()
    pid = settings.get("active_provider")
    p = P.provider_config(catalog, pid)
    return p.get("label", pid) if p else ("未知引擎：" + str(pid))


def provider_entry(catalog, pid):
    for p in catalog.get("providers", []):
        if p.get("id") == pid:
            return p
    return None


def upsert_provider(catalog, entry):
    items = catalog.setdefault("providers", [])
    for i, p in enumerate(items):
        if p.get("id") == entry.get("id"):
            items[i] = entry
            return catalog
    items.append(entry)
    return catalog


def delete_provider(catalog, pid):
    cat = dict(catalog)
    cat["providers"] = [p for p in cat.get("providers", []) if p.get("id") != pid]
    return cat
