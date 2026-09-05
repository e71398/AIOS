#!/usr/bin/env python3
"""
Claude Code → AIOS Event Bus Bridge
=====================================
Translates Claude Code's local telemetry & process activity into
proper AIOS observability events, so the dashboard gets accurate
real-time data without modifying Claude Code itself.

Data sources:
  1. ~/.claude/telemetry/1p_failed_events_*.json  — telemetry events
  2. /proc/<claude_pid>/io                          — I/O activity
  3. /proc/<claude_pid>/fd/                         — file descriptors
  4. ~/.claude/transcripts/*.jsonl                  — session transcripts

Architecture:
  Claude Code ──► /proc (process monitor) ──► claude_bridge ──► Event Bus
                    ~/.claude/telemetry/   ──► emit() / record_token() ──► Redis
                                                                              │
                                                                         Dashboard (SSE)
"""

import json, os, sys, time, glob, base64, threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_observability import (
    emit, record_token, trace_start, trace_span, trace_end,
    get_timeline, OBS_EVENTS,
)


# ── helpers ──

_CC_DIR = os.path.expanduser("~/.claude")
_TELE_DIR = f"{_CC_DIR}/telemetry"
_TRANS_DIR = f"{_CC_DIR}/transcripts"

_SESSION_CACHE = {}       # session_id → {trace_id, last_event}
_PROCESSED_EVENTS = set()  # telemetry event_id dedup
_FILE_POS = {}             # telemetry file path → bytes read



# ══════════════════════════════════════════════════════════════
#  Telemetry File Watcher
# ══════════════════════════════════════════════════════════════

# Map Claude Code event names → AIOS event types + handler
_TELEMETRY_MAP = {
    "tengu_started":           ("agent.online",  None),
    "tengu_exit":              ("agent.offline", None),
    "tengu_shutdown_signal":   ("agent.offline", None),
    "tengu_api_query":         ("model.call.start", None),
    "tengu_api_success":       ("model.call.end", "_handle_api_success"),
    "tengu_unknown_model_cost":("model.call.end", "_handle_unknown_cost"),
    "tengu_tool_use_success":  ("tool.end", None),
    "tengu_tool_use_error":    ("tool.end", None),
    "tengu_file_operation":    ("file.edit", None),
    "tengu_file_changed":      ("file.write", None),
    "tengu_feature_ok":        ("agent.busy", None),
    "tengu_feature_sad":       ("alert.warning", None),
    "tengu_bash_tool_command_failed": ("tool.end", None),
}


def _decode_meta(b64: str) -> dict:
    """Decode base64 additional_metadata from telemetry event."""
    try:
        return json.loads(base64.b64decode(b64).decode())
    except:
        return {}


def _handle_api_success(ed: dict, meta: dict):
    """Extract token data from tengu_api_success and record it."""
    tokens_in = meta.get("inputTokens", 0) or 0
    tokens_out = meta.get("outputTokens", 0) or 0
    tokens_cache = meta.get("cachedInputTokens", 0) or 0
    tokens_reason = meta.get("thinkingContentLength", 0) or 0
    cost = meta.get("costUSD", 0) or 0
    model = meta.get("model", "deepseek-v4-pro")
    system = "claude"
    session_id = ed.get("session_id", "")
    trace_id = _SESSION_CACHE.get(session_id, {}).get("trace_id", "")

    record_token(
        system=system, model=model,
        prompt_tokens=tokens_in + tokens_cache,
        completion_tokens=tokens_out,
        reasoning_tokens=tokens_reason // 4,  # chars → token estimate
        cache_tokens=tokens_cache,
        cost=cost,
        trace_id=trace_id,
        session_id=session_id,
    )

    # Emit model.call.end with details
    emit("model.call.end", source="claude",
         payload={"model": model,
                  "input_tokens": tokens_in,
                  "output_tokens": tokens_out,
                  "cached_tokens": tokens_cache,
                  "cost": round(cost, 6),
                  "stop_reason": meta.get("stop_reason", "")},
         session_id=session_id, trace_id=trace_id)


def _handle_unknown_cost(ed: dict, meta: dict):
    """Handle tengu_unknown_model_cost — has model info but no token breakdown."""
    model = meta.get("model", ed.get("model", "deepseek-v4-pro"))
    session_id = ed.get("session_id", "")
    emit("model.call.end", source="claude",
         payload={"model": model, "note": "cost_unknown"},
         session_id=session_id,
         trace_id=_SESSION_CACHE.get(session_id, {}).get("trace_id", ""))


def _process_telemetry_line(line: str):
    """Parse one telemetry JSON line and emit corresponding event(s)."""
    try:
        ev = json.loads(line)
    except:
        return

    ed = ev.get("event_data", {})
    eid = ed.get("event_id", "")
    if eid in _PROCESSED_EVENTS:
        return
    _PROCESSED_EVENTS.add(eid)

    event_name = ed.get("event_name", "")
    mapping = _TELEMETRY_MAP.get(event_name)
    if not mapping:
        return

    aios_event, handler_name = mapping
    session_id = ed.get("session_id", "")
    meta = _decode_meta(ed.get("additional_metadata", ""))

    # Track session → trace
    if session_id and session_id not in _SESSION_CACHE:
        _SESSION_CACHE[session_id] = {
            "trace_id": "",
            "started": ed.get("client_timestamp", ""),
        }
    trace_id = _SESSION_CACHE.get(session_id, {}).get("trace_id", "")

    # Default payload
    payload = {
        "telemetry_event": event_name,
        "model": ed.get("model", meta.get("model", "")),
    }
    # Add tool name if present
    tn = meta.get("toolName", ed.get("tool_name", ""))
    if tn:
        payload["tool"] = tn
    # Add extra metadata
    if meta.get("stop_reason"):
        payload["stop_reason"] = meta["stop_reason"]

    # Emit the mapped event
    emit(aios_event, source="claude",
         payload=payload,
         session_id=session_id, trace_id=trace_id)

    # Run specific handler if defined
    if handler_name:
        handler = globals().get(handler_name)
        if handler:
            handler(ed, meta)


def watch_telemetry():
    """Poll telemetry directory for new events (daemon thread)."""
    while True:
        try:
            files = sorted(glob.glob(f"{_TELE_DIR}/*.json"),
                           key=os.path.getmtime, reverse=True)
            for fp in files:
                try:
                    sz = os.path.getsize(fp)
                    last = _FILE_POS.get(fp, 0)
                    if sz <= last:
                        continue
                    with open(fp) as fh:
                        fh.seek(last)
                        for line in fh:
                            ln = line.strip()
                            if ln:
                                _process_telemetry_line(ln)
                    _FILE_POS[fp] = os.path.getsize(fp)
                except:
                    pass
        except:
            pass
        time.sleep(8)


# ══════════════════════════════════════════════════════════════
#  Process Activity Monitor
# ══════════════════════════════════════════════════════════════

_LAST_CPU = {}   # pid → (time, utime+stime)
_LAST_IO = {}    # pid → rchar

def monitor_process(pid: int, session_id: str = ""):
    """Monitor a single Claude Code process for activity bursts."""
    trace_id = _SESSION_CACHE.get(session_id, {}).get("trace_id", "")
    is_active = False

    # CPU check (delta-based)
    try:
        with open(f"/proc/{pid}/stat") as f:
            p = f.read().split()
            utime, stime = int(p[13]), int(p[14])
        now = time.time()
        prev = _LAST_CPU.get(pid)
        if prev:
            dt = now - prev[0]
            dc = (utime + stime) - prev[1]
            if dt > 0 and dc >= 0:
                CLK_TCK = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
                pct = (dc / CLK_TCK) / dt * 100
                is_active = pct > 3.0
        _LAST_CPU[pid] = (now, utime + stime)
    except:
        pass

    # I/O check (rchar delta ≈ data received)
    bytes_received = 0
    try:
        with open(f"/proc/{pid}/io") as f:
            for line in f:
                if line.startswith("rchar:"):
                    cur = int(line.split()[1])
                    prev_io = _LAST_IO.get(pid, 0)
                    if prev_io:
                        bytes_received = cur - prev_io
                    _LAST_IO[pid] = cur
                    break
    except:
        pass

    # If significant I/O detected → likely API response received
    if bytes_received > 5000:  # >5KB = real API response
        # Estimate tokens: ~4 bytes/token for API JSON
        est_tokens = bytes_received // 4
        record_token(
            system="claude", model="deepseek-v4-pro",
            completion_tokens=est_tokens,
            cost=0,  # cost unknown from I/O alone
            trace_id=trace_id, session_id=session_id,
        )
        emit("model.call.end", source="claude",
             payload={"model": "deepseek-v4-pro",
                      "estimated_tokens": est_tokens,
                      "bytes_received": bytes_received,
                      "note": "estimated_from_io"},
             session_id=session_id, trace_id=trace_id)

    # Emit busy/idle based on activity
    if is_active:
        emit("agent.busy", source="claude",
             payload={"cpu_pct": round(pct, 1) if 'pct' in dir() else 0},
             session_id=session_id, trace_id=trace_id)


# ══════════════════════════════════════════════════════════════
#  Transcript Parser (post-session token estimation)
# ══════════════════════════════════════════════════════════════

_TRANS_POS = {}

def watch_transcripts():
    """Watch transcript files for completed sessions to backfill token data."""
    while True:
        try:
            files = glob.glob(f"{_TRANS_DIR}/*.jsonl")
            for fp in files:
                try:
                    sz = os.path.getsize(fp)
                    last = _TRANS_POS.get(fp, 0)
                    # Only process completed transcripts (no longer growing)
                    if sz > 0 and sz == last:
                        continue  # already fully processed
                    # Check if file is "stable" (not modified in 60s → session ended)
                    mtime = os.path.getmtime(fp)
                    if time.time() - mtime < 60:
                        continue
                    # Parse for token estimation
                    if sz != last and last > 0:
                        # File grew then stopped → session ended, parse it
                        _parse_transcript(fp)
                    _TRANS_POS[fp] = sz
                except:
                    pass
        except:
            pass
        time.sleep(30)


def _parse_transcript(fp: str):
    """Parse a transcript JSONL file for token estimation."""
    total_chars = 0
    assistant_turns = 0
    try:
        with open(fp) as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                    if entry.get("type") in ("assistant", "user"):
                        content = entry.get("content", "")
                        if isinstance(content, str):
                            total_chars += len(content)
                            if entry["type"] == "assistant":
                                assistant_turns += 1
                except:
                    pass
    except:
        return

    if assistant_turns == 0:
        return

    # Rough estimation: Chinese-heavy → 2 chars/token, English → 4 chars/token
    est_tokens = int(total_chars / 3)  # weighted average
    cost = est_tokens * 0.000002  # approx DeepSeek rate

    record_token(
        system="claude", model="deepseek-v4-pro (estimated)",
        prompt_tokens=0, completion_tokens=est_tokens,
        cost=round(cost, 4),
    )


# ══════════════════════════════════════════════════════════════
#  Main entry — start all bridge daemons
# ══════════════════════════════════════════════════════════════

def start(pid: int = 0, session_id: str = ""):
    """Start all bridge daemon threads."""
    threads = [
        threading.Thread(target=watch_telemetry, daemon=True),
        threading.Thread(target=watch_transcripts, daemon=True),
    ]
    for t in threads:
        t.start()

    # Process monitor runs in-line with PID tracking
    if pid:
        _SESSION_CACHE[session_id or "default"] = {"trace_id": "", "started": datetime.now(timezone.utc).isoformat()}
        while True:
            try:
                monitor_process(pid, session_id or "default")
            except:
                pass
            time.sleep(10)
