#!/usr/bin/env python3
"""P2: Consensus Arbiter — 多Agent死锁仲裁+权重排名+交人决策"""

import sys, json, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import (
    _is_available, _redis_client, KEY_PREFIX, get_queue_status, check_recent,
    list_registered_executors, publish_event,
)

KEY_ARBITER = f"{KEY_PREFIX}:arbiter"          # 仲裁结果记录
KEY_WEIGHT = f"{KEY_PREFIX}:arbiter:weight"    # Agent权重 Hash

# 默认权重 (低=高优先级)
DEFAULT_WEIGHTS = {
    "hermes":    1,   # 最高
    "openclaw":  2,
    "codex":     3,
    "claude":    4,
    "opencode":  5,
}


def get_agent_weight(agent: str) -> int:
    """获取Agent权重 (越小越优先)."""
    if not _is_available():
        return DEFAULT_WEIGHTS.get(agent, 99)
    try:
        raw = _redis_client.hget(KEY_WEIGHT, agent)
        if raw is not None:
            return int(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        pass
    return DEFAULT_WEIGHTS.get(agent, 99)


def set_agent_weight(agent: str, weight: int):
    """动态设置Agent权重."""
    if not _is_available():
        return
    try:
        _redis_client.hset(KEY_WEIGHT, agent, str(weight))
    except Exception:
        pass


def get_success_rate(agent: str, hours: int = 24) -> float:
    """查询Agent最近成功率."""
    recent = check_recent(system=agent, hours=hours, limit=50)
    if not recent:
        return 0.5
    total = len(recent)
    completed = sum(1 for r in recent if r.get("status") == "completed")
    return completed / total if total > 0 else 0.5


def rank_agents() -> list:
    """按权重+成功率对所有Agent排序. 返回 [(agent, weight, success_rate), ...]."""
    weights = dict(DEFAULT_WEIGHTS)
    if _is_available():
        try:
            raw = _redis_client.hgetall(KEY_WEIGHT)
            for k, v in raw.items():
                k_str = k.decode() if isinstance(k, bytes) else k
                weights[k_str] = int(v.decode() if isinstance(v, bytes) else v)
        except Exception:
            pass

    result = []
    for agent in list(DEFAULT_WEIGHTS.keys()):
        w = weights.get(agent, 99)
        sr = get_success_rate(agent)
        result.append((agent, w, sr))
    result.sort(key=lambda x: (x[1], -x[2]))  # 权重优先, 成功率次之
    return result


def detect_agent_contention(age_minutes: int = 5) -> list:
    """检测是否有多个Agent竞争同一任务 (相同task_id被多个agent锁定)."""
    if not _is_available():
        return []
    contentions = []
    try:
        # 扫描所有活跃锁
        lock_keys = _redis_client.keys(f"{KEY_PREFIX}:lock:*")
        lock_map = defaultdict(list)
        for key in lock_keys:
            raw = _redis_client.get(key)
            if raw:
                agent = raw.decode() if isinstance(raw, bytes) else raw
                tid = key.decode().split(":")[-1] if isinstance(key, bytes) else key.split(":")[-1]
                lock_map[tid].append(agent)

        # 找到被多个Agent同时锁定的任务
        for tid, agents in lock_map.items():
            unique = list(set(agents))
            if len(unique) >= 2:
                contentions.append({
                    "task_id": tid,
                    "agents": unique,
                    "type": "lock_contention",
                })

        # 检测长时间锁定的任务 (死锁)
        cutoff = time.time() - age_minutes * 60
        for tid, agents in lock_map.items():
            lock_key = f"{KEY_PREFIX}:lock:{tid}"
            try:
                ttl = _redis_client.ttl(lock_key)
                actual_age = (_redis_client.pttl(lock_key) if ttl < 0 else ttl)  # fallback
                if ttl and ttl < 0:
                    actual_age = -1
                # 检查任务是否已超时 (锁存在但age > age_minutes)
                age_info = _redis_client.object("idletime", lock_key) if hasattr(_redis_client, "object") else None
            except Exception:
                pass

    except Exception:
        pass
    return contentions


def detect_stuck_tasks(minutes: int = 15) -> list:
    """检测 stuck 在 pending/locked/running 状态的任务."""
    if not _is_available():
        return []
    stuck = []
    try:
        state_keys = _redis_client.keys(f"{KEY_PREFIX}:state:*")
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        for key in state_keys:
            try:
                raw = _redis_client.hgetall(key)
                if not raw:
                    continue
                state = {}
                for k, v in raw.items():
                    state[k.decode() if isinstance(k, bytes) else k] = (
                        v.decode() if isinstance(v, bytes) else v
                    )
                status = state.get("status", "")
                if status not in ("locked", "running", "pending", "queued"):
                    continue
                ts_str = state.get("ts_updated", "") or state.get("ts_created", "")
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.replace(tzinfo=timezone.utc) < cutoff.replace(tzinfo=timezone.utc):
                    tid = key.decode().split(":")[-1] if isinstance(key, bytes) else key.split(":")[-1]
                    stuck.append({
                        "task_id": tid,
                        "status": status,
                        "agent": state.get("executor", "?"),
                        "task_name": state.get("task_name", "")[:60],
                        "stuck_minutes": int((datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)).total_seconds() / 60),
                    })
            except Exception:
                pass
    except Exception:
        pass
    return stuck


def arbitrate_contention(contentions: list) -> list:
    """根据Agent权重仲裁争抢任务."""
    ranking = rank_agents()
    ranking_map = {a: {"rank": i, "weight": w, "success": sr} for i, (a, w, sr) in enumerate(ranking)}
    resolutions = []
    for c in contentions:
        ranked_agents = sorted(c["agents"], key=lambda a: ranking_map.get(a, {}).get("rank", 99))
        winner = ranked_agents[0] if ranked_agents else None
        if winner:
            msg = f"仲裁: {c['task_id'][:8]}... 由 {winner} (rank #{ranking_map.get(winner,{}).get('rank',99)+1}) 执行"
            resolutions.append({
                "task_id": c["task_id"],
                "agents": c["agents"],
                "winner": winner,
                "reason": msg,
                "resolution": "assign_to_winner",
            })
    return resolutions


def resolve_stuck(stuck_tasks: list) -> list:
    """解决stuck任务: 按仲裁结果重新入队."""
    if not stuck_tasks:
        return []
    ranking = rank_agents()
    ranking_map = {a: i for i, (a, w, sr) in enumerate(ranking)}
    resolved = []
    for t in stuck_tasks:
        current_agent = t.get("agent", "?")
        current_rank = ranking_map.get(current_agent, 99)
        # 找更高优先级的Agent
        best_agent = None
        best_rank = 99
        for agent, idx in ranking_map.items():
            if idx < current_rank and idx < best_rank:
                best_rank = idx
                best_agent = agent
        if best_agent:
            resolved.append({
                "task_id": t["task_id"],
                "from_agent": current_agent,
                "to_agent": best_agent,
                "task_name": t.get("task_name", ""),
                "resolution": "reassign",
                "reason": f"Agent {current_agent} stuck {t.get('stuck_minutes',0)}min, reassign to {best_agent}",
            })
        else:
            # 没有更高优先级Agent → 提升到human
            resolved.append({
                "task_id": t["task_id"],
                "from_agent": current_agent,
                "to_agent": "human",
                "task_name": t.get("task_name", ""),
                "resolution": "escalate_to_human",
                "reason": f"所有Agent均stuck, 需人工介入: {t.get('task_name','')}",
            })
    return resolved


def run_arbitration() -> dict:
    """完整仲裁周期: 检测争抢→权重仲裁→解决stuck→记录."""
    print(f"\n{'='*50}")
    print(f"  Consensus Arbiter — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*50}")

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_ranking": rank_agents(),
        "contentions": [],
        "stuck_resolved": [],
        "escalated_to_human": [],
    }

    # 1. 检测争抢
    contentions = detect_agent_contention()
    if contentions:
        print(f"  ⚔️ 检测到 {len(contentions)} 个争抢")
        resolutions = arbitrate_contention(contentions)
        report["contentions"] = resolutions
        for r in resolutions:
            print(f"    → {r.get('reason', '')}")
            _record(r.get("task_id", ""), r)
    else:
        print(f"  ✅ 无Agent争抢")

    # 2. 检测stuck任务
    stuck = detect_stuck_tasks(minutes=15)
    if stuck:
        print(f"  ⏰ 检测到 {len(stuck)} 个stuck任务")
        resolved = resolve_stuck(stuck)
        for r in resolved:
            if r["resolution"] == "escalate_to_human":
                report["escalated_to_human"].append(r)
                publish_event("alert.critical", {
                    "type": "arbiter_escalation",
                    "task_id": r["task_id"],
                    "reason": r["reason"],
                }, "consensus_arbiter")
                print(f"    🚨 升级到人: {r.get('task_name','')[:50]}")
            else:
                print(f"    🔄 {r['from_agent']}→{r['to_agent']}: {r.get('task_name','')[:50]}")
                from aios_bus import enqueue_task
                enqueue_task(f"[仲裁重分配-来自{r['from_agent']}] {r['task_name']}",
                             system=r["to_agent"], priority=1, logic_depth="batch",
                             source="consensus_arbiter")
            _record(r.get("task_id", ""), r)
        report["stuck_resolved"] = resolved
    else:
        print(f"  ✅ 无stuck任务")

    # 3. 保存报告
    report_dir = Path("${AIOS_HOME}/knowledge") / "arbitration_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"arbitration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"  📋 报告: {report_file}")

    return report


def _record(task_id: str, entry: dict):
    """记录仲裁结果到Redis."""
    if not _is_available() or not task_id:
        return
    try:
        key = f"{KEY_ARBITER}:{task_id[:30]}"
        _redis_client.hset(key, mapping={
            "ts": datetime.now(timezone.utc).isoformat(),
            "resolution": entry.get("resolution", ""),
            "reason": entry.get("reason", "")[:200],
        })
        _redis_client.expire(key, 7 * 86400)
    except Exception:
        pass


def force_arbitrate(task_id: str) -> dict:
    """强制对指定任务进行仲裁 (供手动调用)."""
    ranking = rank_agents()
    stuck = detect_stuck_tasks(minutes=1)
    match = [t for t in stuck if t["task_id"] == task_id]
    if match:
        resolved = resolve_stuck(match)
        for r in resolved:
            _record(task_id, r)
            return r
    return {"task_id": task_id, "resolution": "not_stuck", "reason": "任务未stuck"}


try:
    from aios_bus import register_pin
    register_pin("arbiter.run", run_arbitration, "Consensus Arbiter: 检测争抢+仲裁+交人决策")
    register_pin("arbiter.rank", rank_agents, "Consensus Arbiter: 查看Agent权重排名")
    register_pin("arbiter.force", force_arbitrate, "Consensus Arbiter: 强制仲裁指定任务")
except Exception:
    pass

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        print(json.dumps(run_arbitration(), ensure_ascii=False, indent=2, default=str))
    elif cmd == "rank":
        for a, w, sr in rank_agents():
            print(f"  {a}: weight={w}, success_rate={sr:.0%}")
    elif cmd == "stuck":
        for t in detect_stuck_tasks():
            print(f"  ⏰ {t['agent']}: {t['task_name'][:50]} ({t['stuck_minutes']}min)")
    elif cmd == "force" and len(sys.argv) > 2:
        print(json.dumps(force_arbitrate(sys.argv[2]), ensure_ascii=False, indent=2))
