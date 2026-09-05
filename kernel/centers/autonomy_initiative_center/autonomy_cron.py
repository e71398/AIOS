"""
Autonomy Cron — 定时调度入口
=============================
AIOS v4.0 Autonomy & Initiative Center
"""
import os, sys, json
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from autonomy_center import AutonomyCenter
from cleanup_decay_engine import CleanupDecayEngine
from self_improvement_engine import SelfImprovementEngine

class AutonomyCron:
    def __init__(self):
        self.center = AutonomyCenter()
        self.cleanup = CleanupDecayEngine()
        self.improvement = SelfImprovementEngine()

    def run_daily_curiosity_scan(self) -> dict:
        return {"action": "curiosity_scan", "status": "completed"}

    def run_daily_cleanup(self) -> dict:
        return self.cleanup.run_full_cleanup()

    def run_daily_score_rollup(self) -> dict:
        return self.center.summarize_daily_autonomy()

    def run_weekly_improvement_review(self) -> dict:
        logs = self.improvement.load_recent_execution_logs(7)
        patterns = {
            "success": self.improvement.extract_success_patterns(logs),
            "failure": self.improvement.extract_failure_patterns(logs),
        }
        recs = self.improvement.generate_improvement_recommendations(logs)
        sops = self.improvement.generate_sop_candidates(logs)
        self.improvement.write_to_knowledge_center(recs + sops)
        return {"action": "weekly_review", "patterns": patterns, "recommendations": len(recs), "sops": len(sops)}

    def run_idle_time_background_jobs(self) -> dict:
        return {"cleanup": self.run_daily_cleanup(), "leaderboard": self.center.summarize_daily_autonomy()}
