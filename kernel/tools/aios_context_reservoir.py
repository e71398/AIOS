#!/usr/bin/env python3
"""
P0-3: Context Reservoir 上下文管理器
======================================
L1热层(Redis秒级) — 跨任务共享即时上下文。
不管哪个AI执行子任务, 都能读到完整链路上下文。

用法:
  save_context(task_id, key, value)  — 存
  load_context(task_id, key)         — 取
  get_task_chain(parent_id)          — 获取父任务+所有子任务上下文
"""

import sys, os, json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import _is_available, generate_task_id, publish_event

KEY_CTX = "aios:ctx"
CTX_TTL = 3600  # 1小时热层


def save_context(task_id: str, key: str, value: Any) -> bool:
    """存入上下文 — L1热层."""
    if not _is_available(): return False
    try:
        import redis as _r
        c = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        data = {"value": json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value,
                "ts": datetime.now(timezone.utc).isoformat()}
        c.hset(f"{KEY_CTX}:{task_id}", key, json.dumps(data, ensure_ascii=False))
        c.expire(f"{KEY_CTX}:{task_id}", CTX_TTL)
        return True
    except: return False


def load_context(task_id: str, key: str) -> Optional[Any]:
    """读取上下文."""
    if not _is_available(): return None
    try:
        import redis as _r
        c = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        raw = c.hget(f"{KEY_CTX}:{task_id}", key)
        if raw:
            data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            val = data.get("value", "")
            try: return json.loads(val)
            except: return val
    except: pass
    return None


def save_task_snapshot(task_id: str, parent_id: str = "", executor: str = "",
                       task_name: str = "", status: str = "running", result: str = ""):
    """保存完整任务快照到上下文库."""
    save_context(task_id, "_parent", parent_id)
    save_context(task_id, "_executor", executor)
    save_context(task_id, "_name", task_name)
    save_context(task_id, "_status", status)
    save_context(task_id, "_result", result[:500] if result else "")
    if parent_id:
        children = load_context(parent_id, "_children") or []
        if task_id not in children:
            children.append(task_id)
            save_context(parent_id, "_children", children)
        publish_event("task.running", {"task_id": task_id, "parent": parent_id, "executor": executor}, "context_reservoir")


def get_task_chain(parent_id: str) -> Dict:
    """获取父任务+所有子任务的完整上下文链."""
    chain = {"parent_id": parent_id, "parent": {}, "children": []}
    chain["parent"]["name"] = load_context(parent_id, "_name") or ""
    chain["parent"]["executor"] = load_context(parent_id, "_executor") or ""
    chain["parent"]["status"] = load_context(parent_id, "_status") or ""
    chain["parent"]["result"] = load_context(parent_id, "_result") or ""
    children = load_context(parent_id, "_children") or []
    for cid in children:
        chain["children"].append({
            "task_id": cid,
            "name": load_context(cid, "_name") or "",
            "executor": load_context(cid, "_executor") or "",
            "status": load_context(cid, "_status") or "",
            "result": load_context(cid, "_result") or "",
        })
    return chain


if __name__ == "__main__":
    # 快速测试
    pid = generate_task_id()
    cid = generate_task_id()
    save_task_snapshot(pid, "", "openclaw", "分析项目结构", "running")
    save_task_snapshot(cid, pid, "claude", "子任务:统计代码行数", "completed", "共3021行")
    chain = get_task_chain(pid)
    print(json.dumps(chain, ensure_ascii=False, indent=2))
