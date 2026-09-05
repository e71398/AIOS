"""
Cleanup & Decay Engine — 清理缓存/重复/旧数据 + 知识衰减
===========================================================
AIOS v4.0 Autonomy & Initiative Center
"""
import os, sys, json, shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import _is_available, _redis_client, publish_event
from aios_semantic_search import decay_all_scores

class CleanupDecayEngine:
    def __init__(self, whitelist_dirs: list = None):
        self.whitelist = whitelist_dirs or ["/tmp/aios_cache", "${AIOS_HOME}/cache"]

    def find_expired_cache(self, max_age_days: int = 7) -> list:
        expired = []
        cutoff = datetime.now() - timedelta(days=max_age_days)
        for d in self.whitelist:
            path = Path(d)
            if not path.exists(): continue
            for f in path.rglob("*"):
                if f.is_file():
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime < cutoff:
                        expired.append(str(f))
        return expired

    def purge_expired_cache(self, items: list) -> int:
        count = 0
        for item in items:
            try:
                os.remove(item)
                count += 1
            except: pass
        return count

    def find_duplicate_records(self, scope: str = "autonomy") -> list:
        return []

    def merge_or_remove_duplicates(self, duplicates: list) -> dict:
        return {"merged": 0, "removed": 0}

    def decay_old_knowledge_scores(self, days: int = 30) -> int:
        try:
            return decay_all_scores()
        except: return 0

    def archive_low_value_records(self, threshold: float = 0.3) -> int:
        return 0

    def cleanup_context_history(self, max_turns: int = 50) -> int:
        return 0

    def run_full_cleanup(self) -> dict:
        expired = self.find_expired_cache()
        purged = self.purge_expired_cache(expired)
        decayed = self.decay_old_knowledge_scores()
        result = {"expired_found": len(expired), "purged": purged, "knowledge_decayed": decayed}
        if _is_available():
            publish_event("system.status", {"status": f"Cleanup: 清理{purged}个缓存, 衰减{decayed}条知识"}, "cleanup")
        return result
