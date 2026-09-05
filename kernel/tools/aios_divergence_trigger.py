#!/usr/bin/env python3
"""
Divergence Trigger — 发散触发器
=================================
执行中遇卡点 → 自动切换思路 → 并行多方案探索 → 选最优继续
"""
import sys, os, json, time
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import check_recent, publish_event, enqueue_task, _is_available

STUCK_THRESHOLD = 2  # 连续失败N次触发发散
STUCK_KEYWORDS = ["超时", "timeout", "拒绝", "refused", "权限", "permission",
                   "not found", "找不到", "error", "错误"]


def detect_stuck(agent_name: str, task_name: str) -> dict:
    """检测Agent是否卡住."""
    recent = check_recent(limit=20)
    failures = [r for r in recent
                if r.get("system") == agent_name
                and r.get("status") == "failed"
                and task_name[:30] in r.get("task_name", "")]
    stuck = len(failures) >= STUCK_THRESHOLD

    # 分析失败原因
    error_patterns = []
    for f in failures:
        summary = f.get("summary", "").lower()
        for kw in STUCK_KEYWORDS:
            if kw.lower() in summary:
                error_patterns.append(kw)

    return {
        "stuck": stuck,
        "consecutive_failures": len(failures),
        "error_patterns": list(set(error_patterns)),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def generate_divergent_approaches(task_name: str, agent: str, failures: list) -> list:
    """基于失败原因生成发散方案."""
    patterns = []
    for f in failures:
        s = f.get("summary", "").lower()
        for kw, alt in [
            ("timeout", f"增加超时时间, 从{agent}换到更高性能执行器"),
            ("超时", f"拆分任务为更小子任务"),
            ("permission", "检查并修正权限配置"),
            ("权限", "使用sudo或调整文件权限"),
            ("not found", "检查路径和依赖是否完整"),
            ("找不到", "扫描文件系统确认目标存在"),
        ]:
            if kw in s:
                patterns.append(alt)

    if not patterns:
        patterns = [
            f"方案B: 从{agent}切换到备用执行器重试",
            f"方案C: 简化任务范围, 先做最小可行版本",
            "方案D: 跳过当前卡点, 先完成其他子任务",
        ]

    return list(set(patterns))


def trigger_divergence(agent_name: str, task_name: str) -> dict:
    """
    发散触发器: 检测卡点 → 生成替代方案 → 入队备用方案
    """
    stuck_check = detect_stuck(agent_name, task_name)

    if not stuck_check["stuck"]:
        return {"triggered": False, "reason": "not_stuck"}

    # 获取最近失败
    recent = check_recent(limit=20)
    failures = [r for r in recent
                if r.get("system") == agent_name
                and r.get("status") == "failed"][:STUCK_THRESHOLD]

    # 生成发散方案
    alternatives = generate_divergent_approaches(task_name, agent_name, failures)

    # 将替代方案入队
    for i, alt in enumerate(alternatives):
        alt_name = f"[发散-{i+1}] {alt[:80]}"
        enqueue_task(alt_name, system=agent_name, priority=2,
                     logic_depth="low", source="divergence_trigger",
                     context=task_name[:200])

    result = {
        "triggered": True,
        "agent": agent_name,
        "task": task_name[:80],
        "consecutive_failures": stuck_check["consecutive_failures"],
        "alternatives_generated": len(alternatives),
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    publish_event("alert.warning", {
        "type": "divergence_triggered",
        "agent": agent_name,
        "task": task_name[:60],
        "alternatives": len(alternatives),
    }, "divergence_trigger")

    return result


# ── CLI ──
if __name__ == "__main__":
    agent = sys.argv[1] if len(sys.argv) > 1 else "openclaw"
    task = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "分析Python文件统计行数"
    result = trigger_divergence(agent, task)
    if result["triggered"]:
        print(f"🔀 触发发散: {result['alternatives_generated']}个替代方案已入队")
    else:
        print(f"✅ 未卡住: {result['reason']}")
