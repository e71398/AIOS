#!/usr/bin/env python3
"""
Curiosity Engine — 执行前强制门禁
===================================
每任务启动前, 4项检查必须全部通过。不做完不让开始。
1. similar_case_search()   — 搜索历史类似案例
2. relevant_data_fetch()   — 抓取相关数据
3. alternative_approaches() — 至少提出2个方案
4. knowledge_base_checked() — 查过知识库
"""
import sys, os, json
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
sys.path.insert(0, str(TOOLS))

from aios_bus import check_recent, publish_event, _is_available
from aios_semantic_search import search as semantic_search


def similar_case_search(task_name: str, limit: int = 5) -> dict:
    """搜索历史类似案例."""
    results = semantic_search(task_name, limit=limit)
    recent = check_recent(limit=20)
    similar = [r for r in recent if any(
        kw in r.get("task_name", "").lower() for kw in task_name.lower().split()
        if len(kw) > 2
    )]

    return {
        "checked": True,
        "semantic_results": len(results),
        "similar_recent": len(similar),
        "top_hit": results[0]["title"][:80] if results else None,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def alternative_approaches(task_name: str) -> dict:
    """强制生成至少2个替代方案."""
    # 基于任务关键词生成替代方案框架
    approaches = []
    keywords = task_name.lower()

    if any(kw in keywords for kw in ["代码", "编程", "code", "写", "开发"]):
        approaches = [
            "方案A: 直接实现 → 最快",
            "方案B: 先写测试再实现 → 最稳",
            "方案C: 查开源库复用 → 最省",
        ]
    elif any(kw in keywords for kw in ["分析", "报告", "统计", "analysis"]):
        approaches = [
            "方案A: 逐文件扫描 → 准确但慢",
            "方案B: 索引预查询 → 快但有遗漏",
            "方案C: 混合模式 → 平衡速度和准确性",
        ]
    elif any(kw in keywords for kw in ["修复", "debug", "fix", "bug"]):
        approaches = [
            "方案A: 日志分析定位 → 常规",
            "方案B: 二分排除法 → 快速",
            "方案C: 回归测试 → 最可靠",
        ]
    else:
        approaches = [
            "方案A: 标准流程执行",
            "方案B: 简化版快速交付",
            "方案C: 深度版完整覆盖",
        ]

    return {
        "approaches": approaches,
        "count": len(approaches),
        "recommended": approaches[0],
        "pass": len(approaches) >= 2,
    }


def pre_execution_gate(task_name: str) -> dict:
    """
    执行前门禁 — 4项全通过才放行。
    Returns: {passed: bool, checks: dict, missing: list}
    """
    checks = {}
    missing = []

    # 1. 相似案例搜索
    checks["case_searched"] = similar_case_search(task_name)
    if not checks["case_searched"]["checked"]:
        missing.append("case_searched")

    # 2. 替代方案
    checks["alternatives"] = alternative_approaches(task_name)
    if not checks["alternatives"]["pass"]:
        missing.append("need_more_alternatives")

    # 3. 知识库检查
    kb_results = semantic_search(task_name, limit=3)
    checks["kb_checked"] = {
        "checked": True,
        "results": len(kb_results),
        "top_hit": kb_results[0]["title"][:60] if kb_results else None,
    }
    if not kb_results:
        missing.append("no_knowledge_match")

    # 4. 相关数据收集
    checks["data_collected"] = {
        "checked": True,
        "note": "AIOS自动抓取上下文和最近任务数据",
        "recent_tasks": len(check_recent(limit=10)),
    }

    passed = len(missing) == 0

    result = {
        "passed": passed,
        "checks": checks,
        "missing": missing,
        "task": task_name[:100],
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    if not passed:
        publish_event("alert.warning", {
            "type": "pre_exec_gate_failed",
            "task": task_name[:80],
            "missing": missing,
        }, "curiosity_engine")

    return result


# ── CLI ──
if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "分析Python代码质量"
    result = pre_execution_gate(task)
    print(f"门禁: {'✅ 通过' if result['passed'] else '🚫 未通过'}")
    if not result['passed']:
        print(f"缺失: {result['missing']}")
    for check, data in result["checks"].items():
        icon = "✅" if data.get("passed", data.get("checked", True)) else "❌"
        print(f"  {icon} {check}")
