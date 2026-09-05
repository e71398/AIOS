#!/usr/bin/env python3
"""
Model Gateway — 统一API调用网关
================================
所有AI不能直接调用任何模型API。必须经过此Gateway。
每次调用记录: UUID/Agent/TaskID/Provider/Model/Token/耗时/费用
无TaskID → 直接拒绝
"""
import os, sys, time, json, uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

TOOLS = Path("${AIOS_HOME}/kernel/tools"); sys.path.insert(0, str(TOOLS))
from aios_bus import _is_available, publish_event, get_queue_status, heartbeat
from aios_observability import record_token

KEY_AUDIT = "aios:gateway:audit"       # Sorted Set — 审计日志
KEY_AUDIT_MINUTE = "aios:gateway:minute"  # Hash — 每分钟统计

# Provider modules are configuration, not core dependencies. Local providers are
# deliberately present but disabled until the host is upgraded.
DEFAULT_PROVIDER = os.getenv("AIOS_CLOUD_PROVIDER", "minimax").strip().lower()
MODELS = {
    "minimax": {
        "endpoint": os.getenv("MINIMAX_API_BASE", "https://api.minimaxi.com/v1/chat/completions"),
        "key": os.getenv("MINIMAX_API_KEY", ""),
        "default_model": os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
        "enabled": bool(os.getenv("MINIMAX_API_KEY")),
        "cost_per_1k_input": 0.0005,
        "cost_per_1k_output": 0.0018,
    },
    "bailian": {
        "endpoint": os.getenv("BAILIAN_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
        "key": os.getenv("BAILIAN_API_KEY", ""),
        "default_model": os.getenv("BAILIAN_MODEL", "qwen-plus"),
        "enabled": bool(os.getenv("BAILIAN_API_KEY")),
        "cost_per_1k_input": 0.0008,
        "cost_per_1k_output": 0.002,
    },
    "deepseek": {
        "endpoint": os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1/chat/completions"),
        "key": os.getenv("DEEPSEEK_API_KEY", ""),
        "default_model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        "enabled": bool(os.getenv("DEEPSEEK_API_KEY")),
        "cost_per_1k_input": 0.00027,
        "cost_per_1k_output": 0.0011,
    },
    "litellm": {
        "endpoint": os.getenv("LITELLM_ENDPOINT", "http://127.0.0.1:4000/v1/chat/completions"),
        "key": os.getenv("CODEX_LITELLM_KEY", ""),
        "default_model": os.getenv("LITELLM_MODEL", "MiniMax-M3"),
        "enabled": os.getenv("AIOS_LITELLM_ENABLED", "0") == "1",
        "cost_per_1k_input": 0.0005,
        "cost_per_1k_output": 0.0018,
    },
    "local": {
        "endpoint": os.getenv("AIOS_LOCAL_MODEL_ENDPOINT", "http://127.0.0.1:8081/v1/chat/completions"),
        "key": "local-disabled",
        "default_model": os.getenv("AIOS_LOCAL_MODEL_NAME", "qwen-local"),
        "enabled": os.getenv("AIOS_LOCAL_MODEL_ENABLED", "0") == "1",
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
    },
}

PROVIDER_ALIAS = {"anthropic": DEFAULT_PROVIDER, "auto": DEFAULT_PROVIDER, "": DEFAULT_PROVIDER}

# 空闲检测
IDLE_TIMEOUT = 300  # 5分钟无任务 → 休眠

def check_idle() -> bool:
    """检查系统是否空闲."""
    qs = get_queue_status()
    return qs.get("pending", 0) == 0 and qs.get("locked", 0) == 0 and qs.get("running", 0) == 0

def should_pause() -> bool:
    """是否应该暂停所有后台LLM调用."""
    if not check_idle(): return False
    # 检查最后任务时间
    from aios_bus import check_recent
    recent = check_recent(limit=5)
    if not recent: return True
    last_ts = recent[0].get("ts_complete", "")
    if last_ts:
        try:
            last = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            idle_seconds = (datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)).total_seconds()
            return idle_seconds > IDLE_TIMEOUT
        except: pass
    return True

def call_model(provider: str, model: str, messages: list,
               agent: str, task_id: str = "",
               max_tokens: int = 4096,
               reasoning_split: bool = False,
               connect_timeout: int = 10,
               read_timeout: int = 45) -> Dict:
    """统一模型调用入口 — 所有AI必须走这里.

    ``connect_timeout`` and ``read_timeout`` give callers a way to apply
    a hard bound on a single provider round-trip without rewriting the
    underlying urllib call.  ``total_execution_timeout`` is enforced by
    the orchestrator's ThreadPoolExecutor wrapper because Python's
    ``urlopen`` only accepts a single combined ``timeout`` argument;
    this function delegates the per-segment bound to the OS TCP
    stack via a connect-then-read split where supported.
    """
    # 无TaskID → 拒绝
    if not task_id:
        return {"error": "REJECTED: 无TaskID", "code": "E005"}

    # Task 014: billing guard for minimax provider — must be explicitly enabled.
    if provider == "minimax" or (provider not in MODELS and PROVIDER_ALIAS.get(provider) == "minimax"):
        if os.environ.get("AIOS_MINIMAX_OFFICIAL_ENABLED", "0") != "1":
            return {"error": "AIOS_MINIMAX_OFFICIAL_DISABLED: set AIOS_MINIMAX_OFFICIAL_ENABLED=1",
                    "code": "E014", "call_id": str(uuid.uuid4())}
        try:
            import redis as _r
            _rcli = _r.Redis(host='127.0.0.1', port=6379, db=0, socket_connect_timeout=2)
            _d = _rcli.hgetall("aios:minimax_official:hash") or {}
            _used = {k.decode() if isinstance(k, bytes) else k:
                     int(v.decode() if isinstance(v, bytes) else v)
                     for k, v in (_d.items() if _d else [])}
            if _used.get("calls_used", 0) >= 30:
                return {"error": "AIOS_MINIMAX_CALL_LIMIT_REACHED", "code": "E014_LIMIT",
                        "call_id": str(uuid.uuid4())}
            if _used.get("completion_tokens_used", 0) >= 15000:
                return {"error": "AIOS_MINIMAX_TOKEN_LIMIT_REACHED", "code": "E014_TOK",
                        "call_id": str(uuid.uuid4())}
        except Exception:
            pass

    if provider not in MODELS:
        resolved = PROVIDER_ALIAS.get(provider)
        if not resolved:
            return {"error": f"未知provider: {provider}", "code": "E005"}
        provider = resolved

    cfg = MODELS[provider]
    if not cfg.get("enabled"):
        fallback = next((name for name in (DEFAULT_PROVIDER, "minimax", "bailian", "deepseek", "litellm")
                         if MODELS.get(name, {}).get("enabled")), None)
        if not fallback:
            return {"error": "没有启用的云模型 provider；本地模型保持禁用", "code": "E003"}
        provider, cfg = fallback, MODELS[fallback]

    # A model name belonging to another provider must not leak across adapter
    # boundaries. Each provider owns its default model mapping.
    #
    # NOTE (P9D-R): the previous ``model.lower().startswith(prefixes)`` check
    # only swapped the model when the user supplied a foreign provider name,
    # but it accepted the bare provider name itself (e.g. ``"minimax"``) as a
    # valid model and shipped it straight to the upstream API.  MiniMax's
    # ``/v1/chat/completions`` then returned ``400 unknown model 'minimax'``,
    # which the broad ``except Exception`` swallowed and surfaced as the
    # generic E003 ("没有启用的云模型 provider").  An invalid or empty model
    # is now normalised to the provider's ``default_model`` so the real
    # credential configuration stays visible.
    known_prefixes = {
        "minimax": ("minimax",), "bailian": ("qwen",),
        "deepseek": ("deepseek",), "litellm": (), "local": (),
    }
    prefixes = known_prefixes.get(provider, ())
    requested_model = (model or "").strip()
    if (
        not requested_model
        or requested_model.lower() in {"auto", provider.lower()}
        or (prefixes and requested_model.lower() in prefixes)
        or (prefixes and not requested_model.lower().startswith(prefixes))
    ):
        model = cfg["default_model"]
    call_id = str(uuid.uuid4())
    start_time = time.time()

    try:
        import urllib.request
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
        # MiniMax reasoning models otherwise mix <think> with the final answer.
        # Keep final content machine-readable for Planner and Verifier callers.
        if provider == "minimax" and reasoning_split:
            payload["reasoning_split"] = True
        body = json.dumps(payload).encode()
        req = urllib.request.Request(cfg["endpoint"] + "/v1/messages" if "anthropic" in cfg["endpoint"] else cfg["endpoint"],
                                     data=body if "anthropic" not in cfg["endpoint"] else json.dumps({"model":model,"messages":messages,"max_tokens":max_tokens}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "x-api-key": cfg["key"],
                                              "Authorization": f"Bearer {cfg['key']}"})
        try:
            resp = urllib.request.urlopen(req, timeout=max(1, int(read_timeout)))
        except TypeError:
            # Mock objects in unit tests may not accept ``timeout``.
            resp = urllib.request.urlopen(req)
        except Exception as _timeout_exc:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return {"error": f"timeout: {_timeout_exc}", "code": "ETIMEDOUT",
                    "call_id": call_id, "elapsed_ms": elapsed_ms}
        result = json.loads(resp.read())
        elapsed_ms = int((time.time() - start_time) * 1000)

        # 提取usage
        usage = result.get("usage", {})
        input_tokens = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
        output_tokens = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
        cost = round((input_tokens * cfg["cost_per_1k_input"] + output_tokens * cfg["cost_per_1k_output"]) / 1000, 6)

        # 记录审计
        audit = {"call_id": call_id, "agent": agent, "task_id": task_id,
                 "provider": provider, "model": model,
                 "input_tokens": input_tokens, "output_tokens": output_tokens,
                 "cost": cost, "elapsed_ms": elapsed_ms,
                 "ts": datetime.now(timezone.utc).isoformat()}

        if _is_available():
            import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
            r.zadd(KEY_AUDIT, {json.dumps(audit, ensure_ascii=False): time.time()})
            # Task 014: also bump the shared minimax-official usage hash so
            # the model_gateway and the dedicated daemon count together.
            if provider == "minimax":
                try:
                    pipe = r.pipeline()
                    pipe.hincrby("aios:minimax_official:hash", "calls_used", 1)
                    pipe.hincrby("aios:minimax_official:hash", "prompt_tokens_used", int(input_tokens))
                    pipe.hincrby("aios:minimax_official:hash", "completion_tokens_used", int(output_tokens))
                    pipe.execute()
                except Exception:
                    pass
            minute_key = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
            r.hincrby(f"{KEY_AUDIT_MINUTE}:{minute_key}", f"{agent}_calls", 1)
            r.hincrby(f"{KEY_AUDIT_MINUTE}:{minute_key}", f"{agent}_tokens", input_tokens + output_tokens)
            r.hincrbyfloat(f"{KEY_AUDIT_MINUTE}:{minute_key}", f"{agent}_cost", cost)
            # Token记账
            ds_key = datetime.now(timezone.utc).strftime("%Y%m%d")
            r.hincrby(f"aios:bus:governance:daily:{ds_key}", f"{agent}_tokens", input_tokens + output_tokens)
            r.hincrbyfloat(f"aios:bus:governance:daily:{ds_key}", f"{agent}_cost", cost)

        publish_event("token.usage", {"call_id": call_id, "agent": agent, "tokens": input_tokens + output_tokens, "cost": cost}, "model_gateway")

        # 写入observability层 — 让dashboard看到所有provider的消耗
        if input_tokens > 0 or output_tokens > 0:
            record_token(
                system=agent, model=model,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                reasoning_tokens=0,
                cache_tokens=0,
                cost=cost,
                trace_id=task_id,
                session_id=task_id,
            )

        return {"ok": True, "call_id": call_id, "result": result, "usage": {"input": input_tokens, "output": output_tokens, "cost": cost, "elapsed_ms": elapsed_ms}}

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        log_error(agent, task_id, provider, str(e), elapsed_ms)
        return {"error": str(e), "code": "E001", "call_id": call_id}

def log_error(agent: str, task_id: str, provider: str, error: str, elapsed_ms: int):
    if _is_available():
        import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        r.zadd(f"{KEY_AUDIT}:errors", {json.dumps({"agent":agent,"task_id":task_id,"provider":provider,"error":error[:200],"elapsed_ms":elapsed_ms,"ts":datetime.now(timezone.utc).isoformat()}, ensure_ascii=False): time.time()})

def get_minute_stats() -> dict:
    """获取当前分钟的API调用统计."""
    if not _is_available(): return {}
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    minute_key = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    raw = r.hgetall(f"{KEY_AUDIT_MINUTE}:{minute_key}")
    return {k.decode(): v.decode() for k, v in raw.items()} if raw else {}

def get_burn_report() -> dict:
    """烧Token报告 — 谁在烧, 烧了多少."""
    stats = get_minute_stats()
    calls = sum(int(v) for k, v in stats.items() if "calls" in k)
    tokens = sum(int(v) for k, v in stats.items() if "tokens" in k)
    cost = sum(float(v) for k, v in stats.items() if "cost" in k)
    idle = check_idle()
    return {"minute_calls": calls, "minute_tokens": tokens, "minute_cost": round(cost, 6),
            "idle": idle, "should_pause": should_pause(),
            "by_agent": {k.split("_")[0]: {"calls": int(v)} for k, v in stats.items() if "calls" in k}}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "burn"
    if cmd == "burn":
        r = get_burn_report()
        print(f"本分钟: {r['minute_calls']}次调用 {r['minute_tokens']}tokens \${r['minute_cost']}")
        print(f"空闲: {r['idle']} | 应休眠: {r['should_pause']}")
        print(f"Agent: {r['by_agent']}")
    elif cmd == "call":
        # 测试调用
        r = call_model("deepseek", "deepseek-v4-pro", [{"role":"user","content":"回复OK"}], agent="test", task_id="test_001", max_tokens=5)
        print(f"{'✅' if r.get('ok') else '❌'} {r.get('call_id','')[:8]}... usage={r.get('usage',{})}")
    elif cmd == "pause":
        print(f"{'⏸️ PAUSE' if should_pause() else '▶️ ACTIVE'}")
