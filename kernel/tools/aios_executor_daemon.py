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

import sys, os, time, subprocess, signal, json, re, shutil
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_secure import (
    check_input_safety, looks_like_shell_command, safe_join_command,
    safe_log_exception,
)
from aios_bus import (claim_next_task, update_task_status, release_lock,
                       heartbeat, get_queue_status, enqueue_task, check_in_task,
                       sweep_stuck_claims, approval_is_valid)
from aios_enforcer import enforce_pipeline
from aios_observability import emit
from aios_metadata_pipeline import report as report_metadata
from aios_tool_adapter import get_adapter
from aios_runtime_revision import compute_executor_revision

# 执行器 → 认领的 logic_depth
EXECUTOR_DEPTH = {
    "hermes":  "low",
    "opencode": "low",
    "claude": "high",
    "codex": "batch",
}

POLL_INTERVAL = 5  # 秒, 无任务时等待
# Runtime revision covers direct + transitive imports so that modifications
# to e.g. aios_agent_mesh.py are detected by reconcile:runtime-loaded-revisions
# whenever the owning Executor daemon has not been restarted.
LOADED_REVISION = compute_executor_revision(TOOLS)


def execute_opencode(task: dict) -> tuple:
    """Execute OpenCode-assigned work through OpenCode's official Server API."""
    task_name = str(task.get("task_name", "")).strip()
    if not task_name:
        return False, "[opencode/blocked] empty task"

    ok, why = check_input_safety(task_name)
    if not ok:
        return False, f"[opencode/blocked] {why}"

    env = dict(os.environ)
    env["PATH"] = (
        "${HOME}/.n/bin:${HOME}/.local/bin:"
        "/usr/local/bin:/usr/bin:/bin"
    )
    try:
        result = subprocess.run(
            get_adapter("opencode").command_for_task(task_name),
            shell=False, capture_output=True, text=True, timeout=300,
            cwd="${AIOS_HOME}/sandbox/coding", env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "[opencode/server] task timed out after 300 seconds"
    except Exception as exc:
        return False, f"[opencode/server] {type(exc).__name__}: {exc}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        try:
            get_adapter("opencode").record_inference_failure(
                stderr or stdout, returncode=result.returncode,
            )
        except Exception as exc:
            safe_log_exception("opencode_health_failure_record", exc)
        return False, f"[opencode/server] {(stderr or stdout)[-4000:]}"
    if not stdout:
        try:
            get_adapter("opencode").record_inference_failure(
                "OpenCode returned an empty result", returncode=1,
            )
        except Exception as exc:
            safe_log_exception("opencode_health_empty_record", exc)
        return False, "[opencode/server] empty result"
    try:
        route_state = json.loads(Path(
            "${AIOS_HOME}/cache/opencode_model_router.json"
        ).read_text(encoding="utf-8"))
        selected = str(route_state.get("last_selected_model", "unknown"))
        get_adapter("opencode").record_inference_success(
            f"real task succeeded; selected_model={selected}",
        )
    except Exception as exc:
        safe_log_exception("opencode_health_success_record", exc)
    return True, f"[opencode] {stdout[-32768:]}"



# 受信任的白名单信息查询 (每个键对应一个 *固定* 的命令模板, 用户文本仅用于路由键)
def _run_tool_command(command: list[str], timeout: int, cwd: str, env: dict):
    """Run one tool in its own process group and reap every child on timeout."""
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        stderr = (stderr or "") + f"\nAIOS_HARD_TIMEOUT={timeout}s"
        return subprocess.CompletedProcess(command, 124, stdout or "", stderr)
    return subprocess.CompletedProcess(command, proc.returncode, stdout or "", stderr or "")


def execute_claude(task: dict) -> tuple:
    """Execute high-depth work through the registered Claude adapter."""
    task_name = str(task.get("task_name", "")).strip()
    task_id = str(task.get("task_id", ""))
    if not task_name:
        return False, "[claude/blocked] empty task"
    adapter = get_adapter("claude")
    env = dict(os.environ)
    env["AIOS_TASK_ID"] = task_id
    env["PATH"] = (
        "${HOME}/.n/bin:${HOME}/.local/bin:"
        "/usr/local/bin:/usr/bin:/bin"
    )
    result = _run_tool_command(
        adapter.command_for_task(task_name),
        int(adapter.config.get("task_timeout_seconds", 600)),
        "${AIOS_HOME}/sandbox/coding",
        env,
    )
    output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
    if result.returncode == 0 and output:
        # Production executor-live-failover 2026-08-10: keep the
        # Claude adapter's inference-side health cache in sync with
        # every real task outcome.  Without this call the cache
        # could stay stale forever (e.g. last probe showed
        # quota_exhausted even though a real task just succeeded)
        # and the orchestrator's ``_executor_model_available``
        # gate would permanently exclude Claude from failover,
        # forcing every repair to land on codex / opencode and
        # cascading into ``opencode → codex → opencode`` loops.
        try:
            adapter.record_inference_success(
                evidence=f"real task succeeded; task_id={task_id[:32]}",
                latency_ms=0,
            )
        except Exception:
            pass
        return True, f"Claude Code: {output[-32768:]}"
    # Production executor-live-failover 2026-08-10: mirror the
    # opencode adapter's contract so call-time claude failures
    # also invalidate the stale positive / negative cache.  The
    # ``_classify_failure`` helper maps the stderr text into the
    # canonical ``model_state`` vocabulary (network_error /
    # quota_exhausted / probe_failed / …) so the orchestrator's
    # ``_executor_model_available`` and the routing engine can
    # treat this as the new ground truth.
    err_payload = output or "claude empty result"
    try:
        adapter.record_inference_failure(
            evidence=err_payload,
            returncode=result.returncode if result.returncode else 1,
            latency_ms=0,
        )
    except Exception:
        pass
    return False, f"Claude Code failure: {err_payload[-4000:]}"


def execute_codex(task: dict) -> tuple:
    """Execute Codex-assigned work only through the registered Codex adapter."""
    task_name = str(task.get("task_name", "")).strip()
    task_id = str(task.get("task_id", ""))
    if not task_name:
        return False, "[codex/blocked] empty task"

    ok, why = check_input_safety(task_name)
    if not ok:
        return False, f"[codex/blocked] {why}"

    adapter = get_adapter("codex")
    if not Path(adapter.executable).is_file():
        return False, "[codex/blocked] registered adapter is missing"
    env = dict(os.environ)
    env["AIOS_TASK_ID"] = task_id
    env["PATH"] = (
        "${HOME}/.n/bin:${HOME}/.local/bin:"
        "/usr/local/bin:/usr/bin:/bin"
    )
    try:
        result = subprocess.run(
            adapter.command_for_task(task_name),
            shell=False, capture_output=True, text=True, timeout=300,
            cwd="${AIOS_HOME}/sandbox/coding", env=env,
        )
    except subprocess.TimeoutExpired:
        # Production executor-live-failover 2026-08-10: propagate
        # the timeout into the codex health cache so the next
        # ``_executor_model_available("codex")`` call returns
        # ``False`` immediately.  Previously the daemon returned
        # the failure string but never updated the cache, so a
        # codex that timed out once stayed AVAILABLE in the
        # routing layer until the next scheduled probe — the
        # repair loop kept selecting the same dead route.
        try:
            adapter.record_inference_failure(
                evidence="[codex/relay] task timed out after 300 seconds",
                returncode=124,
                latency_ms=300000,
            )
        except Exception:
            pass
        return False, "[codex/relay] task timed out after 300 seconds"
    except Exception as exc:
        try:
            adapter.record_inference_failure(
                evidence=f"[codex/relay] {type(exc).__name__}: {exc}",
                returncode=1,
                latency_ms=0,
            )
        except Exception:
            pass
        return False, f"[codex/relay] {type(exc).__name__}: {exc}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    combined = (stdout + ("\n" + stderr if stderr else "")).strip()
    if result.returncode != 0:
        try:
            adapter.record_inference_failure(
                evidence=combined or "codex non-zero exit",
                returncode=result.returncode,
                latency_ms=0,
            )
        except Exception:
            pass
        return False, f"[codex/relay] {combined[-4000:]}"
    if not stdout:
        try:
            adapter.record_inference_failure(
                evidence="[codex/relay] empty result",
                returncode=1,
                latency_ms=0,
            )
        except Exception:
            pass
        return False, "[codex/relay] empty result"
    # Production executor-live-failover 2026-08-10: keep the codex
    # inference-side health cache fresh on real success so the
    # orchestrator's ``_executor_model_available("codex")`` keeps
    # returning ``True`` between scheduled probes.
    try:
        adapter.record_inference_success(
            evidence=f"real task succeeded; task_id={task_id[:32]}",
            latency_ms=0,
        )
    except Exception:
        pass
    return True, f"[codex] {stdout[-32768:]}"

EXECUTORS = {
    "opencode": execute_opencode,
    "claude": execute_claude,
    "codex": execute_codex,
}


def core_write_boundary(task_name: str) -> tuple[bool, str]:
    """Block task-system writes to the frozen AIOS core.

    Core maintenance is deliberately out-of-band and requires the owner's
    explicit authorization. Normal tasks may read status/logs and may write
    only inside sandbox/coding.
    """
    text = (task_name or "").lower()
    # Acceptance criteria describe what must not happen and are not write intent.
    # The governed envelope keeps goal/node above this delimiter.
    if "\nacceptance:\n" in text:
        text = text.split("\nacceptance:\n", 1)[0]

    # Negated mutation words describe safety constraints, not write intent.
    # Strip only explicit negations before evaluating protected-core writes.
    negated_mutation_patterns = (
        r"\b(?:do\s+not|don't|never)\s+(?:modify|edit|write|delete|remove|overwrite|upgrade|install|move)\b",
        r"\bwithout\s+(?:modification|modifying|editing|writing|deleting|removing)\b",
        r"\bno\s+(?:file\s+)?(?:write|writes|modification|changes?)\b",
        r"\bnot\s+(?:modified|changed|written|deleted|removed)\b",
        r"\bread[\s-]*only\b",
        r"(?:\u4e0d\u8981|\u4e0d\u5f97|\u7981\u6b62|\u65e0\u9700)(?:\u4fee\u6539|\u5220\u9664|\u5199\u5165|\u521b\u5efa|\u4f18\u5316|\u4fee\u590d)",
        r"\u53ea\u8bfb",
    )
    for pattern in negated_mutation_patterns:
        text = re.sub(pattern, " [negated-mutation] ", text)

    text = text.replace("${AIOS_HOME}/sandbox/coding", "[allowed-sandbox]")
    mutation_zh = ("修改", "删除", "清理", "覆盖", "重写", "替换", "升级", "安装",
                   "移动", "写入", "创建", "优化", "修复")
    mutation_en = ("patch", "edit", "write", "delete", "remove", "overwrite",
                   "upgrade", "install", "move", "modify", "create", "fix", "optimize")

    def has_mutation(value: str) -> bool:
        # English verbs require token boundaries. Substrings such as
        # installed_version/installation evidence are observations, not intent.
        return any(word in value for word in mutation_zh) or bool(re.search(
            r"\b(?:" + "|".join(re.escape(word) for word in mutation_en) + r")\b",
            value,
        ))
    protected_markers = ("aios核心", "aios core", "kernel/tools",
                         "aios/config", "systemd/user/aios", "aios_module",
                         "aios_bus.py", "aios_executor_daemon.py")
    protected_core_path = bool(re.search(
        r"${AIOS_HOME}(?:/|$|(?=[\s'\"`]))",
        text,
    ))
    protected_target = protected_core_path or any(
        marker in text for marker in protected_markers
    )
    if has_mutation(text) and protected_target:
        # Sentence-level proximity: confirm mutation word and protected path
        # co-occur in the same sentence to reduce false positives from
        # mixed read/write tasks (e.g. "读 config + 创建 sandbox/coding").
        sentences = re.split(r'(?<=[。.!?！？\n])\s*', text)
        for s in sentences:
            if has_mutation(s) and any(p in s for p in protected_markers):
                return False, "core_write_denied: AIOS核心维护必须由所有者明确授权并在任务系统外执行"
        if protected_core_path:
            for s in sentences:
                sentence_has_mutation = has_mutation(s)
                has_aios_path = bool(re.search(r"${AIOS_HOME}(?:/|$|(?=[\s'\"`]))", s))
                if sentence_has_mutation and has_aios_path:
                    return False, "core_write_denied: AIOS核心维护必须由所有者明确授权并在任务系统外执行"
    return True, "ok"

def _executor_dispatch_ready(executor: str) -> tuple:
    """Return whether the daemon may attempt one real task.

    Close-out 20260728 P9C: ``fully_operational`` already covers the
    inference slice (which becomes stale the moment a real task fails).
    If the daemon waited for a fresh ``fully_operational`` it would
    permanently stop consuming tasks because no fresh inference evidence
    exists while the daemon is the only path that can produce it.  This
    helper separates *infrastructure* (binary exists + lightweight HTTP
    probe says reachable + protocol ready) from *inference* and uses
    only the infrastructure slice as the dispatch gate.
    """
    if executor not in ("opencode", "claude", "codex"):
        return True, "non_daemon_role"
    if os.environ.get("AIOS_EXECUTOR_FORCE_DISPATCH") == "1":
        return True, "AIOS_EXECUTOR_FORCE_DISPATCH"
    try:
        health = get_adapter(executor).health()
    except Exception as exc:
        safe_log_exception("executor_dispatch_gate", exc)
        return False, f"health_lookup_failed:{type(exc).__name__}"
    infra_ok = bool(health.get("contract_ok")) and bool(
        health.get("lightweight_reachable")) and bool(
        health.get("lightweight_protocol_ready"))
    if not infra_ok:
        return False, (
            f"infrastructure_unavailable: contract_ok={health.get('contract_ok')}, "
            f"reachable={health.get('lightweight_reachable')}, "
            f"protocol_ready={health.get('lightweight_protocol_ready')}"
        )
    return True, (
        f"dispatch_ready; state={health.get('state')}, "
        f"fully_operational={health.get('fully_operational')}"
    )


def _executor_inference_ready(executor: str) -> bool:
    """Return current cached inference readiness without launching a probe.

    Production-eligibility gate (used by Orchestrator planner and
    health publisher).  Kept distinct from ``_executor_dispatch_ready``
    so the daemon dispatch loop never silently inherits the strict
    semantics that would deadlock it on the first stale failure.
    """
    if executor not in ("opencode", "claude", "codex"):
        return True
    try:
        return bool(get_adapter(executor).health().get("fully_operational", False))
    except Exception as exc:
        safe_log_exception("executor_health_gate", exc)
        return False


# Close-out 20260728 P9C: per-executor daemon liveness hash.  This is a
# daemon-side heartbeat that proves the *consume loop* is alive, distinct
# from the lightweight HTTP probe the P9B publisher runs against the
# executor's binary server.  ``aios:bus:liveness:{executor}`` carries
# the timestamps the health publisher and tests need to distinguish
# "process up but consumer thread dead" from "process up and active".
#
# The keys live alongside the P9B ``aios:bus:agent:{executor}`` hash so
# a single Redis SCAN reveals the full state of each executor.
_LIVENESS_TTL_SECONDS = 600


def _publish_liveness(executor: str, stage: str, reason: str = "",
                      detail: str = "") -> None:
    """Update the daemon-side liveness hash with stage + timestamps."""
    if executor not in ("opencode", "claude", "codex"):
        return
    now = datetime.now(timezone.utc).isoformat()
    key = f"aios:bus:liveness:{executor}"
    payload = {
        "executor": executor,
        "loaded_revision": LOADED_REVISION,
        "last_loop_at": now,
        "queue_poll_at": now,
        "last_stage": stage,
        "last_reason": reason[:200],
        "last_detail": detail[:300],
    }
    try:
        import redis as _rd
        _rc = _rd.Redis(host='localhost', port=6379, socket_connect_timeout=2,
                        socket_timeout=2)
        _rc.hset(key, mapping={k: str(v) for k, v in payload.items()})
        _rc.expire(key, _LIVENESS_TTL_SECONDS)
    except Exception:
        # Liveness update must never break the dispatch loop.
        pass
    # ``last_claimed_at`` / ``last_completed_at`` are only updated by the
    # dedicated helpers below to avoid two writers racing on the same key.
    if stage == "queue_poll":
        try:
            import redis as _rd
            _rc = _rd.Redis(host='localhost', port=6379, socket_connect_timeout=2,
                            socket_timeout=2)
            _rc.hset(key, "queue_poll_at", now)
            _rc.expire(key, _LIVENESS_TTL_SECONDS)
        except Exception:
            pass


def _mark_claim(executor: str, task_id: str) -> None:
    if executor not in ("opencode", "claude", "codex"):
        return
    now = datetime.now(timezone.utc).isoformat()
    key = f"aios:bus:liveness:{executor}"
    try:
        import redis as _rd
        _rc = _rd.Redis(host='localhost', port=6379, socket_connect_timeout=2,
                        socket_timeout=2)
        _rc.hset(key, mapping={
            "last_claimed_at": now,
            "last_claimed_task_id": str(task_id),
        })
        _rc.expire(key, _LIVENESS_TTL_SECONDS)
    except Exception:
        pass


def _mark_completion(executor: str, status: str) -> None:
    if executor not in ("opencode", "claude", "codex"):
        return
    now = datetime.now(timezone.utc).isoformat()
    key = f"aios:bus:liveness:{executor}"
    try:
        import redis as _rd
        _rc = _rd.Redis(host='localhost', port=6379, socket_connect_timeout=2,
                        socket_timeout=2)
        _rc.hset(key, mapping={
            "last_completed_at": now,
            "last_completion_status": str(status),
        })
        _rc.expire(key, _LIVENESS_TTL_SECONDS)
    except Exception:
        pass




def run_once(executor: str):
    """执行一个任务后退出."""
    depth = EXECUTOR_DEPTH.get(executor, "low")
    execute_fn = EXECUTORS.get(executor)

    try:
        from aios_tool_evolution import is_maintenance
        if is_maintenance(executor):
            return False  # this chip is upgrading; other executors keep serving
    except Exception:
        pass

    _publish_liveness(executor, "queue_poll", reason="loop_iteration_start")
    heartbeat(executor, revision=LOADED_REVISION)
    # Close-out 20260728 P9C: dispatch gate uses *infrastructure* (binary +
    # lightweight ping) instead of strict ``fully_operational``.  A stale
    # inference failure must not deadlock the daemon — the daemon is the
    # only path that can refresh inference evidence.  ``fully_operational``
    # stays as the production-eligibility gate used by Orchestrator planner
    # and the health publisher.
    dispatch_ok, dispatch_reason = _executor_dispatch_ready(executor)
    if not dispatch_ok:
        _publish_liveness(executor, "queue_poll", reason="dispatch_blocked",
                          detail=dispatch_reason)
        emit("agent.degraded", source=executor,
             payload={"reason": "inference_health_gate",
                      "detail": dispatch_reason,
                      "legacy_strict_gate": True})
        return False
    _publish_liveness(executor, "queue_poll", reason="dispatch_ready",
                      detail=dispatch_reason)

    task = claim_next_task(executor, depth)
    # Claude is the verified recovery executor.  It may take work owned by an
    # unhealthy OpenCode/Codex chip after its own high-depth queue is empty.
    if not task and executor == "claude":
        for fd in ["batch", "low", "quick", "standard", None]:
            task = claim_next_task(executor, fd)
            if task: break
    elif not task and depth == "low":
        for fd in ["quick", "standard", None]:
            task = claim_next_task(executor, fd)
            if task: break

    if not task:
        # claim_next_task already performed the O(1) pending-queue check.
        # Avoid rescanning every historical task merely to print a counter.
        return False

    tid = task["task_id"]
    task_name = task.get("task_name", "")
    print(f"🔒 [{executor}] 认领: {task_name[:60]} ({tid[:8]}...)")
    _mark_claim(executor, tid)
    emit("agent.busy", source=executor, payload={"task_id": tid, "task": task_name[:128]})

    # P0-7 slot-release contract: any code path between the claim and
    # the explicit ``release_lock(tid, executor)`` at line 621 can
    # raise an exception (network blip during enforce_pipeline, an
    # unhandled failure inside report_metadata, an out-of-band trace
    # emitter, etc.).  When that happened historically the slot was
    # leaked for the executor until the next ``sweep_stuck_claims``
    # cycle — leaving other tasks queued behind a phantom
    # ``queued_behind_executor:codex`` for 600 s+ in production.  The
    # fix wraps the whole post-claim section in a single
    # ``try / finally`` that ALWAYS releases the lock and emits the
    # ``agent.idle`` event, regardless of the success / failure /
    # timeout / cancellation exit path.  Inner ``return`` statements
    # continue to release explicitly so the success / failure
    # status updates remain visible; the ``finally`` is the safety
    # net for unhandled exceptions.
    slot_released = False

    def _release_slot_once() -> None:
        nonlocal slot_released
        if slot_released:
            return
        slot_released = True
        try:
            release_lock(tid, executor)
        except Exception:
            pass
        try:
            emit("agent.idle", source=executor, payload={"task_id": tid})
        except Exception:
            pass

    try:
        if not check_in_task(tid, executor):
            print(f"⏭️ [{executor}] check-in 失败 (锁被抢占或过期), 放弃: {task_name[:60]}")
            return False
        emit("task.running", source=executor, payload={"task_id": tid, "task": task_name[:128]})

        risk_action = str(task.get("risk_action", "") or "")
        if risk_action:
            approval_ok, approval_reason = approval_is_valid(
                str(task.get("approval_id", "") or ""),
                str(task.get("parent_id", "") or ""),
                risk_action,
            )
            if not approval_ok:
                reason = f"l4_approval_denied:{approval_reason}"
                update_task_status(tid, "failed", executor, reason,
                                   metadata={"execution_error": reason})
                return True

        boundary_ok, boundary_reason = core_write_boundary(task_name)
        if not boundary_ok:
            update_task_status(tid, "failed", executor, boundary_reason)
            emit("security.violation", source=executor,
                 payload={"task_id": tid, "type": "core_write_boundary",
                          "reason": boundary_reason, "task": task_name[:128]})
            print(f"BLOCKED [{executor}] {boundary_reason}: {task_name[:80]}")
            return True

        # Execution metadata is recorded here. Learning admission happens only
        # after parent verification through the Orchestrator/Hermes contracts.

        # 通过AIOS执行管线 (Protocol→WorldModel→Execute→Verify)
        pipeline_result = enforce_pipeline(task, executor, execute_fn)
        success = pipeline_result.get("success", False)
        pipeline = pipeline_result.get("pipeline", {})
        executor_summary = pipeline.get("execute", {}).get("summary", "")
        pipeline_error = pipeline_result.get("error", "")
        if not success and pipeline_error.startswith("Verification failed:") and executor_summary:
            summary = (executor_summary + "\n[verification_failed] " + pipeline_error)[:32768]
        else:
            summary = pipeline_error or executor_summary

        verify_step = pipeline.get("verify", {})
        result_metadata = {
            "verification_passed": bool(success and verify_step.get("passed", False)),
            "verification_report": verify_step.get("report", "")[:300],
            "executor_result": executor_summary[:512],
        }
        if not success and pipeline_error:
            result_metadata["execution_error"] = pipeline_error[:300]
        # 更新结果
        status = "completed" if success else "failed"
        update_task_status(tid, status, executor, summary[:32768], metadata=result_metadata)
        _mark_completion(executor, status)

        # Tool learning is intentionally deferred until the AIOS parent
        # workflow accepts the result through the independent Hermes gate.

        # 死信队列: 连续失败入死信, 成功时清零计数
        try:
            import redis as _rd
            _rc = _rd.Redis(host='localhost', port=6379, socket_connect_timeout=2)
            if not success:
                _dc = _rc.incr(f"aios:bus:dead:count:{executor}")
                _rc.expire(f"aios:bus:dead:count:{executor}", 3600)
                if _dc >= 5:
                    _dead = json.dumps({"task_id": tid, "task": task_name[:100],
                                        "error": summary[:200],
                                        "ts": datetime.now(timezone.utc).isoformat()},
                                       ensure_ascii=False)
                    _rc.lpush(f"aios:bus:dead:list:{executor}", _dead)
                    _rc.ltrim(f"aios:bus:dead:list:{executor}", 0, 99)
                    print(f"☠️ [{executor}] 连续{_dc}次失败, 任务入死信: {task_name[:40]}")
            else:
                _rc.delete(f"aios:bus:dead:count:{executor}")
        except Exception:
            pass

        icon = "✅" if success else "❌"
        pipeline_steps = pipeline_result.get("pipeline", {})
        steps_ok = sum(1 for s in pipeline_steps.values() if s.get("passed"))
        steps_total = len(pipeline_steps)
        print(f"{icon} [{executor}] {status} ({steps_ok}/{steps_total}步通过): {task_name[:50]}")
        emit("task." + status, source=executor, payload={"task_id": tid, "task": task_name[:128], "summary": summary[:200]})

        try:
            start_ts = task.get("ts_created") or task.get("ts_enqueued") or ""
            if start_ts:
                import datetime as _dt
                try:
                    started = _dt.datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                except Exception:
                    elapsed = 0.0
            else:
                elapsed = 0.0
            report_metadata(tid, executor, task_name, status, elapsed,
                            summary=summary, error=pipeline_result.get("error", ""))
        except Exception:
            pass

        # No learning event is emitted here: executor completion is evidence,
        # not an accepted outcome. Parent verification owns learning admission.
        return True
    finally:
        # Single funnel for slot release — guarantees the lock is freed
        # even if any inner step raises an unhandled exception, so a
        # network blip or a transient trace-emit failure cannot leak
        # the slot and leave the next task waiting behind a phantom
        # ``queued_behind_executor`` for the full 600 s sweep cycle.
        _release_slot_once()


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
        if iterations % 10 == 0:
            freed = sweep_stuck_claims()
            if freed:
                print(f"🧹 [{executor}] 清扫 {freed} 个 stuck 任务, 放回 pending 队列")
        heartbeat(executor, revision=LOADED_REVISION)
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


# ============================================================
#  Pin Registration — 执行器注册到 AIOS 总线
# ============================================================
try:
    from aios_bus import register_pin
    register_pin("executor.run_once", run_once,
                 "Executor: 执行单个任务 (opencode/claude/codex)")
    register_pin("executor.run_loop", run_loop,
                 "Executor: 持续监听队列循环")
except Exception:
    pass
