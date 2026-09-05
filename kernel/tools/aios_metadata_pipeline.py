#!/usr/bin/env python3
"""元数据上报管线 — 执行器完成后向 Hermes 推结构化数据"""
import sys, json, time
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

try:
    from aios_bus import publish_event, get_hermes_strategy
    from aios_agent_mesh import classify_task
except ImportError:
    classify_task = None


def report(task_id: str, executor: str, task_text: str, status: str,
           elapsed_s: float, summary: str = "", error: str = ""):
    """执行器完成后上报结构化元数据到 Hermes."""
    zodiac = {}
    if classify_task and task_text:
        try:
            zodiac = classify_task(task_text)
        except Exception:
            pass

    meta = {
        "task_id": task_id,
        "executor": executor,
        "status": status,
        "elapsed_s": round(elapsed_s, 2),
        "task_text": task_text[:256],
        "summary": summary[:512],
        "error": error[:512] if error else "",
        "zodiac_id": zodiac.get("zodiac_id", ""),
        "zodiac": zodiac.get("zodiac", ""),
        "workflow": zodiac.get("workflow", ""),
        "confidence": zodiac.get("confidence", 0),
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    try:
        publish_event("metadata.report", meta, executor)
    except Exception:
        pass

    try:
        import redis as _r
        r = _r.Redis(host="localhost", port=6379, db=0, socket_connect_timeout=2)
        key = f"aios:meta:{task_id}"
        r.hset(key, mapping={k: str(v) if not isinstance(v, (str, bytes)) else v for k, v in meta.items()})
        r.expire(key, 86400)
    except Exception:
        pass

    return meta
