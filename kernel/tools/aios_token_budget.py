#!/usr/bin/env python3
"""
Token Budget Circuit Breaker — TOKEN预算熔断
=============================================
每任务前检查预算: >80%告警, >100%拒绝, >$10暂停
"""
import sys, os, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import get_token_stats, publish_event, _is_available, generate_task_id

# 预算配置
DAILY_BUDGET_TOKENS = 1_000_000   # 每日100万token上限
DAILY_BUDGET_COST = 10.0           # 每日$10上限
PER_TASK_BUDGET = 100_000          # 单任务10万token
WARN_THRESHOLD = 0.8               # 80%告警
REJECT_THRESHOLD = 1.0             # 100%拒绝

KEY_BUDGET = "aios:governance:budget"


def get_daily_usage() -> dict:
    """获取今日用量."""
    stats = get_token_stats(1)
    today = list(stats.values())[0] if stats else {}
    return {
        "tokens": today.get("tokens", 0),
        "cost": today.get("cost", 0.0),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def check_budget(task_name: str = "", estimated_tokens: int = 0) -> dict:
    """
    预算检查 — 每任务前调用。
    Returns: {allowed: bool, reason: str, usage_pct: float}
    """
    usage = get_daily_usage()
    tokens_used = usage["tokens"]
    cost_used = usage["cost"]

    token_pct = tokens_used / DAILY_BUDGET_TOKENS if DAILY_BUDGET_TOKENS > 0 else 0
    cost_pct = cost_used / DAILY_BUDGET_COST if DAILY_BUDGET_COST > 0 else 0
    max_pct = max(token_pct, cost_pct)

    # 熔断判断
    if max_pct >= REJECT_THRESHOLD:
        reason = f"预算耗尽: token={token_pct:.0%} cost=${cost_used:.2f}/${DAILY_BUDGET_COST}"
        action = "reject"
        allowed = False
    elif max_pct >= WARN_THRESHOLD:
        reason = f"预算告警: {max_pct:.0%} (token={tokens_used:,}, cost=${cost_used:.2f})"
        action = "warn"
        allowed = True
    elif estimated_tokens > PER_TASK_BUDGET:
        reason = f"单任务超限: 预估{estimated_tokens:,} > {PER_TASK_BUDGET:,}"
        action = "reject_task_too_large"
        allowed = False
    else:
        reason = f"正常: {max_pct:.0%} (剩余token={DAILY_BUDGET_TOKENS - tokens_used:,})"
        action = "allow"
        allowed = True

    result = {
        "allowed": allowed, "action": action, "reason": reason,
        "usage": {"tokens": tokens_used, "cost": cost_used,
                  "token_pct": round(token_pct, 3), "cost_pct": round(cost_pct, 3)},
        "limits": {"daily_tokens": DAILY_BUDGET_TOKENS, "daily_cost": DAILY_BUDGET_COST,
                   "per_task": PER_TASK_BUDGET},
        "task": task_name[:80], "ts": datetime.now(timezone.utc).isoformat(),
    }

    # 记录到Redis
    if _is_available():
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        r.hset(KEY_BUDGET, "last_check", json.dumps(result, ensure_ascii=False))
        r.hset(KEY_BUDGET, "daily_tokens", str(tokens_used))
        r.hset(KEY_BUDGET, "daily_cost", str(cost_used))
        r.hset(KEY_BUDGET, "usage_pct", str(round(max_pct, 3)))

    if not allowed:
        publish_event("alert.critical", {
            "type": "budget_rejected", "task": task_name[:80],
            "usage_pct": round(max_pct, 3), "action": action,
        }, "token_budget")

    return result


def budget_gate(task_name: str, estimated_tokens: int = 0) -> bool:
    """简化接口: 返回True=放行, False=拒绝."""
    return check_budget(task_name, estimated_tokens).get("allowed", False)


def get_budget_status() -> dict:
    """获取预算状态(供Dashboard展示)."""
    usage = get_daily_usage()
    return {
        "daily_tokens": usage["tokens"], "daily_cost": usage["cost"],
        "token_limit": DAILY_BUDGET_TOKENS, "cost_limit": DAILY_BUDGET_COST,
        "token_pct": round(usage["tokens"] / DAILY_BUDGET_TOKENS * 100, 1) if DAILY_BUDGET_TOKENS > 0 else 0,
        "cost_pct": round(usage["cost"] / DAILY_BUDGET_COST * 100, 1) if DAILY_BUDGET_COST > 0 else 0,
        "status": "rejected" if usage["tokens"] >= DAILY_BUDGET_TOKENS else (
            "warning" if usage["tokens"] >= DAILY_BUDGET_TOKENS * WARN_THRESHOLD else "normal"),
    }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        r = check_budget(" ".join(sys.argv[2:]) if len(sys.argv) > 2 else "测试任务")
        print(f"{'✅' if r['allowed'] else '🚫'} {r['action']}: {r['reason']}")
    elif cmd == "status":
        print(json.dumps(get_budget_status(), ensure_ascii=False, indent=2))
    elif cmd == "gate":
        ok = budget_gate("测试", 5000)
        print(f"{'✅ 放行' if ok else '🚫 拒绝'}")
