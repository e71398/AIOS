"""
Proactivity Score — Agent主动性评分引擎
==========================================
AIOS v4.0 Autonomy & Initiative Center

评分写入 Redis: aios:autonomy:score:{agent_id}:{date}
"""
import os, sys, json
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import _is_available, _redis_client, KEY_PREFIX

SCORE_RULES = {
    "searched_knowledge":    +2,
    "searched_external":     +3,
    "provided_alternatives": +4,
    "detected_risk":         +5,
    "contributed_knowledge": +6,
    "performed_cleanup":     +2,
    "self_reflection":       +3,
    "skipped_search":        -4,
    "repeated_failure":      -5,
    "used_expired_info":     -6,
    "idle_loop":             -3,
}

class ProactivityScoreEngine:
    def __init__(self):
        self.date = datetime.now(timezone.utc).strftime("%Y%m%d")

    def _key(self, agent_id: str) -> str:
        return f"aios:autonomy:score:{agent_id}:{self.date}"

    def score_event(self, agent_id: str, event_type: str) -> int:
        """对事件评分, 返回当前总分."""
        delta = SCORE_RULES.get(event_type, 0)
        if delta == 0 or not _is_available():
            return 0
        try:
            r = _redis_client
            r.hincrby(self._key(agent_id), "total_score", delta)
            r.hincrby(self._key(agent_id), event_type, delta)
            r.expire(self._key(agent_id), 7 * 24 * 3600)
            total = int(r.hget(self._key(agent_id), "total_score") or 0)
            r.zadd(f"aios:autonomy:leaderboard:{self.date}", {agent_id: total})
            return total
        except: return 0

    def apply_bonus(self, agent_id: str, bonus_type: str, value: int):
        self.score_event(agent_id, bonus_type)

    def apply_penalty(self, agent_id: str, penalty_type: str, value: int):
        self.score_event(agent_id, penalty_type)

    def get_daily_score(self, agent_id: str) -> dict:
        if not _is_available(): return {}
        try:
            data = _redis_client.hgetall(self._key(agent_id))
            return {k.decode(): int(v.decode()) for k, v in (data or {}).items()}
        except: return {}

    def get_leaderboard(self) -> list:
        if not _is_available(): return []
        try:
            raw = _redis_client.zrevrange(f"aios:autonomy:leaderboard:{self.date}", 0, 9, withscores=True)
            return [{"agent_id": a.decode(), "score": int(s)} for a, s in raw]
        except: return []

    def reset_daily(self):
        if not _is_available(): return
        try:
            for key in _redis_client.scan_iter(match=f"aios:autonomy:score:*:{self.date}"):
                _redis_client.delete(key)
        except: pass
