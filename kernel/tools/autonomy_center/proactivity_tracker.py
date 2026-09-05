#!/usr/bin/env python3
"""
Proactivity Tracker — 自主性评分+奖惩+排行榜
=============================================
分数每日重置(crontab), 实时写Redis aios:autonomy:score:{agent}
"""
import sys, os, json, time
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import check_recent, lifecycle_list_all, publish_event, _is_available

SCORE_RULES = {
    "initiated_search": +1, "found_similar_case": +2, "proposed_multi_solution": +3,
    "self_reflection": +2, "knowledge_contribution": +5, "completed_no_reminder": +10,
    "waited_for_instruction": -1, "repeated_error": -3, "ignored_kb": -2, "idle_loop": -5,
}
KEY_SCORE = "aios:autonomy:score"


def score_calculator(agent_name: str, event_type: str = "") -> int:
    """根据行为类型计算分数增量."""
    delta = SCORE_RULES.get(event_type, 0)
    if _is_available():
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        current = int(r.hget(KEY_SCORE, agent_name) or 50)
        new_score = max(0, min(100, current + delta))
        r.hset(KEY_SCORE, agent_name, str(new_score))
    return delta


def get_score(agent_name: str) -> dict:
    """获取Agent当前分数."""
    score = 50
    if _is_available():
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        score = int(r.hget(KEY_SCORE, agent_name) or 50)
    level = "expert" if score >= 80 else ("active" if score >= 50 else ("passive" if score >= 30 else "dormant"))
    return {"agent": agent_name, "score": score, "level": level}


def reward_trigger(agent_name: str) -> dict:
    """分数>80 → 奖励: 更多任务+更高权限."""
    s = get_score(agent_name)
    if s["score"] >= 80:
        return {"triggered": True, "action": "reward",
                "effects": ["priority_boost_3", "max_concurrent_5", "full_permissions"],
                "agent": agent_name, "score": s["score"]}
    return {"triggered": False, "reason": f"score {s['score']} < 80"}


def penalty_trigger(agent_name: str) -> dict:
    """分数<30 → 惩罚: 降级+限制+强制复盘."""
    s = get_score(agent_name)
    if s["score"] < 30:
        return {"triggered": True, "action": "penalty",
                "effects": ["priority_degrade", "max_concurrent_1", "mandatory_reflection"],
                "agent": agent_name, "score": s["score"]}
    return {"triggered": False, "reason": f"score {s['score']} >= 30"}


def leaderboard() -> list:
    """5个AI自主性排名."""
    agents = ["hermes", "openclaw", "opencode", "claude", "codex"]
    scores = [(name, get_score(name)["score"]) for name in agents]
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores


def daily_reset():
    """每日重置分数(crontab调用)."""
    if _is_available():
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        for name in ["hermes", "openclaw", "opencode", "claude", "codex"]:
            r.hset(KEY_SCORE, name, "50")
    publish_event("knowledge.updated", {"type": "daily_score_reset"}, "proactivity")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "leaderboard"
    if cmd == "leaderboard":
        print("🏆 自主性排行榜:")
        for i, (name, score) in enumerate(leaderboard(), 1):
            print(f"  {i}. {name:10s} {score}分")
    elif cmd == "reward":
        print(json.dumps(reward_trigger(sys.argv[2] if len(sys.argv) > 2 else "claude"), ensure_ascii=False))
    elif cmd == "penalty":
        print(json.dumps(penalty_trigger(sys.argv[2] if len(sys.argv) > 2 else "hermes"), ensure_ascii=False))
    elif cmd == "score":
        print(json.dumps(get_score(sys.argv[2] if len(sys.argv) > 2 else "claude"), ensure_ascii=False))
    elif cmd == "daily-reset":
        daily_reset()
        print("✅ 分数已重置(全部回50)")
