#!/usr/bin/env python3
"""
Agent Supervisor — 监控所有Agent心跳, 自动重启
==============================================
每60s扫描 aios:agent:* 心跳:
  >90s   → 标记 OFFLINE
  >300s  → 触发自动重启事件
  >600s  → 从Registry移除(僵尸Agent)
"""
import sys, os, time, json
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import (lifecycle_get, lifecycle_set, lifecycle_list_all,
                      publish_event, heartbeat, _is_available)

HEARTBEAT_TIMEOUT = 90     # 超时→OFFLINE
RESTART_TIMEOUT = 300      # 超时→触发重启
ZOMBIE_TIMEOUT = 600       # 超时→移除


def scan_and_enforce():
    """扫描所有Agent, 执行超时动作."""
    if not _is_available():
        return {"error": "bus_unavailable"}

    agents = lifecycle_list_all()
    result = {"scanned": len(agents), "offline": [], "restart": [], "zombie": []}

    for a in agents:
        name = a.get("name", "unknown")
        age = a.get("heartbeat_age_s", 999)
        status = a.get("status", "UNKNOWN")

        if age > ZOMBIE_TIMEOUT and status == "OFFLINE":
            # 僵尸Agent → 移除
            if _is_available():
                import redis as _r
                r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
                r.delete(f"aios:bus:agent:{name}")
            result["zombie"].append(name)
            publish_event("agent.offline", {"agent": name, "reason": "zombie_timeout"}, "supervisor")

        elif age > RESTART_TIMEOUT and status in ("RUNNING", "OFFLINE"):
            # 超时 → 触发重启
            lifecycle_set(name, "ERROR")
            result["restart"].append(name)
            publish_event("alert.critical", {
                "type": "agent_restart", "agent": name,
                "age_s": age, "action": "trigger_restart",
            }, "supervisor")

        elif age > HEARTBEAT_TIMEOUT and status == "RUNNING":
            # 超时 → 标记OFFLINE
            lifecycle_set(name, "OFFLINE")
            result["offline"].append(name)
            publish_event("alert.warning", {
                "type": "agent_offline", "agent": name,
                "age_s": age,
            }, "supervisor")

    # 记录审计日志
    if _is_available():
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        r.zadd("aios:bus:agent:audit", {
            json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "scanned": len(agents),
                "offline": len(result["offline"]),
                "restart": len(result["restart"]),
            }, ensure_ascii=False): time.time()
        })

    return result


def run_supervisor(interval: int = 60):
    """持续运行Supervisor."""
    print(f"👁️  Agent Supervisor started (scan every {interval}s)")
    print(f"   OFFLINE > {HEARTBEAT_TIMEOUT}s | RESTART > {RESTART_TIMEOUT}s | ZOMBIE > {ZOMBIE_TIMEOUT}s")
    while True:
        try:
            result = scan_and_enforce()
            if result.get("scanned", 0) > 0:
                issues = len(result["offline"]) + len(result["restart"])
                if issues:
                    print(f"  ⚠️  {issues} issues: offline={result['offline']} restart={result['restart']}")
                else:
                    print(f"  ✅ {result['scanned']} agents healthy")
        except Exception as e:
            print(f"  ❌ scan error: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        result = scan_and_enforce()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        run_supervisor()
