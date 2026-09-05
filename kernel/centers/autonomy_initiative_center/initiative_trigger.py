"""
Initiative Trigger — 三类触发: task/event/schedule
====================================================
AIOS v4.0 Autonomy & Initiative Center
"""
import os, sys, json
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import _is_available, _redis_client, publish_event

TRIGGER_KEY = "aios:autonomy:trigger"

class InitiativeTriggerEngine:
    def on_task_created(self, task: dict) -> dict:
        complexity = task.get("complexity", "low")
        should_curiosity = complexity in ("medium", "high")
        should_divergence = complexity in ("medium", "high")
        return {
            "triggered": should_curiosity or should_divergence,
            "curiosity": should_curiosity,
            "divergence": should_divergence,
            "task_id": task.get("task_id", task.get("task_name","?")[:30]),
        }

    def on_task_failed(self, task: dict, error: dict) -> dict:
        return {
            "triggered": True,
            "action": "analyze_failure",
            "task_id": task.get("task_id", "?"),
            "error_summary": str(error)[:100],
            "retry_recommended": True,
        }

    def on_knowledge_gap_detected(self, signal: dict) -> dict:
        return {
            "triggered": True,
            "action": "fill_knowledge_gap",
            "gap_topic": signal.get("topic", signal.get("task_name","?")),
            "priority": "medium",
        }

    def on_system_idle(self) -> dict:
        return {
            "triggered": True,
            "action": "run_background_jobs",
            "jobs": ["curiosity_scan", "cleanup", "score_rollup"],
        }

    def on_scheduled_tick(self, schedule_name: str) -> dict:
        mapping = {
            "daily_curiosity": {"action": "curiosity_scan", "scope": "24h"},
            "daily_cleanup": {"action": "cleanup", "mode": "archive"},
            "daily_score": {"action": "score_rollup"},
            "weekly_review": {"action": "improvement_review", "scope": "7d"},
        }
        return {"triggered": True, "schedule": schedule_name, **mapping.get(schedule_name, {"action": "unknown"})}

    def should_trigger_curiosity(self, task: dict) -> bool:
        return task.get("complexity", "low") in ("medium", "high")

    def should_trigger_divergence(self, task: dict) -> bool:
        return task.get("complexity", "low") in ("medium", "high")

    def build_trigger_decision(self, context: dict) -> dict:
        decisions = []
        if context.get("new_task"):
            decisions.append(self.on_task_created(context["new_task"]))
        if context.get("task_failed"):
            decisions.append(self.on_task_failed(context["task"], context.get("error", {})))
        if context.get("knowledge_gap"):
            decisions.append(self.on_knowledge_gap_detected(context["knowledge_gap"]))
        if context.get("system_idle"):
            decisions.append(self.on_system_idle())
        return {"decisions": decisions, "total_triggers": len(decisions)}

    def log_trigger(self, trigger_type: str, payload: dict):
        if not _is_available(): return
        try:
            _redis_client.xadd(f"{TRIGGER_KEY}:event_log", {"type": trigger_type, "payload": json.dumps(payload, ensure_ascii=False)})
        except: pass
