"""
Divergence Engine — 发散式解题引擎 (A最快/B最稳/C最低成本)
============================================================
AIOS v4.0 Autonomy & Initiative Center
"""
import json

class DivergenceEngine:
    """对中高复杂任务自动生成3套方案."""

    def generate_fast_path(self, task: dict) -> dict:
        """A方案: 最快路径."""
        return {
            "id": "A", "type": "fast", "label": "最快路径",
            "approach": "直接执行, 跳过冗余检查",
            "risk": "medium", "cost": "low",
            "estimated_steps": 1,
            "description": f"直接调用最合适的执行器处理: {task.get('task_name', task.get('task',''))[:40]}",
        }

    def generate_safe_path(self, task: dict) -> dict:
        """B方案: 最稳路径."""
        return {
            "id": "B", "type": "safe", "label": "最稳路径",
            "approach": "先验证→再执行→后校验",
            "risk": "low", "cost": "high",
            "estimated_steps": 3,
            "description": f"通过Protocol Check→World Model→Execute→Verify管线: {task.get('task_name', task.get('task',''))[:40]}",
        }

    def generate_low_cost_path(self, task: dict) -> dict:
        """C方案: 最低成本."""
        return {
            "id": "C", "type": "low_cost", "label": "最低成本",
            "approach": "使用免费模型/缓存结果/复用历史",
            "risk": "medium", "cost": "low",
            "estimated_steps": 2,
            "description": f"使用openCode免费通道 + 缓存复用: {task.get('task_name', task.get('task',''))[:40]}",
        }

    def compare_solutions(self, solutions: list) -> dict:
        """方案对比."""
        if not solutions:
            return {"solutions": [], "recommended": None}
        # 推荐: 中等复杂度→B安全, 低复杂度→A快, 高复杂度→B安全
        return {
            "solutions": solutions,
            "recommended": "B",
            "recommendation_reason": "默认推荐最稳路径, 确保执行安全",
        }

    def choose_recommended_path(self, comparison: dict) -> dict:
        return next((s for s in comparison.get("solutions", []) if s["id"] == comparison.get("recommended")), None) or {}

    def brainstorm_alternatives(self, task: dict, count: int = 3) -> list:
        solutions = [self.generate_fast_path(task), self.generate_safe_path(task), self.generate_low_cost_path(task)]
        return solutions[:count]
