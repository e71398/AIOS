"""
Protocol Validator — 验证AI是否遵守执行规程
==============================================
在关键节点被调用，确保6步流程不遗漏。
"""
import sys
from pathlib import Path
PIPELINE = Path("${AIOS_HOME}/kernel/orchestration/pipeline_enforcer")
sys.path.insert(0, str(PIPELINE))

class ProtocolValidator:
    def validate_task_classification(self, task: dict, classified_type: str) -> bool:
        from task_classifier import TaskClassifier
        c = TaskClassifier()
        actual = c.check_task_type(task)["type"]
        return actual == classified_type

    def validate_pipeline_registration(self, pipeline_id: str) -> bool:
        from pipeline_registry import PipelineRegistry
        r = PipelineRegistry()
        data = r.get_pipeline(pipeline_id)
        return bool(data and data.get("status") not in ("UNKNOWN", ""))

    def validate_execution_authorization(self, sub_task_id: str, agent_id: str) -> bool:
        from pipeline_registry import PipelineRegistry
        r = PipelineRegistry()
        a = r.get_sub_task_assignment(sub_task_id)
        return a.get("agent_id") == agent_id or a.get("agent_id") == "*"

    def validate_result_reporting(self, task_id: str, report: dict) -> bool:
        return bool(report and report.get("status"))

    def run_full_validation(self, task: dict, pipeline_id: str = None) -> dict:
        results = {}
        results["classification"] = self.validate_task_classification(task, "PIPELINE_TASK")
        if pipeline_id:
            results["registration"] = self.validate_pipeline_registration(pipeline_id)
        return {"all_pass": all(results.values()), "details": results}
