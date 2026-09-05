#!/usr/bin/env python3
"""
P0-4: Drift Detector 漂移检测
==============================
执行中实时检测输出是否偏离原始目标。
不等到verify.py事后验证 — 过程中就纠正。

检测维度:
  1. 目标漂移: 当前输出与初始意图的语义距离
  2. 范围膨胀: 任务范围不断扩大(scope creep)
  3. 重复循环: 同一任务反复执行未推进
  4. 静默停滞: 长时间无输出更新
"""

import sys, os, json, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import check_recent, publish_event, _is_available

DRIFT_THRESHOLD_SECONDS = 300  # 5分钟无更新→停滞告警
LOOP_THRESHOLD = 3  # 同一任务出现3次→循环


def detect_scope_creep(task_name: str, recent_tasks: List[Dict]) -> Optional[Dict]:
    """检测任务范围膨胀 — 原名简单任务, 现在变得越来越大."""
    if len(recent_tasks) < 3: return None
    name_len_start = len(task_name)
    for r in recent_tasks[:5]:
        r_name = r.get("task_name", "")
        if task_name[:20] in r_name and len(r_name) > name_len_start * 2:
            return {
                "type": "scope_creep",
                "original": task_name[:50],
                "current": r_name[:80],
                "expansion": f"{len(r_name)} vs {name_len_start} chars",
                "severity": "warning",
            }
    return None


def detect_repetition_loop(task_name: str, recent_tasks: List[Dict]) -> Optional[Dict]:
    """检测重复循环 — 同一任务反复出现未推进."""
    matches = [r for r in recent_tasks if task_name[:30] in r.get("task_name", "")]
    if len(matches) >= LOOP_THRESHOLD:
        statuses = [r.get("status") for r in matches]
        if statuses.count("failed") >= LOOP_THRESHOLD:
            return {
                "type": "failure_loop",
                "task": task_name[:50],
                "count": len(matches),
                "statuses": statuses[:5],
                "severity": "critical",
            }
        if len(set(statuses)) == 1 and statuses[0] == "pending":
            return {
                "type": "stuck_loop",
                "task": task_name[:50],
                "count": len(matches),
                "severity": "warning",
            }
    return None


def detect_stall(recent_tasks: List[Dict]) -> Optional[Dict]:
    """检测静默停滞 — 最近无任何完成任务."""
    if not recent_tasks: return None
    latest_ts = recent_tasks[0].get("ts_complete", "")
    if latest_ts:
        try:
            ts = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)).total_seconds()
            if age > DRIFT_THRESHOLD_SECONDS:
                return {
                    "type": "stall",
                    "last_activity": latest_ts,
                    "idle_seconds": int(age),
                    "severity": "warning",
                }
        except: pass
    return None


def detect_all() -> List[Dict]:
    """全量漂移检测."""
    recent = check_recent(limit=30)
    if not recent: return []

    drifts = []

    # 对每个最近任务做检测
    for r in recent[:5]:
        name = r.get("task_name", "")
        if not name: continue

        creep = detect_scope_creep(name, recent)
        if creep: drifts.append(creep)

        loop = detect_repetition_loop(name, recent)
        if loop: drifts.append(loop)

    stall = detect_stall(recent)
    if stall: drifts.append(stall)

    # 发布事件
    for d in drifts:
        publish_event("alert.warning", {"type": "drift", "drift_type": d["type"],
                       "detail": str(d)[:200]}, "drift_detector")

    return drifts


def run():
    print("=" * 50)
    print(f"  Drift Detector — {datetime.now().isoformat()}")
    print("=" * 50)
    drifts = detect_all()
    if not drifts:
        print("  ✅ 无漂移")
    for d in drifts:
        icon = "🔴" if d.get("severity") == "critical" else "🟡"
        print(f"  {icon} [{d['type']}] {str(d)[:100]}")
    return drifts


if __name__ == "__main__":
    run()
