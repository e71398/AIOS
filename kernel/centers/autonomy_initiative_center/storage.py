"""Storage — 统一数据存取 (Redis)."""
import json
from aios_bus import _is_available, _redis_client, KEY_PREFIX

PREFIX = "aios:autonomy"

class AutonomyStorage:
    def save_run(self, data: dict) -> str:
        if not _is_available(): return ""
        rid = data.get("task_id", "run")
        _redis_client.hset(f"{PREFIX}:run:{rid}", mapping={k: str(v) for k, v in data.items() if v})
        return rid

    def save_recommendation(self, data: dict) -> str:
        if not _is_available(): return ""
        rid = data.get("title", "rec")
        _redis_client.hset(f"{PREFIX}:rec:{rid}", mapping={k: str(v) for k, v in data.items() if v})
        return rid

    def save_score(self, data: dict) -> str:
        if not _is_available(): return ""
        agent = data.get("agent_id", "?")
        date = data.get("date", "today")
        _redis_client.hset(f"{PREFIX}:score:{agent}:{date}", mapping={k: str(v) for k, v in data.items() if v})
        return f"{agent}:{date}"

    def fetch_recent_runs(self, days: int = 7) -> list:
        return []

    def fetch_scores(self, date: str) -> list:
        return []

    def save_cleanup_stats(self, stats: dict) -> bool:
        if not _is_available(): return False
        _redis_client.hset(f"{PREFIX}:cleanup:stats", mapping={k: str(v) for k, v in stats.items() if v})
        return True
