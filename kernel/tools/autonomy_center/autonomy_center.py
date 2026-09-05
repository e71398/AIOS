#!/usr/bin/env python3
"""
Autonomy Center — 自主中心(第11中心)
=====================================
整合4个子模块: 好奇心引擎→发散触发→自主评分→知识贡献
每次任务先过此门禁: check_and_execute(task)
"""
import sys, os, json, time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))  # tools dir
sys.path.insert(0, str(Path(__file__).parent))          # autonomy_center dir

from curiosity_engine import pre_execution_gate
from divergence_trigger import solution_generator, approach_switcher, brainstorm_mode
from proactivity_tracker import score_calculator, get_score, reward_trigger, penalty_trigger, leaderboard
from knowledge_contribution import contribution_collector, get_contributions
from aios_bus import publish_event, _is_available


def check_and_execute(task_name: str, agent_name: str = "openclaw") -> dict:
    """
    自主中心主入口 — 每次任务先过门禁再执行。
    1. Curiosity Engine: 4项门禁检查
    2. Divergence Trigger: 预生成3个方案
    3. 返回执行许可 + 方案列表
    """
    result = {"task": task_name[:100], "agent": agent_name,
              "ts": datetime.now(timezone.utc).isoformat()}

    # 1. 门禁
    gate = pre_execution_gate(task_name)
    result["gate"] = gate

    # 2. 预生成方案
    solutions = solution_generator(task_name, 3)
    result["solutions"] = solutions

    # 3. 评分: 主动搜索+1, 多方案+3
    score_calculator(agent_name, "initiated_search")
    if solutions["count"] >= 3:
        score_calculator(agent_name, "proposed_multi_solution")

    # 4. 检查奖惩
    r = reward_trigger(agent_name)
    p = penalty_trigger(agent_name)
    result["incentives"] = {"reward": r["triggered"], "penalty": p["triggered"]}

    publish_event("autonomy.check_complete", {
        "task": task_name[:80], "agent": agent_name,
        "gate_passed": gate["passed"],
    }, "autonomy_center")

    return result


def agent_status(agent_name: str) -> dict:
    """返回Agent在Autonomy Center的完整状态."""
    return {
        "score": get_score(agent_name),
        "reward": reward_trigger(agent_name),
        "penalty": penalty_trigger(agent_name),
    }


# ── CLI ──
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    task = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "分析Python代码质量"

    if cmd == "check":
        r = check_and_execute(task)
        print(f"门禁: {'✅' if r['gate']['passed'] else '🚫'} 方案: {r['solutions']['count']}个")
        for a in r["solutions"]["approaches"]: print(f"  {a}")

    elif cmd == "status":
        agent = sys.argv[2] if len(sys.argv) > 2 else "claude"
        s = agent_status(agent)
        print(f"{agent}: {s['score']['score']}分 {s['score']['level']}")

    elif cmd == "leaderboard":
        print("🏆 自主性排行榜:")
        for i, (name, score) in enumerate(leaderboard(), 1):
            print(f"  {i}. {name:10s} {score}分")

    elif cmd == "complete":
        agent = sys.argv[2] if len(sys.argv) > 2 else "claude"
        score_calculator(agent, "completed_no_reminder")
        contribution_collector(agent, task, "自主完成任务", "")
        s = get_score(agent)
        print(f"✅ {agent}: {s['score']}分 (+10 completed_no_reminder, +5 contribution)")

    elif cmd == "contributions":
        for c in get_contributions(5):
            print(f"  [{c['agent']}] q={c['quality']} {c['task'][:50]}")
