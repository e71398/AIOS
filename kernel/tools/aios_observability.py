#!/usr/bin/env python3
"""
AIOS v4.0 Observability Layer
==============================
Built on top of aios_bus.py — 5 engines as a unified layer:

  1. State Engine   — Task/Agent/Tool/Model state machine
  2. Event Bus      — Extended typed events (Pub/Sub + persisted log)
  3. Token Engine   — Provider-level token & cost tracking
  4. Trace Engine   — Per-call tracing with spans
  5. Timeline Engine — Human-readable event timeline

Usage:
    from aios_observability import (emit, trace, timeline, token_tracker,
                                     OBS_EVENTS)
"""

import json, time, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any
from aios_bus import EVENT_LOG_MAX_MEMBERS, TIMELINE_MAX_MEMBERS

# ── Redis connection (reuse aios_bus's client when possible) ──
_REDIS = None
def _rc():
    global _REDIS
    if _REDIS is not None:
        try:
            _REDIS.ping()
            return _REDIS
        except:
            pass
    try:
        import redis
        _REDIS = redis.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        _REDIS.ping()
        return _REDIS
    except:
        return None

# ── Key namespace ──
PREFIX = "aios:obs"
KEY_TRACE     = f"{PREFIX}:trace"       # aios:obs:trace:{trace_id} → Hash of spans
KEY_TIMELINE  = f"{PREFIX}:timeline"     # aios:obs:timeline → Sorted Set (ts → event JSON)
KEY_TOKEN_LOG = f"{PREFIX}:token:log"    # aios:obs:token:log → Sorted Set (ts → entry)
KEY_TOKEN_DAY = f"{PREFIX}:token:daily"  # aios:obs:token:daily:{date}:{provider} → Hash
KEY_SESSION   = f"{PREFIX}:session"      # aios:obs:session:{session_id} → Hash
TTL_TRACE     = 7 * 86400                # 7 days
TTL_TIMELINE  = 3 * 86400                # 3 days

# ── Event types (extended beyond aios_bus.EVENT_TYPES) ──
OBS_EVENTS = {
    # Task lifecycle
    "task.created":     {"severity": "info",  "icon": "📋"},
    "task.assigned":    {"severity": "info",  "icon": "📎"},
    "task.running":     {"severity": "info",  "icon": "▶️"},
    "task.completed":   {"severity": "info",  "icon": "✅"},
    "task.failed":      {"severity": "error", "icon": "❌"},
    "task.blocked":     {"severity": "warn",  "icon": "🚫"},
    # Agent lifecycle
    "agent.online":     {"severity": "info",  "icon": "🟢"},
    "agent.offline":    {"severity": "warn",  "icon": "⚫"},
    "agent.busy":       {"severity": "info",  "icon": "🔄"},
    "agent.idle":       {"severity": "info",  "icon": "💤"},
    # Planning
    "plan.start":       {"severity": "info",  "icon": "🧠"},
    "plan.complete":    {"severity": "info",  "icon": "📐"},
    # Model calls
    "model.call.start": {"severity": "info",  "icon": "🤖"},
    "model.call.end":   {"severity": "info",  "icon": "✅"},
    # Tool calls
    "tool.start":       {"severity": "info",  "icon": "🔧"},
    "tool.end":         {"severity": "info",  "icon": "📎"},
    # File operations
    "file.read":        {"severity": "info",  "icon": "📖"},
    "file.write":       {"severity": "info",  "icon": "✏️"},
    "file.edit":        {"severity": "info",  "icon": "🔍"},
    # Alerts
    "alert.critical":   {"severity": "crit",  "icon": "🚨"},
    "alert.warning":    {"severity": "warn",  "icon": "⚠️"},
    "security.violation": {"severity": "crit", "icon": "🔒"},
}


# ══════════════════════════════════════════════════════════════
#  1. Event Bus — typed events with Pub/Sub + persisted log
# ══════════════════════════════════════════════════════════════

def emit(event_type: str, source: str = "system",
         payload: Optional[Dict] = None,
         session_id: str = "",
         trace_id: str = "") -> bool:
    """
    Publish an observability event.

    - Writes to aios_bus event log (for existing subscribers)
    - Writes to observability timeline (for dashboard)
    - Stores trace data if trace_id provided
    """
    if event_type not in OBS_EVENTS:
        return False
    rc = _rc()
    if not rc:
        return False

    now = datetime.now(timezone.utc)
    event = {
        "type": event_type,
        "source": source,
        "ts": now.isoformat(),
        "ts_human": now.strftime("%H:%M"),
        "icon": OBS_EVENTS[event_type]["icon"],
        "severity": OBS_EVENTS[event_type]["severity"],
        "payload": payload or {},
    }
    if session_id:
        event["session_id"] = session_id
    if trace_id:
        event["trace_id"] = trace_id

    msg = json.dumps(event, ensure_ascii=False)

    try:
        pipe = rc.pipeline(transaction=True)
        # 1. Publish to aios_bus Pub/Sub channel (existing subscribers)
        pipe.publish("aios:bus:events", msg)
        # 2. Write to aios_bus event log (sorted set)
        pipe.zadd("aios:bus:event:log", {msg: time.time()})
        pipe.zremrangebyrank("aios:bus:event:log", 0, -(EVENT_LOG_MAX_MEMBERS + 1))
        pipe.expire("aios:bus:event:log", 7 * 86400)
        # 3. Write to observability timeline (sorted set, 3d TTL)
        pipe.zadd(KEY_TIMELINE, {msg: time.time()})
        pipe.zremrangebyrank(KEY_TIMELINE, 0, -(TIMELINE_MAX_MEMBERS + 1))
        pipe.expire(KEY_TIMELINE, TTL_TIMELINE)
        pipe.execute()
        return True
    except:
        return False


def get_timeline(hours: int = 24, limit: int = 50,
                 source: str = "", event_type: str = "") -> List[Dict]:
    """Get recent timeline events, optionally filtered."""
    rc = _rc()
    if not rc:
        return []
    cutoff = time.time() - hours * 3600
    try:
        raw = rc.zrevrangebyscore(KEY_TIMELINE, "+inf", cutoff,
                                   start=0, num=limit * 3)
        results = []
        for r in raw:
            try:
                ev = json.loads(r.decode() if isinstance(r, bytes) else r)
                if source and ev.get("source") != source:
                    continue
                if event_type and ev.get("type") != event_type:
                    continue
                results.append(ev)
                if len(results) >= limit:
                    break
            except:
                pass
        return results
    except:
        return []


# ══════════════════════════════════════════════════════════════
#  2. Trace Engine — per-call tracing with spans
# ══════════════════════════════════════════════════════════════

def trace_start(session_id: str, agent: str, task: str) -> str:
    """Start a new trace for a task execution. Returns trace_id."""
    trace_id = str(uuid.uuid4())
    rc = _rc()
    if rc:
        try:
            rc.hset(f"{KEY_TRACE}:{trace_id}", mapping={
                "trace_id": trace_id,
                "session_id": session_id,
                "agent": agent,
                "task": task,
                "status": "running",
                "ts_start": datetime.now(timezone.utc).isoformat(),
                "spans": json.dumps([], ensure_ascii=False),
            })
            rc.expire(f"{KEY_TRACE}:{trace_id}", TTL_TRACE)
        except:
            pass
    emit("task.running", source=agent,
         payload={"task": task, "trace_id": trace_id},
         session_id=session_id, trace_id=trace_id)
    return trace_id


def trace_span(trace_id: str, name: str, status: str = "ok",
               metadata: Optional[Dict] = None):
    """Add a span to an existing trace."""
    if not trace_id:
        return
    rc = _rc()
    if not rc:
        return
    try:
        key = f"{KEY_TRACE}:{trace_id}"
        raw_spans = rc.hget(key, "spans")
        spans = json.loads(raw_spans.decode() if isinstance(raw_spans, bytes)
                           else (raw_spans or "[]"))
        span = {
            "name": name,
            "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            span.update(metadata)
        spans.append(span)
        rc.hset(key, "spans", json.dumps(spans, ensure_ascii=False))
        rc.expire(key, TTL_TRACE)
    except:
        pass


def trace_end(trace_id: str, status: str = "completed",
              summary: str = ""):
    """Mark a trace as completed/failed."""
    if not trace_id:
        return
    rc = _rc()
    if not rc:
        return
    try:
        key = f"{KEY_TRACE}:{trace_id}"
        now = datetime.now(timezone.utc).isoformat()
        rc.hset(key, "status", status)
        rc.hset(key, "ts_end", now)
        if summary:
            rc.hset(key, "summary", summary[:512])
        # compute total duration
        ts_start = rc.hget(key, "ts_start")
        if ts_start:
            ts_s = ts_start.decode() if isinstance(ts_start, bytes) else ts_start
            try:
                start_dt = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
                dur = int((end_dt - start_dt).total_seconds() * 1000)
                rc.hset(key, "duration_ms", str(dur))
            except:
                pass
        rc.expire(key, TTL_TRACE)
        # emit completion event
        agent = rc.hget(key, "agent")
        task = rc.hget(key, "task")
        if agent and task:
            agent_s = agent.decode() if isinstance(agent, bytes) else agent
            task_s = task.decode() if isinstance(task, bytes) else task
            emit(
                f"task.{status}" if status in ("completed","failed") else "task.blocked",
                source=agent_s,
                payload={"task": task_s, "trace_id": trace_id, "summary": summary},
                trace_id=trace_id,
            )
    except:
        pass


def get_trace(trace_id: str) -> Optional[Dict]:
    """Retrieve a full trace by ID."""
    rc = _rc()
    if not rc:
        return None
    try:
        raw = rc.hgetall(f"{KEY_TRACE}:{trace_id}")
        if not raw:
            return None
        result = {}
        for k, v in raw.items():
            k_str = k.decode() if isinstance(k, bytes) else k
            v_str = v.decode() if isinstance(v, bytes) else v
            if k_str == "spans":
                try:
                    result[k_str] = json.loads(v_str)
                except:
                    result[k_str] = []
            else:
                result[k_str] = v_str
        return result
    except:
        return None


def list_traces(agent: str = "", limit: int = 20) -> List[Dict]:
    """List recent traces."""
    rc = _rc()
    if not rc:
        return []
    traces = []
    try:
        for key in rc.scan_iter(f"{KEY_TRACE}:*"):
            raw = rc.hgetall(key)
            if not raw:
                continue
            t = {}
            for k, v in raw.items():
                k_str = k.decode() if isinstance(k, bytes) else k
                v_str = v.decode() if isinstance(v, bytes) else v
                if k_str == "spans":
                    try:
                        t[k_str] = json.loads(v_str)
                    except:
                        t[k_str] = []
                else:
                    t[k_str] = v_str
            if agent and t.get("agent") != agent:
                continue
            traces.append(t)
            if len(traces) >= limit:
                break
    except:
        pass
    # sort by ts_start descending
    traces.sort(key=lambda t: t.get("ts_start", ""), reverse=True)
    return traces[:limit]


# ══════════════════════════════════════════════════════════════
#  3. Token Engine — per-provider token & cost tracking
# ══════════════════════════════════════════════════════════════

# Provider registry — all known model providers
PROVIDERS = {
    "deepseek": {"models": ["deepseek-v4-pro", "deepseek-v4-flash"]},
    "minimax":  {"models": ["minimax-m3", "minimax-t2"]},
    "openai":   {"models": ["gpt-4", "gpt-4o", "gpt-4o-mini"]},
    "anthropic": {"models": ["claude-3", "claude-3.5", "claude-4"]},
    "local":    {"models": ["qwen3-8b"]},
}


def resolve_provider(model: str) -> str:
    """Map a model name to its provider."""
    for prov, cfg in PROVIDERS.items():
        if any(m in model.lower() for m in cfg["models"]):
            return prov
    if "deepseek" in model.lower():
        return "deepseek"
    if "minimax" in model.lower() or "abab" in model.lower():
        return "minimax"
    if "gpt" in model.lower():
        return "openai"
    if "claude" in model.lower():
        return "anthropic"
    return "unknown"


def record_token(system: str, model: str,
                 prompt_tokens: int = 0,
                 completion_tokens: int = 0,
                 reasoning_tokens: int = 0,
                 cache_tokens: int = 0,
                 cost: float = 0,
                 trace_id: str = "",
                 session_id: str = "") -> bool:
    """
    Record token usage with full provider breakdown.

    Writes to:
      - aios:obs:token:daily:{date}:{provider}  (aggregated)
      - aios:obs:token:log                       (per-call log)
      - aios:bus:governance:daily:{date}         (existing dashboard compat)
    """
    rc = _rc()
    if not rc:
        return False

    provider = resolve_provider(model)
    now = datetime.now(timezone.utc)
    ds = now.strftime("%Y%m%d")
    total_tokens = prompt_tokens + completion_tokens + reasoning_tokens + cache_tokens
    if total_tokens == 0 and cost == 0:
        return False

    # 1. Per-provider daily aggregation
    day_key = f"{KEY_TOKEN_DAY}:{ds}:{provider}"
    try:
        rc.hincrby(day_key, "prompt_tokens", prompt_tokens)
        rc.hincrby(day_key, "completion_tokens", completion_tokens)
        rc.hincrby(day_key, "reasoning_tokens", reasoning_tokens)
        rc.hincrby(day_key, "cache_tokens", cache_tokens)
        rc.hincrby(day_key, "total_tokens", total_tokens)
        rc.hincrby(day_key, "call_count", 1)
        if cost > 0:
            rc.hincrbyfloat(day_key, "cost", cost)
        rc.expire(day_key, 90 * 86400)
    except:
        pass

    # 2. Per-call log
    entry = {
        "ts": now.isoformat(),
        "system": system,
        "model": model,
        "provider": provider,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_tokens": cache_tokens,
        "total_tokens": total_tokens,
        "cost": round(cost, 6),
    }
    if trace_id:
        entry["trace_id"] = trace_id
    if session_id:
        entry["session_id"] = session_id
    try:
        rc.zadd(KEY_TOKEN_LOG, {json.dumps(entry, ensure_ascii=False): time.time()})
        rc.expire(KEY_TOKEN_LOG, 30 * 86400)
    except:
        pass

    # 3. Backward compat: also write to aios:bus:governance:daily
    try:
        gov_key = f"aios:bus:governance:daily:{ds}"
        if total_tokens > 0:
            rc.hincrby(gov_key, f"{system}_tokens", total_tokens)
        if cost > 0:
            rc.hincrbyfloat(gov_key, f"{system}_cost", cost)
        rc.expire(gov_key, 90 * 86400)
    except:
        pass

    # 4. Emit event
    emit("model.call.end", source=system,
         payload={"model": model, "provider": provider,
                  "tokens": total_tokens, "cost": round(cost, 6)},
         session_id=session_id, trace_id=trace_id)
    return True


def get_token_summary(days: int = 7) -> Dict:
    """
    Get aggregated token summary for the last N days.

    Returns:
      {
        "daily": [{"date": "20260706", "by_provider": {...}, "total": ...}, ...],
        "totals": {"total_tokens": ..., "total_cost": ..., "by_provider": {...}}
      }
    """
    rc = _rc()
    if not rc:
        return {"daily": [], "totals": {}}

    daily = []
    grand_total = {"total_tokens": 0, "total_cost": 0.0,
                   "by_provider": {}, "call_count": 0}

    for d in range(days):
        ds = (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y%m%d")
        day_entry = {"date": ds, "by_provider": {}, "total_tokens": 0,
                     "total_cost": 0.0, "call_count": 0}
        try:
            for key in rc.scan_iter(f"{KEY_TOKEN_DAY}:{ds}:*"):
                prov = key.decode().split(":")[-1] if isinstance(key, bytes) \
                       else key.split(":")[-1]
                raw = rc.hgetall(key)
                if not raw:
                    continue
                prov_data = {}
                for k, v in raw.items():
                    k_str = k.decode() if isinstance(k, bytes) else k
                    v_str = v.decode() if isinstance(v, bytes) else v
                    try:
                        prov_data[k_str] = float(v_str) if "." in v_str else int(v_str)
                    except:
                        prov_data[k_str] = v_str
                day_entry["by_provider"][prov] = prov_data
                day_entry["total_tokens"] += prov_data.get("total_tokens", 0)
                day_entry["total_cost"] += prov_data.get("cost", 0)
                day_entry["call_count"] += prov_data.get("call_count", 0)
        except:
            pass
        daily.append(day_entry)

        # Aggregate into grand total
        grand_total["total_tokens"] += day_entry["total_tokens"]
        grand_total["total_cost"] += day_entry["total_cost"]
        grand_total["call_count"] += day_entry["call_count"]
        for prov, pdata in day_entry["by_provider"].items():
            if prov not in grand_total["by_provider"]:
                grand_total["by_provider"][prov] = {}
            for k, v in pdata.items():
                grand_total["by_provider"][prov][k] = \
                    grand_total["by_provider"][prov].get(k, 0) + v

    return {
        "daily": sorted(daily, key=lambda x: x["date"]),
        "totals": grand_total,
    }


def get_recent_token_calls(limit: int = 20) -> List[Dict]:
    """Get most recent per-call token log entries."""
    rc = _rc()
    if not rc:
        return []
    try:
        raw = rc.zrevrange(KEY_TOKEN_LOG, 0, limit - 1)
        results = []
        for r in raw:
            try:
                results.append(json.loads(r.decode() if isinstance(r, bytes) else r))
            except:
                pass
        return results
    except:
        return []


# ══════════════════════════════════════════════════════════════
#  4. State Engine — unified state query
# ══════════════════════════════════════════════════════════════

def get_agent_state(name: str) -> Dict:
    """Get unified agent state from all available sources.
    
    States:
      - "offline" — no heartbeat, no PID
      - "idle"    — alive but not doing work
      - "running" — actively working (CPU > 5% or active tasks or recent events)
    """
    rc = _rc()
    if not rc:
        return {"name": name, "status": "unknown"}

    result = {"name": name, "status": "offline", "alive": False, "working": False}

    # 1. Heartbeat → alive check (双因子: 需要心跳 + PID 同时存在)
    has_pid = False
    try:
        import subprocess as _sp
        _PATS = {"hermes":"hermes_cli.main gateway",
                 "openclaw":"openclaw-gateway|dist/index.*gateway",
                 "claude":r"claude\b",
                 "codex":"codex-relay|codex-pal",
                 "opencode":r"opencode\b"}
        out = _sp.check_output(["pgrep", "-f", _PATS.get(name, name)], timeout=2).decode().strip()
        has_pid = bool(out)
    except:
        pass

    try:
        hb = rc.get(f"aios:bus:system:{name}:heartbeat")
        if hb:
            hb_str = hb.decode() if isinstance(hb, bytes) else hb
            hb_ts = datetime.fromisoformat(hb_str.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - hb_ts.replace(tzinfo=timezone.utc)).total_seconds()
            result["heartbeat_age_s"] = int(age)
            # alive = 进程在跑 AND 心跳在 90s 内
            result["alive"] = has_pid and age < 90
    except:
        pass

    if not result["alive"]:
        result["status"] = "offline"
        return result

    # Alive but not yet confirmed working
    result["status"] = "idle"

    # 2. Check active tasks (locked/running in queue state)
    try:
        for key in rc.scan_iter("aios:bus:state:*"):
            st = rc.hget(key, "status")
            ex = rc.hget(key, "executor") or rc.hget(key, "system")
            if st and ex:
                s = st.decode() if isinstance(st, bytes) else st
                e = ex.decode() if isinstance(ex, bytes) else ex
                if s in ("running",) and e == name:
                    result["status"] = "running"
                    result["working"] = True
                    break
    except:
        pass

    # 3. CPU activity (only if not already confirmed working)
    if not result["working"]:
        try:
            import subprocess
            _PATS = {"hermes":"hermes_cli.main gateway",
                     "openclaw":"openclaw-gateway|dist/index.*gateway",
                     "claude":r"claude\b",
                     "codex":"codex-relay|codex-pal",
                     "opencode":r"opencode\b"}
            pat = _PATS.get(name, name)
            out = subprocess.check_output(["pgrep", "-f", pat], timeout=2).decode().strip()
            if out:
                for pid_str in out.split("\n"):
                    pid = int(pid_str.strip())
                    CLK_TCK = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
                    with open(f'/proc/{pid}/stat') as f:
                        p = f.read().split()
                        utime, stime = int(p[13]), int(p[14])
                    now = time.time()
                    snap = getattr(get_agent_state, "_cpu_snap", {})
                    prev = snap.get(pid)
                    if prev:
                        dt = now - prev[0]
                        dc = (utime + stime) - prev[1]
                        if dt > 0 and dc >= 0:
                            pct = (dc / CLK_TCK) / dt * 100
                            if pct > 3.0:
                                result["status"] = "running"
                                result["working"] = True
                                result["cpu_pct"] = round(pct, 1)
                    if not hasattr(get_agent_state, "_cpu_snap"):
                        get_agent_state._cpu_snap = {}
                    get_agent_state._cpu_snap[pid] = (now, utime + stime)
                    if result["working"]:
                        break
        except:
            pass

    # 4. Recent busy events from timeline (short window)
    if not result["working"]:
        try:
            events = get_timeline(hours=1, limit=5, source=name)
            if events:
                result["recent_event"] = events[0].get("type", "")
                result["recent_event_ts"] = events[0].get("ts_human", "")
                # agent.busy in last 60s → working
                for ev in events[:3]:
                    if ev.get("type") == "agent.busy" or ev.get("severity") == "info":
                        ts = datetime.fromisoformat(ev.get("ts", "").replace("Z", "+00:00"))
                        age = (datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)).total_seconds()
                        if age < 60:  # 1分钟内
                            result["status"] = "running"
                            result["working"] = True
                            break
        except:
            pass

    return result


def get_all_agents_state() -> List[Dict]:
    """Get state for all known agents."""
    agents = []
    for name in ["hermes", "openclaw", "opencode", "claude", "codex"]:
        agents.append(get_agent_state(name))
    return agents


# ══════════════════════════════════════════════════════════════
#  Quick stats for dashboard
# ══════════════════════════════════════════════════════════════

def get_observability_summary() -> Dict:
    """Single call returning all data needed by the dashboard."""
    token_summary = get_token_summary(days=7)
    timeline = get_timeline(hours=6, limit=80)
    agents = get_all_agents_state()
    recent_calls = get_recent_token_calls(limit=10)

    return {
        "agents": agents,
        "timeline": timeline,
        "tokens": token_summary,
        "recent_calls": recent_calls,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
