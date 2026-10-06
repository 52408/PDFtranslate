# -*- coding: utf-8 -*-
"""mock_openai_server.py —— 本地假翻译服务，用来离线验证整条链路（不发真实请求、不花钱）。

  python mock_openai_server.py [port]
  然后：
  python pdf_bilingual.py INPUT.pdf --base-url http://127.0.0.1:PORT/v1 --api-key test --model mock --pages 1-2

它会按 OpenAI /chat/completions 协议应答，返回 {"translations": [...]}，
把每段原文前加「【译文】」作为假译文。也可用来验证 --list-models。
"""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18123


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send({"data": [{"id": "mock-translator"}, {"id": "mock-fast"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) < 12:
            self._send({"error": {"message": "invalid api key", "type": "authentication_error"}}, 401)
            return
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            req = json.loads(raw)
        except Exception:
            self._send({"error": {"message": "bad json"}}, 400)
            return

        user = ""
        for m in req.get("messages", []):
            if m.get("role") == "user":
                user = m.get("content", "")
        pairs = re.findall(r"\[(\d+)\]\s*(.+?)(?=\n\n\[\d+\]|\Z)", user, re.S)
        texts = [t.strip() for _, t in pairs]
        if not texts:
            texts = ["（未解析到段落）"]

        # 模拟真实翻译：保留公式/引用，前后加标记，便于肉眼确认段落对齐
        out = []
        for t in texts:
            zh = re.sub(r"\s+", " ", t).strip()
            out.append("【译文·%d字】%s" % (len(zh), zh[:160]))

        self._send({
            "id": "mock", "object": "chat.completion", "model": req.get("model", "mock"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": json.dumps({"translations": out}, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": len(user), "completion_tokens": 100, "total_tokens": len(user) + 100},
        })


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("mock openai server on http://127.0.0.1:%d/v1" % PORT, flush=True)
    srv.serve_forever()
