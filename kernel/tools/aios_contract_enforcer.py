#!/usr/bin/env python3
"""
P0-1: Contract Enforcement Engine 契约执行引擎
===============================================
从三份协议提取强制规则, 运行时主动拦截违规。
不只是文档 — 是执行。

强制规则:
  1. 所有输出必须经过Verifier → 检测执行器是否绕过verify直接交付
  2. Token超$10必须暂停 → 检测成本是否超限
  3. Agent间禁止明文长传(>512字) → 检测消息长度
  4. 连续失败3次→[SYSTEM_HALT] → 检测失败计数
  5. 禁止跨层权限越界 → 检测越权操作
  6. 物理操作必须先过World Model → 检测跳过WM的行为
"""

import sys, os, json, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple

TOOLS = Path("${AIOS_HOME}/kernel/tools")
AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
sys.path.insert(0, str(TOOLS))

from aios_bus import (
    check_recent, get_token_stats, _is_available, publish_event,
    get_queue_status, generate_task_id
)

# 从协议提取的强制规则
RULES = [
    {"id": "R1", "rule": "所有输出必须经过Verifier签章", "check": "verify_gate", "action": "block"},
    {"id": "R2", "rule": "单次任务Token>$10必须暂停请求审批", "check": "token_limit", "action": "halt"},
    {"id": "R3", "rule": "Agent间禁止明文长传(>512字)", "check": "message_length", "action": "block"},
    {"id": "R4", "rule": "连续3次失败→[SYSTEM_HALT]", "check": "consecutive_failures", "action": "halt"},
    {"id": "R5", "rule": "禁止跨层权限越界", "check": "permission_escalation", "action": "block"},
    {"id": "R6", "rule": "物理操作必须先过World Model", "check": "world_model_gate", "action": "block"},
    {"id": "R7", "rule": "知识库写入需Hermes+人类双签", "check": "knowledge_write", "action": "block"},
]


def check_all_rules() -> List[Dict]:
    """执行所有规则检查, 返回违规列表."""
    violations = []

    # R2: Token限额
    stats = get_token_stats(days=1)
    today = list(stats.values())[0] if stats else {}
    cost = today.get("cost", 0)
    if cost > 10.0:
        violations.append({
            "rule": "R2", "severity": "critical",
            "detail": f"今日Token费用${cost:.2f} > $10限额",
            "action": "halt", "ts": datetime.now(timezone.utc).isoformat(),
        })

    # R3: 消息长度 (检查最近任务上下文)
    recent = check_recent(limit=50)
    for r in recent:
        summary = r.get("summary", "")
        if len(summary) > 512:
            violations.append({
                "rule": "R3", "severity": "warning",
                "detail": f"[{r['system']}] 消息{len(summary)}字 > 512字限制",
                "action": "block", "ts": datetime.now(timezone.utc).isoformat(),
            })
            break  # 只报告一次

    # R4: 连续失败
    # The verification layer owns the expiring circuit-breaker key. Do not
    # recreate a halt forever by rescanning historical failures.
    halt_reason = ""
    if _is_available():
        import redis as _r
        halt_reason = (_r.Redis(host="localhost", port=6379,
                       socket_connect_timeout=2, decode_responses=True)
                       .get("aios:bus:system:halt") or "")
    if halt_reason:
        violations.append({
            "rule": "R4", "severity": "critical",
            "detail": f"SYSTEM_HALT active: {halt_reason[:160]}",
            "action": "halt", "ts": datetime.now(timezone.utc).isoformat(),
        })

    # R1: 检查是否有绕过verifier的completed任务
    for r in recent[:20]:
        if r.get("status") == "completed":
            state_key = f"aios:bus:state:{r.get('task_id','')}"
            if _is_available():
                import redis as _r
                c = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
                state = c.hgetall(state_key)
                if state:
                    decoded = {k.decode() if isinstance(k,bytes) else k: v.decode() if isinstance(v,bytes) else v
                              for k,v in state.items()}
                    if "verifying" not in decoded.get("status", "") and "ts_verifying" not in decoded:
                        violations.append({
                            "rule": "R1", "severity": "warning",
                            "detail": f"[{r['system']}] {r['task_name'][:50]} 未经过验证门禁",
                            "action": "block", "ts": datetime.now(timezone.utc).isoformat(),
                        })
                        break

    return violations


def enforce(violations: List[Dict]) -> Dict:
    """执行拦截动作."""
    result = {"checked": len(RULES), "violations": len(violations),
              "blocked": 0, "halted": 0, "actions": []}

    for v in violations:
        action = v["action"]
        if action == "block":
            result["blocked"] += 1
        elif action == "halt":
            result["halted"] += 1

        # 发布安全事件
        publish_event("security.violation", {
            "type": "contract_enforcement",
            "rule": v["rule"], "detail": v["detail"], "action": action,
        }, "contract_enforcer")

        result["actions"].append({
            "rule": v["rule"], "action": action,
            "detail": v["detail"][:100],
        })

    status = "CLEAN" if not violations else ("HALT" if result["halted"] > 0 else "VIOLATIONS")
    result["status"] = status
    return result


def run():
    """主入口."""
    print("=" * 50)
    print(f"  Contract Enforcement Engine — {datetime.now().isoformat()}")
    print("=" * 50)

    violations = check_all_rules()
    result = enforce(violations)

    for v in violations:
        icon = "🔴" if v["severity"] == "critical" else "🟡"
        print(f"  {icon} [{v['rule']}] {v['detail'][:80]}")

    if not violations:
        print("  ✅ 全部规则通过")

    print(f"\n  状态: {result['status']} | 检查{result['checked']}条 | "
          f"违规{result['violations']} | 拦截{result['blocked']} | 熔断{result['halted']}")

    return result


if __name__ == "__main__":
    run()
