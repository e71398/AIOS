"""
Task Classifier — 任务分类与打标
================================
判断任务是否必须走管线，拆解子任务。
"""
import re

class TaskClassifier:
    PIPELINE_KW = ["并行","多个","协作","各自","分工","一起","parallel","concurrent","multi-agent"]

    def check_task_type(self, task: dict) -> dict:
        task_text = task.get("task_name", task.get("task", ""))
        task_id = task.get("task_id", "")
        subs = self.extract_sub_tasks(task)
        complexity = self.analyze_complexity(task)
        needs_multi = self.detect_multi_agent_requirement(task)
        is_pipeline = needs_multi or len(subs) > 1 or any(kw in task_text for kw in self.PIPELINE_KW)
        return {
            "task_id": task_id,
            "type": "PIPELINE_TASK" if is_pipeline else "DIRECT_TASK",
            "complexity": complexity,
            "requires_multi_agent": needs_multi,
            "sub_tasks": [{
                "id": f"sub_{i:03d}",
                "capability_needed": self._infer_capability(st),
                "assigned_agent": None,
                "level": self.assign_execution_level(task)
            } for i, st in enumerate(subs)],
            "pipeline_id": f"pl_{task_id[:8]}" if is_pipeline else None,
        }

    def analyze_complexity(self, task: dict) -> str:
        text = (task.get("task_name") or task.get("task") or "").lower()
        if any(k in text for k in ["架构","重构","安全","审计","critical"]): return "critical"
        if any(k in text for k in ["复杂","多个","并行","分析报告","optimize"]): return "high"
        if any(k in text for k in ["检查","扫描","统计","查询","list","check"]): return "low"
        return "medium"

    def detect_multi_agent_requirement(self, task: dict) -> bool:
        text = (task.get("task_name") or task.get("task") or "")
        return any(kw in text for kw in self.PIPELINE_KW)

    def extract_sub_tasks(self, task: dict) -> list:
        text = (task.get("task_name") or task.get("task") or "")
        parts = re.split(r'[；;。\n]|然后|同时|并且|分别|各自', text)
        return [p.strip() for p in parts if len(p.strip()) > 5] or [text.strip()]

    def assign_execution_level(self, task: dict) -> str:
        c = self.analyze_complexity(task)
        return {"low":"L1","medium":"L2","high":"L3","critical":"L4"}.get(c, "L2")

    def _infer_capability(self, text: str) -> str:
        t = text.lower()
        if any(k in t for k in ["代码","编程","code","脚本"]): return "coding"
        if any(k in t for k in ["shell","bash","扫描","检查","scan","check","ps","pgrep","curl"]): return "shell"
        if any(k in t for k in ["分析","日志","报告","analyze","report"]): return "analysis"
        return "general"
