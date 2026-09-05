#!/usr/bin/env python3
"""优化1: 任务优先级队列 — Redis Sorted Set P0/P1/P2/P3"""
import sys, time
from pathlib import Path
TOOLS = Path("${AIOS_HOME}/kernel/tools"); sys.path.insert(0, str(TOOLS))
from aios_bus import _is_available, generate_task_id, publish_event

KEY_PQ = "aios:queue:priority"  # Sorted Set: score=priority*1e12+ts
PRIORITY = {"P0": -0, "P1": -1, "P2": -2, "P3": -3}

def enqueue(task_name: str, priority: str = "P2", agent_id: str = "", data: dict = None):
    if not _is_available(): return None
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    tid = generate_task_id()
    score = PRIORITY.get(priority, -2) * 1e12 + time.time()
    payload = {"id": tid, "task": task_name[:200], "priority": priority, "agent": agent_id,
               "data": data or {}, "ts": time.time()}
    r.zadd(KEY_PQ, {tid: score})
    r.hset(f"{KEY_PQ}:data", tid, __import__('json').dumps(payload, ensure_ascii=False))
    publish_event("task.created", {"task_id": tid, "priority": priority}, "priority_queue")
    return tid

def dequeue(agent_id: str = ""):
    if not _is_available(): return None
    import redis as _r, json; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    results = r.zpopmin(KEY_PQ, 1)
    if not results: return None
    tid = results[0][0].decode() if isinstance(results[0][0], bytes) else results[0][0]
    raw = r.hget(f"{KEY_PQ}:data", tid)
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else None

def requeue(task_id: str, new_priority: str):
    if not _is_available(): return False
    import redis as _r, json; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    raw = r.hget(f"{KEY_PQ}:data", task_id)
    if not raw: return False
    data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    data["priority"] = new_priority
    score = PRIORITY.get(new_priority, -2) * 1e12 + time.time()
    r.zadd(KEY_PQ, {task_id: score})
    r.hset(f"{KEY_PQ}:data", task_id, json.dumps(data, ensure_ascii=False))
    return True

def queue_size():
    if not _is_available(): return 0
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    return r.zcard(KEY_PQ)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if cmd == "test":
        t1 = enqueue("紧急修复", "P0"); t2 = enqueue("日常任务", "P2")
        print(f"enqueued: {t1}, {t2} | size: {queue_size()}")
        print(f"dequeue: {dequeue()}")  # 应该先出P0
