#!/usr/bin/env python3
"""优化2: Agent死锁检测 — 循环依赖+超时+自动切换"""
import sys, time, json
from pathlib import Path; from datetime import datetime, timezone, timedelta
TOOLS = Path("${AIOS_HOME}/kernel/tools"); sys.path.insert(0, str(TOOLS))
from aios_bus import check_recent, lifecycle_list_all, publish_event, enqueue_task, _is_available

TIMEOUT_MINUTES = 30

def detect_cycles():
    """检测任务间循环依赖(图遍历)."""
    recent = check_recent(limit=50)
    deps = {}  # task_id → [depends_on_task_ids]
    for r in recent:
        ctx = r.get("context", "") or r.get("summary", "")
        if not ctx: continue
        tid = r.get("task_id", "")
        for r2 in recent:
            if r2["task_id"] != tid and r2["task_id"] in ctx:
                deps.setdefault(tid, []).append(r2["task_id"])
    # 简单循环检测: A依赖B, B依赖A
    cycles = []
    for a, bs in deps.items():
        for b in bs:
            if b in deps and a in deps[b]:
                cycles.append((a[:8], b[:8]))
    return {"cycles_found": len(cycles), "details": cycles}

def detect_timeouts():
    """检测超时任务(>30分钟无更新)."""
    recent = check_recent(limit=100)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=TIMEOUT_MINUTES)
    stuck = []
    for r in recent:
        if r.get("status") not in ("locked", "running"): continue
        ts_str = r.get("ts_complete", "") or r.get("ts_start", "")
        if not ts_str: continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts < cutoff:
                stuck.append({"task_id": r["task_id"], "name": r.get("task_name", "")[:60],
                              "agent": r.get("system", "?"), "age_min": int((datetime.now(timezone.utc)-ts).total_seconds()/60)})
        except: pass
    return stuck

def handle_deadlock():
    """检测到死锁/超时→自动切Codex."""
    stuck = detect_timeouts()
    if not stuck: return {"action": "none"}
    for s in stuck:
        enqueue_task(f"[死锁转移-来自{s['agent']}] {s['name']}", system="codex",
                     priority=1, logic_depth="batch", source="deadlock_detector")
        publish_event("alert.critical", {"type": "deadlock_timeout", "task": s["name"], "age_min": s["age_min"]}, "deadlock")
    cycles = detect_cycles()
    return {"timeouts": len(stuck), "cycles": cycles["cycles_found"], "transferred_to_codex": len(stuck)}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check": print(json.dumps(handle_deadlock(), ensure_ascii=False, indent=2))
    elif cmd == "timeouts":
        for s in detect_timeouts(): print(f"  ⏰ {s['agent']}: {s['name'][:50]} ({s['age_min']}min)")
    elif cmd == "cycles": print(json.dumps(detect_cycles(), ensure_ascii=False, indent=2))
