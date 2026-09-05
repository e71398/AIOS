#!/usr/bin/env python3
"""结果回推送守护 — 监听 task.completed/failed 事件，通过回调把结果推回用户"""

import sys, os, json, time, urllib.request, urllib.error, subprocess
from pathlib import Path
import uuid

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import consume_callback, _is_available, _redis_client, KEY_EVENT_CHANNEL

POLL_INTERVAL = 1
MAX_MSG_LEN = 1000

# OpenClaw 网关 API 配置 (用于 sessionKey 回推)
OPENCLAW_GW = "http://localhost:18789"
OPENCLAW_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "77bc257010dde9b82ed1058ac61f5f95259f0745978d3cee")

# --- 飞书 OAuth 缓存 ---
_feishu_token = None
_feishu_token_expires = 0

def _get_feishu_tenant_token() -> str | None:
    """获取飞书 tenant_access_token（自动刷新）."""
    global _feishu_token, _feishu_token_expires
    now = time.time()
    if _feishu_token and now < _feishu_token_expires - 60:
        return _feishu_token

    app_id = os.environ.get("FEISHU_APP_ID", "") or "cli_a942baf79e39dbcb"
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        return None

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            token = body.get("tenant_access_token", "")
            expire = body.get("expire", 7200)
            _feishu_token = token
            _feishu_token_expires = now + expire
            return token
    except Exception as e:
        print(f"  ⚠️ feishu auth error: {e}")
        return None


def _push_feishu(reply_key: str, text: str, sender_id: str = ""):
    """通过 message_id 回复飞书消息."""
    token = _get_feishu_tenant_token()
    if not token:
        print(f"  ⚠️ feishu push skipped: no token")
        return False

    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_key}/reply"
    body_data = {
        "content": json.dumps({"text": text[:2000]}, ensure_ascii=False),
        "msg_type": "text",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body_data).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            ok = result.get("code", -1) == 0
            if not ok:
                print(f"  ⚠️ feishu reply error: {result.get('msg', '')}")
            return ok
    except urllib.error.HTTPError as e:
        print(f"  ⚠️ feishu reply http {e.code}: {e.read().decode()[:200]}")
        return False
    except Exception as e:
        print(f"  ⚠️ feishu reply error: {e}")
        return False


def _push_telegram(chat_id: str, text: str):
    """通过 sendMessage 推送结果到 Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print(f"  ⚠️ telegram push skipped: no token")
        return False

    api_base = f"https://api.telegram.org/bot{token}"
    for chunk in [text[i:i+MAX_MSG_LEN] for i in range(0, len(text), MAX_MSG_LEN)]:
        url = f"{api_base}/sendMessage"
        body_data = {"chat_id": int(chat_id) if chat_id.isdigit() else chat_id,
                     "text": chunk, "parse_mode": "Markdown"}
        req = urllib.request.Request(url, data=json.dumps(body_data).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    print(f"  ⚠️ telegram send error: {result}")
                    return False
        except Exception as e:
            print(f"  ⚠️ telegram send error: {e}")
            return False
    return True


def _is_openclaw_session_key(key: str) -> bool:
    """判断 reply_key 是否是 OpenClaw sessionKey (含 '/' 路径分隔符)."""
    return "/" in key or key.startswith("agent:")


OPENCLAW_CLI = "${HOME}/.n/bin/openclaw"

def _extract_channel_target(session_key: str) -> tuple:
    """提取 channel 和 target."""
    key = session_key
    if "/" in key:
        key = key.split("/", 1)[1]
    # key now like 'feishu:open_id:ou_xxx' or 'feishu:chat_id:oc_xxx'
    parts = key.split(":", 2)
    if len(parts) >= 2:
        channel = parts[0]
        # target = rest after first colon: "open_id:ou_xxx" or "chat_id:oc_xxx"
        target = ":".join(parts[1:])
        return channel, target
    return "feishu", session_key


FEISHU_DEFAULT_ACCOUNT = "main"

def _push_openclaw(session_key: str, text: str):
    channel, target = _extract_channel_target(session_key)
    safe_text = text[:1500].replace('"', "'").replace("\n", " ").replace("\r", "")
    cmd = [
        OPENCLAW_CLI, "message", "send",
        "--channel", channel,
        "--account", FEISHU_DEFAULT_ACCOUNT,
        "--target", target,
        "--message", safe_text,
        "--json",
    ]
    try:
        result = subprocess.check_output(cmd, timeout=15, encoding='utf-8', stderr=subprocess.STDOUT)
        data = json.loads(result) if result.strip() else {}
        ok = data.get("ok", False) if isinstance(data, dict) else bool(data)
        if not ok:
            print(f"  ⚠️ openclaw send result: {result[:500]}")
        else:
            print(f"  📬 openclaw: pushed to {channel}/{target}")
        return ok
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️ openclaw send cli error: {e.output[:500] if e.output else e}")
        return False
    except Exception as e:
        print(f"  ⚠️ openclaw send error: {e}")
        return False


def _format_result(task_id: str, status: str, summary: str) -> str:
    """格式化为用户可读的消息."""
    icon = "✅" if status == "completed" else "❌"
    lines = [
        f"{icon} 任务 `{task_id[:8]}...` **{status}**",
    ]
    if summary:
        lines.append(f"📋 {summary[:500]}")
    return "\n".join(lines)


def push_result(task_id: str, status: str, summary: str = ""):
    """根据回调推送结果. 返回 True 如果推送成功."""
    callback = consume_callback(task_id)
    if not callback:
        # [FIX] 2026-08-12 entry/feishu-result-push observability:
        #   previously this branch was a silent ``return False``, which made
        #   every "callback missing" condition invisible in journalctl and
        #   impossible to distinguish from "callback consumed, push failed".
        #   Surface the reason on stderr so systemd/journald can collect it
        #   (the unit sets ``PYTHONUNBUFFERED=1`` and python3 writes line-
        #   buffered by default).  This is observation only and does not
        #   change push semantics, consume semantics, or add any retry.
        print(
            f"  ⚠️ result_push: callback_missing task_id={task_id[:8]}... "
            f"status={status} (no aios:bus:callback:{task_id} to consume)",
            file=sys.stderr, flush=True,
        )
        return False

    source = callback.get("source", "")
    reply_key = callback.get("reply_key", "")
    sender_id = callback.get("sender_id", "")
    text = _format_result(task_id, status, summary)

    # [FIX] 2026-08-12 entry/feishu-result-push delivery:
    #   When a user message enters through the OpenClaw gateway (e.g. via
    #   ``modules/openclaw-aios-bridge``) the callback registered in
    #   ``aios_orchestrator.submit`` carries ``source="openclaw"`` (not
    #   ``"feishu"``).  The reply_key is still an OpenClaw sessionKey of the
    #   form ``agent:_main:feishu:direct:ou_xxx`` and must be routed through
    #   the OpenClaw gateway to deliver the reply back to the original Feishu
    #   user.  Previously this branch only matched ``source == "feishu"``, so
    #   ``source="openclaw"`` fell through to the unsupported_source handler
    #   and the user never received a reply.  Treat both ``"feishu"`` and
    #   ``"openclaw"`` as Feishu-family sources and dispatch on reply_key
    #   shape, exactly as the original code intended.
    if source in ("feishu", "openclaw"):
        if _is_openclaw_session_key(reply_key):
            pushed = _push_openclaw(reply_key, text)
        else:
            pushed = _push_feishu(reply_key, text, sender_id)
    elif source == "telegram":
        pushed = _push_telegram(reply_key, text)
    else:
        print(
            f"  ⚠️ result_push: unsupported_source task_id={task_id[:8]}... "
            f"source={source!r}", file=sys.stderr, flush=True,
        )
        pushed = False

    if not pushed:
        print(
            f"  ⚠️ result_push: push_failed task_id={task_id[:8]}... "
            f"source={source} reply_key={reply_key!r}",
            file=sys.stderr, flush=True,
        )
    return pushed


# ============================================================================
#  User-facing result filter (parent-only, no internal telemetry)
#  ----------------------------------------------------------------
#  Only AIOS parent terminal events are allowed to deliver a reply to the
#  originating channel.  Child ``task.completed`` events, repair attempts and
#  internal telemetry summaries (executor names, task_id, repair counters,
#  sandbox prompts) must never reach the user's phone.
#
#  A parent terminal event is identified by
#      event.payload.parent_id == event.payload.task_id
#      and event.source == "aios-orchestrator"
#      and event.payload.status in {completed, failed, cancelled, blocked}
#  Any other event type or shape is treated as non-terminal / internal and
#  must NOT trigger a user push.
# ============================================================================

_PARENT_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled", "blocked"}


def _is_parent_terminal_event(event: dict) -> bool:
    """Return True only for events that represent a *parent* reaching a
    terminal state, as published by the orchestrator itself.
    """
    try:
        etype = event.get("type", "") or ""
        if etype not in ("task.completed", "task.failed"):
            return False
        payload = event.get("payload") or {}
        tid = payload.get("task_id", "") or ""
        pid = payload.get("parent_id", "") or ""
        src = event.get("source", "") or ""
        if not (tid and pid and tid == pid):
            return False
        if src != "aios-orchestrator":
            return False
        status = (payload.get("status") or etype.split(".", 1)[-1]).lower()
        return status in _PARENT_TERMINAL_STATUSES
    except Exception:
        return False


_INTERNAL_SUMMARY_PREFIXES = (
    "[opencode]",
    "[codex]",
    "[claude]",
    "[hermes]",
    "AIOS workflow failed:",
    "AIOS workflow blocked:",
    "Executor deliverable does not satisfy",
    "Multiple acceptance criteria are not demonstrated",
    "Original user goal (authoritative):",
)
_INTERNAL_LINE_PREFIXES = (
    "Assigned node:",
    "Acceptance summary:",
    "Evidence mode:",
    "System metadata:",
    "Bounded repair:",
    "SANDBOX CONSTRAINT",
    "FACT-USE:",
    "AUTHORITATIVE",
    "Trusted correction",
    "Copy requested",
)


def _extract_user_facing_summary(summary: str, status: str = "completed") -> str:
    """Strip executor / sandbox / prompt boilerplate from a parent
    ``result_summary`` so only the user's actual answer remains.

    For ``status == "failed"`` the orchestrator typically writes a single
    line like ``"AIOS workflow failed: <reason>"``; we surface the reason
    only (after the last colon) so the user does not see the literal
    telemetry header.
    """
    if not summary:
        return ""
    text = str(summary).strip()
    if status == "failed":
        if ":" in text:
            reason = text.split(":", 1)[1].strip()
            return reason[:500]
        return text[:500]
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    # Drop leading telemetry / prompt preamble that appears before the
    # real answer (e.g. "Original user goal (authoritative):").
    while lines and any(lines[0].startswith(p) for p in _INTERNAL_SUMMARY_PREFIXES):
        lines.pop(0)
    cleaned = []
    for ln in lines:
        s = ln.lstrip()
        if any(s.startswith(p) for p in _INTERNAL_LINE_PREFIXES):
            continue
        # Drop any inline executor / prompt tags that may appear later.
        if any(s.startswith(p) for p in _INTERNAL_SUMMARY_PREFIXES):
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip()[:1500]


def _format_user_terminal(event: dict) -> str:
    """Build a human-readable, user-facing message for a verified parent
    terminal event.  Never includes task_id, executor name, repair counter
    or any other internal metadata.
    """
    payload = event.get("payload") or {}
    status = (payload.get("status") or "completed").lower()
    summary = _extract_user_facing_summary(payload.get("summary", ""), status=status)
    if status == "completed":
        return f"✅ {summary}" if summary else "✅ AIOS 已完成"
    if status == "failed":
        return f"❌ AIOS 任务失败：{summary}" if summary else "❌ AIOS 任务失败"
    return "⚠️ AIOS 任务被中止"


def run_loop():
    """持续监听事件并推送结果 — 使用持久 PubSub 连接，不丢失事件."""
    if not _is_available():
        print("  ⚠️ Redis 不可用, 退出")
        return

    print(f"\n{'='*50}")
    print(f"  AIOS Result Push Daemon v1.0")
    print(f"  Polling every {POLL_INTERVAL}s")
    print(f"  Feishu: configured")
    print(f"  Telegram: {'configured' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'no token'}")
    print(f"{'='*50}\n")

    # 持久 PubSub 连接
    pubsub = _redis_client.pubsub()
    pubsub.subscribe(KEY_EVENT_CHANNEL)

    # 清掉 subscribe 确认消息
    for msg in pubsub.listen():
        if msg["type"] == "message":
            break  # 第一条真实消息就退出 listen, 进入轮询循环
    # listen() 是阻塞的, 上面已经等到了第一条真实消息, 这里改用 get_message 轮询
    # 但 listen() 的迭代器不能直接切换, 所以重新创建
    pubsub.close()

    pubsub = _redis_client.pubsub()
    pubsub.subscribe(KEY_EVENT_CHANNEL)
    # 清掉 subscribe 确认
    for _ in range(5):
        m = pubsub.get_message(timeout=0.1)
        if m is None or m["type"] == "message":
            break

    print(f"  📡 已订阅事件通道, 等待任务完成事件...\n")

    while True:
        try:
            for _ in range(5):  # 每次循环最多读 5 条消息
                msg = pubsub.get_message(timeout=0.2)
                if msg is None:
                    break
                if msg["type"] != "message":
                    continue
                try:
                    event = json.loads(msg["data"].decode() if isinstance(msg["data"], bytes) else msg["data"])
                    etype = event.get("type", "")
                    if etype in ("task.completed", "task.failed"):
                        # [FIX] 2026-08-12 entry/feishu-result-push boundary:
                        #   Only AIOS parent terminal events (orchestrator
                        #   self-publishes ``parent_id == task_id``) may produce
                        #   a user-visible reply.  Child ``task.completed``,
                        #   repair attempts and any non-orchestrator event are
                        #   ignored here so the user never sees internal
                        #   ``task_id`` / executor / repair telemetry.
                        if not _is_parent_terminal_event(event):
                            print(
                                f"  • result_push: event_ignored_non_parent "
                                f"type={etype} source={event.get('source', '?')} "
                                f"task_id={(event.get('payload') or {}).get('task_id', '')[:8] or '-'}",
                                file=sys.stderr, flush=True,
                            )
                            continue
                        payload = event.get("payload", {})
                        task_id = payload.get("task_id", "")
                        if not task_id:
                            print(
                                f"  ⚠️ result_push: event_ignored task_id_missing "
                                f"type={etype} source={event.get('source', '?')}",
                                file=sys.stderr, flush=True,
                            )
                            continue
                        user_text = _format_user_terminal(event)
                        status = (payload.get("status") or "completed").lower()
                        pushed = push_result(task_id, status, user_text)
                        if pushed:
                            print(
                                f"  📬 result pushed: {task_id[:8]}... "
                                f"(status={status}, bytes={len(user_text)})",
                                file=sys.stderr, flush=True,
                            )
                        # ``push_result`` already prints ``callback_missing`` or
                        # ``push_failed`` reasons on its own.
                except Exception as e:
                    print(f"  ⚠️ result_push: event_parse_error err={e}", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            print("\nShutting down...")
            break
        except Exception as e:
            print(f"  ⚠️ push error: {e}", file=sys.stderr, flush=True)
        time.sleep(POLL_INTERVAL)

    pubsub.unsubscribe(KEY_EVENT_CHANNEL)
    pubsub.close()


if __name__ == "__main__":
    run_loop()


try:
    from aios_bus import register_pin
    register_pin("result_push.start", run_loop, "结果回推: 监听事件并推送结果到飞书/Telegram")
    register_pin("result_push.now", run_once, "结果回推: 立即检查并推送")
except Exception:
    pass
