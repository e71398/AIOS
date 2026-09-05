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
                      generate_task_id, KEY_PREFIX, enqueue_task)


def scan_failure_patterns(days: int = 7) -> list:
    """从总线最近N天记录中提取失败模式."""
    if not _is_available():
        return []

    patterns = []
    recent = check_recent(hours=days * 24, limit=100, status_filter="failed")
    for r in recent:
        # Test fixtures and legacy autonomous tasks are not production
        # evidence and must not poison routing calibration.
        if r.get("source") in ("test", "autonomous", "self_diagnose"):
            continue
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


def _normalize_task(task_name: str) -> str:
    """规范化任务名: 去除UUID/时间戳/编号等噪声, 提取模式原名."""
    t = re.sub(r'[\[\(]\s*[a-f0-9]{6,12}\s*[\]\)]', '', task_name)
    t = re.sub(r'\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b', '', t)
    t = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', '', t)
    t = re.sub(r'#[0-9]+', '', t)
    t = re.sub(r'\b\d+\b', '', t)
    t = re.sub(r'_{2,}', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:80] if t else task_name[:80]


def distill_skills(days: int = 7) -> list:
    """经验提炼官: 从成功任务中提取执行模式, 生成成功路径+避坑指南."""
    if not _is_available():
        return []

    recent = check_recent(hours=days * 24, limit=200, status_filter="completed")
    failed = check_recent(hours=days * 24, limit=100, status_filter="failed")

    skill_clusters = {}
    for r in recent:
        if r.get("parent_verification") != "accepted":
            continue
        raw = r.get("task_name", "")
        pattern = _normalize_task(raw)
        if len(pattern) < 3:
            continue
        if pattern not in skill_clusters:
            skill_clusters[pattern] = {
                "count": 0, "executor": r.get("executor", ""),
                "system": r.get("system", ""), "raw_samples": []
            }
        skill_clusters[pattern]["count"] += 1
        if len(skill_clusters[pattern]["raw_samples"]) < 2:
            skill_clusters[pattern]["raw_samples"].append(raw[:120])

    # 提取避坑指南: 看失败任务里有没有和成功模式相似但失败的
    pitfall_map = {}
    for f in failed:
        fn = _normalize_task(f.get("task_name", ""))
        if fn in skill_clusters:
            if fn not in pitfall_map:
                pitfall_map[fn] = []
            pitfall_map[fn].append(f.get("error", "")[:100])

    skills = []
    for pattern, data in skill_clusters.items():
        if data["count"] >= 2:
            skill_entry = {
                "task_pattern": pattern,
                "frequency": data["count"],
                "confidence": min(0.95, 0.5 + data["count"] * 0.08),
                "executor": data["executor"],
                "sample": data["raw_samples"][0][:80] if data["raw_samples"] else "",
            }
            # 附上避坑指南
            if pattern in pitfall_map:
                skill_entry["pitfalls"] = list(set(pitfall_map[pattern]))[:3]
                skill_entry["avoid_guide"] = f"该任务曾失败{len(pitfall_map[pattern])}次, 避免: {'; '.join(skill_entry['pitfalls'][:2])}"
            skills.append(skill_entry)

    skills.sort(key=lambda x: -x["frequency"])

    # 写入 Knowledge Center
    _write_skill_library(skills[:10])

    return skills[:10]


def _write_skill_library(skills: list):
    """将提炼的技能写入 Knowledge Center."""
    lib_dir = Path(AIOS_HOME) / "knowledge" / "skill_library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "skills": skills}
    (lib_dir / "hermes_distilled.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))


def _check_executor_performance(days: int = 7) -> dict:
    if not _is_available():
        return {}
    recent = check_recent(hours=days * 24, limit=200)
    if not recent:
        return {}

    stats = {}
    for r in recent:
        ex = r.get("executor", "") or r.get("system", "unknown")
        status = r.get("status", "")
        if not ex:
            continue
        if ex not in stats:
            stats[ex] = {"total": 0, "completed": 0, "failed": 0}
        stats[ex]["total"] += 1
        if status == "completed":
            stats[ex]["completed"] += 1
        elif status == "failed":
            stats[ex]["failed"] += 1

    perf = {}
    for ex, s in stats.items():
        if s["total"] == 0:
            continue
        fail_rate = round(s["failed"] / s["total"], 2)
        perf[ex] = {
            "total_tasks": s["total"],
            "completed": s["completed"],
            "failed": s["failed"],
            "fail_rate": fail_rate,
            "health": "healthy" if fail_rate < 0.1 else ("warning" if fail_rate < 0.3 else "degraded"),
        }
    return perf


def generate_calibration_report() -> dict:
    """系统校准员: 对比World Model + 执行器性能 → 产出 actionable 校准建议."""
    reports_dir = Path(AIOS_HOME) / "logs" / "world_model_reports"
    predictions = []
    if reports_dir.exists():
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

    wm_data = {"status": "no_data"}
    if predictions:
        approved = sum(1 for p in predictions if p.get("verdict") == "APPROVED")
        blocked = sum(1 for p in predictions if p.get("verdict") == "BLOCKED")
        wm_data = {
            "total_predictions": len(predictions),
            "approved": approved,
            "blocked": blocked,
            "accuracy": round(approved / len(predictions), 2) if predictions else 0,
            "status": "accurate" if blocked == 0 else "needs_calibration",
        }

    perf = _check_executor_performance()
    router_hints = _generate_router_hints(perf, wm_data)

    return {
        "world_model": wm_data,
        "executor_performance": perf,
        "router_hints": router_hints,
    }


CALIBRATION_ALERTS = {
    "world_model_drift": "World Model 预测准确率低于70%, 建议重新训练校准参数",
    "executor_degraded": "{executor} 失败率{fail_rate}%过高, 建议暂时降权或切换备用执行器",
    "executor_overloaded": "{executor} 任务量过大({total}个), 建议增加并行度或分流到其他执行器",
}

def _generate_router_hints(perf: dict, wm_data: dict) -> list:
    """生成 Model Router 可用的校准提示."""
    hints = []
    # 从失败分析推校准
    if isinstance(wm_data.get("accuracy"), (int, float)) and wm_data["accuracy"] < 0.7:
        hints.append({"type": "world_model_drift", "action": "recalibrate", "detail": CALIBRATION_ALERTS["world_model_drift"]})

    # 从执行器性能推校准
    for ex, s in perf.items():
        if s["health"] == "degraded":
            hints.append({
                "type": "executor_degraded",
                "executor": ex,
                "fail_rate": s["fail_rate"],
                "action": "downgrade",
                "detail": CALIBRATION_ALERTS["executor_degraded"].format(executor=ex, fail_rate=int(s["fail_rate"] * 100)),
            })
        elif s.get("total_tasks", 0) > 50 and s["health"] == "warning":
            hints.append({
                "type": "executor_overloaded",
                "executor": ex,
                "total": s["total_tasks"],
                "action": "rebalance",
                "detail": CALIBRATION_ALERTS["executor_overloaded"].format(executor=ex, total=s["total_tasks"]),
            })
    return hints


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
            "executor_performance": json.dumps(analysis.get("calibration", {}).get("executor_performance", {}), ensure_ascii=False),
            "router_hints": json.dumps(analysis.get("calibration", {}).get("router_hints", []), ensure_ascii=False),
        })
        _redis_client.expire(key, 7 * 24 * 3600)
        return True
    except Exception:
        return False


def _detect_knowledge_gaps(failure_analysis: dict, skills: list) -> list:
    """检测知识缺口: 失败密集领域 + 缺技能的领域 → 生成研究任务."""
    gaps = []
    seen = set()

    # 失败密集领域 → 需要复盘补知识
    fa = failure_analysis if isinstance(failure_analysis, dict) else {}
    top_errors = fa.get("top_errors", [])
    for err in top_errors:
        if err.get("count", 0) >= 3:
            kw = err["keyword"]
            if kw not in seen:
                gaps.append(f"复盘错误模式: '{kw}' (出现{err['count']}次), 分析根因并生成避坑指南")
                seen.add(kw)

    # 缺技能的领域 → 需要学习
    systems = set(fa.get("systems_affected", []))
    if len(skills) < 3:
        gaps.append("技能库不足(<3个), 需要扫描近期成功任务, 提取更多可复用技能")
    if "opencode" in systems and "claude" in systems:
        gaps.append("多执行器同时受影响, 检查是否是公共基础设施问题(Redis/磁盘/网络)")

    return gaps


def _dispatch_proactive_tasks(analysis: dict) -> int:
    """自主性激励者: 发现知识缺口时主动向 OpenClaw 发送研究任务."""
    if os.getenv("AIOS_HERMES_PROACTIVE_TASKS", "false").lower() != "true":
        return 0
    if not _is_available():
        return 0

    fa = analysis.get("failure_analysis", {})
    skills = analysis.get("skills", [])
    gaps = _detect_knowledge_gaps(fa, skills)

    cal = analysis.get("calibration", {})
    wm = cal.get("world_model", {})
    if isinstance(wm, dict) and wm.get("accuracy", 1) < 0.7:
        gaps.append("World Model 预测准确率偏低, 需要重新校准 World Model 参数")

    # 跨AI协作模式分析
    collab_tasks = _analyze_collaboration_patterns()
    gaps.extend(collab_tasks)

    dispatched = 0
    for task_text in gaps:
        tid = enqueue_task(
            task_name=task_text,
            system="hermes",
            priority=2,
            logic_depth="standard",
            source="autonomous",
            context="Hermes 自主驱动: 发现知识缺口 → 主动研究",
        )
        if tid:
            dispatched += 1
    return dispatched


def _analyze_collaboration_patterns() -> list:
    """跨AI模式发现: 分析成功协作案例 → 提炼共享模式."""
    if not _is_available():
        return []
    tasks = []
    recent = check_recent(hours=168, limit=50, status_filter="completed")
    exec_counts = Counter(r.get("executor", r.get("system", "?")) for r in recent)
    # 如果多个执行器都成功完成了同一类任务 → 有共享价值
    if len(exec_counts) >= 2:
        top_exec = exec_counts.most_common(2)
        tasks.append(f"分析成功协作: {top_exec[0][0]}({top_exec[0][1]}次) + {top_exec[1][0]}({top_exec[1][1]}次) — 提炼可复用协作模式")

    # 检查知识库评分衰减
    try:
        from aios_semantic_search import decay_all_scores
        decayed = decay_all_scores()
        if decayed > 10:
            tasks.append(f"知识库评分衰减: {decayed}条知识已重新评分, 建议审查低分条目")
    except Exception:
        pass
    return tasks


def run_learning_cycle() -> dict:
    """完整学习周期."""
    print("=" * 50)
    print(f"  Hermes 慢循环学习 — {datetime.now().isoformat()}")
    print("=" * 50)

    # 1. 失败模式
    print("\n[1/5] 扫描失败模式...")
    failures = scan_failure_patterns(days=7)
    failure_analysis = analyze_failures(failures)
    print(f"  发现 {failure_analysis['total']} 次失败")
    for e in failure_analysis.get("top_errors", [])[:3]:
        print(f"    • {e['keyword']}: {e['count']}次")

    # 2. 技能蒸馏 (经验提炼官)
    print("\n[2/5] 提炼技能...")
    skills = distill_skills(days=7)
    print(f"  提炼 {len(skills)} 个技能")
    for s in skills[:3]:
        pitfalls = f" [避坑: {len(s.get('pitfalls',[]))}条]" if s.get("pitfalls") else ""
        print(f"    • {s['task_pattern'][:50]} (×{s['frequency']}){pitfalls}")

    # 3. 校准 (系统校准员)
    print("\n[3/5] World Model + 执行器校准...")
    calibration = generate_calibration_report()
    wm = calibration.get("world_model", {})
    print(f"  World Model 准确率: {wm.get('accuracy', 'N/A')}")
    perf = calibration.get("executor_performance", {})
    for ex, s in perf.items():
        print(f"  {ex}: {s['total_tasks']}任务 失败率{s['fail_rate']:.0%} [{s['health']}]")
    hints = calibration.get("router_hints", [])
    if hints:
        print(f"  ⚠ {len(hints)}条校准建议")
        for h in hints[:2]:
            print(f"    → {h['detail'][:60]}")

    # 4. 推送策略
    print("\n[4/5] 推送策略到 Redis...")
    analysis = {
        "failure_analysis": failure_analysis,
        "skills": skills,
        "calibration": calibration,
    }
    ok = push_strategy_to_redis(analysis)
    print(f"  {'✅ 推送成功' if ok else '❌ 推送失败'}")

    # 5. 自主性激励 (新)
    print("\n[5/5] 自主性激励: 主动扫描知识缺口...")
    dispatched = _dispatch_proactive_tasks(analysis)
    print(f"  {'🚀' if dispatched else '💤'} 主动派发 {dispatched} 个研究任务")

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

    # Refresh each replaceable tool chip's own learning profile. These files
    # live behind the stable adapter and never modify another tool or AIOS core.
    try:
        from aios_tool_evolution import learn_all
        tool_profiles = learn_all()
        analysis["tool_profiles"] = {name: {"samples": profile.get("samples", 0),
                                             "failures": profile.get("failures", 0)}
                                     for name, profile in tool_profiles.items()}
    except Exception as e:
        print(f"  工具芯片学习刷新跳过: {e}")

    # Tool-local gaps are absorbed automatically. Only changes to AIOS core or
    # the stable adapter contract become owner-approved evolution proposals.
    try:
        sys.path.insert(0, "${AIOS_HOME}/kernel/centers/evolution_center")
        from evolution_controller import ingest_hermes_analysis
        proposals = ingest_hermes_analysis(analysis)
        analysis["core_evolution_proposals"] = [p["proposal_id"] for p in proposals]
    except Exception as e:
        print(f"  核心进化提案生成跳过: {e}")

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
    try:
        from aios_tool_evolution import learn
        analysis["hermes_profile"] = learn("hermes")
    except Exception:
        pass
    return analysis


# ============================================================
#  Pin Registration — Hermes 学习能力注册到 AIOS 总线
# ============================================================
try:
    from aios_bus import register_pin
    register_pin("hermes.learning_cycle", run_learning_cycle,
                 "Hermes: 完整学习循环（失败分析→策略校准→技能提取）")
    register_pin("hermes.quick_learn", quick_learn,
                 "Hermes: 轻量实时学习")
    register_pin("hermes.push_strategy", push_strategy_to_redis,
                 "Hermes: 推送调度策略到 Redis")
except Exception:
    pass

if __name__ == "__main__":
    run_learning_cycle()
