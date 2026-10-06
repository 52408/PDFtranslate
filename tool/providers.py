# -*- coding: utf-8 -*-
"""providers.py —— 翻译引擎注册表与实现。

设计目标：设置界面里能直接增删引擎，后期加新 API 不用改主程序。

内置引擎（providers.json，可在线更新/被用户覆盖）：
  deepseek        DeepSeek 官方           OpenAI 兼容
  openai          OpenAI 官方             OpenAI 兼容
  siliconflow     硅基流动                OpenAI 兼容（国内直连，价格低）
  dashscope       阿里云百炼（通义千问）  OpenAI 兼容
  moonshot        Kimi / Moonshot         OpenAI 兼容
  zhipu           智谱 GLM                OpenAI 兼容
  ollama          本地 Ollama             自有协议，完全离线
  google          免费网页引擎（Google）  免费、免 Key、质量一般
  custom-*        用户自己加的任意 OpenAI 兼容服务

新增一个 OpenAI 兼容服务只需要在 providers.json 里加一条记录。
"""

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TARGET_LANG = "简体中文"


# --------------------------------------------------------------------------- TLS

_SSL_CTX = None


def ssl_ctx():
    """优先用系统/certifi 根证书；环境缺证书时退化为不校验（仅访问用户配置的翻译服务）。"""
    global _SSL_CTX
    if _SSL_CTX is None:
        try:
            import certifi
            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL_CTX = ssl._create_unverified_context()
    return _SSL_CTX


def http_json(url, payload=None, headers=None, timeout=180, method=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    hdrs = {"Content-Type": "application/json", "User-Agent": "pdf-bilingual/2.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx()) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


# --------------------------------------------------------------------------- 内置引擎

BUILTIN_PROVIDERS = [
    {
        "id": "deepseek",
        "label": "DeepSeek 官方（推荐）",
        "api": "openai",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "note": "性价比最高，学术长句翻译质量好。密钥在 platform.deepseek.com 申请。",
        "homepage": "https://platform.deepseek.com/api_keys",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "api": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "deepseek-ai/DeepSeek-V3",
        "note": "国内直连快，有免费额度；模型名形如 deepseek-ai/DeepSeek-V3。",
        "homepage": "https://cloud.siliconflow.cn/account/ak",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼（通义千问）",
        "api": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "note": "通义千问系列，中文语感好；也可换 qwen-max / qwen-turbo。",
        "homepage": "https://bailian.console.aliyun.com/",
    },
    {
        "id": "moonshot",
        "label": "Kimi / Moonshot",
        "api": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "note": "长上下文友好，适合整页长段落。",
        "homepage": "https://platform.moonshot.cn/console/api-keys",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "api": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "note": "glm-4-flash 有免费额度。",
        "homepage": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    {
        "id": "openai",
        "label": "OpenAI 官方",
        "api": "openai",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "note": "需要能直连 OpenAI；国内通常要走中转站（用「新增引擎」填自己的地址）。",
        "homepage": "https://platform.openai.com/api-keys",
    },
    {
        "id": "ollama",
        "label": "本地 Ollama（离线，不花钱）",
        "api": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "default_model": "qwen2.5:7b",
        "note": "完全不出本机。需要你先装好 Ollama 并 pull 一个模型，速度取决于显卡。",
        "homepage": "https://ollama.com/download",
    },
    {
        "id": "google",
        "label": "免费网页引擎（Google，免 Key）",
        "api": "google_web",
        "base_url": "https://translate.googleapis.com",
        "default_model": "google-web",
        "note": "不需要任何密钥，零成本。质量明显不如大模型，公式/术语可能翻错；适合先看大意。",
        "homepage": "",
    },
]

API_TYPES = [
    ("openai", "OpenAI 兼容（/chat/completions）"),
    ("ollama", "Ollama 本地（/api/chat）"),
    ("google_web", "免费网页引擎（无需密钥）"),
]


# --------------------------------------------------------------------------- 基类

class TranslationError(RuntimeError):
    pass


class BaseProvider:
    api = "openai"
    needs_key = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.pid = cfg.get("id", "?")
        self.label = cfg.get("label", self.pid)
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.model = cfg.get("model") or cfg.get("default_model") or ""
        self.api_key = cfg.get("api_key") or ""
        self.timeout = int(cfg.get("timeout", 180))
        self.calls = 0
        self.chars = 0

    # --- 要子类实现 ---
    def _chat(self, system, user, max_tokens, json_mode):  # pragma: no cover
        raise NotImplementedError

    def _list_models(self):  # pragma: no cover
        raise NotImplementedError

    # --- 通用逻辑 ---
    def check_ready(self):
        if self.needs_key and not self.api_key:
            raise TranslationError("「%s」还没有填 API Key。请在设置界面里填好并保存。" % self.label)
        if not self.base_url:
            raise TranslationError("「%s」缺少接口地址（Base URL）。" % self.label)
        if not self.model:
            raise TranslationError("「%s」没有填模型名。" % self.label)

    def models(self):
        try:
            return self._list_models()
        except Exception:
            return []

    def chat(self, system, user, max_tokens=8000, json_mode=True, retries=3):
        self.check_ready()
        last = None
        jm = json_mode
        for attempt in range(retries):
            try:
                out = self._chat(system, user, max_tokens, jm)
                self.calls += 1
                self.chars += len(user)
                return out
            except urllib.error.HTTPError as e:
                detail = e.read()[:400].decode("utf-8", "replace")
                last = "HTTP %s %s" % (e.code, detail)
                if e.code in (401, 403):
                    raise TranslationError("「%s」拒绝访问，请检查 API Key：%s" % (self.label, detail))
                if e.code == 404:
                    raise TranslationError(
                        "「%s」接口地址或模型名不对（404）。\n当前：%s\n模型：%s"
                        % (self.label, self.base_url, self.model))
                if e.code == 429:
                    last = "限流/余额不足：" + detail
                if e.code == 400 and jm:
                    jm = False
                    continue
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, e)
            time.sleep(1.5 * (attempt + 1))
        raise TranslationError("「%s」请求失败：%s" % (self.label, last))


# --------------------------------------------------------------------------- OpenAI 兼容

class OpenAICompatibleProvider(BaseProvider):
    api = "openai"

    def _headers(self):
        h = {}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        return h

    def _chat(self, system, user, max_tokens, json_mode):
        url = self.base_url + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return http_json(url, payload, self._headers(), self.timeout)["choices"][0]["message"]["content"]

    def _list_models(self):
        data = http_json(self.base_url + "/models", None, self._headers(), 30)
        items = data.get("data") or data.get("models") or []
        out = []
        for it in items:
            if isinstance(it, dict):
                out.append(it.get("id") or it.get("name"))
            elif isinstance(it, str):
                out.append(it)
        return [m for m in out if m]


# --------------------------------------------------------------------------- Ollama

class OllamaProvider(BaseProvider):
    api = "ollama"
    needs_key = False

    def _chat(self, system, user, max_tokens, json_mode):
        url = self.base_url + "/api/chat"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        data = http_json(url, payload, None, self.timeout)
        msg = data.get("message") or {}
        return msg.get("content", "")

    def _list_models(self):
        data = http_json(self.base_url + "/api/tags", None, None, 20)
        return [m.get("name") for m in data.get("models", []) if m.get("name")]


# --------------------------------------------------------------------------- 免费 Google 网页引擎

class GoogleWebProvider(BaseProvider):
    """不需要密钥的免费引擎。逐段调用，带重试和限速。"""

    api = "google_web"
    needs_key = False

    def check_ready(self):
        if not self.base_url:
            raise TranslationError("免费引擎缺少接口地址。")

    def _chat(self, system, user, max_tokens, json_mode):
        raise TranslationError("免费引擎不支持 chat 调用，请用 translate_units。")

    def _list_models(self):
        return ["google-web"]

    def translate_units(self, texts, target="zh-CN", sleep=0.12):
        out = []
        for t in texts:
            out.append(self._one(t, target, sleep))
        return out

    def _one(self, text, target, sleep=0.12):
        params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text})
        url = self.base_url.rstrip("/") + "/translate_a/single?" + params
        last = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with urllib.request.urlopen(req, timeout=45, context=ssl_ctx()) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                self.calls += 1
                self.chars += len(text)
                chunks = data[0] or []
                res = "".join(c[0] for c in chunks if c and c[0])
                time.sleep(sleep)
                return res
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, e)
                time.sleep(1.0 * (attempt + 1))
        raise TranslationError("免费引擎请求失败（可能被限流，建议改用大模型接口）：%s" % last)


# --------------------------------------------------------------------------- 工厂

def build(cfg):
    api = (cfg.get("api") or "openai").lower()
    if api == "ollama":
        return OllamaProvider(cfg)
    if api in ("google_web", "google", "free"):
        return GoogleWebProvider(cfg)
    return OpenAICompatibleProvider(cfg)


def provider_config(catalog, provider_id):
    """从 catalog 里取出某个引擎的配置，并套用 settings 里的覆盖值。"""
    for p in catalog.get("providers", []):
        if p.get("id") == provider_id:
            return dict(p)
    return None


def merge_settings(cfg, settings):
    """把 settings.json 里该引擎的 api_key / model / base_url 覆盖到配置上。"""
    out = dict(cfg)
    ov = (settings.get("providers") or {}).get(cfg.get("id"), {})
    for k in ("api_key", "model", "base_url", "timeout"):
        if ov.get(k):
            out[k] = ov[k]
    if not out.get("model"):
        out["model"] = out.get("default_model", "")
    return out
