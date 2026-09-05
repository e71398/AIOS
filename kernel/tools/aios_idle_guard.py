#!/usr/bin/env python3
"""Idle Guard — 队列空>5分钟→PAUSE所有后台LLM调用"""
import sys, os, time, json
from pathlib import Path; from datetime import datetime, timezone, timedelta
TOOLS = Path("${AIOS_HOME}/kernel/tools"); sys.path.insert(0, str(TOOLS))
from aios_bus import get_queue_status, check_recent, publish_event, _is_available

KEY_IDLE = "aios:gateway:idle"
IDLE_TIMEOUT = 300  # 5分钟

def is_idle():
    qs = get_queue_status()
    if qs.get("pending",0) > 0 or qs.get("locked",0) > 0 or qs.get("running",0) > 0:
        return False
    recent = check_recent(limit=5)
    if not recent: return True
    ts = recent[0].get("ts_complete","")
    if ts:
        try:
            last = datetime.fromisoformat(ts.replace("Z","+00:00"))
            return (datetime.now(timezone.utc)-last.replace(tzinfo=timezone.utc)).total_seconds() > IDLE_TIMEOUT
        except: pass
    return True

def set_idle_mode(pause: bool):
    if _is_available():
        import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        r.set(KEY_IDLE, "paused" if pause else "active")
        r.expire(KEY_IDLE, 600)
    if pause:
        publish_event("alert.warning", {"type":"system_idle","action":"pause_all_llm_calls"},"idle_guard")

def guard_cycle():
    idle = is_idle()
    set_idle_mode(idle)
    if idle: print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏸️ IDLE → 所有后台LLM调用暂停")
    return idle

if __name__ == "__main__":
    idle = guard_cycle()
    print(f"{'⏸️ PAUSED' if idle else '▶️ ACTIVE'}")
