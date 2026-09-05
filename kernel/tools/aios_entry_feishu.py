#!/usr/bin/env python3
"""飞书入口 — WebSocket直连 + Webhook备用，转发到AIOS调度器"""
import sys, os, json, time, threading, traceback
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import call_pin, init_registry, publish_event, register_callback, check_emergency_mode
from aios_enforcer import protocol_check
from aios_feishu_formatter import format_success, format_error, format_warning

from aios_secure import safe_bind_host, safe_log_exception  # noqa: E402

VERSION = "1.2.0"
WEBHOOK_PORT = 18802
FEISHU_APP_ID = "cli_a942baf79e39dbcb"
# [SECURITY] 移除明文默认 fallback: 如果 FEISHU_APP_SECRET 未设置,
# 显式报错并拒绝启动, 而不是用已知的弱 secret 静默工作.
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
if not FEISHU_APP_SECRET:
    FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET_FALLBACK", "")
if not FEISHU_APP_SECRET:
    print("⚠️  警告: FEISHU_APP_SECRET 未设置; 飞书相关功能将被禁用.",
          file=sys.stderr)

# ── 消息处理核心 ──────────────────────────────────────────────

def _extract_text(data: dict) -> dict | None:
    """从飞书事件体提取消息字段。兼容 WebSocket(p2) 和 Webhook 格式"""
    header = data.get("header", {})
    event = data.get("event", {})
    msg_type = header.get("event_type", "") or event.get("message_type", "")

    if "im.message.receive_v1" not in msg_type and event.get("message_type") != "text":
        return None

    msg = event.get("message", {})
    content_raw = msg.get("content", "{}")
    if isinstance(content_raw, str):
        try:
            content = json.loads(content_raw)
        except json.JSONDecodeError:
            content = {"text": content_raw}
    else:
        content = content_raw

    text = content.get("text", "") or content.get("content", "")
    sender = ""
    if event.get("sender", {}).get("sender_id", {}).get("open_id"):
        sender = event["sender"]["sender_id"]["open_id"]
    elif msg.get("chat_id"):
        sender = msg["chat_id"]
    elif data.get("sender", {}).get("sender_id", {}).get("open_id"):
        sender = data["sender"]["sender_id"]["open_id"]

    return {
        "text": text.strip(),
        "sender_id": sender or "feishu_user",
        "message_id": msg.get("message_id", ""),
        "chat_id": msg.get("chat_id", ""),
    }


def _handle_message_text(text: str, sender_id: str, message_id="", chat_id="", source="feishu"):
    """统一处理消息文本：协议检查 → 紧急模式 → dispatch"""
    ok, reason = protocol_check(text, sender_id)
    if not ok:
        print(f"  ⛔ protocol_violation from {sender_id}: {reason}")
        return {"ok": False, "error": f"protocol: {reason}"}

    emergency = check_emergency_mode()
    if emergency.get("emergency"):
        print(f"  🚨 emergency mode: {emergency.get('message', '')}")
        return {"ok": False, "error": emergency.get("message", "OpenClaw不可用")}

    init_registry()
    ok, result = call_pin("openclaw.dispatch", text, source=source, sender_id=sender_id)
    # [FIX] 2026-08-12 entry/feishu-result-push: ``call_pin('openclaw.dispatch', ...)``
    #   returns the dict from ``orchestrator.submit``, NOT a list of task_ids.
    #   The previous line ``task_ids = result if ok else []`` therefore assigned
    #   the whole response dict, and the subsequent ``for tid in task_ids``
    #   iterated the dict's *keys* (``task_ids``, ``parent_id``, ``count``,
    #   ``plan_mode``, ``status``, ``approval_required``, ``approval_id``,
    #   ``risk_action``) and called ``register_callback(<key_name>, ...)`` for
    #   each.  The real parent/child task ids were never registered, so
    #   result-push could never resolve the Feishu reply_key and the user
    #   never received a reply.  Normalise the response shape here.
    if not isinstance(result, dict):
        result = {}
    parent_id = result.get("parent_id") or ""
    _raw_tids = result.get("task_ids")
    if isinstance(_raw_tids, list) and _raw_tids:
        task_ids = [str(t) for t in _raw_tids if t]
    elif parent_id:
        task_ids = [str(parent_id)]
    else:
        task_ids = []

    if task_ids:
        publish_event("task.created", {
            "count": len(task_ids), "source": source,
            "sender": sender_id, "task_ids": task_ids,
        }, "feishu_entry")
        reply_key = message_id or chat_id or sender_id
        if reply_key:
            for tid in task_ids:
                register_callback(tid, source="feishu", reply_key=reply_key, sender_id=sender_id)
        print(f"  ✅ {source} -> dispatch: {text[:50]}... ({len(task_ids)} tasks)")
        return {"ok": True, "task_ids": task_ids, "count": len(task_ids)}
    else:
        print(f"  ❌ {source} -> dispatch failed: {result}")
        return {"ok": False, "error": str(result)}


# ── WebSocket 客户端 ──────────────────────────────────────────

def start_ws_client():
    """通过 lark_oapi WebSocket 直连飞书事件流"""
    # 延迟导入，避免依赖缺失导致整个脚本崩溃
    try:
        from lark_oapi.ws import Client as WSClient
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    except ImportError:
        print("  ⚠️ lark_oapi 未安装，WebSocket 模式不可用")
        return

    class FeishuEventHandler(EventDispatcherHandler):
        """覆写 do_without_validation 直接处理事件，绕过复杂的 processor 注册"""
        def do_without_validation(self, payload: bytes) -> None:
            try:
                pl = payload.decode("utf-8")
                data = json.loads(pl)
                msg = _extract_text(data)
                if msg and msg["text"]:
                    _handle_message_text(
                        msg["text"], msg["sender_id"],
                        msg.get("message_id", ""), msg.get("chat_id", ""),
                        source="feishu-ws"
                    )
            except Exception as e:
                print(f"  ⚠️ feishu-ws handle error: {e}")
                traceback.print_exc()
            return None

    event_handler = FeishuEventHandler()
    ws_client = WSClient(
        app_id=FEISHU_APP_ID,
        app_secret=FEISHU_APP_SECRET,
        event_handler=event_handler,
        auto_reconnect=True,
    )
    print(f"  🔌 Feishu WebSocket connecting (app_id={FEISHU_APP_ID})...")
    try:
        ws_client.start()
    except Exception as e:
        print(f"  ❌ Feishu WebSocket failed: {e}")
        traceback.print_exc()


# ── Webhook 服务（备用） ─────────────────────────────────────

class FeishuWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] [feishu-webhook] {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") == "/health":
            _json_response(self, 200, {"ok": True, "service": "aios-feishu-entry", "version": VERSION, "status": "live"})
        else:
            _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        body = _read_body(self)
        try:
            if path == "/webhook/feishu":
                self._handle_webhook(body)
            else:
                _json_response(self, 404, {"ok": False, "error": "not_found"})
        except Exception as e:
            _json_response(self, 500, {"ok": False, "error": str(e)})

    def _handle_webhook(self, data: dict):
        # 飞书 URL 验证
        if _verify_challenge(data):
            _json_response(self, 200, {"challenge": data["challenge"]})
            return

        msg = _extract_text(data)
        if not msg or not msg["text"]:
            _json_response(self, 200, {"ok": True, "msg": "ignored"})
            return

        result = _handle_message_text(
            msg["text"], msg["sender_id"],
            msg.get("message_id", ""), msg.get("chat_id", ""),
            source="feishu-webhook"
        )
        _json_response(self, 200, result)


def _json_response(handler, code, data):
    body = json.dumps(data, ensure_ascii=False, default=str).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw.decode("utf-8", errors="replace")}


def _verify_challenge(data: dict) -> bool:
    return "challenge" in data


def start_webhook():
    """启动 Webhook 服务（备用入口）"""
    host = safe_bind_host()
    server = HTTPServer((host, WEBHOOK_PORT), FeishuWebhookHandler)
    print(f"\n  🌐 Webhook server: http://{host}:{WEBHOOK_PORT}")
    print(f"     POST /webhook/feishu  ← 飞书事件回调（备用）")
    print(f"     GET  /health          健康检查")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


# ── 主入口 ────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  AIOS Feishu Entry v{VERSION}")
    print(f"  Mode: WebSocket(主) + Webhook(备)")
    print(f"{'='*50}")

    # WebSocket 在单独线程运行（阻塞）
    ws_thread = threading.Thread(target=start_ws_client, daemon=True, name="feishu-ws")
    ws_thread.start()

    # Webhook 在主线程运行
    start_webhook()


# ── 注册为 AIOS 服务 ─────────────────────────────────────────

try:
    from aios_bus import register_pin

    def start_both():
        t = threading.Thread(target=start_ws_client, daemon=True, name="feishu-ws")
        t.start()
        start_webhook()

    register_pin("feishu.start", start_both, "飞书入口: WebSocket + Webhook (默认:18802)")
    register_pin("feishu.health", lambda: {"status": "live", "version": VERSION}, "飞书入口健康检查")
except Exception:
    pass
