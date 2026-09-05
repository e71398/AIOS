"""
Pipeline Center — 统一入口，整合所有子模块
============================================
AIOS v4.0 管线强制系统
"""
import sys
from pathlib import Path
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from task_classifier import TaskClassifier
from pipeline_registry import PipelineRegistry
from execution_enforcer import ExecutionEnforcer, PipelineViolationError
from state_machine import PipelineStateMachine
from violation_detector import ViolationDetector
from violation_detector import RollbackEngine, PipelineMonitor, ComplianceReporter, PipelineEventHandler, PipelineIntegration

class PipelineCenter:
    def __init__(self):
        self.classifier = TaskClassifier()
        self.registry = PipelineRegistry()
        self.enforcer = ExecutionEnforcer()
        self.state = PipelineStateMachine()
        self.violation = ViolationDetector()
        self.rollback = RollbackEngine()
        self.monitor = PipelineMonitor()
        self.compliance = ComplianceReporter()
        self.events = PipelineEventHandler()
        self.integration = PipelineIntegration()

    def receive_task(self, task: dict) -> dict:
        """接收任务，分类+注册."""
        classification = self.classifier.check_task_type(task)
        if classification["type"] == "PIPELINE_TASK":
            pid = self.registry.register_pipeline(classification)
            self.state.transition_state(pid, "REGISTERED", "任务分类完成")
            self.events.publish("PipelineCreated", {"pipeline_id": pid})
            return {"pipeline_id": pid, "status": "REGISTERED", "sub_tasks": len(classification["sub_tasks"])}
        return {"status": "DIRECT_TASK", "can_execute": True}

    def dispatch_sub_task(self, pipeline_id: str, sub_task_id: str, agent_id: str,
                          capability: str = "general", level: str = "L2") -> dict:
        state = self.state.get_state(pipeline_id)
        if state not in ("REGISTERED", "DISPATCHING"):
            raise PipelineViolationError(f"无法在状态 {state} 下分发")
        self.registry.register_sub_task(pipeline_id, sub_task_id, agent_id, capability, level)
        self.state.transition_state(pipeline_id, "DISPATCHING", f"分发{sub_task_id}给{agent_id}")
        self.events.publish("SubTaskAssigned", {"sub_task_id": sub_task_id, "agent_id": agent_id})
        return {"status": "ASSIGNED", "agent_id": agent_id}

    def execute_sub_task(self, sub_task_id: str, agent_id: str) -> dict:
        """执行子任务 — 强制检查授权."""
        self.enforcer.pre_execution_check(sub_task_id, agent_id)
        v = self.violation.detect_unauthorized(agent_id, sub_task_id)
        if v:
            self.events.handle_violation(v)
            raise PipelineViolationError(f"违规: {v['type']}")
        self.enforcer.record_execution_start(sub_task_id, agent_id)
        self.events.publish("ExecutionStarted", {"sub_task_id": sub_task_id})
        return {"status": "AUTHORIZED", "can_execute": True}

    def complete_sub_task(self, sub_task_id: str, agent_id: str, result: str, success: bool = True):
        self.enforcer.record_execution_end(sub_task_id, agent_id, result,
            "SUCCESS" if success else "FAILED")
        self.events.publish("ExecutionCompleted", {"sub_task_id": sub_task_id, "success": success})

    def get_status(self, pipeline_id: str) -> dict:
        return self.monitor.get_status(pipeline_id)
