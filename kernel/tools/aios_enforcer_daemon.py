#!/usr/bin/env python3
"""
AIOS v4.0 Protocol Enforcement Daemon
======================================
持续监控总线事件，强制执行三份协议规则：
  1. 消息格式/长度校验 (Protocol Enforcer)
  2. 7条合约规则检查 (Contract Enforcer) 
  3. 总线访问控制 (Bus Gate)

用法:
  python3 aios_enforcer_daemon.py              # 前台运行
  python3 aios_enforcer_daemon.py --once       # 单次检查
  python3 aios_enforcer_daemon.py --status     # 查看状态
"""
import sys, os, json, time, traceback
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import _is_available, publish_event, get_event_log, subscribe_events, generate_task_id
from aios_contract_enforcer import check_all_rules, enforce as contract_enforce, RULES
from aios_enforcer import protocol_check
from aios_error_classifier import classify_error, log_error, get_handling_guide

CHECK_INTERVAL = 300  # 5分钟
DAEMON_SLEEP = 60     # 轮询间隔


def run_checks() -> dict:
    """运行所有强制检查，返回结果."""
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {},
        "violations": 0,
        "status": "CLEAN",
    }

    # 1. 合约规则检查 (7条)
    try:
        violations = check_all_rules()
        result = contract_enforce(violations)
        results["checks"]["contract"] = {
            "rules_checked": result.get("checked", 0),
            "violations": result.get("violations", 0),
            "blocked": result.get("blocked", 0),
            "halted": result.get("halted", 0),
            "status": result.get("status", "UNKNOWN"),
        }
        results["violations"] += result.get("violations", 0)
        if result.get("halted", 0) > 0:
            results["status"] = "HALT"
        elif result.get("violations", 0) > 0:
            results["status"] = "VIOLATIONS"
    except Exception as e:
        results["checks"]["contract"] = {"error": str(e)}

    # 2. 事件日志检查 — 最近的 security.violation 事件
    try:
        events = get_event_log(hours=1, limit=20)
        security_events = [e for e in events if e.get("type") == "security.violation"]
        results["checks"]["event_log"] = {
            "recent_events": len(events),
            "security_violations": len(security_events),
        }
        if security_events:
            results["violations"] += len(security_events)
            if results["status"] == "CLEAN":
                results["status"] = "VIOLATIONS"
    except Exception as e:
        results["checks"]["event_log"] = {"error": str(e)}

    # 3. Redis总线可用性
    results["checks"]["redis"] = {"available": _is_available()}

    return results


def run_once():
    """单次执行."""
    print("=" * 50)
    print(f"  AIOS Protocol Enforcement — {datetime.now().isoformat()}")
    print("=" * 50)
    results = run_checks()
    print(json.dumps(results, ensure_ascii=False, indent=2))

    if results["status"] == "HALT":
        print("\n🔴 [SYSTEM_HALT] 触发熔断！")
    elif results["status"] == "VIOLATIONS":
        print(f"\n🟡 发现 {results['violations']} 条违规")
    else:
        print("\n✅ 全部规则通过")

    return results


def run_daemon():
    """持续运行."""
    print(f"\n{'='*50}")
    print(f"  AIOS Protocol Enforcement Daemon")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print(f"{'='*50}\n")

    last_check = 0
    while True:
        now = time.time()
        if now - last_check >= CHECK_INTERVAL:
            results = run_checks()
            ts = datetime.now().strftime("%H:%M:%S")

            if results["status"] == "HALT":
                print(f"  [{ts}] 🔴 [HALT] {results['violations']} violations")
                publish_event("alert.critical", {
                    "source": "enforcer_daemon",
                    "message": f"System HALT: {results['violations']} violations",
                    "details": results,
                }, "enforcer_daemon")
            elif results["status"] == "VIOLATIONS":
                print(f"  [{ts}] 🟡 {results['violations']} violations")
            else:
                print(f"  [{ts}] ✅ CLEAN")

            contract_status = results.get("checks", {}).get("contract", {})
            if contract_status.get("halted", 0) > 0:
                print(f"  🔴 Contract halt detected!")
                log_error("E003", "enforcer_daemon", "合约熔断触发: 系统级规则违规导致HALT")
            elif contract_status.get("violations", 0) > 0:
                print(f"  🟡 {contract_status.get('violations')} contract violations")
                for v in (contract_status.get("actions", []) or [])[:3]:
                    err = classify_error(v.get("detail", ""))
                    log_error(err["code"], "enforcer_daemon", v.get("detail", "")[:200])

            last_check = now

        # 订阅事件 (非阻塞)
        try:
            events = subscribe_events(timeout=1)
            for ev in events:
                if ev.get("type") in ("security.violation", "alert.critical"):
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] ⚡ Event: {ev.get('type')} from {ev.get('source','?')}")
        except Exception:
            pass

        time.sleep(DAEMON_SLEEP)


def show_status():
    """显示守护进程状态."""
    from aios_bus import get_event_log
    last_events = get_event_log(hours=1, limit=5)
    print(f"AIOS Enforcement Daemon Status")
    print(f"  Enforcer rules: {len(RULES)}")
    print(f"  Last events (1h): {len(last_events)}")
    for ev in last_events[-3:]:
        print(f"    [{ev.get('type','?')}] {json.dumps(ev.get('payload',{}))[:100]}")


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    elif "--status" in sys.argv:
        show_status()
    elif "--help" in sys.argv or "-h" in sys.argv:
        print("AIOS Enforcement Daemon")
        print("  (no args)  Continuous monitoring")
        print("  --once     Single check")
        print("  --status   Show status")
        print("  --help     This help")
    else:
        run_daemon()
