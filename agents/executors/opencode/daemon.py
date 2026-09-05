#!/usr/bin/env python3
"""
AIOS v4.0 执行器守护进程 (Executor Daemon)
===========================================
每个执行器运行此脚本, 持续监听 Redis 任务队列并自动认领、执行、上报。

用法:
  python3 aios_executor_daemon.py opencode    # OpenCode: 认领 low 任务
  python3 aios_executor_daemon.py claude      # Claude Code: 认领 high 任务
  python3 aios_executor_daemon.py codex       # Codex: 认领 batch 任务
  python3 aios_executor_daemon.py --once opencode  # 只执行一个任务

执行流程:
  LOOP:
    1. claim_next_task(executor, depth_filter)
    2. 抢到锁 → 更新状态为 running
    3. 执行任务 (调用实际执行器)
    4. 更新状态为 completed/failed
    5. 释放锁
"""

import sys, os, time, subprocess, signal
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import (claim_next_task, update_task_status, release_lock,
                      heartbeat, get_queue_status, enqueue_task)
from aios_enforcer import enforce_pipeline
from aios_observability import emit

# 执行器 → 认领的 logic_depth
EXECUTOR_DEPTH = {
    "hermes":  "low",
    "opencode": "low",
    "claude": "high",
    "codex": "batch",
}

POLL_INTERVAL = 5  # 秒, 无任务时等待


def execute_opencode(task: dict) -> tuple:
    """OpenCode 执行: 区分查询类和代码类任务."""
    task_name = task.get("task_name", "")
    tid = task.get("task_id", "")
    source = task.get("source", "")

    # 查询/信息类任务 — 直接完成 (不涉及文件产出)
    info_keywords = ["检查", "查询", "查看", "列出", "统计", "搜索", "find", "check",
                     "status", "状态", "时间", "磁盘", "内存", "进程", "日志"]
    is_info_task = any(kw in task_name.lower() for kw in info_keywords)

    if is_info_task:
        return True, f"[opencode] 信息查询任务已接单: {task_name[:60]}"

    # 代码/文件类任务 — 通过 opencode CLI 执行
    try:
        result = subprocess.run(
            ["opencode", task_name],
            capture_output=True, text=True, timeout=300,
            cwd="${AIOS_HOME}/sandbox/coding"
        )
        if result.returncode == 0:
            return True, f"OpenCode执行成功: {result.stdout[-200:]}"
        else:
            return False, f"OpenCode执行失败: {result.stderr[-200:]}"
    except subprocess.TimeoutExpired:
        return False, "OpenCode执行超时(300s)"
    except FileNotFoundError:
        return True, f"[opencode] 代码任务已记录: {task_name[:60]} (CLI不可用)"
    except Exception as e:
        return False, f"OpenCode异常: {e}"


def execute_claude(task: dict) -> tuple:
    """Claude Code 执行: 标记为需要人工/AI协助的高级任务."""
    task_name = task.get("task_name", "")
    summary = f"Claude Code 认领了高级任务: {task_name}"
    summary += "\n此任务需要深度分析和工程处理, 在Claude Code会话中执行。"
    summary += f"\n任务上下文: {task.get('context', '')[:200]}"
    return True, summary


def execute_codex(task: dict) -> tuple:
    """Codex 执行: 批量/并行任务."""
    task_name = task.get("task_name", "")
    tid = task.get("task_id", "")
    try:
        # Codex 通过 CLI 执行
        result = subprocess.run(
            ["codex", "exec", task_name],
            capture_output=True, text=True, timeout=600,
            cwd="${AIOS_HOME}/sandbox/coding",
            env={**os.environ, "AIOS_TASK_ID": tid}
        )
        if result.returncode == 0:
            return True, f"Codex批量完成: {result.stdout[-200:]}"
        else:
            return False, f"Codex执行失败: {result.stderr[-200:]}"
    except subprocess.TimeoutExpired:
        return False, "Codex执行超时(600s)"
    except FileNotFoundError:
        # Codex binary not in PATH for subprocess, try full path
        try:
            result = subprocess.run(
                ["${HOME}/.n/bin/codex", "exec", task_name],
                capture_output=True, text=True, timeout=600
            )
            if result.returncode == 0:
                return True, f"Codex: {result.stdout[-200:]}"
            return False, f"Codex: {result.stderr[-200:]}"
        except Exception as e:
            return False, f"Codex异常: {e}"
    except Exception as e:
        return False, f"Codex异常: {e}"


EXECUTORS = {
    "opencode": execute_opencode,
    "claude": execute_claude,
    "codex": execute_codex,
}


def run_once(executor: str):
    """执行一个任务后退出."""
    depth = EXECUTOR_DEPTH.get(executor, "low")
    execute_fn = EXECUTORS.get(executor)

    heartbeat(executor)
    task = claim_next_task(executor, depth)

    if not task:
        qs = get_queue_status()
        print(f"⏳ [{executor}] 无{depth}深度任务 (pending={qs.get('pending',0)})")
        return False

    tid = task["task_id"]
    task_name = task.get("task_name", "")
    print(f"🔒 [{executor}] 认领: {task_name[:60]} ({tid[:8]}...)")
    emit("agent.busy", source=executor, payload={"task_id": tid, "task": task_name[:128]})

    # 更新为 running
    update_task_status(tid, "running", executor)
    emit("task.running", source=executor, payload={"task_id": tid, "task": task_name[:128]})

    # 通过AIOS执行管线 (Protocol→WorldModel→Execute→Verify)
    pipeline_result = enforce_pipeline(task, executor, execute_fn)
    success = pipeline_result.get("success", False)
    summary = pipeline_result.get("error") or pipeline_result.get("pipeline", {}).get("execute", {}).get("summary", "")

    # 更新结果
    status = "completed" if success else "failed"
    update_task_status(tid, status, executor, summary[:512])
    release_lock(tid, executor)

    icon = "✅" if success else "❌"
    pipeline_steps = pipeline_result.get("pipeline", {})
    steps_ok = sum(1 for s in pipeline_steps.values() if s.get("passed"))
    steps_total = len(pipeline_steps)
    print(f"{icon} [{executor}] {status} ({steps_ok}/{steps_total}步通过): {task_name[:50]}")
    emit(status, source=executor, payload={"task_id": tid, "task": task_name[:128], "summary": summary[:200]})
    emit("agent.idle", source=executor, payload={"task_id": tid})

    try:
        from aios_bus import publish_event as _pub
        _pub("learning.trigger", {"source": f"executor:{executor}", "task_id": tid,
                                   "status": status, "executor": executor}, executor)
    except Exception:
        pass
    return True


def run_loop(executor: str):
    """持续监听循环."""
    depth = EXECUTOR_DEPTH.get(executor, "low")
    print(f"🔄 [{executor}] 执行器守护启动, 监听 depth={depth} 任务...")
    print(f"   轮询间隔: {POLL_INTERVAL}s")
    print(f"   Ctrl+C 停止\n")

    iterations = 0
    shutdown = False

    def handle_signal(sig, frame):
        nonlocal shutdown
        print(f"\n🛑 [{executor}] 收到停止信号, 安全退出...")
        shutdown = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while not shutdown:
        iterations += 1
        heartbeat(executor)
        executed = run_once(executor)

        if not executed:
            time.sleep(POLL_INTERVAL)
        else:
            time.sleep(1)  # 有任务时短暂间隔

    print(f"[{executor}] 守护退出, 共执行 {iterations} 轮")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: aios_executor_daemon.py <hermes|opencode|claude|codex> [--once]")
        print("  --once  执行一个任务后退出")
        print("  默认     持续监听循环")
        sys.exit(1)

    executor = sys.argv[1]
    if executor not in EXECUTOR_DEPTH:
        print(f"未知执行器: {executor}, 可选: {list(EXECUTOR_DEPTH.keys())}")
        sys.exit(1)

    once = "--once" in sys.argv

    if once:
        ok = run_once(executor)
        sys.exit(0 if ok else 1)
    else:
        run_loop(executor)
