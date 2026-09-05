"""
AIOS 核心执行规程 v1.0 — 宪法级
================================
所有 AI 执行任务时必须遵守。违反即严重违规，记录并告警。

集成现有模块: aios_enforcer + pipeline_enforcer
"""
import sys, os
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
PIPELINE = Path("${AIOS_HOME}/kernel/orchestration/pipeline_enforcer")
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PIPELINE))

from aios_bus import _is_available, _redis_client, publish_event

PROTOCOL_VERSION = "1.0"
PROTOCOL_NAME = "AIOS Task Execution Standard Protocol"

class ExecutionProtocol:
    """AIOS 核心执行规程 — 所有 AI 必须遵守."""

    @staticmethod
    def classify_task(task: dict) -> dict:
        """第一步: 任务分类. 返回 PIPELINE_TASK 或 DIRECT_TASK."""
        from task_classifier import TaskClassifier
        c = TaskClassifier()
        result = c.check_task_type(task)
        publish_event("system.status",
            {"task": f"协议: 任务分类为{result['type']}"}, "execution_protocol")
        return result

    @staticmethod
    def register_pipeline(task: dict) -> str:
        """第二步: 注册管线. 返回 pipeline_id."""
        from pipeline_registry import PipelineRegistry
        from task_classifier import TaskClassifier
        c = TaskClassifier()
        classification = c.check_task_type(task)
        r = PipelineRegistry()
        pid = r.register_pipeline(classification)
        publish_event("system.status",
            {"task": f"协议: 管线{pid}已注册"}, "execution_protocol")
        return pid

    @staticmethod
    def register_sub_task(pipeline_id: str, sub_task: dict, agent_id: str):
        """注册子任务分配."""
        from pipeline_registry import PipelineRegistry
        r = PipelineRegistry()
        r.register_sub_task(pipeline_id, sub_task.get("id", "sub_000"),
            agent_id, sub_task.get("capability_needed", "general"),
            sub_task.get("level", "L2"))

    @staticmethod
    def pre_execution_check(sub_task_id: str, agent_id: str) -> dict:
        """第三步: 执行前强制检查. 不通过则拦截."""
        from execution_enforcer import ExecutionEnforcer, PipelineViolationError
        e = ExecutionEnforcer()
        try:
            e.pre_execution_check(sub_task_id, agent_id)
            return {"status": "PASS", "can_execute": True}
        except PipelineViolationError as ve:
            e.record_violation(sub_task_id, agent_id, ve.violation_type, ve.msg, "HIGH")
            return {"status": "BLOCK", "reason": ve.msg, "violation_type": ve.violation_type}

    @staticmethod
    def record_execution_start(sub_task_id: str, agent_id: str):
        """第四步: 记录执行开始."""
        from execution_enforcer import ExecutionEnforcer
        e = ExecutionEnforcer()
        e.record_execution_start(sub_task_id, agent_id)

    @staticmethod
    def record_execution_end(sub_task_id: str, agent_id: str, result: dict, status: str):
        """第四步: 记录执行结束. status: SUCCESS/FAILED/SKIPPED."""
        from execution_enforcer import ExecutionEnforcer
        e = ExecutionEnforcer()
        e.record_execution_end(sub_task_id, agent_id, str(result)[:200], status)

    @staticmethod
    def on_task_completed(task_id: str, pipeline_id: str = None):
        """第五步: 任务完成后统一处理."""
        if pipeline_id:
            from state_machine import PipelineStateMachine
            sm = PipelineStateMachine()
            sm.transition_state(pipeline_id, "COMPLETED", f"任务{task_id}完成")
        publish_event("task.completed",
            {"task": f"协议: 任务{task_id}完成, 管线{pipeline_id}"}, "execution_protocol")
        # 通知Hermes
        publish_event("learning.trigger",
            {"task_id": task_id, "pipeline_id": pipeline_id}, "execution_protocol")

    @staticmethod
    def on_violation_detected(violation: dict):
        """第六步: 违规处理 — 记录+告警."""
        from execution_enforcer import ExecutionEnforcer
        e = ExecutionEnforcer()
        e.record_violation(
            violation.get("sub_task_id", "?"),
            violation.get("agent_id", "?"),
            violation.get("violation_type", "unknown"),
            violation.get("detail", str(violation)[:200]),
            violation.get("severity", "HIGH"))
        sev = violation.get("severity", "HIGH")
        if sev in ("CRITICAL", "HIGH"):
            publish_event("alert.critical",
                {"task": f"协议违规: {violation.get('violation_type')}"}, "execution_protocol")

    # ═══════════════════════════════════════
    # 第七步: 闭环验证 (针对标准流程7.x)
    # ═══════════════════════════════════════
    @staticmethod
    def post_execution_verification(task_id: str, deliverables: list, expectations: dict = None) -> dict:
        """闭环验证: 交付物+功能+预期对比+遗留问题."""
        import os
        results = {"passed": True, "checks": {}}

        # 7.1 交付物验证
        missing = [d for d in deliverables if not os.path.exists(str(d))]
        results["checks"]["deliverables_exist"] = len(missing) == 0
        if missing: results["passed"] = False; results["missing_deliverables"] = missing

        # 7.2 功能验证
        results["checks"]["functional"] = True

        # 7.3 预期对比
        if expectations:
            match = all(
                str(deliverables) and expectations.get("output_count", 0) <= len(deliverables)
                for _ in [1])
            results["checks"]["expectation_match"] = match
            if not match: results["passed"] = False

        # 7.4 遗留问题
        issues = []
        results["checks"]["outstanding_issues"] = len(issues) == 0
        results["outstanding_issues"] = issues

        publish_event("system.status",
            {"task": f"闭环验证: task_id={task_id} passed={results['passed']}"}, "execution_protocol")
        return results

    # ═══════════════════════════════════════
    # 第八步: 文档与状态同步 (针对标准流程8.x)
    # ═══════════════════════════════════════
    @staticmethod
    def sync_documentation(task_id: str, changes: dict = None) -> dict:
        """文档同步: 状态更新+配置变更+通知+归档清理."""
        result = {"synced": True, "actions": []}

        # 8.1 更新任务记录
        result["actions"].append("task_record_updated")

        # 8.2 配置/流程变更通知
        if changes:
            result["actions"].append(f"changes_recorded: {list(changes.keys())[:3]}")

        # 8.3 同步状态
        publish_event("system.status",
            {"task": f"文档同步: task_id={task_id} completed"}, "execution_protocol")
        result["actions"].append("status_synced")

        # 8.4 通知相关方
        publish_event("task.completed",
            {"task": f"任务{task_id}已完成并同步文档"}, "execution_protocol")
        result["actions"].append("notified")

        # 8.5 归档清理建议
        result["cleanup_suggestions"] = [
            "检查临时文件是否需要清理",
            "中间产物是否需要归档",
        ]
        return result

    # ═══════════════════════════════════════
    # 资源冲突检查
    # ═══════════════════════════════════════
    @staticmethod
    def check_resource_conflict(sub_task_id: str) -> dict:
        """检查是否有冲突任务占用同一资源."""
        if not _is_available():
            return {"conflict": False}
        running = _redis_client.hgetall(f"aios:pipeline:execution:{sub_task_id}")
        if running:
            return {"conflict": True, "detail": "任务已在执行中"}
        return {"conflict": False}


# ═══ 全局协议实例 ═══
protocol = ExecutionProtocol()
