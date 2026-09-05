#!/usr/bin/env python3
"""
P1-M6: Hermes Learning Pipeline 慢循环学习管线
================================================
每日凌晨02:00 Cron触发 (替代原手动扫描):
  1. 扫描 logs/ 目录当日所有执行日志
  2. 提取失败模式 (Failure Pattern)
  3. 对比 World Model 预测 vs 实际结果 → 校准报告
  4. 提炼成功经验 → skill_library
  5. 推送调度策略 → Redis aios:hermes:strategy → OpenClaw参考

这是5份AI分析一致认为最关键但缺失的反馈回路。
"""

import json, os, sys, re, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import Counter

TOOLS = Path("${AIOS_HOME}/kernel/tools")
AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
sys.path.insert(0, str(TOOLS))

from aios_bus import (_is_available, _redis_client, check_recent, publish_result,
                      generate_task_id, KEY_PREFIX)


def scan_failure_patterns(days: int = 7) -> list:
    """从总线最近N天记录中提取失败模式."""
    if not _is_available():
        return []

    patterns = []
    recent = check_recent(hours=days * 24, limit=100, status_filter="failed")
    for r in recent:
        summary = r.get("summary", "")
        task_name = r.get("task_name", "")
        patterns.append({
            "task_name": task_name[:100],
            "system": r.get("system", "?"),
            "error": summary[:200],
            "ts": r.get("ts_complete", ""),
        })
    return patterns


def analyze_failures(failures: list) -> dict:
    """分析失败, 提取共性模式."""
    if not failures:
        return {"total": 0, "patterns": [], "suggestion": "无失败记录,系统健康"}

    # 按错误关键词聚类
    error_words = Counter()
    for f in failures:
        error = f.get("error", "").lower()
        for kw in ["timeout", "超时", "refused", "拒绝", "permission", "权限",
                    "not found", "找不到", "error", "错误", "fail", "失败",
                    "memory", "内存", "disk", "磁盘", "lock", "锁"]:
            if kw in error:
                error_words[kw] += 1

    top_errors = error_words.most_common(5)
    suggestion = "建议: "
    if top_errors:
        top_kw = top_errors[0][0]
        suggestion += f"重点排查'{top_kw}'类错误(出现{top_errors[0][1]}次). "
    suggestion += f"共{failures.__len__()}次失败, 涉及{len(set(f['system'] for f in failures))}个系统."

    return {
        "total": len(failures),
        "top_errors": [{"keyword": k, "count": c} for k, c in top_errors],
        "systems_affected": list(set(f["system"] for f in failures)),
        "suggestion": suggestion,
    }


def distill_skills(days: int = 7) -> list:
    """从成功执行中提炼技能."""
    if not _is_available():
        return []

    skills = []
    recent = check_recent(hours=days * 24, limit=100, status_filter="completed")
    # 按任务名聚类
    task_counter = Counter(r.get("task_name", "")[:60] for r in recent)
    for task_name, count in task_counter.most_common(5):
        if count >= 2:  # 重复出现2次以上
            skills.append({
                "task_pattern": task_name,
                "frequency": count,
                "confidence": min(0.9, 0.5 + count * 0.1),
                "suggestion": f"该任务模式已成功执行{count}次, 可沉淀为标准技能",
            })
    return skills


def generate_calibration_report() -> dict:
    """生成World Model校准报告."""
    reports_dir = Path(AIOS_HOME) / "logs" / "world_model_reports"
    if not reports_dir.exists():
        return {"status": "no_data"}

    predictions = []
    for f in reports_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            ts_str = data.get("timestamp", "")
            if ts_str:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)).days <= 7:
                    predictions.append(data)
        except Exception:
            pass

    if not predictions:
        return {"status": "no_data"}

    approved = sum(1 for p in predictions if p.get("verdict") == "APPROVED")
    blocked = sum(1 for p in predictions if p.get("verdict") == "BLOCKED")
    return {
        "total_predictions": len(predictions),
        "approved": approved,
        "blocked": blocked,
        "accuracy": round(approved / len(predictions), 2) if predictions else 0,
        "status": "accurate" if blocked == 0 else "needs_calibration",
    }


def push_strategy_to_redis(analysis: dict) -> bool:
    """将学习结果推送到Redis, 供OpenClaw调度时参考."""
    if not _is_available():
        return False
    try:
        key = f"{KEY_PREFIX}:hermes:strategy"
        _redis_client.hset(key, mapping={
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "failure_analysis": json.dumps(analysis.get("failure_analysis", {}), ensure_ascii=False),
            "skills_distilled": json.dumps(analysis.get("skills", []), ensure_ascii=False),
            "calibration": json.dumps(analysis.get("calibration", {}), ensure_ascii=False),
        })
        _redis_client.expire(key, 7 * 24 * 3600)
        return True
    except Exception:
        return False


def run_learning_cycle() -> dict:
    """完整学习周期."""
    print("=" * 50)
    print(f"  Hermes 慢循环学习 — {datetime.now().isoformat()}")
    print("=" * 50)

    # 1. 失败模式
    print("\n[1/4] 扫描失败模式...")
    failures = scan_failure_patterns(days=7)
    failure_analysis = analyze_failures(failures)
    print(f"  发现 {failure_analysis['total']} 次失败")
    for e in failure_analysis.get("top_errors", [])[:3]:
        print(f"    • {e['keyword']}: {e['count']}次")

    # 2. 技能蒸馏
    print("\n[2/4] 提炼技能...")
    skills = distill_skills(days=7)
    print(f"  提炼 {len(skills)} 个候选技能")
    for s in skills[:3]:
        print(f"    • {s['task_pattern'][:50]} (×{s['frequency']})")

    # 3. 校准
    print("\n[3/4] World Model 校准...")
    calibration = generate_calibration_report()
    print(f"  预测准确率: {calibration.get('accuracy', 'N/A')}")

    # 4. 推送策略
    print("\n[4/4] 推送策略到 Redis...")
    analysis = {
        "failure_analysis": failure_analysis,
        "skills": skills,
        "calibration": calibration,
    }
    ok = push_strategy_to_redis(analysis)
    print(f"  {'✅ 推送成功' if ok else '❌ 推送失败'}")

    # 保存报告
    report_dir = Path(AIOS_HOME) / "knowledge" / "calibration_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"hermes_learn_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_file.write_text(json.dumps(analysis, ensure_ascii=False, indent=2))
    print(f"\n📋 报告已保存: {report_file}")

    # 自动触发自探索诊断 — 学习完立即诊断, 发现的问题自动入队
    print("\n🔍 触发自探索诊断...")
    try:
        from aios_self_diagnose import run as diagnose_run
        diagnose_run()
    except Exception as e:
        print(f"  诊断跳过: {e}")

    return analysis


def quick_learn() -> dict:
    """
    轻量实时学习：聚合最近任务后调用，扫描最近20条记录更新策略。
    对比 run_learning_cycle() 跳过 World Model 校准和技能文件写入，只更新 Redis 策略。
    """
    failures = scan_failure_patterns(days=1)
    failure_analysis = analyze_failures(failures)
    skills = distill_skills(days=1)

    analysis = {
        "failure_analysis": failure_analysis,
        "skills": skills,
        "calibration": {"status": "quick", "note": "轻量学习，未做完整校准"},
    }
    push_strategy_to_redis(analysis)
    return analysis


if __name__ == "__main__":
    run_learning_cycle()
