#!/usr/bin/env python3
"""Model Gateway HTTP Server — 所有AI的API请求都走这里"""
import json, sys, os, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request

TOOLS = "${AIOS_HOME}/kernel/tools"
sys.path.insert(0, TOOLS)
from aios_model_gateway import call_model, check_idle, should_pause
from aios_http_server import BoundedThreadingHTTPServer

PORT = 9998

def _detect_provider(model: str, req_provider: str) -> str:
    """Resolve an explicit provider or let the gateway select its configured default."""
    # 显式 provider 优先
    if req_provider in ('deepseek', 'minimax', 'bailian', 'litellm', 'local'):
        return req_provider
    return 'auto'

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        try:
            req = json.loads(body)
            agent = self.headers.get('X-AIOS-Agent', 'hermes')
            task_id = self.headers.get('X-AIOS-TaskID', '') or req.get('task_id', 'gateway_' + str(int(time.time())))
            provider = _detect_provider(req.get('model', ''), req.get('provider', ''))
            model = req.get('model', 'auto')
            messages = req.get('messages', req.get('input', [{'role':'user','content':str(req)}]))
            if isinstance(messages, str): messages = [{'role':'user','content':messages}]
            max_tok = req.get('max_tokens', 4096)

            result = call_model(provider, model, messages, agent=agent, task_id=task_id, max_tokens=max_tok)

            if result.get('ok'):
                # 透明代理: 直接返回原始 API 响应 (不包装)
                raw = result.get('result', result)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps(raw, ensure_ascii=False).encode())
            else:
                self.send_response(403)
                self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({"error": result.get('error', 'unknown'),
                                              "code": result.get('code', 'E999')}).encode())
        except Exception as e:
            self.send_response(500); self.end_headers()
            self.wfile.write(json.dumps({"error":str(e)}).encode())

    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(json.dumps({"gateway":"AIOS Model Gateway","idle":check_idle(),"paused":should_pause()}).encode())

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {fmt%args}")

if __name__ == '__main__':
    print(f"🔌 Model Gateway: http://127.0.0.1:{PORT}")
    # P9A fix: use the bounded concurrent server so that one slow
    # /v1/chat/completions call (e.g. an in-flight MiniMax-M3 planner
    # round-trip) cannot pin the entire model gateway and starve
    # parallel /v1/chat/completions calls from the orchestrator's
    # _WORKFLOW_POOL (e.g. reviewer scans).  The cap is the same
    # default as the entry gateway and is operator-tunable via
    # ``AIOS_GATEWAY_MAX_HANDLERS``.
    BoundedThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
