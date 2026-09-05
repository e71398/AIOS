#!/usr/bin/env python3
"""Codex DeepSeek Proxy — 通过 LiteLLM 统一出口转发"""
import json, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request

LITELLM_URL = "http://localhost:4000/v1/chat/completions"
LITELLM_KEY = os.environ.get("CODEX_LITELLM_KEY", "sk-" + "dummy-placeholder")
PORT = 57321

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            req = json.loads(body)
            model = req.get("model", "deepseek-chat")
            msgs = req.get("input", req.get("messages", [{"role": "user", "content": str(req)}]))
            if isinstance(msgs, str): msgs = [{"role": "user", "content": msgs}]
            max_tok = req.get("max_output_tokens", 4096)

            ds_body = json.dumps({"model": model, "messages": msgs, "max_tokens": max_tok, "stream": True}).encode()
            resp = urlopen(Request(LITELLM_URL, data=ds_body, headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {LITELLM_KEY}"
            }), timeout=120)

            resp_id, content = "", ""
            for line in resp:
                line = line.decode().strip()
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    try:
                        c = json.loads(line[6:])
                        if not resp_id: resp_id = c.get("id", "resp")
                        content += c.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    except: pass

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("x-request-id", resp_id)
            self.end_headers()
            self.wfile.write(f'data: {json.dumps({"type":"response.output_text.delta","delta":content})}\n\n'.encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except Exception as e:
            self.send_response(500); self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, fmt, *args): pass

HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
