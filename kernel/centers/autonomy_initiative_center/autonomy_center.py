"""
Autonomy Center — 主动性与能动中心 主入口
===========================================
AIOS v4.0

串联: curiosity_engine → divergence_engine → proactivity_score
"""
import os, sys, json
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path("${AIOS_HOME}/kernel/tools")))

from autonomy_policy import AutonomyPolicy
from curiosity_engine import CuriosityEngine
from divergence_engine import DivergenceEngine
from proactivity_score import ProactivityScoreEngine
from aios_bus import publish_event, _is_available

class AutonomyCenter:
    def __init__(self):
        self.policy = AutonomyPolicy()
        self.curiosity = CuriosityEngine()
        self.divergence = DivergenceEngine()
        self.scorer = ProactivityScoreEngine()

    def check_and_plan(self, task: dict) -> dict:
        """执行前: 检查自治等级 → 收集参考 → 生成方案."""
        level = self.policy.get_autonomy_level(task)
        result = {"autonomy_level": level, "reference_pack": None, "solutions": None}

        if self.policy.should_trigger_curiosity(task):
            ref = self.curiosity.collect_minimum_reference_pack(task)
            result["reference_pack"] = ref
            publish_event("task.dispatched", {"task": f"Curiosity: 收集{ref.get('total_refs',0)}条参考"}, "autonomy_center")

        if self.policy.should_trigger_divergence(task):
            solutions = self.divergence.brainstorm_alternatives(task)
            result["solutions"] = self.divergence.compare_solutions(solutions)
            publish_event("task.dispatched", {"task": f"Divergence: 生成{len(solutions)}个方案"}, "autonomy_center")

        return result

    def pre_execution_check(self, task: dict) -> dict:
        """执行前门禁."""
        return self.check_and_plan(task)

    def post_execution_report(self, task: dict, result: dict, agent_id: str = "unknown") -> dict:
        """执行后: 评分 + 复盘."""
        success = result.get("success", result.get("ok", False))
        if success:
            self.scorer.score_event(agent_id, "searched_knowledge")
        else:
            self.scorer.score_event(agent_id, "repeated_failure")
        return {"agent_id": agent_id, "scored": True}

    def trigger_background_jobs(self) -> dict:
        """定时后台任务."""
        jobs = {}
        try:
            leaderboard = self.scorer.get_leaderboard()
            jobs["leaderboard"] = leaderboard
            publish_event("system.status", {"status": f"Autonomy: 排行榜{len(leaderboard)}人"}, "autonomy_center")
        except: pass
        return jobs

    def summarize_daily_autonomy(self, date: str = None) -> dict:
        """每日总结."""
        date = date or datetime.now(timezone.utc).strftime("%Y%m%d")
        leaderboard = self.scorer.get_leaderboard()
        return {
            "date": date,
            "leaderboard": leaderboard,
            "active_agents": len(leaderboard),
            "total_score": sum(s["score"] for s in leaderboard),
        }
