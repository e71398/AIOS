#!/usr/bin/env python3
"""
AIOS v4.0 Self-Diagnosis Engine (自探索诊断引擎)
=================================================
不需要人督促。自动扫描系统、发现问题、生成修复任务入队。

触发:
  - Hermes 学习管线完成后自动调用
  - 每小时 Cron 兜底
  - 总线启动时调用一次

产出:
  - 诊断报告 → ${AIOS_HOME}/logs/diagnosis/
  - 修复任务 → 自动入队 aios:queue:pending
"""

import sys, os, json, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

TOOLS = Path("${AIOS_HOME}/kernel/tools")
AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
sys.path.insert(0, str(TOOLS))

from aios_bus import (
    _is_available, check_bus_health, cleanup_stale_queue,
    get_queue_status, check_recent, get_system_status,
    list_registered_executors, check_dependency_health,
    init_core_dependencies, get_event_log, publish_event,
    enqueue_task, generate_task_id, subscribe_events, heartbeat
)
from aios_firewall import get_mode, BUS_NAMESPACE
from aios_contract_enforcer import check_all_rules as enforce_contracts
from aios_drift_detector import detect_all as detect_drifts

def diagnose_all() -> dict:
    """全系统诊断, 返回发现的问题列表."""
    issues = []

    # 1. 总线健康
    health = check_bus_health()
    if not health["healthy"]:
        issues.append({"severity": "critical", "category": "bus",
                        "title": "Redis总线不可用", "detail": str(health)})

    # 2. 队列僵尸
    qs = get_queue_status()
    pending = qs.get("pending", 0)
    locked = qs.get("locked", 0)
    if pending > 10:
        issues.append({"severity": "warning", "category": "queue",
                        "title": f"任务队列积压({pending}个)", "auto_fix": "cleanup_stale_queue"})
    if locked > 5:
        issues.append({"severity": "warning", "category": "queue",
                        "title": f"锁死任务过多({locked}个)", "auto_fix": "release_stale_locks"})

    # 3. 系统心跳
    alive = get_system_status()
    for sys_name in ["hermes", "openclaw", "opencode", "claude", "codex"]:
        s = alive.get(sys_name, {})
        if not s.get("alive"):
            issues.append({"severity": "warning", "category": "heartbeat",
                            "title": f"{sys_name} 心跳丢失", "auto_fix": None})

    # 4. 防火墙模式
    from aios_firewall import get_mode as fw_get_mode
    mode = fw_get_mode()
    if mode != "enforce":
        issues.append({"severity": "info", "category": "security",
                        "title": f"防火墙非enforce(当前:{mode})", "auto_fix": "set_enforce_mode"})

    # 5. 依赖健康
    deps_ok = True
    for comp in ["executor_daemon", "dispatcher", "hermes_learn"]:
        h = check_dependency_health(comp)
        if not h["all_healthy"]:
            deps_ok = False
            issues.append({"severity": "warning", "category": "dependency",
                            "title": f"{comp} 依赖不健康: {h['dependencies']}"})

    # 6. 最近失败率
    recent = check_recent(hours=24, limit=50)
    if recent:
        failed = sum(1 for r in recent if r.get("status") == "failed")
        rate = failed / len(recent)
        if rate > 0.3:
            issues.append({"severity": "warning", "category": "execution",
                            "title": f"24h失败率{rate:.0%}({failed}/{len(recent)})"})

    # 7. 契约执行 (从三份宪法提取强制规则, 主动拦截)
    contract_violations = enforce_contracts()
    for v in contract_violations:
        issues.append({"severity": "critical" if v["severity"] == "critical" else "warning",
                        "category": "contract", "title": f"[{v['rule']}] {v['detail'][:80]}"})

    # 8. 漂移检测 (执行中偏离目标)
    drifts = detect_drifts()
    for d in drifts:
        issues.append({"severity": d.get("severity", "warning"),
                        "category": "drift", "title": f"[{d['type']}] {str(d)[:120]}"})

    # 9. 世界模型校准
    wm_dir = Path(AIOS_HOME) / "logs" / "world_model_reports"
    wm_files = list(wm_dir.glob("*.json")) if wm_dir.exists() else []
    if len(wm_files) > 10:
        blocked = 0
        for f in wm_files[-20:]:
            try:
                d = json.loads(f.read_text())
                if d.get("verdict") == "BLOCKED": blocked += 1
            except: pass
        if blocked > 3:
            issues.append({"severity": "info", "category": "world_model",
                            "title": f"World Model近期拦截{blocked}次, 可能需校准"})

    required_modules = {
        "aios_bus.py": "总线", "aios_dispatcher.py": "调度器",
        "aios_executor_daemon.py": "执行器", "aios_agent_mesh.py": "Agent注册",
        "aios_entry_feishu.py": "飞书入口", "aios_entry_telegram.py": "电报入口",
        "aios_metadata_pipeline.py": "元数据管线", "aios_model_gateway.py": "模型网关",
        "aios_gateway_server.py": "网关服务", "aios_standby_daemon.py": "热备守护",
        "aios_web.py": "TaskConsole", "aios_monitor.py": "监控中心",
        "aios_contract_enforcer.py": "合约执行", "aios_enforcer.py": "执行管线",
        "aios_enforcer_daemon.py": "合约守护", "aios_hermes_learn.py": "学习引擎",
        "aios_self_diagnose.py": "自诊断", "aios_firewall.py": "防火墙",
        "aios_context_reservoir.py": "上下文水库", "aios_observability.py": "可观测性",
        "world_model_runner.py": "世界模型", "verify.py": "验证门禁",
        "claude_bridge.py": "Claude桥接", "aios_codex_proxy.py": "Codex代理",
        "aios_semantic_search.py": "语义搜索", "aios_agent_supervisor.py": "Agent监控",
    }
    found_all = True
    for mod, label in required_modules.items():
        if not (TOOLS / mod).exists():
            issues.append({"severity": "warning", "category": "compliance",
                            "title": f"缺少模块: {label}({mod})"})
            found_all = False
    if found_all:
        issues.append({"severity": "info", "category": "compliance",
                        "title": f"设计合规: {len(required_modules)}个核心模块全部存在"})

    # 10. 自主性评分: 检查各执行器自主状态
    try:
        from aios_proactivity_tracker import calculate_score, get_reward
        for _pn in ["hermes", "openclaw", "opencode", "claude", "codex"]:
            _sr = get_reward(_pn)
            _lv = _sr.get("level", "unknown")
            if _lv == "dormant":
                issues.append({"severity": "warning", "category": "autonomy",
                                "title": f"{_pn} 自主性休眠(score={_sr['score']}), 需强制复盘"})
            elif _lv == "passive":
                issues.append({"severity": "info", "category": "autonomy",
                                "title": f"{_pn} 自主性被动(score={_sr['score']}), 限制任务并发"})
    except Exception:
        pass

    try:
        from aios_enforcer import is_system_halted
        if is_system_halted():
            issues.append({"severity": "critical", "category": "halt",
                            "title": "系统处于HALT熔断状态, 需手动 clear_system_halt()"})
    except Exception:
        pass

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "total_issues": len(issues),
        "critical": sum(1 for i in issues if i["severity"] == "critical"),
        "warnings": sum(1 for i in issues if i["severity"] == "warning"),
        "issues": issues,
    }


def auto_fix_issues(diagnosis: dict) -> int:
    """自动修复可自动处理的问题, 不能自动修的生成任务入队."""
    fixed = 0
    for issue in diagnosis["issues"]:
        auto = issue.get("auto_fix")
        if auto == "cleanup_stale_queue":
            n = cleanup_stale_queue(24)
            if n > 0: fixed += 1
            print(f"  🔧 清理{n}个僵尸任务")
        elif auto == "set_enforce_mode":
            from aios_firewall import set_mode
            set_mode("enforce")
            fixed += 1
            print(f"  🔧 防火墙→enforce")
        elif auto == "release_stale_locks":
            # 释放超过1h的死锁
            if _is_available():
                import redis as r
                c = r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
                for key in c.scan_iter("aios:bus:lock:*"):
                    ttl = c.ttl(key)
                    if ttl and ttl < 0:  # 无过期时间的锁
                        c.delete(key); fixed += 1
            print(f"  🔧 释放死锁")
        else:
            # 不能自动修 → 生成任务入队
            tid = enqueue_task(
                task_name=f"[自诊断] {issue['title']}",
                system="aios",
                priority=1 if issue["severity"] == "critical" else 2,
                logic_depth="low",
                source="self_diagnose",
                context=json.dumps(issue, ensure_ascii=False)[:500],
            )
            if tid:
                print(f"  📋 入队修复任务: {issue['title'][:50]} → {tid[:8]}...")
                fixed += 1
    return fixed


def send_all_heartbeats():
    """每分钟心跳+记录token预估."""
    for s, model, est_tokens in [("hermes","MiniMax-M3",0), ("openclaw","MiniMax-M3",0),
                                   ("opencode","none",0), ("claude","deepseek-v4-pro",2000),
                                   ("codex","deepseek-v4-pro",0)]:
        heartbeat(s)
        if est_tokens > 0:
            # 预估每分钟token消耗 (实际应由AI自己记录)
            pass  # record_token_usage 在AI实际调用时记录

def proactive_listen(timeout_seconds: int = 300):
    """
    Proactive Agent: 监听Event Bus, 检测到关键事件立即触发诊断。
    不等人催 — 系统自己发现异常就行动。
    """
    print(f"👂 Proactive Agent 启动 (监听{timeout_seconds}s)...")
    for s in ["hermes", "openclaw", "opencode", "claude", "codex"]:
        heartbeat(s)

    start = time.time()
    while time.time() - start < timeout_seconds:
        events = subscribe_events(timeout=10.0)
        for e in events:
            etype = e.get("type", "")
            # 关键事件 → 立即诊断
            if any(t in etype for t in ["failed", "violation", "critical", "halt", "offline"]):
                print(f"\n⚡ 检测到关键事件: {etype} → 立即诊断")
                diagnosis = diagnose_all()
                auto_fix_issues(diagnosis)
                publish_event("task.completed", {"type": "proactive_fix", "trigger": etype,
                              "issues": diagnosis["total_issues"]}, "proactive_agent")
    print("👂 Proactive Agent 周期结束")


def run(apply_fixes: bool = False):
    """Run diagnosis. Mutations require the explicit --fix flag."""
    print("=" * 50)
    print(f"  AIOS 自探索诊断 — {datetime.now().isoformat()}")
    print("=" * 50)

    # 初始化
    init_core_dependencies()
    if apply_fixes:
        cleanup_stale_queue(24)

    # 诊断
    diagnosis = diagnose_all()
    print(f"\n发现 {diagnosis['total_issues']} 个问题 "
          f"({diagnosis['critical']}严重, {diagnosis['warnings']}警告)")

    for i, issue in enumerate(diagnosis["issues"], 1):
        icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(issue["severity"], "⚪")
        print(f"  {icon} [{issue['category']}] {issue['title']}")

    # 修复
    fixed = auto_fix_issues(diagnosis) if apply_fixes and diagnosis["issues"] else 0
    mode = "修复" if apply_fixes else "只读报告"
    print(f"\n{mode}: {fixed}/{diagnosis['total_issues']}")

    # 发布事件
    publish_event("task.completed" if diagnosis["total_issues"] == 0 else "alert.warning",
                  {"type": "self_diagnose", "issues": diagnosis["total_issues"],
                   "fixed": fixed, "mode": "fix" if apply_fixes else "report"}, "aios")

    # 保存报告
    report_dir = Path(AIOS_HOME) / "logs" / "diagnosis"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"diagnose_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_file.write_text(json.dumps({"diagnosis": diagnosis, "fixed": fixed},
                                       ensure_ascii=False, indent=2))
    print(f"📋 报告: {report_file}")

    return diagnosis


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--listen":
        proactive_listen(int(sys.argv[2]) if len(sys.argv) > 2 else 300)
    else:
        run(apply_fixes="--fix" in sys.argv[1:])
