#!/usr/bin/env python3
"""
Proactivity Tracker — 自主性评分 + 奖惩
=========================================
每个Agent有0-100自主分, 影响任务分配优先级。
高分→更多任务+更大权限+优先调度
低分→降级+限制权限+强制复盘
"""
import sys, os, json
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import check_recent, lifecycle_list_all, publish_event, _is_available

# 评分规则
SCORE_RULES = {
    "initiated_web_search": +1,
    "found_similar_case": +2,
    "proposed_multiple_solutions": +3,
    "self_reflection_completed": +2,
    "contributed_to_knowledge": +5,
    "completed_without_reminder": +10,
    "waited_for_instruction": -1,
    "repeated_same_error": -3,
    "ignored_knowledge_base": -2,
    "idle_in_loop": -5,
}

KEY_SCORES = "aios:autonomy:scores"
KEY_LEADERBOARD = "aios:autonomy:leaderboard"


def calculate_score(agent_name: str) -> dict:
    """计算Agent的自主性分数."""
    score = 50  # 基础分
    details = []

    recent = check_recent(limit=50)
    agent_tasks = [r for r in recent if r.get("system") == agent_name]

    # 主动搜索(+1)
    searches = sum(1 for r in agent_tasks if "搜索" in r.get("task_name", "") or "search" in r.get("context", ""))
    if searches > 0:
        score += searches * SCORE_RULES["initiated_web_search"]
        details.append(f"主动搜索 ×{searches}")

    # 多方案(+3)
    multi = sum(1 for r in agent_tasks if "方案" in r.get("task_name", ""))
    if multi > 0:
        score += multi * SCORE_RULES["proposed_multiple_solutions"]
        details.append(f"多方案 ×{multi}")

    # 无督促完成(+10)
    completed = [r for r in agent_tasks if r.get("status") == "completed"]
    if completed:
        score += SCORE_RULES["completed_without_reminder"]
        details.append("自主完成")

    # 重复错误(-3)
    failures = [r for r in agent_tasks if r.get("status") == "failed"]
    error_names = [r.get("task_name", "")[:30] for r in failures]
    repeats = len(error_names) - len(set(error_names))
    if repeats > 0:
        score += repeats * SCORE_RULES["repeated_same_error"]
        details.append(f"重复错误 ×{repeats}")

    # 知识贡献(+5)
    contributed = sum(1 for r in agent_tasks if "贡献" in r.get("task_name", "") or "knowledge" in r.get("summary", ""))
    if contributed > 0:
        score += contributed * SCORE_RULES["contributed_to_knowledge"]
        details.append(f"知识贡献 ×{contributed}")

    score = max(0, min(100, score))

    result = {
        "agent": agent_name,
        "score": score,
        "level": "expert" if score >= 80 else ("active" if score >= 50 else ("passive" if score >= 30 else "dormant")),
        "details": details,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    # 写入Redis
    if _is_available():
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        r.hset(KEY_SCORES, agent_name, json.dumps(result, ensure_ascii=False))
        r.zadd(KEY_LEADERBOARD, {agent_name: score})

    return result


def get_reward(agent_name: str) -> dict:
    """根据分数返回奖惩."""
    score_data = calculate_score(agent_name)
    score = score_data["score"]

    if score >= 80:
        reward = {
            "level": "expert",
            "priority_boost": 3,
            "max_concurrent": 5,
            "permissions": "all",
            "message": "🏆 专家级自主性 — 优先调度, 不受任务限制",
        }
    elif score >= 50:
        reward = {
            "level": "active",
            "priority_boost": 1,
            "max_concurrent": 3,
            "permissions": "standard",
            "message": "✅ 活跃 — 正常执行任务",
        }
    elif score >= 30:
        reward = {
            "level": "passive",
            "priority_boost": -1,
            "max_concurrent": 1,
            "permissions": "limited",
            "message": "⚠️ 被动 — 需要更多自主性, 限制任务并发",
        }
    else:
        reward = {
            "level": "dormant",
            "priority_boost": -3,
            "max_concurrent": 0,
            "permissions": "restricted",
            "message": "🚫 休眠 — 强制复盘后恢复, 当前禁止接新任务",
        }

    reward["score"] = score
    reward["agent"] = agent_name
    return reward


def leaderboard() -> list:
    """自主性排行榜."""
    if not _is_available():
        return []
    import redis as _r
    r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    rankings = r.zrevrange(KEY_LEADERBOARD, 0, -1, withscores=True)
    return [(a.decode() if isinstance(a, bytes) else a, int(s))
            for a, s in rankings]


# ── CLI ──
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"

    if cmd == "all":
        print("自主性评分:")
        for name in ["hermes", "openclaw", "opencode", "claude", "codex"]:
            r = get_reward(name)
            print(f"  {r['agent']:10s} {r['score']:3d}分 {r['level']:8s} {r['message']}")

    elif cmd == "leaderboard":
        print("🏆 排行榜:")
        for i, (name, score) in enumerate(leaderboard(), 1):
            icon = ["🥇","🥈","🥉","4️⃣","5️⃣"][i-1] if i <= 5 else f"{i}."
            print(f"  {icon} {name:10s} {score}分")

    elif cmd == "reward":
        agent = sys.argv[2] if len(sys.argv) > 2 else "claude"
        print(json.dumps(get_reward(agent), ensure_ascii=False, indent=2))
