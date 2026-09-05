"""
Execution Enforcer — 执行强制器 (核心)
=======================================
确保只有被分配的AI才能执行任务，拦截违规。
"""
import sys, os
from datetime import datetime, timezone
sys.path.insert(0, '${AIOS_HOME}/kernel/tools')
from aios_bus import _redis_client, _is_available, publish_event

PREFIX = "aios:pipeline"

class PipelineViolationError(Exception):
    def __init__(self, msg, violation_type="unauthorized_execution"):
        self.msg = msg
        self.violation_type = violation_type
        super().__init__(msg)

class ExecutionEnforcer:
    def pre_execution_check(self, sub_task_id: str, agent_id: str):
        """执行前强制检查 — 这行代码是防跑偏的核心."""
        if not _is_available():
            return True
        assignment = _redis_client.hgetall(f"{PREFIX}:assignments:{sub_task_id}")
        if not assignment:
            raise PipelineViolationError(f"子任务 {sub_task_id} 未在管线注册表中", "unregistered_task")
        a = {k.decode(): v.decode() for k, v in assignment.items()}
        if a.get("status") in ("COMPLETED", "CANCELLED"):
            raise PipelineViolationError(f"子任务 {sub_task_id} 已完成/取消", "already_done")
        assigned = a.get("agent_id", "")
        if assigned != agent_id and assigned != "*":
            raise PipelineViolationError(
                f"❌ Agent {agent_id} 无权执行子任务 {sub_task_id}。授权执行者: {assigned}。管线强制拦截。",
                "unauthorized_execution")
        if a.get("execution_level") == "L4":
            if not _redis_client.exists(f"{PREFIX}:approval:{sub_task_id}"):
                raise PipelineViolationError(f"L4任务 {sub_task_id} 需要审批", "l4_approval_required")
        return True

    def authorize_execution(self, sub_task_id: str, agent_id: str):
        if not _is_available(): return
        _redis_client.setex(f"{PREFIX}:auth:{sub_task_id}:{agent_id}", 3600,
            datetime.now(timezone.utc).isoformat())

    def record_execution_start(self, sub_task_id: str, agent_id: str):
        if not _is_available(): return
        now = datetime.now(timezone.utc).isoformat()
        _redis_client.hset(f"{PREFIX}:execution:{sub_task_id}", mapping={
            "agent_id": agent_id, "status": "RUNNING", "started_at": now})
        _redis_client.hset(f"{PREFIX}:assignments:{sub_task_id}", "status", "RUNNING")

    def record_execution_end(self, sub_task_id: str, agent_id: str, result: str, status: str):
        if not _is_available(): return
        _redis_client.hset(f"{PREFIX}:execution:{sub_task_id}", mapping={
            "status": status, "completed_at": datetime.now(timezone.utc).isoformat(),
            "result_summary": result[:200]})
        _redis_client.hset(f"{PREFIX}:assignments:{sub_task_id}", "status", status)
        publish_event("task.completed" if status == "SUCCESS" else "task.failed",
            {"task": f"pipeline sub_task {sub_task_id}", "status": status}, agent_id)

    def record_violation(self, sub_task_id: str, agent_id: str, violation_type: str, detail: str, severity: str = "HIGH"):
        if not _is_available(): return
        ev = {"sub_task_id": sub_task_id, "agent_id": agent_id,
              "violation_type": violation_type, "detail": detail, "severity": severity,
              "ts": datetime.now(timezone.utc).isoformat()}
        _redis_client.xadd(f"{PREFIX}:violation_log", ev)
        publish_event("alert.critical" if severity in ("CRITICAL","HIGH") else "alert.warning",
            {"task": f"管线违规: {violation_type}", "detail": detail}, "pipeline_enforcer")
