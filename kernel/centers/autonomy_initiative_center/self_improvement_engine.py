"""
Self Improvement Engine — 从执行日志提炼模式
=============================================
AIOS v4.0 Autonomy & Initiative Center
"""
import os, sys, json
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import check_recent, _is_available, publish_event
from aios_semantic_search import index_document

class SelfImprovementEngine:
    def load_recent_execution_logs(self, days: int = 7) -> list:
        if not _is_available(): return []
        return check_recent(hours=days * 24, limit=200)

    def extract_success_patterns(self, logs: list) -> list:
        completed = [r for r in logs if r.get("status") == "completed"]
        task_counter = Counter(r.get("task_name", "")[:60] for r in completed)
        patterns = []
        for task_name, count in task_counter.most_common(10):
            if count >= 2:
                patterns.append({"pattern": task_name, "frequency": count, "type": "success", "confidence": min(0.95, 0.5 + count * 0.1)})
        return patterns

    def extract_failure_patterns(self, logs: list) -> list:
        failed = [r for r in logs if r.get("status") == "failed"]
        patterns = []
        error_words = Counter()
        for r in failed:
            summary = r.get("summary", "").lower()
            for kw in ["timeout","超时","refused","error","fail","memory","disk","lock","权限"]:
                if kw in summary: error_words[kw] += 1
        for kw, count in error_words.most_common(5):
            patterns.append({"pattern": kw, "frequency": count, "type": "failure"})
        return patterns

    def detect_capability_gaps(self, logs: list) -> list:
        gaps = []
        failed = [r for r in logs if r.get("status") == "failed"]
        if len(failed) > 5:
            gaps.append({"gap": "high_failure_rate", "detail": f"失败{len(failed)}次", "severity": "warning"})
        completed = [r for r in logs if r.get("status") == "completed"]
        if len(completed) < 10:
            gaps.append({"gap": "low_throughput", "detail": f"仅{len(completed)}次完成", "severity": "info"})
        return gaps

    def generate_sop_candidates(self, logs: list) -> list:
        patterns = self.extract_success_patterns(logs)
        return [{"sop": p["pattern"], "repeat_count": p["frequency"], "status": "candidate"} for p in patterns[:5]]

    def generate_improvement_recommendations(self, logs: list) -> list:
        recs = []
        failures = self.extract_failure_patterns(logs)
        for f in failures:
            recs.append({"category": "failure_fix", "recommendation": f"重点排查 '{f['pattern']}' (出现{f['frequency']}次)"})
        gaps = self.detect_capability_gaps(logs)
        for g in gaps:
            recs.append({"category": "capability_gap", "recommendation": g["detail"]})
        return recs

    def write_to_knowledge_center(self, insights: list) -> int:
        count = 0
        for ins in insights:
            try:
                title = ins.get("sop", ins.get("recommendation", "insight"))[:80]
                content = json.dumps(ins, ensure_ascii=False)
                index_document("autonomy:improvement", title, content)
                count += 1
            except: pass
        publish_event("system.status", {"status": f"SelfImprovement: 写入{count}条洞察"}, "self_improvement")
        return count
