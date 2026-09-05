#!/usr/bin/env python3
"""Divergence Trigger — 发散: 多方案生成+自动切换+脑暴"""
import sys, os, json, time
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import check_recent, publish_event, enqueue_task, _is_available


def solution_generator(task_name: str, count: int = 3) -> dict:
    """一次生成N个方案, 不只给1个."""
    base_approaches = {
        "代码": ["直接实现(最快)", "TDD先写测试(最稳)", "复用开源库(最省)"],
        "分析": ["逐文件扫描(准确)", "索引查询(快速)", "混合模式(平衡)"],
        "修复": ["日志定位(常规)", "二分排除(快速)", "回归测试(可靠)"],
        "默认": ["标准流程", "简化快速", "深度完整"],
    }
    for kw, approaches in base_approaches.items():
        if kw in task_name: break
    else: approaches = base_approaches["默认"]

    return {"task": task_name[:80], "count": min(count, len(approaches)),
            "approaches": [f"方案{i+1}: {a}" for i, a in enumerate(approaches[:count])],
            "recommended": approaches[0]}


def approach_switcher(agent_name: str, task_name: str) -> dict:
    """方案A失败→自动切换到B."""
    recent = check_recent(limit=20)
    failures = [r for r in recent if r.get("system") == agent_name
                and r.get("status") == "failed"
                and task_name[:30] in r.get("task_name", "")]
    if len(failures) < 2: return {"switched": False, "reason": "not_stuck_yet"}
    alt = solution_generator(task_name, 3)
    for i, approach in enumerate(alt["approaches"][1:], 1):
        enqueue_task(f"[切换方案{i}] {approach}", system=agent_name,
                     priority=2, logic_depth="low", source="approach_switcher",
                     context=task_name[:200])
    publish_event("alert.warning", {"type":"approach_switched","agent":agent_name,
                   "task":task_name[:60],"alternatives":len(alt["approaches"])-1},"divergence")
    return {"switched": True, "alternatives_queued": len(alt["approaches"]) - 1}


def brainstorm_mode(task_name: str, count: int = 5) -> dict:
    """并行探索多思路 — 发散思维."""
    angles = [f"角度{i+1}: {a}" for i, a in enumerate([
        "最直接的解决方案", "从用户视角出发", "从系统架构层面",
        "参考业界最佳实践", "反向思考: 什么会导致失败"
    ][:count])]
    return {"task": task_name[:80], "angles": angles, "count": len(angles)}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "generate"
    task = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "分析Python代码质量"
    if cmd == "generate":
        r = solution_generator(task, 3)
        for a in r["approaches"]: print(f"  {a}")
    elif cmd == "switch":
        agent = sys.argv[2] if len(sys.argv) > 2 else "openclaw"
        print(json.dumps(approach_switcher(agent, task), ensure_ascii=False))
    elif cmd == "brainstorm":
        for a in brainstorm_mode(task, 5)["angles"]: print(f"  {a}")
