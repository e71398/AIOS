"""
Pipeline Registry — 管线注册表
===============================
Redis key: aios:pipeline:*
"""
import json, sys, os
from datetime import datetime, timezone
sys.path.insert(0, '${AIOS_HOME}/kernel/tools')
from aios_bus import _is_available, _redis_client

PREFIX = "aios:pipeline"

class PipelineRegistry:
    def register_pipeline(self, classification: dict) -> str:
        pid = classification.get("pipeline_id", f"pl_{int(datetime.now(timezone.utc).timestamp())}")
        if _is_available():
            _redis_client.hset(f"{PREFIX}:registry:{pid}", mapping={
                "type": classification.get("type","?"),
                "task_id": classification.get("task_id",""),
                "complexity": classification.get("complexity","medium"),
                "status": "INIT",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        return pid

    def register_sub_task(self, pipeline_id: str, sub_task_id: str, agent_id: str,
                          capability: str = "general", level: str = "L2"):
        if not _is_available(): return
        _redis_client.hset(f"{PREFIX}:assignments:{sub_task_id}", mapping={
            "pipeline_id": pipeline_id,
            "agent_id": agent_id,
            "capability_required": capability,
            "execution_level": level,
            "status": "ASSIGNED",
            "assigned_at": datetime.now(timezone.utc).isoformat(),
        })
        _redis_client.hset(f"{PREFIX}:registry:{pipeline_id}", "status", "DISPATCHING")

    def get_pipeline(self, pipeline_id: str) -> dict:
        if not _is_available(): return {}
        data = _redis_client.hgetall(f"{PREFIX}:registry:{pipeline_id}")
        return {k.decode(): v.decode() for k, v in (data or {}).items()}

    def get_sub_task_assignment(self, sub_task_id: str) -> dict:
        if not _is_available(): return {}
        data = _redis_client.hgetall(f"{PREFIX}:assignments:{sub_task_id}")
        return {k.decode(): v.decode() for k, v in (data or {}).items()}

    def list_active(self) -> list:
        if not _is_available(): return []
        keys = _redis_client.keys(f"{PREFIX}:registry:*")
        return [k.decode().split(":")[-1] for k in keys]

    def archive_pipeline(self, pipeline_id: str):
        if not _is_available(): return
        _redis_client.hset(f"{PREFIX}:registry:{pipeline_id}", "status", "ARCHIVED")
        _redis_client.expire(f"{PREFIX}:registry:{pipeline_id}", 7 * 24 * 3600)
