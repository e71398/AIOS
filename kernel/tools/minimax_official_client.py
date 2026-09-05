#!/usr/bin/env python3
"""MiniMax 官方 API (Provider ID: minimax-official) 客户端.

只允许作为本任务的 MVP Provider 使用:
    - PROVIDER_ID = "minimax-official"
    - BASE_URL    = https://api.minimaxi.com/v1 (MiniMax 官方 API 域名)
    - MODEL       = MiniMax-M3
    - 凭证来源   = 环境变量 MINIMAX_API_KEY (不在源码或 Git 中保存)
    - 调用方必须自行通过 _ai_guard_call() 检查:
        * AIOS_MINIMAX_OFFICIAL_ENABLED == 1
        * 单进程 + Redis 全局调用计数 < MODEL_CALL_LIMIT
        * Token 累计 < TOTAL_COMPLETION_TOKEN_LIMIT
    - 不允许 fallback 到其他 Provider; 不允许重试到其他 endpoint
    - 不允许在 prompt 中输出 API Key 任何字符
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

PROVIDER_ID = "minimax-official"
MODEL = "MiniMax-M3"
BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_TOKENS = 512

MODEL_CALL_LIMIT = int(os.environ.get("AIOS_MINIMAX_CALL_LIMIT", "30"))
TOTAL_COMPLETION_TOKEN_LIMIT = int(
    os.environ.get("AIOS_MINIMAX_TOKEN_LIMIT", "15000")
)

REDIS_HASH_KEY = "aios:minimax_official:hash"


class MiniMaxOfficialError(RuntimeError):
    pass


class MiniMaxDisabledError(MiniMaxOfficialError):
    pass


_state_lock = threading.Lock()
_state = {
    "calls_used": 0,
    "prompt_tokens_used": 0,
    "completion_tokens_used": 0,
    "total_tokens_used": 0,
    "enabled": os.environ.get("AIOS_MINIMAX_OFFICIAL_ENABLED", "0") == "1",
    "log": [],
}


def _api_key() -> str:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not key:
        raise MiniMaxOfficialError("MINIMAX_API_KEY_NOT_SET")
    if len(key) < 20:
        raise MiniMaxOfficialError("MINIMAX_API_KEY_LENGTH_SUSPICIOUS")
    return key


def enable() -> None:
    with _state_lock:
        _state["enabled"] = True


def disable() -> None:
    with _state_lock:
        _state["enabled"] = False


def set_limits(call_limit: int, token_limit: int) -> None:
    global MODEL_CALL_LIMIT, TOTAL_COMPLETION_TOKEN_LIMIT
    MODEL_CALL_LIMIT = int(call_limit)
    TOTAL_COMPLETION_TOKEN_LIMIT = int(token_limit)


def _redis_avail() -> bool:
    try:
        import redis
        r = redis.Redis(host='127.0.0.1', port=6379, db=0, socket_connect_timeout=2)
        r.ping()
        return True
    except Exception:
        return False


def _redis_r():
    import redis
    return redis.Redis(host='127.0.0.1', port=6379, db=0, socket_connect_timeout=2)


def _global_used() -> Dict[str, int]:
    """Return Redis-stored cumulative usage. Fallback to local state."""
    try:
        if _redis_avail():
            d = _redis_r().hgetall(REDIS_HASH_KEY) or {}
            out = {"calls_used": 0, "prompt_tokens_used": 0,
                   "completion_tokens_used": 0}
            for k, v in d.items():
                kk = k.decode() if isinstance(k, bytes) else k
                vv = int(v.decode() if isinstance(v, bytes) else v)
                out[kk] = vv
            return out
    except Exception:
        pass
    with _state_lock:
        return {
            "calls_used": _state["calls_used"],
            "prompt_tokens_used": _state["prompt_tokens_used"],
            "completion_tokens_used": _state["completion_tokens_used"],
        }


def _bump_global(prompt_tokens: int, completion_tokens: int) -> None:
    try:
        if _redis_avail():
            pipe = _redis_r().pipeline()
            pipe.hincrby(REDIS_HASH_KEY, "calls_used", 1)
            pipe.hincrby(REDIS_HASH_KEY, "prompt_tokens_used", int(prompt_tokens or 0))
            pipe.hincrby(REDIS_HASH_KEY, "completion_tokens_used", int(completion_tokens or 0))
            pipe.execute()
    except Exception:
        pass


def _record_call(*, success: bool, prompt_tokens: int, completion_tokens: int,
                 elapsed_ms: int, http_status: int, purpose: str,
                 error: str = "") -> None:
    with _state_lock:
        _state["calls_used"] += 1
        _state["prompt_tokens_used"] += int(prompt_tokens or 0)
        _state["completion_tokens_used"] += int(completion_tokens or 0)
        _state["total_tokens_used"] += int(
            (prompt_tokens or 0) + (completion_tokens or 0)
        )
        _state["log"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "purpose": purpose[:80],
            "http": int(http_status or 0),
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "elapsed_ms": int(elapsed_ms or 0),
            "ok": bool(success),
            "error": (error or "")[:200],
        })
        if len(_state["log"]) > 100:
            _state["log"] = _state["log"][-100:]


def _ai_guard_call(purpose: str) -> None:
    """调用前 _ai_guard_call: 强制 owner 开关 + 全局 (Redis) 限额检查."""
    with _state_lock:
        if not _state["enabled"]:
            raise MiniMaxDisabledError(
                "AIOS_MINIMAX_OFFICIAL_DISABLED: set AIOS_MINIMAX_OFFICIAL_ENABLED=1"
            )
    g = _global_used()
    if g["calls_used"] >= MODEL_CALL_LIMIT:
        raise MiniMaxDisabledError(
            f"AIOS_MINIMAX_CALL_LIMIT_REACHED:{g['calls_used']}/{MODEL_CALL_LIMIT}"
        )
    if g["completion_tokens_used"] >= TOTAL_COMPLETION_TOKEN_LIMIT:
        raise MiniMaxDisabledError(
            f"AIOS_MINIMAX_TOKEN_LIMIT_REACHED:"
            f"{g['completion_tokens_used']}/{TOTAL_COMPLETION_TOKEN_LIMIT}"
        )


def get_stats() -> Dict[str, Any]:
    g = _global_used()
    with _state_lock:
        out = dict(_state)
    out.update({"global_used": g})
    return out


def reset_stats() -> None:
    """Task §14: 确认调用计数从 0 开始."""
    with _state_lock:
        _state["calls_used"] = 0
        _state["prompt_tokens_used"] = 0
        _state["completion_tokens_used"] = 0
        _state["total_tokens_used"] = 0
        _state["log"] = []
    try:
        if _redis_avail():
            _redis_r().delete(REDIS_HASH_KEY)
    except Exception:
        pass


def chat(messages: List[Dict[str, str]],
         *,
         model: str = MODEL,
         timeout: int = DEFAULT_TIMEOUT,
         max_tokens: int = DEFAULT_MAX_TOKENS,
         temperature: float = 0.2,
         extra_body: Optional[Dict[str, Any]] = None,
         purpose: str = "unspecified") -> Dict[str, Any]:
    """一次独立调用. 不 fallback, 不重试到其他 provider."""
    _ai_guard_call(purpose)
    key = _api_key()
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
    }
    if extra_body:
        payload.update(extra_body)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    raw, status, err = "", 0, ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        status = e.code
        err = f"HTTP_{status}"
    except urllib.error.URLError as e:
        err = f"NETWORK_ERROR:{e.reason}"
    except Exception as e:
        err = f"CLIENT_ERROR:{type(e).__name__}:{e}"
    elapsed_ms = int((time.monotonic() - started) * 1000)

    success = (status == 200 and not err)
    prompt_tokens = completion_tokens = total_tokens = 0
    data: Dict[str, Any] = {}
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            err = err or "BAD_JSON"
            success = False
    if success:
        base_resp = data.get("base_resp", {}) or {}
        if base_resp.get("status_code") not in (0, None):
            err = f"PROVIDER_REJECTED:{base_resp}"
            success = False
        usage = data.get("usage", {}) or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens))

    _record_call(
        success=success, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens, elapsed_ms=elapsed_ms,
        http_status=status, purpose=purpose, error=err,
    )
    if success:
        _bump_global(prompt_tokens, completion_tokens)

    if not success:
        raise MiniMaxOfficialError(err or f"HTTP_{status}:{raw[:200]}")
    content = _extract_content(data)
    if not content:
        raise MiniMaxOfficialError("EMPTY_RESPONSE")
    return {
        "provider_id": PROVIDER_ID,
        "model": data.get("model", model),
        "content": content,
        "elapsed_ms": elapsed_ms,
        "usage": data.get("usage", {}),
        "raw_status": status,
        "purpose": purpose,
    }


def _extract_content(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return (msg.get("content") or "").strip()


def probe_round_trip(token: str, purpose: str = "probe") -> Tuple[bool, str, int]:
    prompt = f"Output this exact string and nothing else: AIOS-MINIMAX-PROBE-{token}"
    r = chat([{"role": "user", "content": prompt}], max_tokens=DEFAULT_MAX_TOKENS,
             purpose=purpose)
    content = r["content"]
    expected = f"AIOS-MINIMAX-PROBE-{token}"
    return (expected in content), content, r["elapsed_ms"]


__all__ = [
    "PROVIDER_ID", "MODEL", "BASE_URL", "MODEL_CALL_LIMIT",
    "TOTAL_COMPLETION_TOKEN_LIMIT",
    "chat", "probe_round_trip", "get_stats", "reset_stats",
    "enable", "disable", "set_limits",
    "MiniMaxOfficialError", "MiniMaxDisabledError",
    "REDIS_HASH_KEY",
]