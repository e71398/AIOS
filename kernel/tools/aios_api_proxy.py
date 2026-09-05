#!/usr/bin/env python3
"""实时Token代理 — 拦截API调用, 从响应中提取usage"""
import json, sys, os, threading, re, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request

# DeepSeek Anthropic端点
TARGET_URL = "https://api.deepseek.com/anthropic"
API_KEY = os.environ.get("AIOS_API_KEY", "sk-" + "dummy-placeholder")
PORT = 9999

sys.path.insert(0, '${AIOS_HOME}/kernel/tools')
from aios_bus import _is_available
import redis as _r

def record_tokens(input_tokens, output_tokens):
    """实时记录token到Redis"""
    if not _is_available(): return
    try:
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        from datetime import datetime
        ds = datetime.now().strftime("%Y%m%d")
        key = f"aios:bus:governance:daily:{ds}"
        total = input_tokens + output_tokens
        cost = round((input_tokens * 0.14 + output_tokens * 0.28) / 1_000_000, 6)
        r.hincrby(key, "claude_tokens", total)
        r.hincrbyfloat(key, "claude_cost", cost)
        # 标记活跃
        r.set("aios:bus:system:claude:heartbeat", 
              __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
              ex=300)
        print(f"  📊 +{total:,}t \${cost:.6f} (in:{input_tokens} out:{output_tokens})", flush=True)
    except Exception as e:
        print(f"  ⚠️ record error: {e}", flush=True)

class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            # 转发到DeepSeek
            req = Request(TARGET_URL + self.path, data=body, headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {API_KEY}',
                'x-api-key': API_KEY,
            })
            # 去掉host限制
            for k, v in self.headers.items():
                if k.lower() in ('host', 'content-length', 'authorization'): continue
                req.add_header(k, v)
            
            resp = urlopen(req, timeout=120)
            resp_body = resp.read()
            
            # 提取usage
            try:
                data = json.loads(resp_body)
                usage = data.get('usage', {})
                inp = usage.get('input_tokens', 0) or usage.get('prompt_tokens', 0)
                out = usage.get('output_tokens', 0) or usage.get('completion_tokens', 0)
                if inp or out:
                    record_tokens(inp, out)
            except: pass
            
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in ('transfer-encoding', 'content-encoding'):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b'{"status":"proxy running"}')

    def log_message(self, fmt, *args): pass

if __name__ == '__main__':
    print(f"🔌 Token代理: http://127.0.0.1:{PORT} → {TARGET_URL}")
    HTTPServer(('127.0.0.1', PORT), ProxyHandler).serve_forever()
