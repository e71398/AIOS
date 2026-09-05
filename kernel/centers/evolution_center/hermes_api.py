"""Hermes API — 其他中心/AI调用的统一接口"""
import sys
sys.path.insert(0, '${AIOS_HOME}/kernel/centers/evolution_center')
from hermes_monitor import HermesMonitor

class HermesAPI:
    def __init__(self):
        self.monitor = HermesMonitor()

    def task_completed(self, task_id: str, agent_id: str, task_type: str = "unknown",
                       pre_check_passed: bool = True, solution_count: int = 1,
                       result_success: bool = True):
        self.monitor.on_event("task_completed", {
            "task_id": task_id, "agent_id": agent_id, "task_type": task_type,
            "pre_check_passed": pre_check_passed, "solution_count": solution_count,
            "result": {"success": result_success}})
        return {"status":"recorded","agent":agent_id}

    def violation(self, violation_type: str, agent_id: str, task_id: str, details: str = ""):
        self.monitor.on_event("violation_detected",
            {"type": violation_type, "agent_id": agent_id, "task_id": task_id, "details": details})
        return {"status":"recorded"}

    def get_report(self):
        return self.monitor.get_compliance_report()

    def get_agent(self, agent_id: str):
        return self.monitor.get_agent_compliance(agent_id)

    def register_agent(self, agent_id: str, agent_type: str = "ai"):
        self.monitor.register_agent(agent_id, agent_type)
        return {"status":"registered","agent":agent_id}

    def status(self):
        return {"module":"HermesMonitor","status":"active",
                "agents":self.monitor.get_registered_agents()}
