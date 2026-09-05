"""Violation Detector + Rollback Engine + Monitor + Reporter + EventHandler + Integration"""
import sys
sys.path.insert(0, '${AIOS_HOME}/kernel/tools')
from aios_bus import _redis_client, _is_available, publish_event
from datetime import datetime, timezone

PREFIX = "aios:pipeline"

# ═══ Violation Detector ═══
class ViolationDetector:
    def detect_unauthorized(self, agent_id: str, sub_task_id: str) -> dict:
        a = _redis_client.hgetall(f"{PREFIX}:assignments:{sub_task_id}") if _is_available() else {}
        assigned = a.get(b"agent_id", b"").decode() if a else ""
        if assigned and assigned != agent_id and assigned != "*":
            return {"type": "unauthorized_execution", "agent": agent_id, "assigned": assigned, "severity": "HIGH"}
        return None

    def detect_repeated_failure(self, sub_task_id: str) -> dict:
        if not _is_available(): return None
        count = int(_redis_client.hget(f"{PREFIX}:execution:{sub_task_id}", "fail_count") or 0)
        if count >= 3:
            return {"type": "repeated_failure", "count": count, "severity": "MEDIUM"}
        return None

# ═══ Rollback Engine ═══
class RollbackEngine:
    def save_checkpoint(self, pipeline_id: str, data: dict):
        if _is_available():
            _redis_client.hset(f"{PREFIX}:checkpoint:{pipeline_id}", mapping={k:str(v) for k,v in data.items()})

    def rollback(self, pipeline_id: str):
        if _is_available():
            _redis_client.hset(f"{PREFIX}:registry:{pipeline_id}", "status", "ROLLING_BACK")

# ═══ Monitor ═══
class PipelineMonitor:
    def get_status(self, pipeline_id: str) -> dict:
        if not _is_available(): return {}
        d = _redis_client.hgetall(f"{PREFIX}:registry:{pipeline_id}")
        return {k.decode(): v.decode() for k,v in (d or {}).items()}

    def list_all(self) -> list:
        if not _is_available(): return []
        return [k.decode().split(":")[-1] for k in _redis_client.keys(f"{PREFIX}:registry:*")]

# ═══ Compliance Reporter ═══
class ComplianceReporter:
    def daily_report(self, date: str = None) -> dict:
        date = date or datetime.now(timezone.utc).strftime("%Y%m%d")
        pipes = PipelineMonitor().list_all()
        violations = 0
        for p in pipes:
            d = _redis_client.hgetall(f"{PREFIX}:registry:{p}") if _is_available() else {}
            if d.get(b"status", b"").decode() in ("BLOCKED","FAILED"):
                violations += 1
        return {"date": date, "total_pipelines": len(pipes), "violations": violations}

# ═══ Event Handler ═══
class PipelineEventHandler:
    EVENTS = ["PipelineCreated","SubTaskAssigned","ExecutionStarted","ExecutionCompleted","ViolationDetected","PipelineCompleted"]

    def publish(self, event_type: str, payload: dict):
        if event_type in self.EVENTS:
            publish_event("system.status",
                {"task": f"{event_type}: {str(payload)[:80]}", **payload},
                "pipeline_enforcer")

    def handle_violation(self, violation: dict):
        if _is_available():
            _redis_client.xadd(f"{PREFIX}:violation_log",
                {"type": violation.get("type","?"), "detail": str(violation)[:200],
                 "ts": datetime.now(timezone.utc).isoformat()})
        publish_event("alert.critical",
            {"task": f"管线违规: {violation.get('type','?')}"}, "pipeline_enforcer")

# ═══ Integration ═══
class PipelineIntegration:
    def integrate_with_dispatcher(self, task: dict) -> dict:
        from task_classifier import TaskClassifier
        from pipeline_registry import PipelineRegistry
        c = TaskClassifier()
        r = PipelineRegistry()
        classification = c.check_task_type(task)
        if classification["type"] == "PIPELINE_TASK":
            pid = r.register_pipeline(classification)
            return {"pipeline_id": pid, "status": "REGISTERED", "classification": classification}
        return {"status": "DIRECT_TASK"}

    def integrate_with_hermes(self, pipeline_id: str):
        publish_event("learning.trigger",
            {"pipeline_id": pipeline_id, "action": "record_execution"},
            "pipeline_enforcer")
