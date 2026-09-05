#!/usr/bin/env python3
"""P2: 中循环 — 验证失败后自动重分析+换Agent+重新调度"""

import sys, json, time
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import (
    _is_available, _redis_client, KEY_PREFIX, publish_event,
    get_task_state, enqueue_task, generate_task_id, check_recent,
)
from aios_observability import emit

MAX_RETRIES = 3
KEY_RETRY = f"{KEY_PREFIX}:midloop:retry"  # aios:bus:midloop:retry:{task_id} → count
KEY_VERIFIED = f"{KEY_PREFIX}:midloop:verified"  # aios:bus:midloop:verified:{task_id} → ts


def _get_retry_count(task_id: str) -> int:
    """获取任务已重试次数."""
    if not _is_available():
        return 0
    try:
        raw = _redis_client.get(f"{KEY_RETRY}:{task_id}")
        if raw is not None:
            return int(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        pass
    return 0


def _increment_retry(task_id: str, ttl: int = 86400):
    """递增重试次数."""
    if not _is_available():
        return
    key = f"{KEY_RETRY}:{task_id}"
    try:
        count = _redis_client.incr(key)
        _redis_client.expire(key, ttl)
        return count
    except Exception:
        pass


def _record_verified(task_id: str):
    """记录任务已验证通过 (防止重复处理)."""
    if not _is_available():
        return
    try:
        _redis_client.setex(f"{KEY_VERIFIED}:{task_id}", 86400,
                            datetime.now(timezone.utc).isoformat())
    except Exception:
        pass


def _is_already_verified(task_id: str) -> bool:
    """检查任务是否已被中循环处理过."""
    if not _is_available():
        return False
    try:
        return _redis_client.exists(f"{KEY_VERIFIED}:{task_id}")
    except Exception:
        return False


def _pick_different_agent(current_agent: str, failed_task_name: str) -> str:
    """根据失败任务选择不同Agent."""
    fallback_map = {
        "opencode": "claude",
        "claude": "opencode",
        "codex": "opencode",
        "hermes": "opencode",
    }
    diff = fallback_map.get(current_agent)
    if diff:
        return diff
    # 如果当前Agent不在映射中, 尝试使用hermes_strategy
    try:
        from aios_bus import get_hermes_strategy
        strategy = get_hermes_strategy()
        fa = strategy.get("failure_analysis", {})
        if isinstance(fa, dict):
            affected = fa.get("systems_affected", [])
            if current_agent in affected:
                for candidate in ["claude", "opencode", "codex"]:
                    if candidate not in affected:
                        return candidate
    except Exception:
        pass
    return "opencode"


def _re_analyse(failed_task: dict) -> dict:
    """重新分析失败任务, 返回新调度参数."""
    task_name = failed_task.get("task_name", "")
    source = failed_task.get("source", "mid_loop")
    current_agent = failed_task.get("executor", failed_task.get("system", "opencode"))
    new_agent = _pick_different_agent(current_agent, task_name)
    # 调整任务名加前缀标记重试
    retry_count = _get_retry_count(failed_task.get("task_id", ""))
    return {
        "task_name": task_name,
        "new_agent": new_agent,
        "source": source,
        "retry_count": retry_count + 1,
        "logic_depth": "batch" if retry_count >= 2 else "low",
    }


def handle_verification_failed(task_id: str, error: str = "") -> dict:
    """处理验证失败的任务: 重分析→换Agent→重新入队."""
    if not task_id or _is_already_verified(task_id):
        return {"action": "skipped", "reason": "already_processed"}

    retry_count = _get_retry_count(task_id)
    if retry_count >= MAX_RETRIES:
        # 超过最大重试次数 → 交人决策
        publish_event("alert.critical", {
            "type": "midloop_max_retries",
            "task_id": task_id,
            "retries": retry_count,
            "error": error[:200],
        }, "mid_loop")
        _record_verified(task_id)
        return {
            "action": "escalate_to_human",
            "task_id": task_id,
            "retries": retry_count,
            "reason": f"超过最大重试次数 ({MAX_RETRIES})",
        }

    # 获取任务当前状态
    state = get_task_state(task_id)
    state["task_id"] = task_id
    task_name = state.get("task_name", "?")
    current_agent = state.get("executor", state.get("system", "?"))

    # 递增重试计数
    _increment_retry(task_id)

    # 重分析
    analysis = _re_analyse(state)
    new_agent = analysis["new_agent"]
    new_task_name = f"[重试#{retry_count + 1}] {task_name}"

    print(f"  🔄 mid-loop: {task_id[:8]}... {current_agent}→{new_agent} (retry #{retry_count + 1})")

    # 重新入队到不同Agent
    enqueued = enqueue_task(
        new_task_name,
        system=new_agent,
        priority=1,
        logic_depth=analysis["logic_depth"],
        source=f"mid_loop:{task_id[:12]}",
    )

    # 发布中循环事件
    emit("task.retry", source="mid_loop", payload={
        "task_id": task_id,
        "new_task_id": enqueued.get("task_id", "") if isinstance(enqueued, dict) else "",
        "from_agent": current_agent,
        "to_agent": new_agent,
        "retry_count": retry_count + 1,
        "error": error[:200],
    })

    publish_event("learning.trigger", {
        "source": "mid_loop",
        "task_id": task_id,
        "status": "retry",
        "executor": current_agent,
        "new_agent": new_agent,
    }, "mid_loop")

    return {
        "action": "re_enqueued",
        "task_id": task_id,
        "from_agent": current_agent,
        "to_agent": new_agent,
        "retry_count": retry_count + 1,
    }


def run_once() -> dict:
    """扫描一次所有验证失败的running任务并处理."""
    if not _is_available():
        return {"scanned": 0, "handled": 0}

    print(f"\n{'='*50}")
    print(f"  Mid-Loop 中循环 — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*50}")

    handled = []
    scanned = 0

    # 扫描最近任务中 verification_failed 标记的
    recent = check_recent(hours=24, limit=100)
    for r in recent:
        status = r.get("status", "")
        if status != "running":
            continue
        task_id = r.get("task_id", "")
        if not task_id or _is_already_verified(task_id):
            continue
        state = get_task_state(task_id)
        if not state.get("verification_failed"):
            continue

        scanned += 1
        result = handle_verification_failed(task_id,
                                            error=state.get("error", r.get("summary", "")))
        handled.append(result)
        print(f"    {result['action']}: {task_id[:8]}... → {result.get('to_agent', '?')}")

    # 也检查 event log 中未处理的 task.verification_failed
    try:
        from aios_bus import KEY_EVENT_LOG as _log_key
        cutoff = time.time() - 3600
        raw_events = _redis_client.zrevrangebyscore(_log_key, "+inf", cutoff, start=0, num=50)
        for e in raw_events:
            try:
                ev = json.loads(e if isinstance(e, str) else e.decode())
                if ev.get("type") != "task.verification_failed":
                    continue
                tid = ev.get("payload", {}).get("task_id", "")
                if tid and not _is_already_verified(tid):
                    state = get_task_state(tid)
                    if state.get("verification_failed"):
                        result = handle_verification_failed(tid, error=ev.get("payload", {}).get("error", ""))
                        handled.append(result)
            except Exception:
                pass
    except Exception:
        pass

    summary = {"scanned": scanned, "handled": len(handled)}
    print(f"  📊 {summary}")
    return summary


def run_loop():
    """持续监听循环."""
    print(f"\n{'='*50}")
    print(f"  AIOS Mid-Loop Daemon")
    print(f"  监听验证失败→自动重分析→换Agent→重新调度")
    print(f"  最大重试: {MAX_RETRIES}次")
    print(f"{'='*50}\n")

    while True:
        try:
            result = run_once()
            if result.get("handled", 0) > 0:
                print(f"  ✅ mid-loop 处理 {result['handled']} 个任务")
        except KeyboardInterrupt:
            print("\nShutting down...")
            break
        except Exception as e:
            print(f"  ⚠️ mid-loop error: {e}")
        time.sleep(30)


try:
    from aios_bus import register_pin
    register_pin("midloop.run", run_once, "中循环: 扫描验证失败任务→重分析→换Agent重新调度")
    register_pin("midloop.handle", handle_verification_failed,
                 "中循环: 处理指定验证失败任务 (task_id)")
    register_pin("midloop.daemon", run_loop, "中循环: 持续监听守护")
except Exception:
    pass

if __name__ == "__main__":
    if "--once" in sys.argv:
        print(json.dumps(run_once(), ensure_ascii=False, indent=2))
    else:
        run_loop()
