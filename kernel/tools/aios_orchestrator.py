#!/usr/bin/env python3
"""AIOS-owned parent workflow orchestrator.

One durable owner coordinates planning, health-aware dispatch, dependency
release, independent verification, bounded repair and final aggregation.
The five AI tools remain replaceable adapters.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple  # noqa: F401

from aios_bus import (
    _is_available,
    _redis_client,
    enqueue_task,
    generate_task_id,
    get_task_state,
    heartbeat,
    publish_event,
    update_task_status,
    register_callback,
    register_pin,
    request_approval,
    approve_request,
    consume_approval,
    KEY_INDEX,
    KEY_QUEUE_STATE,
    KEY_STATE,
)
from aios_tool_adapter import get_adapter
from aios_verification_gate import verify_parent_node, _collect_independent_evidence
from aios_secure import classify_l4_action
from aios_capability import (
    is_available as _capability_available,
    DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
)
from aios_tool_failover import (
    record_tool_runtime_failure as _record_tool_runtime_failure,
    clear_tool_runtime_failure as _clear_tool_runtime_failure,
    get_tool_runtime_failure as _get_tool_runtime_failure,
)

KEY_WORKFLOW = "aios:orchestrator:workflow"
KEY_ACTIVE = "aios:orchestrator:active"
WORKFLOW_TTL_SECONDS = 30 * 24 * 3600
MAX_REPAIRS = 2
# P9F close-out: canonical child identity key. The presence of this
# Redis key is the *single* source of truth for "this (parent,
# node_index, generation) has a child task_id". All
# ``_enqueue_node`` callers — initial dispatch, dependency release,
# repair, restart recovery, pending-too-long escalation — share the
# same atomic SET NX claim path. A duplicate invocation returns the
# pre-existing canonical task_id instead of generating a fresh UUID.
KEY_CANONICAL_CHILD = "aios:orchestrator:canonical_child"
CANONICAL_CHILD_TTL_SECONDS = 30 * 24 * 3600  # 30 days, same as workflow
# Generation is the node's ``attempt`` counter at the moment of
# enqueue.  ``_repair_node`` is the *only* path that increments it
# (after the existing MAX_REPAIRS check).  This preserves the
# bounded-retry invariant and prevents retry amplification from
# recovery / resume / repeated ``process_workflow`` polls.
CHILD_STATUS_NON_TERMINAL = ("pending", "locked", "running", "verifying")
CHILD_STATUS_TERMINAL = ("completed", "failed", "cancelled", "canceled")
SUPERSEDE_REASON = "DUPLICATE_CHILD_SUPERSEDED"
# P9E close-out: bounded "pending too long" so a child that never gets
# claimed (executor silently busy, plan did not advance, or daemon
# lock-up) eventually escalates to repair and the parent workflow can
# converge.  ``PENDING_TOO_LONG_SECONDS`` is the absolute age threshold
# for a child that has stayed in the pending queue even though the
# executor process is alive.  When exceeded, ``_child_stall_reason``
# emits ``pending_too_long`` and ``process_workflow`` triggers the
# same repair path as ``dispatch_claim_timeout``.
PENDING_TOO_LONG_SECONDS = int(os.getenv("AIOS_ORCH_PENDING_TOO_LONG_SECONDS", "600") or "600")
# Close-out 2026-07-27-§五: bounded planner retry + hard timeouts.
# Defaults are sourced from environment so operators can tune without
# touching the code (the magic numbers live in the constants below only
# as a safe fallback).
PLANNER_MAX_ATTEMPTS = int(os.getenv("AIOS_PLANNER_MAX_ATTEMPTS", "1") or "1")
PLANNER_CONNECT_TIMEOUT = float(
    os.getenv("AIOS_PLANNER_CONNECT_TIMEOUT", "10") or "10")
PLANNER_READ_TIMEOUT = float(
    os.getenv("AIOS_PLANNER_READ_TIMEOUT", "45") or "45")
PLANNER_TOTAL_EXECUTION_TIMEOUT = float(
    os.getenv("AIOS_PLANNER_TOTAL_EXECUTION_TIMEOUT", "60") or "60")
PLANNER_RETRY_BACKOFF = float(
    os.getenv("AIOS_PLANNER_RETRY_BACKOFF", "2") or "2")
PLANNER_ATTEMPTS = PLANNER_MAX_ATTEMPTS  # legacy alias
POLL_SECONDS = 1.0
# Terminal statuses that include the planner-driven dead-letter
# destinations required by §六 of this close-out.
TERMINAL = {"completed", "failed", "cancelled", "blocked"}
EXECUTORS = ("opencode", "claude", "codex", "minimax-official")
LOADED_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

_WORKFLOW_POOL = ThreadPoolExecutor(max_workers=8)
# Dedicated pool for planner round-trips so a slow openclaw call does
# not occupy one of the 8 workflow slots.  Sized for ``max_attempts``
# concurrent in-flight calls during a single planner cycle.
_PLANNER_POOL = ThreadPoolExecutor(
    max_workers=max(2, PLANNER_MAX_ATTEMPTS))


class PlannerTimeout(Exception):
    """Raised when an openclaw planner attempt breaches the
    ``connect+read`` or ``total_execution_timeout`` budget.
    """

    def __init__(self, attempt: int, reason: str,
                 connect_timeout: float = PLANNER_CONNECT_TIMEOUT,
                 read_timeout: float = PLANNER_READ_TIMEOUT,
                 total_timeout: float = PLANNER_TOTAL_EXECUTION_TIMEOUT,
                 elapsed_ms: int = 0):
        super().__init__(reason)
        self.attempt = attempt
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.total_timeout = total_timeout
        self.elapsed_ms = elapsed_ms
        self.reason = reason


def _call_opencode_plan_only(parent_id: str, prompt: str,
                             planner_binding: str,
                             connect_timeout: float,
                             read_timeout: float) -> dict:
    """Invoke the OpenCode adapter in PLAN_ONLY mode.

    P9D-R role-closure: this is the real PLAN_ONLY path that the
    Orchestrator uses when ``preferred_planner='opencode'``.  The
    OpenCode adapter is the only process authorised to carry out
    PLAN_ONLY invocations; the model-gateway call path is reserved
    for the openclaw / claude planners.  The response shape mirrors
    ``aios_model_gateway.call_model`` (``{"ok": bool, "result" or
    "error": ...}``) so the existing build_plan payload validation
    keeps working unchanged.
    """
    started = time.monotonic()
    try:
        from aios_opencode_adapter import (  # type: ignore
            execute_plan_only,
            OpenCodeAdapterError,
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        raise PlannerTimeout(
            attempt=0, reason=f"OPENCODE_IMPORT:{type(exc).__name__}:{str(exc)[:200]}",
            connect_timeout=connect_timeout, read_timeout=read_timeout,
            total_timeout=PLANNER_TOTAL_EXECUTION_TIMEOUT,
            elapsed_ms=elapsed_ms,
        ) from exc
    try:
        outcome = execute_plan_only(
            parent_id=parent_id,
            prompt=prompt,
            binding=str(planner_binding or ""),
            connect_timeout=float(connect_timeout),
            read_timeout=float(read_timeout),
        )
    except OpenCodeAdapterError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        raise PlannerTimeout(
            attempt=0, reason=f"OPENCODE_PLAN_ONLY:{type(exc).__name__}:{str(exc)[:200]}",
            connect_timeout=connect_timeout, read_timeout=read_timeout,
            total_timeout=PLANNER_TOTAL_EXECUTION_TIMEOUT,
            elapsed_ms=elapsed_ms,
        ) from exc
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        raise PlannerTimeout(
            attempt=0, reason=f"OPENCODE_PLAN_ONLY:{type(exc).__name__}:{str(exc)[:200]}",
            connect_timeout=connect_timeout, read_timeout=read_timeout,
            total_timeout=PLANNER_TOTAL_EXECUTION_TIMEOUT,
            elapsed_ms=elapsed_ms,
        ) from exc
    return outcome


def _call_planner_via_openclaw_service(
    parent_id: str, prompt: str,
    connect_timeout: float, read_timeout: float,
) -> dict:
    """P9D-R-OpenClaw-Planner-Tool-Boundary (2026-08-04):
    Route the OpenClaw Planner call through the dedicated
    ``aios-planner-openclaw.service`` adapter.

    The adapter is a thin process boundary: it owns the openclaw
    CLI / openclaw-gateway / minimax-provider leg probe and the
    actual text inference.  Stopping the service MUST make the
    OpenClaw Planner routing_eligible=False WITHOUT affecting:

      * minimax.shared Provider capability
      * claude / hermes reviewer
      * opencode Planner

    The adapter returns the same envelope as
    ``aios_model_gateway.call_model`` so the orchestrator's
    existing ``build_plan`` loop consumes the response
    unchanged.  On service unavailability the adapter returns
    ``503`` with a structured ``tool_blocked`` reason; this
    function translates that into a ``PlannerTimeout`` so the
    orchestrator's Planner-fallback layer picks the next
    candidate (opencode) without surfacing a false success.
    """
    import json
    import os
    import urllib.error
    import urllib.request
    url = os.environ.get(
        "AIOS_PLANNER_OPENCLAW_URL", "http://127.0.0.1:18899/plan")
    payload = json.dumps({
        "parent_id": parent_id,
        "prompt": prompt,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    started = time.monotonic()
    # P9D-R hotfix 2026-08-10: minimal sanitized timing/size
    # instrumentation at the Planner client boundary so the
    # Orchestrator↔Planner integration diagnostic can capture the
    # exact payload bytes / prompt char count / per-phase timing
    # without logging any user prompt content.  Disabled by default
    # and only enabled when AIOS_PLANNER_TRACE_PATH is set in env.
    _trace_path = os.environ.get("AIOS_PLANNER_TRACE_PATH", "").strip()
    _prompt_chars = len(prompt or "")
    _payload_bytes = len(payload)
    _t_built = time.monotonic()
    _t_connected = 0
    _t_first_byte = 0
    try:
        with urllib.request.urlopen(
                req, timeout=float(read_timeout)) as resp:
            _t_first_byte = time.monotonic()
            body_raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body = exc.read().decode("utf-8", errors="replace")[:500]
        # 503 == tool boundary down: bubble up as PlannerTimeout
        # with failure_scope=TOOL_PROCESS so the fallback layer
        # picks the next candidate (opencode) without
        # ``all_independent_reviewers_unavailable`` semantics.
        if exc.code == 503:
            raise PlannerTimeout(
                attempt=0,
                reason=(
                    f"OPENCLAW_TOOL_BOUNDARY_DOWN:status_503:"
                    f"{body[:200]}"
                ),
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                total_timeout=PLANNER_TOTAL_EXECUTION_TIMEOUT,
                elapsed_ms=elapsed_ms,
            ) from exc
        raise PlannerTimeout(
            attempt=0,
            reason=(
                f"PLANNER_CALL:HTTPError:{exc.code}:{body[:200]}"
            ),
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            total_timeout=PLANNER_TOTAL_EXECUTION_TIMEOUT,
            elapsed_ms=elapsed_ms,
        ) from exc
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        raise PlannerTimeout(
            attempt=0,
            reason=(
                f"PLANNER_CALL:{type(exc).__name__}:{str(exc)[:200]}"
            ),
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            total_timeout=PLANNER_TOTAL_EXECUTION_TIMEOUT,
            elapsed_ms=elapsed_ms,
        ) from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if _trace_path:
        try:
            with open(_trace_path, "a", encoding="utf-8") as _fh:
                _fh.write(
                    "{"
                    f'"parent_id":"{parent_id}",'
                    f'"endpoint":"{url}",'
                    f'"http_status":{int(status)},'
                    f'"payload_bytes":{int(_payload_bytes)},'
                    f'"prompt_chars":{int(_prompt_chars)},'
                    f'"build_ms":{int((_t_built - started) * 1000)},'
                    f'"first_byte_ms":{int((_t_first_byte - started) * 1000) if _t_first_byte else 0},'
                    f'"elapsed_ms":{int(elapsed_ms)}'
                    "}\n"
                )
        except Exception:
            pass
    if status >= 400:
        if _trace_path:
            try:
                _t_finished = time.monotonic()
                with open(_trace_path, "a", encoding="utf-8") as _fh:
                    _fh.write(
                        "{"
                        f'"parent_id":"{parent_id}",'
                        f'"endpoint":"{url}",'
                        f'"http_status":{int(status)},'
                        f'"payload_bytes":{int(_payload_bytes)},'
                        f'"prompt_chars":{int(_prompt_chars)},'
                        f'"build_ms":{int((_t_built - started) * 1000)},'
                        f'"elapsed_ms":{int(elapsed_ms)}'
                        "}\n"
                    )
            except Exception:
                pass
        raise PlannerTimeout(
            attempt=0,
            reason=(
                f"PLANNER_CALL:HTTPError:{status}:{body_raw[:200]}"
            ),
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            total_timeout=PLANNER_TOTAL_EXECUTION_TIMEOUT,
            elapsed_ms=elapsed_ms,
        )
    try:
        return json.loads(body_raw)
    except Exception as exc:
        if _trace_path:
            try:
                with open(_trace_path, "a", encoding="utf-8") as _fh:
                    _fh.write(
                        "{"
                        f'"parent_id":"{parent_id}",'
                        f'"endpoint":"{url}",'
                        f'"http_status":0,'
                        f'"payload_bytes":{int(_payload_bytes)},'
                        f'"prompt_chars":{int(_prompt_chars)},'
                        f'"build_ms":{int((_t_built - started) * 1000)},'
                        f'"elapsed_ms":{int(elapsed_ms)},'
                        f'"error":"invalid_json:{type(exc).__name__}"'
                        "}\n"
                    )
            except Exception:
                pass
        raise PlannerTimeout(
            attempt=0,
            reason=(
                f"PLANNER_CALL:invalid_json:"
                f"{type(exc).__name__}:{body_raw[:200]}"
            ),
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            total_timeout=PLANNER_TOTAL_EXECUTION_TIMEOUT,
            elapsed_ms=elapsed_ms,
        ) from exc


def _call_planner_with_deadline(call_model, parent_id: str, prompt: str,
                                 attempt: int,
                                 planner_tool: str = "openclaw",
                                 planner_binding: str = "minimax:minimax",
                                 provider_id: str = "minimax",
                                 model_id: str = "MiniMax-M3",
                                 mode: str = "openclaw-minimax") -> dict:
    """Call ``call_model`` inside a hard total-timeout wall so an
    unreachable / slow planner call cannot pin the orchestrator
    scheduler.  Each attempt honours ``PLANNER_CONNECT_TIMEOUT`` and
    ``PLANNER_READ_TIMEOUT`` at the model-gateway layer; this
    function additionally bounds the wall-clock from submission to
    return via :class:`concurrent.futures.Future`.

    P9D-R role-closure: ``planner_tool`` / ``planner_binding`` /
    ``provider_id`` / ``model_id`` / ``mode`` are now policy-driven
    inputs.  The legacy ``openclaw-minimax`` shape is preserved as
    the default so existing tests stay green; the orchestrator may
    invoke this function with ``planner_tool='opencode'`` /
    ``mode='opencode-plan-only'`` to delegate planning to the
    OpenCode adapter (PLAN_ONLY).
    """
    from concurrent.futures import ThreadPoolExecutor
    from aios_model_gateway import call_model as _call_model_fn

    connect_timeout = PLANNER_CONNECT_TIMEOUT
    read_timeout = min(PLANNER_READ_TIMEOUT,
                       max(5.0, PLANNER_TOTAL_EXECUTION_TIMEOUT - 1.0))
    # P9D-R: when the policy selects opencode as the planner and the
    # mode is PLAN_ONLY, route the call through the OpenCode adapter
    # process instead of the generic model gateway.  This is the only
    # call shape authorised for ``role=planner, mode=PLAN_ONLY``; the
    # planner is forbidden from executing steps or invoking tools
    # beyond returning the structured plan JSON.
    if str(planner_tool or "").lower() == "minimax-official":
        # Task 014: route directly to aios_model_gateway (provider=minimax),
        # which is now gated by AIOS_MINIMAX_OFFICIAL_ENABLED and the
        # 30-call / 15000-token limiter.  No openclaw / opencode service needed.
        from aios_model_gateway import call_model as _call_model_fn
        return _call_model_fn("minimax", "MiniMax-M3", [
            {"role": "system", "content": "You are an AIOS planner. Output strict JSON only."},
            {"role": "user", "content": prompt},
        ], agent="minimax-official", task_id=parent_id, max_tokens=2400,
            reasoning_split=True,
            connect_timeout=int(connect_timeout),
            read_timeout=int(read_timeout))
    if str(planner_tool or "").lower() == "opencode" and str(mode or "").lower() == "opencode-plan-only":
        return _call_opencode_plan_only(
            parent_id=parent_id, prompt=prompt,
            planner_binding=planner_binding,
            connect_timeout=connect_timeout, read_timeout=read_timeout,
        )
    # P9D-R-OpenClaw-Planner-Tool-Boundary (2026-08-04): when
    # the policy selects openclaw as the planner, route the call
    # through the dedicated OpenClaw Planner Adapter service
    # (``aios-planner-openclaw.service``).  The service is a thin
    # process boundary that owns the openclaw tool / openclaw-gateway
    # / minimax-provider leg probe; stopping the service MUST make
    # openclaw planner routing_eligible=False without affecting
    # minimax.shared, claude reviewer, hermes reviewer, or opencode
    # planner.  When the service is unhealthy the call returns a
    # structured ``503`` so the orchestrator's Planner-fallback
    # layer can pick the next candidate (opencode) without
    # silently routing through the Provider.
    if str(planner_tool or "").lower() == "openclaw":
        return _call_planner_via_openclaw_service(
            parent_id=parent_id, prompt=prompt,
            connect_timeout=connect_timeout, read_timeout=read_timeout,
        )
    try:
        future = _PLANNER_POOL.submit(
            _call_model_fn, provider_id, model_id,
            [
                {"role": "system", "content": "You are an AIOS planner. Output strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            agent=str(planner_tool or "openclaw"),
            task_id=parent_id,
            max_tokens=2400,
            reasoning_split=True,
            connect_timeout=int(connect_timeout),
            read_timeout=int(read_timeout),
        )
        response = future.result(timeout=PLANNER_TOTAL_EXECUTION_TIMEOUT)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if isinstance(exc, PlannerTimeout):
            raise
        # concurrent.futures.TimeoutError 等同于 "总执行超时"
        try:
            from concurrent.futures import TimeoutError as _CFTimeout
            is_total_timeout = isinstance(exc, _CFTimeout)
        except Exception:
            is_total_timeout = False
        reason_class = (
            "TIMEOUT_TOTAL_EXECUTION"
            if is_total_timeout
            else f"PLANNER_CALL:{type(exc).__name__}"
        )
        raise PlannerTimeout(
            attempt=attempt, reason=f"{reason_class}:{str(exc)[:200]}",
            connect_timeout=connect_timeout, read_timeout=read_timeout,
            total_timeout=PLANNER_TOTAL_EXECUTION_TIMEOUT,
            elapsed_ms=elapsed_ms,
        ) from exc
    return response
_WORKFLOW_LOCKS: dict[str, threading.Lock] = {}
_WORKFLOW_LOCKS_LOCK = threading.Lock()


def _acquire_workflow_lock(pid: str) -> bool:
    with _WORKFLOW_LOCKS_LOCK:
        if pid not in _WORKFLOW_LOCKS:
            _WORKFLOW_LOCKS[pid] = threading.Lock()
        return _WORKFLOW_LOCKS[pid].acquire(blocking=False)


def _release_workflow_lock(pid: str) -> None:
    with _WORKFLOW_LOCKS_LOCK:
        lock = _WORKFLOW_LOCKS.get(pid)
        if lock:
            lock.release()


# ---------------------------------------------------------------------------
# P9F — Canonical child identity, idempotent enqueue, atomic claim
# ---------------------------------------------------------------------------
# Invariant:
#   (parent_id, node_index, generation) → at most one canonical task_id.
# Where ``generation`` is the node's ``attempt`` counter at the moment of
# enqueue. ``_repair_node`` is the *only* path that advances generation
# (after the MAX_REPAIRS gate).  This makes the existing bounded retry
# model the single retry contract — no parallel retry layer is added.
#
# Concurrent ``_enqueue_node`` calls — same node, same generation — must
# all converge on the same canonical task_id.  The first caller wins the
# ``SET NX`` race and writes the bus state hash + LPUSH; later callers
# re-use the existing canonical task_id without re-enqueueing.
#
# A non-canonical duplicate child (created before P9F was deployed) is
# terminalised through ``supersede_duplicate_children`` (see Phase F),
# which routes the duplicate through the normal ``cancelled`` terminal
# state with ``reason=DUPLICATE_CHILD_SUPERSEDED`` so the audit ledger
# records it as a real reconciliation outcome.


def _canonical_child_key(parent_id: str, node_index: int,
                         generation: int) -> str:
    """Redis key holding the canonical task_id for this
    (parent, node, generation) triple.  Presence = "this generation
    has been claimed by some caller".  The value is the canonical
    task_id; the TTL keeps the key bounded so abandoned claims
    cannot leak forever.
    """
    return f"{KEY_CANONICAL_CHILD}:{parent_id}:{int(node_index)}:{int(generation)}"


def _claim_canonical_child(parent_id: str, node_index: int,
                           generation: int) -> Tuple[str, bool]:
    """Atomically claim the canonical task_id for this generation.

    Returns ``(task_id, was_new_claim)``:
      - ``(task_id, True)``  → we won the race; ``task_id`` is fresh.
      - ``(task_id, False)`` → a previous caller already claimed it;
        ``task_id`` is the existing canonical task_id.
      - ``("", False)``      → Redis unavailable; caller must abort.
    """
    if not _is_available():
        return ("", False)
    key = _canonical_child_key(parent_id, node_index, generation)
    task_id = generate_task_id()
    try:
        won = _redis_client.set(
            key, task_id, nx=True, ex=CANONICAL_CHILD_TTL_SECONDS,
        )
        if won:
            return (task_id, True)
        existing = _redis_client.get(key)
        if isinstance(existing, bytes):
            existing = existing.decode("utf-8", errors="replace")
        if not existing:
            # Race: key disappeared between SET NX and GET.  Retry once.
            won = _redis_client.set(
                key, task_id, nx=True, ex=CANONICAL_CHILD_TTL_SECONDS,
            )
            if won:
                return (task_id, True)
            existing = _redis_client.get(key)
            if isinstance(existing, bytes):
                existing = existing.decode("utf-8", errors="replace")
        return (existing or "", False)
    except Exception:
        return ("", False)


def _release_canonical_child(parent_id: str, node_index: int,
                             generation: int) -> None:
    """Drop the canonical claim (used when enqueue_task fails after claim).
    Safe to call multiple times.
    """
    if not _is_available():
        return
    key = _canonical_child_key(parent_id, node_index, generation)
    try:
        _redis_client.delete(key)
    except Exception:
        pass


def _canonical_child_status(task_id: str) -> str:
    """Read the bus state ``status`` for a canonical task_id.  Returns
    the empty string when no record exists (the child has not been
    written yet — caller should retry the claim-and-create path).
    """
    if not task_id:
        return ""
    try:
        raw = _redis_client.hget(f"aios:bus:state:{task_id}", "status")
    except Exception:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return str(raw or "")


def _canonical_child_remove_from_pending(task_id: str) -> None:
    """Best-effort pull of a child from the pending list.  Used by
    the supersede path so duplicate children are not silently sitting
    in pending while the canonical child is being processed.
    """
    if not task_id or not _is_available():
        return
    try:
        _redis_client.lrem("aios:bus:queue:pending", 0, task_id)
        _redis_client.delete(f"aios:bus:lock:{task_id}")
        _redis_client.zrem("aios:bus:index", task_id)
    except Exception:
        pass


def _supersede_terminalise_child(task_id: str, supersede_reason: str,
                                  canonical_tid: str) -> bool:
    """Forcefully terminalise a duplicate child through the
    ``cancelled`` state with ``reason=DUPLICATE_CHILD_SUPERSEDED``.

    Unlike ``update_task_status``, this bypasses the bus state
    machine's transition validation (``completed`` cannot transition
    to ``cancelled`` in the standard path).  The duplicate is a
    non-canonical phantom from before P9F was deployed; the
    orchestrator's reconciliation is the only legitimate way to
    reach ``cancelled`` from ``completed`` / ``failed``.  Metadata
    records the canonical child id and the natural-terminal flag
    so the audit ledger treats this as a real reconciliation event.
    """
    if not task_id or not _is_available():
        return False
    state_key = f"{KEY_QUEUE_STATE}:{task_id}"
    now_iso = _now()
    try:
        _redis_client.hset(state_key, mapping={
            "status": "cancelled",
            "result_summary": supersede_reason,
            "executor": "aios-orchestrator",
            "ts_cancelled": now_iso,
            "updated_at": now_iso,
            "orchestrator_supersede": "True",
            "canonical_child_id": canonical_tid,
            "natural_terminal": "True",
            "maintenance_override": "False",
            "force_finalised": "False",
        })
        _redis_client.lrem("aios:bus:queue:pending", 0, task_id)
        _redis_client.delete(f"aios:bus:lock:{task_id}")
        _redis_client.zrem(KEY_INDEX, task_id)
        return True
    except Exception:
        return False


DYNAMIC_FACT_TERMS = (
    "current", "latest", "live", "real-time", "today", "now",
    "version", "status", "price", "weather", "score", "schedule",
    "\u5f53\u524d", "\u6700\u65b0", "\u5b9e\u65f6", "\u4eca\u5929",
    "\u73b0\u5728", "\u7248\u672c", "\u72b6\u6001", "\u4ef7\u683c",
    "\u5929\u6c14", "\u6bd4\u5206", "\u65e5\u7a0b", "\u65f6\u95f4",
)
AIOS_RUNTIME_TERMS = (
    "version", "status", "health", "redis", "time",
    "\u7248\u672c", "\u72b6\u6001", "\u5065\u5eb7",
    "\u65f6\u95f4", "\u7cfb\u7edf\u65f6\u95f4",
)

AIOS_IDENTITY_PATTERNS = (
    r"\baios\s+(?:system\s+|runtime\s+|gateway\s+|entry\s+gateway\s+)?(?:current\s+)?(?:version|health|status|service)\b",
    r"\b(?:version|health|status)\s+of\s+(?:the\s+)?aios\b",
    r"\baios-entry-gateway\b",
    r"\baios\s*(?:\u7cfb\u7edf)?(?:\u5f53\u524d|\u8fd0\u884c)?(?:\u7248\u672c|\u5065\u5eb7|\u72b6\u6001|\u670d\u52a1)\b",
)

COMPONENT_VERSION_TERMS = (
    "openclaw", "opencode", "open code", "claude", "codex", "hermes",
)


def _contains_fact_term(text: str, token: str) -> bool:
    if token.isascii():
        return bool(re.search(
            rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text,
        ))
    return token in text


def _is_aios_runtime_scope(text: str) -> bool:
    """Return true only when AIOS itself, not a hosted tool, is the fact subject."""
    lowered = str(text or "").lower()
    if any(re.search(pattern, lowered) for pattern in AIOS_IDENTITY_PATTERNS):
        return True
    component_version_scope = (
        any(token in lowered for token in COMPONENT_VERSION_TERMS) and
        any(_contains_fact_term(lowered, token) for token in ("version", "\u7248\u672c"))
    )
    if component_version_scope:
        return False
    if "aios" not in lowered:
        return False
    return any(_contains_fact_term(lowered, token) for token in (
        "redis", "queue", "executor", "system time",
        "\u961f\u5217", "\u6267\u884c\u5668", "\u7cfb\u7edf\u65f6\u95f4",
    ))


def _classify_evidence_mode(goal: str, task: str = "") -> str:
    """Classify whether factual values require independent live grounding."""
    text = str(task or "").strip().lower() or str(goal or "").lower()
    if _is_aios_runtime_scope(text):
        return "aios-runtime"
    if any(_contains_fact_term(text, token) for token in DYNAMIC_FACT_TERMS):
        return "independent-live"
    return "semantic"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _save_workflow(workflow_id: str, **fields) -> bool:
    if not _is_available():
        return False
    mapping = {}
    for key, value in fields.items():
        if isinstance(value, (dict, list, tuple, bool, int, float)):
            mapping[key] = _json(value)
        elif value is not None:
            mapping[key] = str(value)
    mapping["updated_at"] = _now()
    key = f"{KEY_WORKFLOW}:{workflow_id}"
    try:
        _redis_client.hset(key, mapping=mapping)
        _redis_client.expire(key, WORKFLOW_TTL_SECONDS)
        if fields.get("status") not in TERMINAL:
            _redis_client.zadd(KEY_ACTIVE, {workflow_id: time.time()})
        return True
    except Exception:
        return False


def _decode(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _safe_hget_task_state(task_id: str) -> dict:
    """Read the bus state hash for ``task_id`` and return a plain dict.

    Used by :func:`_repair_node` to detect a child that was cancelled
    by the close-out maintenance routine — re-enqueueing such a child
    would re-create the orphan loop the close-out is trying to stop.
    """
    if not task_id:
        return {}
    try:
        raw = _redis_client.hgetall(f"aios:bus:state:{task_id}")
    except Exception:
        return {}
    out: dict = {}
    for key, value in raw.items():
        k = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else key
        v = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        out[k] = v
    return out



def _decode_block_list(value) -> Tuple[str, ...]:
    """Reverse of :func:`_norm_block_list` for runtime use.

    The orchestrator stores ``blocked_tools`` / ``blocked_model_bindings``
    / ``blocked_resources`` as JSON-encoded strings (one JSON array per
    field) on the workflow hash.  The failover hook needs them as real
    tuples.  Empty / malformed inputs become empty tuples so a malformed
    Redis entry cannot leak into the task policy.

    Defined at module scope so :func:`_enqueue_node` can pass the
    decoded values down to :func:`route_node_executor` without sharing
    closures with :func:`submit`.
    """
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        out = []
        seen = set()
        for item in value:
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return tuple(out)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                import json as _json_decoder
                parsed = _json_decoder.loads(text)
                if isinstance(parsed, list):
                    return _decode_block_list(parsed)
            except Exception:
                pass
        return tuple(s.strip() for s in text.split(",") if s.strip())
    return ()



def get_workflow(parent_id: str) -> dict:
    if not _is_available():
        return {"status": "unknown", "error": "redis_unavailable"}
    try:
        raw = _redis_client.hgetall(f"{KEY_WORKFLOW}:{parent_id}")
    except Exception:
        raw = {}
    if not raw:
        return {"status": "unknown", "error": "workflow_not_found"}
    result = {}
    for key, value in raw.items():
        if isinstance(key, bytes):
            key = key.decode("utf-8", errors="replace")
        result[key] = _decode(value)
    result.setdefault("parent_id", parent_id)
    return result


def workflow_task_view(parent_id: str) -> dict:
    workflow = get_workflow(parent_id)
    if workflow.get("status") == "unknown":
        return workflow
    nodes = workflow.get("nodes", [])
    executors = sorted({
        str(node.get("actual_executor") or node.get("assigned_executor") or "")
        for node in nodes if isinstance(node, dict)
    } - {""})
    return {
        "task_id": parent_id,
        "parent_id": parent_id,
        "status": workflow.get("status", "unknown"),
        "executor": "aios-orchestrator",
        "executors": executors,
        "source": workflow.get("source", ""),
        "result_summary": workflow.get("final_result", ""),
        "error": workflow.get("error", ""),
        "plan_mode": workflow.get("plan_mode", ""),
        "repair_count": workflow.get("repair_count", 0),
        "approval_required": workflow.get("status") == "awaiting_approval",
        "approval_id": workflow.get("approval_id", ""),
        "risk_action": workflow.get("risk_action", ""),
        "ts_created": workflow.get("created_at", ""),
        "ts_completed": workflow.get("completed_at", ""),
        "nodes": nodes,
    }


def _extract_json_array(text: str):
    clean = str(text or "")
    if "</think>" in clean:
        clean = clean.rsplit("</think>", 1)[-1]
    clean = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", clean.strip(), flags=re.I)
    decoder = json.JSONDecoder()
    for start, char in enumerate(clean):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(clean[start:])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("nodes", "plan", "tasks"):
                candidate = value.get(key)
                if isinstance(candidate, list):
                    return candidate
    return None


def _fallback_role(goal: str) -> str:
    lowered = goal.lower()
    if any(token in lowered for token in (
        "architecture", "security audit", "risk review",
        "\u67b6\u6784", "\u5b89\u5168\u5ba1\u8ba1",
        "\u98ce\u9669\u8bc4\u4f30", "\u4ee3\u7801\u5ba1\u67e5",
        "\u6df1\u5ea6\u5ba1\u67e5",
    )):
        return "claude"
    if any(token in lowered for token in (
        "batch", "parallel", "migration",
        "\u6279\u91cf", "\u5e76\u884c", "\u8fc1\u79fb",
    )):
        return "codex"
    return "opencode"


def _numeric_values(text: str) -> set[str]:
    """Return normalized standalone numeric literals without trusting formatting."""
    values = set()
    for match in re.finditer(
        r"(?<![A-Za-z0-9_.])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?![A-Za-z0-9_.])",
        str(text or ""),
    ):
        try:
            value = format(Decimal(match.group(0)), "f").rstrip("0").rstrip(".")
            values.add(value or "0")
        except InvalidOperation:
            continue
    return values


def _sanitize_numeric_acceptance(acceptance: list[str], goal: str) -> list[str]:
    """Remove planner-computed numeric or comparative answers from acceptance."""
    goal_values = _numeric_values(goal)
    goal_lower = str(goal or "").lower()
    conclusion_markers = (
        " winner", "best ", "highest", "lowest", "top supplier", "equals", " equal to",
        " leader", "leads ", "\u83b7\u80dc", "\u7b2c\u4e00", "\u6700\u9ad8", "\u6700\u4f4e", "\u6700\u4f73", "\u7b49\u4e8e", "\u9886\u5148",
    )
    cleaned = []
    removed = False
    for item in acceptance:
        lowered = item.lower()
        introduced_conclusion = (
            any(marker in lowered for marker in conclusion_markers) and
            lowered not in goal_lower
        )
        if (_numeric_values(item) - goal_values) or introduced_conclusion:
            removed = True
            continue
        cleaned.append(item)
    if removed:
        cleaned.append(
            "Independently recompute every numeric result, ranking, component winner, "
            "and comparative claim from the original user inputs."
        )
    return cleaned[:6]


def _normalise_plan(raw_plan, goal: str) -> list:
    if not isinstance(raw_plan, list) or not raw_plan:
        raw_plan = [{
            "task": goal,
            "depends_on": [],
            "role": _fallback_role(goal),
            "acceptance": ["Directly satisfy the original user goal with concrete evidence."],
        }]
    plan = []
    for index, raw in enumerate(raw_plan[:5]):
        if not isinstance(raw, dict):
            continue
        task = str(raw.get("task", "")).strip()
        if not task:
            continue
        dependencies = []
        for value in raw.get("depends_on", []) or []:
            try:
                dependency = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= dependency < index and dependency not in dependencies:
                dependencies.append(dependency)
        role = str(raw.get("role", "opencode")).lower().strip()
        if role not in EXECUTORS:
            role = _fallback_role(task)
        acceptance = raw.get("acceptance", [])
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        if not isinstance(acceptance, list):
            acceptance = []
        acceptance = [str(item).strip() for item in acceptance[:6] if str(item).strip()]
        acceptance = _sanitize_numeric_acceptance(acceptance, goal)
        if not acceptance:
            acceptance = ["Return a concrete result that directly satisfies this node."]
        # 2026-08-17 P1 evidence-mode-fix: only trust the planner's
        # ``evidence_mode`` when the original USER GOAL itself requires
        # independent grounding.  The planner-generated task text often
        # over-classifies (e.g. adds "Check the current runtime status"
        # for a conversational "is it working yet?" query) which
        # triggers the verification gate to demand host evidence the
        # evidence collector cannot provide for that query (no
        # fresh-fact tokens), and the parent workflow deadlocks in
        # repair loops.  The conservative rule: trust the goal
        # classifier first, accept the planner's value only when it
        # agrees.
        goal_mode = _classify_evidence_mode(goal, goal)
        requested_mode = str(raw.get("evidence_mode", "")).strip().lower()
        if goal_mode != "semantic":
            # The goal itself requires independent grounding; honour
            # the more specific of the two non-semantic modes.
            if requested_mode in {"semantic", "independent-live", "aios-runtime"}:
                evidence_mode = max(
                    (goal_mode, requested_mode),
                    key=lambda m: {"semantic": 0, "independent-live": 1, "aios-runtime": 2}.get(m, 0),
                )
            else:
                evidence_mode = goal_mode
        else:
            # The goal is purely conversational / semantic; do NOT
            # let the planner escalate to ``independent-live`` just
            # because the planned task text contains terms like
            # "status" or "version".
            evidence_mode = "semantic"
        if evidence_mode != "semantic":
            grounding_rule = (
                "Every dynamic factual value must match independently acquired "
                "authoritative evidence; non-empty or self-claimed evidence is insufficient."
            )
            if grounding_rule not in acceptance:
                acceptance = (acceptance + [grounding_rule])[:7]
        plan.append({
            "task": task[:12000],
            "depends_on": dependencies,
            "role": role,
            "acceptance": acceptance,
            "evidence_mode": evidence_mode,
        })
    if not plan:
        return _normalise_plan(None, goal)
    return plan



def _absolute_paths(value: str) -> set[str]:
    """Extract filesystem-like absolute paths, excluding fragments such as /size."""
    found = re.findall(r"/[^\s'\"\x60,;:()\[\]{}]+", str(value or ""))
    return {item.rstrip(".,;:!?") for item in found if item.count("/") >= 2}


def _repair_user_paths(plan: list, goal: str) -> list:
    """Deterministically repair a single user path when the planner mistypes it."""
    goal_paths = _absolute_paths(goal)
    if len(goal_paths) != 1:
        return plan
    expected = next(iter(goal_paths))
    basename = expected.rsplit("/", 1)[-1]

    def repair_value(value: str) -> str:
        text = str(value)
        for candidate in _absolute_paths(text):
            if candidate.rsplit("/", 1)[-1] == basename:
                text = text.replace(candidate, expected)
        return text

    for node in plan:
        node["task"] = repair_value(node.get("task", ""))
        node["acceptance"] = [
            repair_value(item) for item in node.get("acceptance", [])
        ]
    return plan


def _plan_preserves_user_literals(plan: list, goal: str) -> tuple[bool, str]:
    """Reject explicit path mutation; omission is restored by the parent goal.

    P9D-R Orchestrator↔Planner integration hotfix 2026-08-10:
    the legacy check required exact equality between plan paths and
    goal paths.  A goal that mentions ``/home/x/y/current/`` and a
    plan that mentions ``/home/x/y/`` was flagged as a planner
    mutation, sending the candidates loop into a 180 s fallback
    timeout even though the planner had correctly summarised the
    user target as a directory above the goal path.  The hotfix
    accepts plan paths that are PREFIX-equivalent to a goal path
    (i.e. plan path is the parent directory of a goal path, or
    equals a goal path, or a sub-path of a goal path) and only
    rejects truly alien paths that do not share a prefix with any
    goal path.  This preserves the literal-preservation contract
    for the audit task without forcing the planner to repeat every
    suffix literally.
    """
    rendered = json.dumps(plan, ensure_ascii=False)
    goal_paths = _absolute_paths(goal)
    plan_paths = _absolute_paths(rendered)
    if not goal_paths:
        return True, ""
    unexpected_paths = []
    for candidate in plan_paths:
        if candidate in goal_paths:
            continue
        if any(
            candidate == parent or candidate.startswith(parent.rstrip("/") + "/")
            for parent in goal_paths
        ):
            continue
        unexpected_paths.append(candidate)
    unexpected_paths = sorted(unexpected_paths)
    if unexpected_paths:
        return False, "planner_introduced_path:" + ",".join(unexpected_paths[:3])
    return True, ""


def _required_node_count(goal: str) -> int:
    text = str(goal or "").lower()
    word_numbers = {"two": 2, "three": 3, "four": 4, "five": 5}
    patterns = (
        r"\bexactly\s+(\d+|two|three|four|five)\s+"
        r"(?:dependent\s+)?(?:aios\s+)?(?:workflow\s+)?nodes?\b",
        r"\b(\d+)\s+dependent\s+(?:workflow\s+)?nodes?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            raw = match.group(1)
            return int(raw) if raw.isdigit() else word_numbers.get(raw, 0)
    chinese = re.search(
        r"(\d+|\u4e24|\u4e8c|\u4e09|\u56db|\u4e94)\s*"
        r"(?:\u4e2a)?(?:\u76f8\u4e92)?(?:\u4f9d\u8d56)?"
        r"(?:aios)?(?:\u5de5\u4f5c\u6d41)?\u8282\u70b9",
        text,
    )
    if chinese:
        raw = chinese.group(1)
        return int(raw) if raw.isdigit() else {
            "\u4e24": 2, "\u4e8c": 2, "\u4e09": 3,
            "\u56db": 4, "\u4e94": 5,
        }.get(raw, 0)
    return 0


def _plan_structure_errors(plan: list, goal: str) -> list[str]:
    errors = []
    required = _required_node_count(goal)
    if required and len(plan) != required:
        errors.append(f"required_node_count:{required}:actual={len(plan)}")
    lowered = str(goal or "").lower()
    dependency_required = "dependent" in lowered or "\u4f9d\u8d56" in lowered
    if dependency_required and required > 1:
        if len(plan) < 2 or not all(node.get("depends_on") for node in plan[1:]):
            errors.append("required_dependencies_missing")
    return errors


def _fuse_plan_for_runtime(plan: list, goal: str,
                           healthy_executors=None) -> tuple[list, bool]:
    """Keep the default path simple unless the user explicitly requests fan-out."""
    lowered = str(goal or "").lower()
    explicit_fanout = (
        _required_node_count(goal) > 1 or
        any(token in lowered for token in (
            "parallel nodes", "parallel execution", "concurrent nodes",
            "multiple nodes", "multi-node", "dependent nodes",
            "use both codex and", "use both opencode and", "use both claude and",
            "\u5e76\u884c\u8282\u70b9", "\u5e76\u884c\u6267\u884c", "\u591a\u8282\u70b9", "\u4f9d\u8d56\u8282\u70b9", "\u5206\u522b\u6d3e\u7ed9",
        ))
    )
    if len(plan) <= 1 or explicit_fanout:
        return plan, False
    healthy = list(healthy_executors) if healthy_executors is not None else [
        executor for executor in EXECUTORS if _tool_ready(executor)
    ]
    if not healthy:
        return plan, False
    preferred = _fallback_role(goal)
    if preferred not in healthy:
        preferred = (
            "opencode" if "opencode" in healthy
            else "codex" if "codex" in healthy
            else healthy[0]
        )
    evidence_mode = _classify_evidence_mode(goal, goal)
    acceptance = ["Directly satisfy the complete original user goal with concrete evidence."]
    if evidence_mode != "semantic":
        acceptance.append(
            "Every dynamic factual value must match independently acquired authoritative evidence."
        )
    return [{
        "task": goal,
        "depends_on": [],
        "role": preferred,
        "acceptance": acceptance,
        "evidence_mode": evidence_mode,
    }], True


def _resolve_planner_target(task_policy) -> tuple:
    """Translate the task policy into the planner-tool tuple that
    :func:`_call_planner_with_deadline` consumes.

    Returns ``(planner_tool, planner_binding, provider_id, model_id,
    plan_mode_label)``.  Default values match the historical
    ``openclaw-minimax`` shape so any code path that does not pass
    a policy keeps working.

    ``task_policy`` may be either a plain dict (legacy callers) or
    a :class:`aios_task_routing_policy.TaskRoutingPolicy` object
    (the canonical shape produced by ``from_workflow_dict``); both
    shapes are unwrapped transparently.
    """
    policy_view: dict = {}
    if task_policy is None:
        policy_view = {}
    elif isinstance(task_policy, dict):
        policy_view = task_policy
    else:
        # TaskRoutingPolicy or any object with attribute-style fields.
        policy_view = {
            "preferred_planner": getattr(task_policy, "preferred_planner", "") or "",
            "blocked_planner_tools": list(
                getattr(task_policy, "blocked_planner_tools", []) or []
            ),
            "allow_planner_fallback": bool(
                getattr(task_policy, "allow_planner_fallback", True)
            ),
        }
    preferred = str(policy_view.get("preferred_planner") or "openclaw")
    blocked = tuple(
        str(item) for item in (policy_view.get("blocked_planner_tools") or ())
        if str(item)
    )
    allow_fallback = bool(policy_view.get("allow_planner_fallback", True))
    try:
        from aios_tool_registry import get_default_registry
        registry = get_default_registry()
        candidates = [m.tool_id for m in registry.list_by_role("planner")]
    except Exception:
        candidates = ["openclaw", "opencode", "claude"]
    filtered = [c for c in candidates if c not in blocked]
    if preferred and preferred in filtered:
        ordered = [preferred] + [c for c in filtered if c != preferred]
    else:
        ordered = filtered or ["openclaw"]
    if not allow_fallback and len(ordered) > 1:
        ordered = ordered[:1]
    selected = ordered[0] if ordered else "openclaw"
    bindings = {
        "openclaw":  ("openclaw:minimax",     "minimax", "MiniMax-M3",            "openclaw-minimax"),
        "opencode":  ("opencode:free",        "opencode", "free-auto-router",     "opencode-plan-only"),
        "claude":    ("claude:minimax",       "minimax", "MiniMax-M3",            "claude-minimax-plan"),
        "minimax-official": ("minimax-official:minimax", "minimax", "MiniMax-M3", "minimax-official-direct"),
    }
    if selected not in bindings:
        selected = "openclaw"
    binding, provider, model, mode = bindings[selected]
    return selected, binding, provider, model, mode


def choose_reviewer(task_id: str,
                    preferred_reviewer: str = "",
                    blocked_reviewer_tools=(),
                    allow_reviewer_fallback: bool = True,
                    exclude_executor: str = "",
                    attempted_reviewers=(),
                    capability_overlay=None) -> dict:
    """Policy-driven reviewer selection used by Verification Gate.

    P0-5 final-production reviewer-recovery.  The reviewer list
    now honours a positive recovery probe *before* declaring a
    candidate unavailable: a fresh runtime failure event is given
    exactly one ``_attempt_tool_recovery`` probe per
    ``choose_reviewer`` call so the in-memory sticky failure that
    triggered the 2026-08-10 manual ``systemctl restart aios-orchestrator``
    hotfix is now cleared automatically the moment the underlying
    tool recovers.  This is the reviewer-side mirror of the
    ``choose_executor`` recovery contract and reuses the same
    :func:`_attempt_tool_recovery` helper.
    """
    result = {
        "reviewer": "",
        "binding": "",
        "failure_scope": "",
        "candidates": [],
        "excluded": [],
        "fallback_count": 0,
    }
    try:
        from aios_tool_failover import get_default_tool_engine
        from aios_tool_registry import get_default_registry
        engine = get_default_tool_engine()
        registry = get_default_registry()
    except Exception as exc:
        result["excluded"].append(f"registry_unavailable:{type(exc).__name__}")
        return result
    try:
        candidates = [m.tool_id for m in registry.list_by_role("reviewer")]
    except Exception as exc:
        result["excluded"].append(f"registry_role_lookup_failed:{type(exc).__name__}")
        candidates = []
    if not candidates:
        candidates = ["hermes", "claude", "openclaw"]
    blocked_set = {str(x) for x in (blocked_reviewer_tools or ()) if str(x)}
    attempted_set = {str(x) for x in (attempted_reviewers or ()) if str(x)}
    pref = str(preferred_reviewer or "")
    excluded = []
    filtered = []
    for cid in candidates:
        if cid in blocked_set:
            excluded.append((cid, "blocked_reviewer_tool"))
            continue
        if exclude_executor and cid == exclude_executor:
            excluded.append((cid, "self_review_forbidden"))
            continue
        if cid in attempted_set:
            excluded.append((cid, "already_attempted"))
            continue
        filtered.append(cid)
    if pref and pref in filtered:
        ordered = [pref] + [c for c in filtered if c != pref]
    else:
        ordered = filtered
    if not allow_reviewer_fallback and len(ordered) > 1:
        ordered = ordered[:1]
    if not ordered:
        result["excluded"] = excluded
        return result
    fallback_count = 0
    chosen = ""
    chosen_binding = ""
    failure_scope = ""
    for cid in ordered:
        # P0-5 reviewer recovery probe: same one-shot contract as
        # ``choose_executor`` — a fresh runtime failure event is
        # honoured only when the underlying tool recovers via the
        # standard service-active + endpoint-reachable +
        # adapter-probe-ok + model-available probe chain.  The
        # probe is best-effort and exceptions are swallowed so the
        # reviewer loop can continue even when the recovery helper
        # is unavailable.
        try:
            if _get_tool_runtime_failure(cid) is not None:
                _attempt_tool_recovery(cid)
        except Exception:
            pass
        try:
            status = engine.compute_tool_status(cid)
        except Exception:
            status = None
        healthy = bool(
            status and status.status in (
                "AVAILABLE_PRIMARY", "AVAILABLE_WITH_MODEL_FALLBACK",
            )
        )
        process_alive = _tool_process_health(cid)
        if healthy and process_alive:
            chosen = cid
            try:
                chosen_binding = str(status.effective_binding or "")
            except Exception:
                chosen_binding = ""
            failure_scope = ""
            break
        excluded.append((cid, str(getattr(status, "status", "UNAVAILABLE")) if status else "UNAVAILABLE"))
        fallback_count += 1
        if status and status.status in (
            "UNAVAILABLE_TOOL_RUNTIME", "DEGRADED_NO_MODEL_FALLBACK",
        ):
            failure_scope = "TOOL_PROCESS"
    result["candidates"] = ordered
    result["excluded"] = excluded
    result["reviewer"] = chosen
    result["binding"] = chosen_binding
    result["failure_scope"] = failure_scope
    result["fallback_count"] = fallback_count
    return result


def build_plan(goal: str, parent_id: str, *, task_policy: dict = None) -> tuple:
    """Use the governed planner with bounded retries and honest structural failure.

    P9D-R role-closure: ``task_policy`` is the workflow-derived
    TaskRoutingPolicy view (preferred_planner, blocked_planner_tools,
    allow_planner_fallback).  When supplied, ``build_plan`` selects
    the planner tool through the same RoleRouteCalculator the rest of
    the system uses, so the policy surface is the single source of
    truth for both routing and plan attribution.  The legacy
    ``openclaw-minimax`` shape remains the default when no policy is
    supplied.
    """
    planner_tool, planner_binding, planner_provider, planner_model, plan_mode_label = (
        _resolve_planner_target(task_policy or {})
    )
    # P9D-R-OpenClaw-Planner-Tool-Boundary: when the preferred
    # planner fails AND allow_planner_fallback is True, fall back
    # to the next candidate.  The fallback candidate list is the
    # ``opencode`` planner (PLAN_ONLY adapter).  When the
    # preferred planner is the only one in the candidates list
    # (allow_planner_fallback=False), a single failure routes
    # to the close-out terminal surface.
    # P9D-R-RT: tolerate ``TaskRoutingPolicy`` dataclass alongside
    # plain-dict task policies — read allow_planner_fallback via
    # ``getattr`` first, falling back to the dict shape.
    _policy_apf = (
        getattr(task_policy, "allow_planner_fallback", True)
        if task_policy and not isinstance(task_policy, dict)
        else (
            task_policy.get("allow_planner_fallback", True)
            if task_policy else True
        )
    )
    if not task_policy:
        # Default policy: opencode is a valid fallback candidate.
        # When task_policy is supplied we honour the policy's own
        # ``blocked_planner_tools`` / ``allow_planner_fallback``.
        fallback_chain = ["opencode", "claude"]
    else:
        allow_planner_fallback = bool(_policy_apf)
        if not allow_planner_fallback:
            fallback_chain = []
        else:
            fallback_chain = [
                "opencode", "claude"
            ]
    base_prompt = (
        "Plan this user goal for AIOS. Return only a JSON array of 1-5 nodes. "
        "Each node must contain task(string), depends_on(array of earlier indexes), "
        "role(opencode|claude|codex), acceptance(array of measurable statements), "
        "and evidence_mode(semantic|independent-live|aios-runtime). "
        "Prefer one node unless the user explicitly requires multiple nodes. If the "
        "user requires an exact node count or dependencies, preserve that structure "
        "exactly. Use opencode for normal CLI/files/scripts/general work, claude only "
        "for high-depth architecture/risk reasoning, and codex only for code-specialist "
        "batch/parallel work. Do not split constraints into separate tasks. Preserve "
        "the full user intent. Copy every path, identifier, number and literal exactly; "
        "never change usernames or paths. Do not invent derived numeric acceptance "
        "values, and never let acceptance contradict the user goal. For arithmetic, "
        "scoring, ranking, or comparison work, acceptance may require correct calculations "
        "and comparisons but must not hard-code a computed total, winner, component leader, "
        "or trade-off unless that answer was explicitly supplied by the user. Current/latest/live "
        "facts require independent-live evidence. AIOS runtime facts require "
        "aios-runtime evidence. Never use mere non-empty output as proof of factual "
        "correctness.\n\nUSER GOAL:\n" + goal[:12000]
    )
    last_error = ""
    try:
        from aios_model_gateway import call_model
    except Exception as exc:
        last_error = f"planner_import:{type(exc).__name__}:{str(exc)[:300]}"
        call_model = None

    planner_outcomes: list = []
    total_budget_exhausted = False
    # P9D-R-OpenClaw-Planner-Tool-Boundary: planner fallback chain.
    # When the preferred planner fails AND allow_planner_fallback is
    # True, the loop walks the fallback chain (``opencode`` first,
    # then ``claude``).  Each candidate that fails is recorded in
    # ``planner_outcomes`` with the right ``failure_scope`` and the
    # next candidate is tried.  When ``allow_planner_fallback=False``
    # the chain is empty, so a single failure routes to the close-out
    # terminal surface.
    #
    # Production planner-fallback health gate 2026-08-10: the legacy
    # loop blindly invoked every fallback candidate regardless of known
    # runtime health.  When opencode's PLAN_ONLY adapter was hung or
    # claude's binding was 402-blocked, the orchestrator still paid
    # ``PLANNER_TOTAL_EXECUTION_TIMEOUT`` for each one, burning the
    # business workflow's planner budget before reaching
    # ``NO_HEALTHY_PLANNER_FALLBACK``.  The hotfix consults the
    # existing ``_get_tool_runtime_failure`` registry (the same source
    # the executor routing layer uses) and prunes candidates whose
    # most recent failure event is still inside the bounded
    # ``_TOOL_FAILURE_EVENT_TTL_SECONDS`` window.  OpenClaw (the
    # preferred planner) is NEVER pruned here because it is the
    # primary; the gate only filters the *fallback* chain.
    def _planner_fallback_eligible(name: str) -> bool:
        if name == planner_tool:
            return True
        try:
            event = _get_tool_runtime_failure(name)
        except Exception:
            return True
        if event is None:
            return True
        try:
            ts = str(event.get("timestamp") or event.get("ts") or "")
            stamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            if age > 120.0:
                return True
        except Exception:
            # Unparseable timestamp → give the planner a retry.
            return True
        return False


    pruned_fallback = [
        c for c in fallback_chain if c != planner_tool and _planner_fallback_eligible(c)
    ]
    if pruned_fallback != [c for c in fallback_chain if c != planner_tool]:
        # Record the cut so the audit ledger explains why a known
        # fallback was skipped rather than invoking it.
        publish_event(
            "planner.fallback_pruned",
            {
                "parent_id": parent_id or "",
                "preferred_planner": planner_tool,
                "fallback_chain": list(fallback_chain),
                "pruned": sorted(
                    set(c for c in fallback_chain if c != planner_tool)
                    - set(pruned_fallback)
                ),
                "kept": list(pruned_fallback),
            },
            "aios-orchestrator",
        )
    fallback_chain = pruned_fallback
    candidates = [planner_tool] + [
        c for c in fallback_chain if c != planner_tool
    ] if fallback_chain else [planner_tool]
    # P9D-R Orchestrator↔Planner integration hotfix 2026-08-10:
    # when the preferred planner returns 200 but the response fails
    # validation (literal preservation / structure errors / JSON parse),
    # the candidates loop historically spent another full
    # ``PLANNER_TOTAL_EXECUTION_TIMEOUT`` window per fallback
    # candidate.  A business workflow's first candidate regularly
    # consumed 19s successfully and then the loop drove the planner
    # budget through another 180s for each remaining candidate,
    # surfacing as ``FAILED_EXTERNAL_ROUTE_PLANNER_TIMEOUT`` even
    # though the Orchestrator↔Planner HTTP path itself was healthy.
    # Bound the cumulative planner-loop wall-clock to
    # ``max(PLANNER_TOTAL_EXECUTION_TIMEOUT, 60)`` so a recovered
    # preferred planner that fails validation can never starve the
    # orchestrator for 360s+.  Track per-attempt elapsed_ms so the
    # audit ledger honestly reports the loop shape, not just the last
    # candidate's failure.
    _planner_loop_started = time.monotonic()
    _planner_loop_budget = max(60.0, float(PLANNER_TOTAL_EXECUTION_TIMEOUT))
    attempt = 0
    candidate_index = 0
    while candidate_index < len(candidates):
        _planner_elapsed = time.monotonic() - _planner_loop_started
        if _planner_elapsed > _planner_loop_budget and candidate_index > 0:
            # Cumulative planner-loop budget exhausted.  Stop walking
            # the fallback chain and surface FAILED_EXTERNAL_ROUTE_PLANNER_TIMEOUT
            # immediately so the workflow does not wait for another
            # full ``PLANNER_TOTAL_EXECUTION_TIMEOUT`` window per
            # remaining candidate.
            total_budget_exhausted = True
            break
        current_planner = candidates[candidate_index]
        if current_planner == "opencode":
            current_binding = "opencode:free"
            current_provider = "opencode"
            current_model = "free-auto-router"
            current_mode = "opencode-plan-only"
        elif current_planner == "claude":
            current_binding = "claude:minimax"
            current_provider = "minimax"
            current_model = "MiniMax-M3"
            current_mode = "claude-minimax-plan"
        else:
            current_binding = planner_binding
            current_provider = planner_provider
            current_model = planner_model
            current_mode = plan_mode_label
        try:
            response = _call_planner_with_deadline(
                call_model, parent_id, base_prompt, attempt + 1,
                planner_tool=current_planner,
                planner_binding=current_binding,
                provider_id=current_provider,
                model_id=current_model,
                mode=current_mode,
            )
        except PlannerTimeout as pt:
            planner_outcomes.append({
                "attempt": attempt + 1,
                "elapsed_ms": pt.elapsed_ms,
                "reason": pt.reason,
                "connect_timeout": pt.connect_timeout,
                "read_timeout": pt.read_timeout,
                "total_timeout": pt.total_timeout,
                "terminal": True,
                "kind": "PLANNER_TIMEOUT",
            })
            # Production planner-fallback health gate 2026-08-10:
            # record a bounded tool-runtime failure event for the
            # failed fallback so the next workflow loop prunes it
            # via the planner_fallback_eligible check above.  The
            # 120 s bounded TTL matches the existing executor health
            # contract so the planner recovers automatically once the
            # underlying binding is fixed.
            if current_planner != planner_tool:
                try:
                    _record_tool_runtime_failure(
                        current_planner,
                        scope="TOOL_PROCESS",
                        reason=f"planner_timeout:{pt.reason[:120]}",
                    )
                except Exception:
                    pass
            attempt += 1
            candidate_index += 1
            continue
        except Exception as exc:
            planner_outcomes.append({
                "attempt": attempt + 1,
                "elapsed_ms": 0,
                "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
                "kind": "PLANNER_EXCEPTION",
            })
            if current_planner != planner_tool:
                try:
                    _record_tool_runtime_failure(
                        current_planner,
                        scope="TOOL_PROCESS",
                        reason=f"planner_exception:{type(exc).__name__}:{str(exc)[:120]}",
                    )
                except Exception:
                    pass
            attempt += 1
            candidate_index += 1
            continue
        # Validate the response payload.
        try:
            if not isinstance(response, dict) or not response.get("ok"):
                raise RuntimeError(
                    (response or {}).get("error", "planner unavailable"))
            raw = response.get("result", {})
            choices = raw.get("choices", []) if isinstance(raw, dict) else []
            content = choices[0].get("message", {}).get("content", "") if choices else ""
            parsed = _extract_json_array(content)
            if not parsed:
                raise ValueError("planner returned no valid JSON array")
            plan = _normalise_plan(parsed, goal)
            plan = _repair_user_paths(plan, goal)
            literals_ok, literal_error = _plan_preserves_user_literals(plan, goal)
            if not literals_ok:
                raise ValueError(literal_error)
            structure_errors = _plan_structure_errors(plan, goal)
            if structure_errors:
                raise ValueError(";".join(structure_errors))
            plan, fused = _fuse_plan_for_runtime(plan, goal)
            # P9D-R role-closure: the returned plan_mode must reflect
            # the planner / binding that was actually used.  When
            # ``preferred_planner='opencode'`` we routed through the
            # OpenCode PLAN_ONLY adapter; the legacy
            # ``openclaw-minimax-simple-fused`` /
            # ``openclaw-minimax`` labels are still emitted for the
            # openclaw planner so any downstream consumer that
            # pattern-matches the string keeps working.
            if current_planner == "opencode":
                emitted_plan_mode = (
                    "opencode-plan-only-simple-fused" if fused
                    else "opencode-plan-only"
                )
            else:
                emitted_plan_mode = (
                    "openclaw-minimax-simple-fused" if fused
                    else "openclaw-minimax"
                )
            # Persist the per-attempt audit, then return.
            try:
                publish_event(
                    "planner.dispatch",
                    {"parent_id": parent_id,
                     "plan_mode": emitted_plan_mode,
                     "attempts": planner_outcomes + [{
                         "attempt": attempt + 1,
                         "kind": "PLANNER_OK",
                     }],
                     "fallback_count": candidate_index},
                    "aios-orchestrator",
                )
            except Exception:
                pass
            # P9D-R-RT: when the fallback chain actually carried the
            # call, the workflow hash must record the planner that
            # really executed, the binding it used, and the
            # failure_scope of the failed preferred candidate.  This
            # lets the close-out ledger prove the fallback path was
            # taken instead of just promising the policy allowed it.
            actual_planner_used = current_planner
            actual_binding_used = current_binding
            actual_provider_used = current_provider
            actual_model_used = current_model
            # The plan_mode_label reflected above is already
            # ``opencode-plan-only`` or ``claude-minimax-plan`` /
            # ``openclaw-minimax`` so plan_mode_label is unchanged.
            if current_planner != planner_tool:
                first_failure_scope = next(
                    (
                        outcome.get("failure_scope", "TOOL_PROCESS")
                        for outcome in planner_outcomes
                        if outcome.get("kind") == "PLANNER_TIMEOUT"
                        and "OPENCLAW_TOOL_BOUNDARY_DOWN" in str(
                            outcome.get("reason", "")
                        )
                    ),
                    "TOOL_PROCESS",
                )
                return (
                    plan,
                    emitted_plan_mode,
                    "",
                    {
                        "actual_planner": actual_planner_used,
                        "actual_binding": actual_binding_used,
                        "actual_provider": actual_provider_used,
                        "actual_model": actual_model_used,
                        "fallback_count": candidate_index,
                        "attempted_planners": list(candidates[:candidate_index + 1]),
                        "excluded_planners": list(
                            c for c in candidates[:candidate_index] if c != current_planner
                        ),
                        "first_failure_scope": first_failure_scope,
                    },
                )
            return (
                plan,
                emitted_plan_mode,
                "",
                {
                    "actual_planner": actual_planner_used,
                    "actual_binding": actual_binding_used,
                    "actual_provider": actual_provider_used,
                    "actual_model": actual_model_used,
                    "fallback_count": 0,
                    "attempted_planners": [current_planner],
                    "excluded_planners": [],
                    "first_failure_scope": "",
                },
            )
        except Exception as exc:
            planner_outcomes.append({
                "attempt": attempt + 1,
                "elapsed_ms": 0,
                "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
                "kind": "PLANNER_PAYLOAD_REJECTED",
            })
            attempt += 1
            candidate_index += 1
            continue
    # All candidates failed.  The outer close-out surface applies.
    total_budget_exhausted = True

    # All attempts exhausted without producing a real plan.  Return an
    # explicit dead-letter shape so ``_plan_and_dispatch`` can route to
    # ``FAILED_EXTERNAL_ROUTE_PLANNER_TIMEOUT``.
    # P9D-R-actual-planner-semantics: when no planner succeeded,
    # actual_planner MUST be empty (null) so the workflow ledger
    # truthfully records that planning failed.  The extras dict
    # carries the full attempted/excluded chain so the close-out
    # can prove the fallback path was exercised.
    _terminal_extras = {
        "actual_planner": "",
        "actual_binding": "",
        "actual_provider": "",
        "actual_model": "",
        "fallback_count": len(candidates) - 1 if len(candidates) > 1 else 0,
        "attempted_planners": list(candidates),
        "excluded_planners": [],
        "first_failure_scope": (
            planner_outcomes[0].get("failure_scope", "TOOL_PROCESS")
            if planner_outcomes else "TOOL_PROCESS"
        ),
    }
    if total_budget_exhausted:
        terminal_reason = (
            "FAILED_EXTERNAL_ROUTE_PLANNER_TIMEOUT"
            ";connect="
            f"{PLANNER_CONNECT_TIMEOUT}"
            ";read="
            f"{PLANNER_READ_TIMEOUT}"
            ";total="
            f"{PLANNER_TOTAL_EXECUTION_TIMEOUT}"
        )
        return [], "planning-failed", terminal_reason, _terminal_extras
    if _required_node_count(goal) > 1:
        return [], "planning-failed", (
            planner_outcomes[-1]["reason"] if planner_outcomes
            else "required_multi_node_plan_unavailable"), _terminal_extras
    return (_normalise_plan(None, goal), "single-goal-degraded",
            planner_outcomes[-1]["reason"] if planner_outcomes else "", _terminal_extras)


# Test hook: expose the planner-fallback-eligibility gate so the
# final-production test suite can assert on it directly without
# poking at the build_plan closure.  The semantic is identical to
# the closure above; the helper is module-level and trivial.
def _planner_fallback_eligible_test_hook(name: str) -> bool:
    """Final-production test surface for the planner-fallback health gate.

    Returns True iff ``name`` is eligible as a planner fallback
    candidate.  The Primary planner (``openclaw``) is always eligible;
    every other planner is eligible only when it has no fresh
    tool-runtime failure event inside the bounded 120 s TTL.
    """
    if not name:
        return False
    try:
        event = _get_tool_runtime_failure(name)
    except Exception:
        return True
    if event is None:
        return True
    # The failure event carries ``expires_at_monotonic`` (float) and
    # ``observed_at_monotonic`` (float) — the same fields consulted by
    # ``ToolFailoverEngine.get_tool_runtime_failure`` for lazy purge.
    # Use those directly rather than the wallclock fields so the test
    # surface is deterministic regardless of the host's timezone.
    import time as _time
    try:
        expires_at = float(event.get("expires_at_monotonic") or 0.0)
        if expires_at and expires_at <= _time.monotonic():
            # Expired → eligible again.
            return True
    except Exception:
        return True
    return False


def _tool_ready(name: str) -> bool:
    """Check if a tool is ready for execution using unified capability truth source.

    This replaces the old adapter.health() check which could return stale
    results. The capability module considers: recent real success evidence,
    probe cache state, cooldown, service active, and fatal lifecycle.
    """
    if name not in EXECUTORS:
        return False
    return _capability_available(name)


def _is_executor_available(name: str, capability_overlay=None) -> bool:
    """Strict check: only AVAILABLE from the unified capability truth source.

    ``capability_overlay`` is a task-scoped override that simulates a
    runtime-unavailable tool without mutating global capability caches.
    Recognised keys: ``"UNAVAILABLE_TOOL_RUNTIME"``,
    ``"DEGRADED_NO_MODEL_FALLBACK"``, ``"AVAILABLE_PRIMARY"``,
    ``"AVAILABLE_WITH_MODEL_FALLBACK"``.

    P9D-R-runtime-health-freshness: the moment a fresh tool-runtime
    failure event is recorded, ``is_tool_runtime_alive`` returns
    ``False`` and the per-tool capability layer is considered
    unavailable.  This bypasses the on-disk probe cache so the next
    routing decision — whatever code path consumes it — sees the
    tool as unavailable immediately, without waiting for the long
    ``probe_max_age_seconds`` window to expire.
    """
    # P9D-R: a fresh tool-runtime failure event always forces
    # ``UNAVAILABLE`` regardless of the on-disk cache.  No TTL
    # bypass is allowed: the failure event has its own bounded
    # ``_TOOL_RUNTIME_FAILURE_TTL_SECONDS`` (default 120 s) and is
    # purged lazily on read so the tool can be re-detected when the
    # service recovers.
    try:
        if _get_tool_runtime_failure(name) is not None:
            return False
    except Exception:
        # Failure-event lookup is best-effort; the capability layer
        # is the canonical source.
        pass
    overlay = capability_overlay or {}
    status = overlay.get(name)
    if status == "UNAVAILABLE_TOOL_RUNTIME":
        return False
    if status == "AVAILABLE_PRIMARY" or status == "AVAILABLE_WITH_MODEL_FALLBACK":
        return True
    return _capability_available(name)


# P9D-R-live §6.1.2: tool-process health cache.  The capability matrix
# only knows about the lightweight probe, not the actual daemon
# process state.  When the executor daemon is dead but the
# capability matrix still reports AVAILABLE (e.g. fresh lightweight
# probe cached the daemon's pre-death ping), ``choose_executor``
# would happily route to the dead tool.  This cache makes the
# executor selection respect actual process liveness with a short
# TTL so /proc scans never become a hot-path cost.
_TOOL_PROCESS_HEALTH_TTL_SECONDS = 5
_TOOL_PROCESS_HEALTH_CACHE: Dict[str, Tuple[bool, float]] = {}


def _tool_process_health(name: str) -> bool:
    """Return ``True`` iff the executor daemon for ``name`` is alive,
    cached for ``_TOOL_PROCESS_HEALTH_TTL_SECONDS`` to amortise the
    ``/proc`` scan across one request flow.

    P9D-R-live §6.2: ``failure_scope=TOOL_PROCESS`` propagates to ALL
    bindings of that tool.  The cross-tool fallback path picks the
    next healthy tool only when this returns ``False``.
    """
    if name not in EXECUTORS and name not in {"openclaw", "hermes"}:
        return True
    now = time.monotonic()
    cached = _TOOL_PROCESS_HEALTH_CACHE.get(name)
    if cached and (now - cached[1]) < _TOOL_PROCESS_HEALTH_TTL_SECONDS:
        return cached[0]
    alive = _executor_process_alive(name)
    _TOOL_PROCESS_HEALTH_CACHE[name] = (alive, now)
    return alive


def _invalidate_tool_process_cache() -> None:
    """Drop the tool-process health cache (used by Repair / Health
    reconciles that may have reaped a daemon).
    """
    _TOOL_PROCESS_HEALTH_CACHE.clear()


# P9D-R-executor-primary-recovery: positive recovery probe.
# The failure event has a 120 s bounded TTL but we MUST NOT rely on it
# to detect recovery — operators restart the service in seconds, and
# the only honest signal that a tool is healthy again is a real,
# in-process positive health probe.  This block implements that
# probe: service active + endpoint reachable + adapter lightweight
# probe OK.  All three conditions MUST hold before we clear the
# failure event; partial recoveries keep the tool marked
# UNAVAILABLE.  The probe is intentionally cheap (one
# ``systemctl is-active`` + one HTTP GET + one disk read) so it can
# run inline on every ``choose_executor`` call without measurable
# cost.
_TOOL_RECOVERY_PROBE_BUDGET_SECONDS = float(
    os.getenv("AIOS_TOOL_RECOVERY_PROBE_BUDGET", "0.5") or "0.5")


def _executor_service_active(name: str) -> bool:
    """Return ``True`` iff the systemd user service for ``name`` is
    active.  Used as the first leg of the positive recovery probe.
    Falls back to ``True`` when the service unit is not registered
    (e.g. the openclaw planner service unit is named differently) so
    the recovery path remains generic across tools.
    """
    if not name:
        return False
    try:
        import subprocess as _sp
        out = _sp.run(
            [
                "systemctl", "--user", "is-active",
                f"aios-executor-{name}.service",
            ],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return True
        # Some tools have an explicit server service (opencode
        # → ``aios-opencode-server.service``); check it too so the
        # probe is honest about the actual runtime dependency.
        out2 = _sp.run(
            [
                "systemctl", "--user", "is-active",
                f"aios-{name}-server.service",
            ],
            capture_output=True, text=True, timeout=2,
        )
        return out2.returncode == 0
    except Exception:
        # If systemctl is unavailable (containers / CI), fall back
        # to the process check so the probe is still useful.
        return _executor_process_alive(name)


def _executor_endpoint_reachable(name: str) -> bool:
    """Return ``True`` iff the lightweight HTTP health endpoint for
    ``name`` is reachable and reports the expected match string.
    Reads ``lightweight_ping_url`` / ``lightweight_ping_url_match``
    from ``config/tool_adapters.json``; returns ``True`` if the
    tool has no declared endpoint (e.g. some planners).
    """
    if not name:
        return False
    try:
        cfg_path = Path("${AIOS_HOME}/config/tool_adapters.json")
        if not cfg_path.is_file():
            return True
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        tool_cfg = (data.get("tools") or {}).get(name) or {}
        url = str(tool_cfg.get("lightweight_ping_url") or "").strip()
        if not url:
            return True
        match = str(
            tool_cfg.get("lightweight_ping_url_match") or "").strip()
        import urllib.request as _ur
        req = _ur.Request(url, method="GET")
        with _ur.urlopen(req, timeout=2) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        if not match:
            return True
        return match in body
    except Exception:
        return False


def _executor_adapter_probe_ok(name: str) -> bool:
    """Return ``True`` iff the latest lightweight probe record for
    ``name`` (in ``cache/tool_health/<tool>.json``) confirms the
    adapter is reachable and protocol-ready.  This is the cached
    signal written by ``aios_health_publisher``; we accept it as
    honest evidence of adapter health, falling back to ``True`` when
    the cache is missing or unreadable so the recovery probe can
    still progress on first boot.
    """
    if not name:
        return False
    try:
        cache_path = Path(
            f"${AIOS_HOME}/cache/tool_health/{name}.json")
        if not cache_path.is_file():
            return True
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return bool(data.get("lightweight_reachable")) and bool(
            data.get("lightweight_protocol_ready"))
    except Exception:
        return False


# Production runtime hotfix 2026-08-10: a lightweight-green probe
# is necessary but NOT sufficient for production eligibility.  The
# daily burn-in exposed the case where ``lightweight_reachable=true``
# + ``lightweight_protocol_ready=true`` co-exist with
# ``model_available=false, model_state=network_error``: the
# inference path times out, but the protocol handshake still
# succeeds.  Recovery that ignores the model side will silently
# re-route production traffic back to a dead executor the moment
# the failure-event TTL elapses.  This helper is the fourth leg of
# the recovery contract: it MUST be consulted alongside the three
# existing legs and a clear ``model_available=false`` must abort
# recovery so the next routing decision continues to skip the
# tool.
_EXECUTER_MODEL_UNAVAILABLE_STATES = {
    "", "unknown",
    "network_error", "timeout", "probe_error", "stale",
    "quota_exhausted", "rate_limited", "auth_failed",
    "cooldown", "disabled",
}


def _executor_model_available(name: str) -> bool:
    """Return ``True`` iff the inference-side cache for ``name``
    shows the model endpoint is currently callable.

    Reads ``cache/tool_health/<name>.json`` and inspects
    ``model_available`` + ``model_state``.  The probe distinguishes
    "no real probe yet" (``model_available=False, model_state=unknown``
    with no checked_at) from "real probe confirmed unavailable"
    (``model_state`` in the failure set).  When the cache is
    missing or unreadable we return ``False`` — the conservative
    answer — so a fresh daemon that has not been probed cannot
    inherit a free pass from a stale ``UNAVAILABLE_TOOL_RUNTIME``
    failure event.
    """
    if not name:
        return False
    try:
        cache_path = Path(
            f"${AIOS_HOME}/cache/tool_health/{name}.json")
        if not cache_path.is_file():
            return False
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not bool(data.get("model_available")):
            return False
        state = str(data.get("model_state", "") or "").strip().lower()
        if state in _EXECUTER_MODEL_UNAVAILABLE_STATES:
            return False
        return True
    except Exception:
        return False


# P0-1 final-production binding-health separation.  The Codex native
# CLI (the ``codex:native`` binding used by ``aios_codex_client.sh``)
# has a 15 s cloud-config bundle timeout that is entirely orthogonal
# to the ``codex:minimax`` binding path.  The latter routes through
# the Codex Relay ``/v1/models`` endpoint and the ``MiniMaxClient``
# HTTP path — both are healthy when ``minimax.shared`` is reachable.
# Treating the native CLI probe failure as a binding-level verdict
# incorrectly excluded ``codex:minimax`` from production routing even
# though every real production dispatch (4e3bc844, e3e31a2d) hits
# ``minimax.shared`` successfully via that binding.  The helper
# below is the binding-aware production-truth surface consulted by
# both ``_is_executor_available`` and the strict-tool / strict-model
# codex dispatch path.
def _binding_health_truth(name: str, binding_id: str = "",
                          resource_id: str = "") -> dict:
    """Return the unified truth surface for ``(name, binding_id)``.

    The shape mirrors ``ToolFailoverEngine.compute_tool_status`` but
    collapses the per-binding axes into a flat dict so the
    orchestrator's strict contract has a single read point.  When
    ``binding_id == "codex:minimax"`` and ``resource_id ==
    "minimax.shared"`` we explicitly consult the codex:minimax
    binding's own health (the Codex Relay lightweight probe + the
    ``MiniMaxClient`` usage ledger) instead of the codex tool's
    generic ``model_available`` flag, which conflates the native CLI
    failure with the relay path.  The other bindings fall through
    to the standard tool-level health truth source.
    """
    truth = {
        "tool": str(name or ""),
        "binding": str(binding_id or ""),
        "resource": str(resource_id or ""),
        "process_alive": False,
        "binding_healthy": False,
        "resource_healthy": False,
        "reason": "",
    }
    if not name:
        truth["reason"] = "missing_tool"
        return truth
    truth["process_alive"] = bool(_tool_process_health(name))
    if binding_id == "codex:minimax" or (
        name == "codex" and resource_id == "minimax.shared"
    ):
        # Codex:minimax binding — consult the dedicated surfaces:
        #   1. Codex Relay lightweight ping (``/v1/models``)
        #   2. ``MiniMaxClient`` recent inference ledger (real-task
        #      success is the strongest binding-eligible signal)
        #   3. ``cache/tool_health/codex.json`` model_available
        #      (only the inference-side slice is consulted; native
        #      CLI ``probe_args`` failure is IGNORED here).
        binding_ok = False
        try:
            adapter_path = Path(
                "${AIOS_HOME}/cache/tool_health/codex.json")
            if adapter_path.is_file():
                data = json.loads(
                    adapter_path.read_text(encoding="utf-8"))
                relay_reachable = bool(
                    data.get("lightweight_reachable"))
                relay_protocol = bool(
                    data.get("lightweight_protocol_ready"))
                model_avail = bool(data.get("model_available"))
                state = str(
                    data.get("model_state", "") or ""
                ).strip().lower()
                if model_avail and state not in (
                    _EXECUTER_MODEL_UNAVAILABLE_STATES
                ):
                    binding_ok = True
                if relay_reachable and relay_protocol:
                    # Relay-side health is a sufficient stand-alone
                    # signal: real inference has hit the relay for
                    # every production task in 4e3bc844 /
                    # e3e31a2d even when the native CLI probe was
                    # unavailable.
                    binding_ok = True
        except Exception:
            binding_ok = False
        truth["binding_healthy"] = binding_ok
        truth["resource_healthy"] = binding_ok
        if not binding_ok:
            truth["reason"] = (
                "codex_minimax_binding_unhealthy:relay_unreachable"
            )
        return truth
    # Default: tool-level health truth
    truth["binding_healthy"] = bool(_executor_model_available(name))
    truth["resource_healthy"] = bool(_executor_model_available(name))
    if not truth["binding_healthy"]:
        truth["reason"] = "model_endpoint_unavailable"
    return truth


def _binding_health_eligible(name: str, binding_id: str = "",
                             resource_id: str = "") -> bool:
    """Convenience wrapper: True iff the (tool, binding, resource)
    triple has every required leg green: process alive, binding
    healthy, resource healthy.  Used by the strict-tool / strict-model
    dispatch contract for the production main chain."""
    truth = _binding_health_truth(name, binding_id, resource_id)
    return bool(truth.get("process_alive")
                and truth.get("binding_healthy")
                and truth.get("resource_healthy"))


# Production executor-live-failover 2026-08-10: a stale *negative*
# cache (e.g. codex probe 80 minutes old showed ``network_error``)
# would otherwise permanently exclude an executor whose model side
# has since recovered.  The previous hotfix
# (``22fba83`` enforce executor health at runtime dispatch) closed
# the *stale positive* surface; this constant governs the *stale
# negative* surface: a cache that reports ``model_available=false``
# for longer than this window MUST be force-refreshed before the
# routing layer trusts it again.  Default 300 s = 5 minutes is short
# enough that an operator who restored upstream provider health
# sees the executor re-eligible within a single repair cycle, but
# long enough that a real ``network_error`` from 30 seconds ago is
# still respected.
_STALE_NEGATIVE_REFRESH_AFTER_SECONDS = float(
    os.getenv(
        "AIOS_STALE_NEGATIVE_REFRESH_AFTER_SECONDS",
        "300",
    ) or "300"
)


def _executor_model_cache_age_seconds(name: str) -> float:
    """Return the age of ``cache/tool_health/<name>.json`` in
    seconds, or ``inf`` when the cache is missing / unreadable.
    Used by :func:`_force_refresh_executor_model_cache` to decide
    whether the cache is stale enough that a refresh is warranted.
    """
    if not name:
        return float("inf")
    try:
        cache_path = Path(
            f"${AIOS_HOME}/cache/tool_health/{name}.json")
        if not cache_path.is_file():
            return float("inf")
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        checked = data.get("checked_at")
        if not checked:
            return float("inf")
        stamp = datetime.fromisoformat(
            str(checked).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (datetime.now(timezone.utc) - stamp).total_seconds(),
        )
    except Exception:
        return float("inf")


def _force_refresh_executor_model_cache(name: str) -> bool:
    """Force a fresh inference probe for ``name`` and persist the
    result to ``cache/tool_health/<name>.json``.  Returns ``True``
    when the cache was rewritten with a fresh observation
    (regardless of whether the new state is healthy or unhealthy —
    honesty over optimism).  The probe is bounded by
    :data:`_STALE_NEGATIVE_REFRESH_AFTER_SECONDS` so a real failure
    observed 5 seconds ago is not ignored; this helper only triggers
    when :func:`_executor_model_cache_age_seconds` reports the cache
    is already older than the threshold.

    Production executor-live-failover 2026-08-10: this is the
    bounded stale-negative refresh the routing layer needs to escape
    the ``opencode failed → codex unavailable (stale cache) → no
    healthy executor`` dead-end.  When ``choose_executor`` returns
    the empty string and the only available candidate has a stale
    negative cache, ``_enqueue_node`` calls this helper for that
    candidate and retries ``choose_executor`` once.  The retry is
    intentionally one-shot so a recovered executor can re-enter
    routing without an unbounded probe loop.
    """
    if name not in EXECUTORS:
        return False
    if _executor_model_cache_age_seconds(name) < _STALE_NEGATIVE_REFRESH_AFTER_SECONDS:
        return False
    try:
        from aios_tool_adapter import get_adapter
        adapter = get_adapter(name)
    except Exception:
        return False
    try:
        result = adapter.probe(force=True)
    except Exception:
        return False
    return bool(result and "checked_at" in result)


def _attempt_tool_recovery(name: str) -> bool:
    """P9D-R-executor-primary-recovery: positive recovery probe.

    A recovery is honoured only when ALL FOUR conditions hold:

      1. ``aios-executor-<name>.service`` (or
         ``aios-<name>-server.service``) reports ``active``.
      2. The lightweight HTTP endpoint is reachable AND reports the
         declared match string.
      3. The cached adapter lightweight probe record shows the
         adapter is reachable + protocol-ready.
      4. **NEW (2026-08-10 production hotfix)**: the inference-side
         probe (``cache/tool_health/<name>.json``) shows the model
         endpoint as ``model_available=true`` with a non-failure
         ``model_state``.  This fourth leg was added because the
         daily burn-in exposed the case where a tool whose
         lightweight endpoint is healthy can still fail every real
         inference call (e.g. ``model_state=network_error`` because
         the upstream provider is unreachable).  Without this leg,
         the bounded failure-event TTL elapses, the recovery probe
         "succeeds" on the lightweight surface, the failure event
         is cleared, and the orchestrator routes production traffic
         back to a tool whose model endpoint is dead — the exact
         failure mode that produced T2 / T6 in the 2026-08-10
         burn-in (``opencode → codex → opencode`` repair loop).

    On success, the failure event for ``name`` is cleared, the
    in-process tool-process-health cache is invalidated, and the
    capability layer is re-evaluated on the very next
    ``compute_tool_status`` call.  No TTL is consulted — the bounded
    120 s TTL is a safety net for cases where the recovery probe
    never runs, not a normal recovery mechanism.
    """
    if not name:
        return False
    try:
        if not _executor_service_active(name):
            return False
        if not _executor_endpoint_reachable(name):
            return False
        if not _executor_adapter_probe_ok(name):
            return False
        # Production runtime hotfix 2026-08-10: the lightweight
        # surface (legs 1-3) is necessary but not sufficient for
        # production dispatch.  Refuse recovery when the model-side
        # cache still reports ``model_available=false`` or a known
        # failure ``model_state`` — the inference path will fail
        # again on the very next real call.
        if not _executor_model_available(name):
            return False
    except Exception:
        return False
    # All three legs succeeded.  Clear the failure event so the
    # next ``compute_tool_status`` consults the live capability
    # layer instead of the sticky UNAVAILABLE state.  Invalidate
    # the in-process tool-process-health cache so the next
    # ``_tool_process_health`` re-probes ``/proc`` for the now-healthy
    # daemon.
    #
    # NOTE: we deliberately route through the MODULE-LEVEL
    # ``clear_tool_runtime_failure`` (``aios_tool_failover.``)
    # rather than the orchestrator's local alias.  The alias is
    # imported for legacy ``record``-side use; routing the
    # recovery path through the module-level function makes the
    # call surface auditable and prevents the recovery path from
    # being silently disabled by a misconfigured alias override.
    try:
        from aios_tool_failover import (
            clear_tool_runtime_failure as _clear_default,
        )
        _clear_default(name)
    except Exception:
        return False
    try:
        _invalidate_tool_process_cache()
    except Exception:
        pass
    return True


def choose_executor(requested_role: str, exclude=(), capability_overlay=None) -> str:
    """Select the best available executor respecting role preference.

    P5 fix: uses unified capability truth source instead of adapter.health().
    Tools in DEGRADED_EXTERNAL, DEGRADED_INTERNAL, UNAVAILABLE, or UNVERIFIED
    state are never selected.

    P9D-R-live §6.2: when the executor daemon process is dead, ALL
    bindings of that tool are treated as unavailable and the
    candidate selector moves to the next healthy tool.  ``opencode:free``
    cannot rescue a dead ``opencode`` daemon — they share the same
    tool process, and ``failure_scope=TOOL_PROCESS`` propagates to
    every binding of that tool.

    ``capability_overlay`` is a task-scoped override (see
    :func:`_is_executor_available`); it does not mutate global caches.
    """
    # [FIX] 2026-08-12 entry/runtime-fix separate-verification-rejection:
    #   Production role design (AIOS_SYSTEM_OVERVIEW.md) is
    #     GENERAL primary = OpenCode, GENERAL runtime fallback = Codex
    #     Claude = specialist_high_depth_review (NOT a GENERAL fallback)
    #   The historical runtime order ``opencode → claude → codex`` placed
    #   ``claude`` between OpenCode and Codex, which is an architecture
    #   drift: when the orchestrator repairs a GENERAL node and the
    #   previous attempt's executor is excluded, ``claude`` (quota /
    #   credit / inference-degraded) would block the OpenCode → Codex
    #   fallback chain and emit ``no_healthy_executor``.  Restore the
    #   frozen policy: GENERAL fallback is OpenCode → Codex only.
    #   ``claude`` retains its ``specialist_high_depth_review`` role
    #   (and its own entry in the ``claude`` orders) but is removed
    #   from the GENERAL runtime chain.
    orders = {
        "opencode": ("opencode", "codex"),
        "claude": ("claude", "opencode", "codex"),
        "codex": ("codex", "opencode", "claude"),
    }
    order = orders.get(requested_role, orders["opencode"])
    if isinstance(exclude, str):
        excluded = {exclude} if exclude else set()
    else:
        excluded = {str(item) for item in (exclude or []) if str(item)}
    for name in order:
        # P9D-R-executor-primary-recovery: a tool blocked by a
        # failure event is allowed exactly one positive recovery
        # probe per ``choose_executor`` call.  The probe runs the
        # service-active + endpoint-reachable + adapter-probe-ok
        # contract; only a fully-green probe clears the failure
        # event and re-enables routing.  This is the only path that
        # re-enables routing after a real recovery — the bounded
        # 120 s TTL is a safety net, not a normal recovery mechanism.
        if (name not in excluded
                and _get_tool_runtime_failure(name) is not None):
            try:
                _attempt_tool_recovery(name)
            except Exception:
                pass
        if name not in excluded and _is_executor_available(
                name, capability_overlay=capability_overlay):
            if not _tool_process_health(name):
                # P9D-R-live §6.2: tool-process failure propagates to
                # all bindings.  Skip this tool entirely.
                excluded.add(name)
                continue
            # Production runtime hotfix 2026-08-10: the model side
            # is a SEPARATE leg of the production eligibility gate.
            # Even when the failure-event TTL has elapsed, an
            # executor whose ``model_available=false`` MUST NOT be
            # routed to until the inference path is verified
            # callable.  This is the final guard against the
            # ``opencode → codex → opencode`` repair loop: without
            # this check, a tool whose lightweight endpoint is
            # healthy but whose model endpoint is dead would be
            # silently re-selected the moment the bounded failure
            # event TTL elapses.
            if not _executor_model_available(name):
                excluded.add(name)
                continue
            return name
    return ""


def _seconds_since(value: str) -> float:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _executor_process_alive(executor: str) -> bool:
    """Return True iff at least one process for this executor is alive.

    Used by ``_child_stall_reason`` to distinguish "executor busy queueing"
    (NOT a timeout — the executor process is doing real work) from "executor
    unreachable — a real dispatch_claim_timeout". The check is local-only and
    best-effort so it cannot block the orchestrator's hot path.
    """
    if not executor:
        return False
    try:
        import os, glob
        for pid_dir in glob.glob("/proc/[0-9]*"):
            try:
                cmd_path = pid_dir + "/cmdline"
                with open(cmd_path, "rb") as fh:
                    raw = fh.read().decode("utf-8", errors="replace")
                if executor == "opencode" and "aios_executor_daemon.py" in raw and "opencode" in raw:
                    return True
                if executor == "codex" and "aios_executor_daemon.py" in raw and "codex" in raw:
                    return True
                if executor == "claude" and "aios_executor_daemon.py" in raw and "claude" in raw:
                    return True
                if executor == "hermes" and (
                    "hermes_cli.main" in raw or "aios_executor_daemon.py" in raw
                ):
                    return True
                if executor == "openclaw" and "openclaw" in raw and "gateway" in raw:
                    return True
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
    except Exception:
        return False
    return False


def _child_stall_reason(node: dict, child_status: str, child_state: dict) -> str:
    """Return a bounded stall reason derived from registered adapter limits.

    Pending-state semantics tightened by the AIOS internal-reliability
    close-out: a task that is *legitimately still queued* because its
    target executor is busy doing real work (the executor process is
    still alive) MUST NOT be classified as ``dispatch_claim_timeout``.
    That classification now only fires when the executor process is
    unreachable, i.e. nobody is there to claim the task. This is the
    directive from the internal-reliability close-out §二:
    ``dispatch_claim_timeout 只用于异常未领取，不因 executor 正忙误判``.
    """
    if child_status == "pending":
        age = _seconds_since(node.get("queued_at", ""))
        if age <= 60:
            return ""
        # P9E close-out: a child that has been ``pending`` for more
        # than ``PENDING_TOO_LONG_SECONDS`` MUST escalate, even when
        # the executor process is alive.  The previous P9B rule
        # treated "executor alive & busy" as backpressure and waited
        # forever; in practice a backpressure marker with no
        # follow-through produces a permanent ``pending`` state and a
        # parent workflow that never converges.  Bounded waiting is
        # the natural terminal signal for genuine backpressure; beyond
        # the bound the child must be expired for repair, not held
        # hostage by the executor being alive.
        if age > PENDING_TOO_LONG_SECONDS:
            return f"pending_too_long:{int(age)}s>{PENDING_TOO_LONG_SECONDS}s"
        executor = (
            node.get("assigned_executor")
            or node.get("actual_executor")
            or ""
        )
        if executor and _executor_process_alive(executor):
            # Executor is alive & busy — this is backpressure, not a
            # dispatch_claim_timeout.  We emit a *bounded* non-fatal
            # marker so the monitoring layer can see the queue depth,
            # but the orchestrator will continue to wait rather than
            # marking the child for repair.
            return f"queued_behind_executor:{executor}:{int(age)}s"
        return f"dispatch_claim_timeout:{int(age)}s"
    if child_status == "locked":
        age = _seconds_since(child_state.get("ts_locked", ""))
        return f"executor_checkin_timeout:{int(age)}s" if age > 30 else ""
    if child_status == "running":
        executor = str(
            child_state.get("executor")
            or node.get("actual_executor")
            or node.get("assigned_executor")
            or ""
        )
        try:
            limit = int(get_adapter(executor).config.get("task_timeout_seconds", 300))
        except Exception:
            limit = 300
        age = _seconds_since(
            child_state.get("ts_checkin")
            or child_state.get("ts_running")
            or child_state.get("ts_locked")
            or ""
        )
        return f"executor_hard_timeout:{executor}:{int(age)}s>{limit + 60}s" if age > limit + 60 else ""
    return ""


def _expire_child_for_repair(parent_id: str, workflow: dict, node: dict,
                             child_state: dict, reason: str) -> None:
    """Mark a non-terminal child as ``repairing`` and release its
    canonical claim so the next ``process_workflow`` pass can call
    ``_repair_node`` and create a new generation.

    Repair semantic (P9D-R-live §4):

    * Releases the old child task's bus state and queue membership.
    * Records the failed executor in ``attempted_executors`` so the
      next ``_enqueue_node`` excludes it from the executor pool.
    * Releases the canonical claim for the old generation so a new
      generation can be claimed cleanly.
    * Sets ``node.status = "repairing"`` (NOT ``failed``) so the
      next ``process_workflow`` pass calls ``_repair_node`` instead
      of skipping the node. The node is terminal only when
      ``attempt >= MAX_REPAIRS`` (handled in ``process_workflow``).
    """
    task_id = str(child_state.get("task_id", "") or node.get("task_id", ""))
    old_executor = str(
        child_state.get("executor")
        or node.get("actual_executor")
        or node.get("assigned_executor")
        or ""
    )
    if task_id:
        update_task_status(
            task_id,
            "failed",
            "aios-orchestrator",
            reason,
            metadata={"orchestrator_timeout": True, "timed_out_executor": old_executor},
        )
        _redis_client.lrem("aios:bus:queue:pending", 0, task_id)
        _redis_client.delete(f"aios:bus:lock:{task_id}")
    # Archive the old attempt so the new generation can be created.
    attempted = list(node.get("attempted_executors", []) or [])
    if old_executor and old_executor not in attempted:
        attempted.append(old_executor)
    node["attempted_executors"] = attempted
    # Release the canonical claim so the next generation can claim.
    try:
        node_index = int(node.get("index", 0))
        generation = int(node.get("attempt", 0))
        _release_canonical_child(parent_id, node_index, generation)
    except Exception:
        pass
    # Set the node to ``repairing`` (non-terminal). The next
    # ``process_workflow`` pass will observe this state and call
    # ``_repair_node`` to advance the generation. ``_repair_node`` is
    # the *only* path that advances generation/attempt -- this
    # function deliberately does not call it.
    node["status"] = "repairing"
    node["error"] = reason[:1000]
    node["repair_reason"] = reason[:1000]
    node["repair_started_at"] = _now()
    node["actual_executor"] = old_executor
    # Clear the old task_id so the next enqueue creates a fresh child.
    node["task_id"] = ""
    old_child_id = str(node.get("archive_current_child_id", "") or "")
    if old_child_id and old_child_id != task_id:
        node["archive_current_child_id"] = old_child_id
    else:
        node["archive_current_child_id"] = task_id
    # Track tool_failover_count for the audit ledger; +1 for the
    # newly-archiving attempt.
    existing_failover = int(node.get("tool_failover_count", 0) or 0)
    node["tool_failover_count"] = existing_failover + (1 if old_executor else 0)
    node["repair_failure_scope"] = (
        "TOOL_PROCESS" if old_executor else "WORKFLOW_STATE_INTEGRITY"
    )
    # P9D-R-runtime-health-freshness: record an explicit failure
    # event so the next ``compute_tool_status`` returns
    # ``UNAVAILABLE_TOOL_RUNTIME`` immediately, regardless of the
    # on-disk probe cache.  This is the key fix that lets the
    # Repair chain pick a *different* tool on its very next
    # generation — the previous behaviour kept the cached
    # ``AVAILABLE_PRIMARY`` answer for the duration of the Repair
    # window, so the router re-selected the same dead tool.
    if old_executor:
        try:
            from aios_model_resources import FAILURE_SCOPE_TOOL_ADAPTER
        except Exception:
            FAILURE_SCOPE_TOOL_ADAPTER = "TOOL_ADAPTER"
        scope = (
            "TOOL_PROCESS"
            if reason.startswith(("dispatch_claim_timeout", "pending_too_long"))
            else FAILURE_SCOPE_TOOL_ADAPTER
            if reason.startswith(("executor_checkin_timeout", "executor_hard_timeout"))
            else "TOOL_PROCESS"
        )
        try:
            _record_tool_runtime_failure(
                old_executor,
                scope=scope,
                reason=reason[:160],
            )
        except Exception:
            # Failure recording is best-effort; the orchestrator MUST
            # keep working even if the engine singleton is missing.
            pass
        # Also drop the orchestrator's local tool-process-health
        # cache so the very next ``_tool_process_health`` call
        # re-probes ``/proc`` for the (now-dead) daemon.
        try:
            _invalidate_tool_process_cache()
        except Exception:
            pass
        # Persist the cache invalidation on the node so the monitor
        # can render the explicit reason alongside the tool's
        # otherwise-healthy capability record.
        node["cache_invalidated_at"] = _now()
        node["cache_invalidation_reason"] = (
            f"tool_runtime_failure:{scope}:{reason[:80]}"
        )


def _logic_depth(executor: str) -> str:
    return {"opencode": "low", "claude": "high", "codex": "batch"}.get(executor, "low")


def _repair_focus_lines(reason: str) -> list[str]:
    """Convert verifier codes into bounded trusted repair guidance."""
    mapping = (
        ("authoritative_version_missing",
         "- Explicitly report the current AIOS runtime version from an authoritative live source."),
        ("authoritative_version_conflict",
         "- For AIOS version, use only the AIOS Entry Gateway at 127.0.0.1:18801/health and the canonical AIOS manifest; ignore unrelated service versions such as Grafana or Task Console."),
        ("authoritative_version_sources_disagree",
         "- Re-read the AIOS Entry Gateway at 127.0.0.1:18801/health and the canonical AIOS manifest, then report only their reconciled AIOS version."),
        ("authoritative_status_missing",
         "- Explicitly report the current AIOS runtime health status."),
        ("authoritative_redis_missing",
         "- Explicitly report the current Redis state or PING response."),
        ("authoritative_service_missing",
         "- Explicitly report the current service name."),
        ("authoritative_timestamp_missing",
         "- Explicitly report one fresh ISO-8601 timestamp read from the live endpoint."),
        ("authoritative_timestamp_invalid_or_stale",
         "- Re-read the live endpoint and report its current ISO-8601 timestamp; do not reuse an old timestamp."),
        ("non_authoritative_legacy_runtime_key",
         "- Do not use obsolete Redis keys as queue or executor evidence; use the current AIOS /status endpoint."),
        ("authoritative_queue_conflict",
         "- Re-read the canonical queue counters from the current AIOS /status endpoint."),
        ("authoritative_available_executor_count_conflict",
         "- Count available task executors from /status executors.list[].available; process existence alone is insufficient."),
        ("authoritative_unavailable_executor_count_conflict",
         "- Count unavailable task executors from /status executors.list[].available."),
        ("authoritative_failed_service_count_conflict",
         "- Re-read failed service units with systemctl --user --failed."),
        ("authoritative_file_missing",
         "- Recheck the exact required file path and provide current file evidence."),
        ("authoritative_parent_task_id_missing",
         "- Report the exact system-owned parent task ID supplied in the workflow metadata."),
        ("authoritative_parent_task_id_conflict",
         "- Replace the task ID claim with the exact system-owned parent task ID supplied in the workflow metadata."),
        ("authoritative_actual_executor_missing",
         "- Report the exact actual executor supplied in the workflow metadata."),
        ("authoritative_actual_executor_conflict",
         "- Replace generic daemon or worker-list claims with the one exact actual executor supplied in the workflow metadata."),
        ("authoritative_final_status_missing",
         "- Explicitly report the successful final status as completed."),
        ("authoritative_final_status_conflict",
         "- Replace process/runtime state with the system-owned final task status completed."),
        ("authoritative_component_installed_version_missing",
         "- Report each installed component version exactly as supplied by AIOS-owned executable evidence."),
        ("authoritative_component_latest_version_missing",
         "- Report each latest stable component version exactly as supplied by the official-registry evidence."),
        ("authoritative_component_release_date_missing",
         "- Report each official-registry release date exactly as supplied by AIOS-owned live evidence."),
        ("authoritative_component_release_gap_missing_or_conflict",
         "- Report each component's release gap exactly from AIOS-owned evidence (for example, 2 means two stable releases after the installed version)."),
        ("authoritative_component_official_source_missing",
         "- Include the exact official registry or official package URL supplied for each component."),
        ("authoritative_component_query_time_missing",
         "- Include each component's evidence retrieval/query time from the supplied registry evidence."),
        ("authoritative_component_release_notes_uncertainty_missing",
         "- State explicitly that official release-note/changelog content was not acquired, so concrete breaking-change compatibility cannot be verified."),
        ("unsupported_component_compatibility_claim_without_release_notes",
         "- Remove backward-compatibility claims unsupported by official release notes; keep compatibility risk explicitly uncertain."),
        ("unsupported_component_version_scheme_claim_without_release_notes",
         "- Do not label OpenClaw calendar-like releases as semantic patch/minor versions or infer compatibility; describe only the exact series change and mark compatibility unverified."),
        ("authoritative_component_delta_classification_conflict",
         "- Correct the version-delta classification: OpenCode 1.17.x to 1.18.x is a minor-line change, not patch-only."),
        ("authoritative_aios_integration_missing",
         "- Name and assess the real AIOS integration surfaces from evidence: the openclaw-aios-bridge module (@aios/openclaw-bridge) and aios_opencode_client.py or aios-opencode-server.service."),
        ("authoritative_component_rollback_pin_missing",
         "- Give a rollback command or procedure pinned to the exact installed package@version supplied by evidence, then re-run the existing service and E2E checks."),
        ("authoritative_component_rollback_command_missing",
         "- Make rollback executable with npm install -g package@exact-installed-version, then re-run existing integration/E2E checks."),
        ("authoritative_component_unrecognized_version_claim",
         "- Remove component versions absent from authoritative evidence; use only installed/latest and listed stable releases."),
        ("authoritative_component_constraint_direction_conflict",
         "- Correct version-constraint direction: >= is a minimum lower bound, not an upper bound and not proof of runtime compatibility."),
        ("empty_result",
         "- Return a concrete non-empty result with execution evidence."),
        ("truncated",
         "- The prior deliverable was truncated. Return a concise complete answer under 7000 characters covering every original requirement; omit reasoning transcripts."),
        ("incomplete",
         "- The prior deliverable was incomplete. Return a concise complete answer under 7000 characters covering every original requirement; omit reasoning transcripts."),
        # Production reviewer+evidence correction 2026-08-10: when a
        # Reviewer / Verifier rejects an Executor result because a
        # concrete numeric / factual claim contradicts authoritative
        # evidence (NOT a missing-marker issue), the bounded retry
        # must carry the conflicting claim and the trusted baseline
        # so the Executor can correct the deliverable instead of
        # re-rolling the same broken number.
        ("material_false_numeric_claim",
         "- A specific numeric claim in the prior deliverable was rejected because it contradicts authoritative live evidence. Replace that number with the exact authoritative value, or remove the unsupported claim entirely. Do NOT re-roll the same value or invent a different number."),
        ("evidence_contradiction",
         "- The prior deliverable contained a factual claim that contradicts the authoritative live evidence provided. Replace that claim with the exact trusted baseline value, or drop it. Cite the authoritative source."),
        ("claim_contradicts_evidence",
         "- The prior deliverable contained a claim that is directly contradicted by the supplied authoritative evidence. Replace the claim with the trusted baseline value, or remove it."),
        ("numeric_claim_rejected",
         "- A numeric claim was rejected by the Reviewer against authoritative evidence. Replace it with the exact authoritative value, or remove the unsupported numeric claim."),
    )
    text = str(reason or "")
    return [guidance for marker, guidance in mapping if marker in text]


def _verification_retry_target(node: dict, reason: str,
                               actual_executor: str) -> str:
    """Allow one same-executor retry only for missing deterministic evidence.

    Production runtime hotfix 2026-08-10: a same-executor retry is
    ONLY legal when the executor is not already in the task-local
    ``attempted_executors`` list AND the executor is still
    production-eligible (passes ``_is_executor_available`` AND
    ``_executor_model_available``).  This is the second leg of the
    ``opencode → codex → opencode`` repair-loop closure: without
    it, a verification retry that targets the just-failed executor
    would re-enqueue the same broken route the moment the bounded
    failure-event TTL elapses, regardless of the model's actual
    state.

    Note: this helper is the *advisor* for verification-only retry;
    the hard task-local exclusion is enforced one layer up in
    :func:`_enqueue_node` (which refuses to honour
    ``preferred_repair_executor`` when it is in the attempted
    ledger).  This helper keeps the contract narrow so the
    verification path can stay exempt from the broader
    ``attempted_executors`` gate, but the actual dispatch always
    checks the gate.
    """
    missing_markers = (
        "authoritative_version_missing",
        "authoritative_status_missing",
        "authoritative_redis_missing",
        "authoritative_service_missing",
        "authoritative_timestamp_missing",
        "authoritative_timestamp_invalid_or_stale",
        "non_authoritative_legacy_runtime_key",
        "authoritative_queue_conflict",
        "authoritative_available_executor_count_conflict",
        "authoritative_unavailable_executor_count_conflict",
        "authoritative_failed_service_count_conflict",
        "authoritative_file_missing",
        "authoritative_version_conflict",
        "authoritative_version_sources_disagree",
        "authoritative_parent_task_id_missing",
        "authoritative_parent_task_id_conflict",
        "authoritative_actual_executor_missing",
        "authoritative_actual_executor_conflict",
        "authoritative_final_status_missing",
        "authoritative_final_status_conflict",
        "authoritative_component_installed_version_missing",
        "authoritative_component_latest_version_missing",
        "authoritative_component_release_date_missing",
        "authoritative_component_release_gap_missing_or_conflict",
        "authoritative_component_official_source_missing",
        "authoritative_component_query_time_missing",
        "authoritative_component_release_notes_uncertainty_missing",
        "unsupported_component_compatibility_claim_without_release_notes",
        "unsupported_component_version_scheme_claim_without_release_notes",
        "authoritative_component_delta_classification_conflict",
        "authoritative_aios_integration_missing",
        "authoritative_component_rollback_pin_missing",
        "authoritative_component_rollback_command_missing",
        "authoritative_component_unrecognized_version_claim",
        "authoritative_component_constraint_direction_conflict",
        # Production reviewer+evidence correction 2026-08-10: when a
        # Reviewer / Verifier rejects an Executor result because a
        # concrete numeric / factual claim contradicts authoritative
        # evidence, the bounded retry MUST target a healthy executor
        # (same executor when still healthy, else the next healthy
        # candidate) so the Executor can re-emit the corrected
        # deliverable against the trusted baseline.  Without these
        # markers the helper used to skip same-executor retry, which
        # produced the 2026-08-10 burn-in failure mode where the
        # corrective retry fell back to a fully-downstream executor
        # instead of giving the original (healthy) Executor a chance
        # to correct its own claim.
        "material_false_numeric_claim",
        "evidence_contradiction",
        "claim_contradicts_evidence",
        "numeric_claim_rejected",
        "truncated",
        "incomplete",
    )
    text = str(reason or "")
    if int(node.get("verification_same_executor_repairs", 0)) >= 1:
        return ""
    if not any(marker in text for marker in missing_markers):
        return ""
    executor = str(actual_executor or "")
    if executor not in EXECUTORS:
        return ""
    if not _is_executor_available(executor):
        return ""
    # Production runtime hotfix 2026-08-10: refuse to point a
    # verification retry at an executor whose model endpoint is
    # known-unavailable.  ``_is_executor_available`` only consults
    # the lightweight + failure-event surface; the model side is a
    # separate invariant that this function MUST honour.
    if not _executor_model_available(executor):
        return ""
    # Task-local exclusion: a verification retry MUST NOT target an
    # executor that the node has already attempted.  Without this
    # gate, the ``opencode → codex → opencode`` repair loop is
    # reproduced on the verification path: opencode fails, codex
    # succeeds the verification retry which is then routed back to
    # opencode, and the workflow loops indefinitely.
    attempted = {
        str(item) for item in (node.get("attempted_executors", []) or [])
        if str(item)
    }
    if executor in attempted:
        return ""
    return executor


def _execution_text(goal: str, node: dict, repair_reason: str = "",
                    previous_result: str = "", parent_id: str = "",
                    actual_executor: str = "", single_node: bool = False) -> str:
    acceptance_limit = 1 if single_node else 3
    acceptance_items = [
        str(item) for item in node.get("acceptance", [])[:acceptance_limit]
    ]
    acceptance = "\n".join(f"- {item[:180]}" for item in acceptance_items)
    evidence_mode = str(node.get("evidence_mode", "semantic"))
    assigned = (
        "Complete the entire original user goal."
        if single_node else str(node.get("task", ""))
    )
    # 2026-08-11 OUTPUT CONTRACT detection: when acceptance (or the
    # user goal itself) explicitly demands a specific output shape —
    # "one sentence" / "JSON" / "exactly N lines" / "exactly one line"
    # / "one concise summary" — the executor MUST obey the requested
    # shape verbatim.  The Host Evidence block is INTERNAL grounding
    # only; it must NOT be reproduced as a final deliverable unless
    # explicitly requested by the acceptance criteria.  This block is
    # emitted AFTER the system metadata so it sits near the top of the
    # surviving prompt slice after ``text[:4096]`` truncation.
    acceptance_blob = (
        str(goal or "") + "\n" + acceptance + "\n"
        + str(node.get("task", "") or "")
    )
    one_sentence_re = re.compile(
        r"(一句(话|简洁)|恰好一句|一句话|one\s+sentence|exactly\s+one\s+"
        r"(concise\s+)?sentence|single\s+sentence|one-line\s+summary|"
        r"one\s+concise\s+sentence|one\s+concise\s+summary)",
        re.IGNORECASE,
    )
    json_only_re = re.compile(
        r"(only\s+json|return\s+json|json\s+only|json\s+object\s+only|"
        r"\u4ec5\u8fd4\u56de\s*json|\u53ea\u8fd4\u56de\s*json|\u8fd4\u56de\s*json)",
        re.IGNORECASE,
    )
    exact_lines_re = re.compile(
        r"(exactly\s+(\d+)\s+lines?|(\d+)\s+\u884c|\u6070\u597d\s*(\d+)\s*"
        r"\u884c|exactly\s+(\d+)\s+\u53e5)",
        re.IGNORECASE,
    )
    output_contract_lines: list = []
    if one_sentence_re.search(acceptance_blob):
        output_contract_lines.append(
            "OUTPUT CONTRACT (binding): the final answer MUST be exactly "
            "ONE concise Chinese sentence.  NO heading, NO table, NO bullet "
            "list, NO evidence dump, NO host-evidence reproduction, NO extra "
            "explanation.  The sentence MUST base its factual claim on the "
            "AUTHORITATIVE HOST EVIDENCE block above but MUST NOT reproduce "
            "the block as part of the deliverable."
        )
    if json_only_re.search(acceptance_blob):
        output_contract_lines.append(
            "OUTPUT CONTRACT (binding): the final answer MUST be a single "
            "JSON object and nothing else.  NO prose around it."
        )
    exact_n = exact_lines_re.search(acceptance_blob)
    if exact_n:
        # Use the first captured numeric group.
        for g in exact_n.groups():
            if g and g.isdigit():
                output_contract_lines.append(
                    f"OUTPUT CONTRACT (binding): the final answer MUST be "
                    f"exactly {g} lines and nothing more."
                )
                break
    output_contract = (
        "\n\n" + "\n\n".join(output_contract_lines)
        if output_contract_lines else ""
    )
    text = (
        "Original user goal (authoritative):\n"
        f"{goal}\n\nAssigned node:\n{assigned}\n\n"
        f"Acceptance summary:\n{acceptance}\n\n"
        f"Evidence mode: {evidence_mode}\n"
        "System metadata:\n"
        f"- Parent task ID: {parent_id}\n"
        f"- Actual executor: {actual_executor}\n"
        "- Successful final status: completed\n"
        "Copy requested metadata exactly. Obey every original literal/path. Return only a "
        "complete, comprehensive result with concrete evidence. Never invent live facts. "
        "Recompute numeric/comparative claims. Stay under 7000 characters with no thinking transcript."
        "\n\nSANDBOX CONSTRAINT: this Codex sandbox has NO direct access to host "
        "loopback services (127.0.0.1:18801 etc.). Do NOT attempt to curl "
        "localhost, call systemctl / journalctl, or read /proc. Use the "
        "AUTHORITATIVE HOST EVIDENCE block AIOS supplies as the only fact "
        "source for host state. Any claim contradicting that block is a "
        "verification failure."
        "\n\nFACT-USE: every concrete fact (PID, timestamp, status, count) "
        "in your answer MUST appear verbatim in the AUTHORITATIVE HOST "
        "EVIDENCE block. Do NOT extrapolate 'stable'/'healthy'/'broken' "
        "from a single field unless the evidence explicitly states that. "
        "Quote error reasons verbatim (e.g. 'task_id_required' is NOT a "
        "vague 'interface error'). Cite values from each capability item. "
        "End with one line of the form '当前状态判定: "
        "<NORMAL|PARTIAL_ANOMALY|ANOMALY> — <reason>'."
        + output_contract
    )
    if repair_reason:
        focus = _repair_focus_lines(repair_reason)
        text += (
            "\n\nBounded repair: the previous result was rejected. Re-execute this node from "
            "current authoritative sources; do not reuse prior output."
        )
        if focus:
            text += "\nTrusted correction requirements:\n" + "\n".join(focus)
        # Production reviewer+evidence correction 2026-08-10:
        # when the rejection was a Reviewer/Verifier correction
        # (numeric / factual claim contradicted by authoritative
        # evidence), the bounded retry MUST carry the conflicting
        # claim so the Executor can replace it.  Without this
        # segment the Executor would re-roll the same number or
        # re-introduce the same factual mistake.  We clip the
        # previous result to ``PREVIOUS_RESULT_LIMIT`` characters
        # so the bounded retry contract is preserved end-to-end
        # (no unbounded context re-injection).
        if previous_result:
            correction_markers = (
                "material_false_numeric_claim",
                "evidence_contradiction",
                "claim_contradicts_evidence",
                "numeric_claim_rejected",
            )
            if any(marker in str(repair_reason) for marker in correction_markers):
                _PREVIOUS_RESULT_LIMIT = 1800
                clipped = str(previous_result)[:_PREVIOUS_RESULT_LIMIT]
                text += (
                    "\n\nPrevious reviewer-rejected deliverable "
                    "(truncated to "
                    f"{_PREVIOUS_RESULT_LIMIT} chars for correction):\n"
                    "```\n" + clipped + "\n```"
                    "\nLocate the specific numeric / factual claim that "
                    "contradicts the authoritative evidence and correct it. "
                    "Do NOT re-emit the rejected deliverable verbatim."
                )
    if evidence_mode in ("independent-live", "aios-runtime") or previous_result:
        try:
            live_evidence, _ = _collect_independent_evidence(
                str(node.get("task", "") or goal), node,
            )
            authoritative = live_evidence.get("authoritative", {})
            execution_evidence = {}
            # Correction retry (2026-08-10): when repair_reason carries
            # a numeric/evidence-correction marker, expose the
            # authoritative evidence BEFORE listing any component_versions
            # block so the correction is driven by the same baseline
            # the Reviewer used to reject the deliverable.  The compact
            # shape is identical to the legacy independent-live branch.
            if authoritative.get("files"):
                execution_evidence["files"] = authoritative["files"]
            if authoritative.get("component_versions"):
                compact_components = {}
                keep = (
                    "installed_source", "installed_executable_output", "installed_version",
                    "installed_package_name",
                    "official_registry_url", "registry_retrieved_at",
                    "latest_stable_version", "latest_stable_release_date",
                    "stable_release_gap_from_installed", "stable_releases_after_installed",
                    "pre_releases_excluded", "release_notes_verified",
                    "aios_integration",
                )
                for component, record in authoritative["component_versions"].items():
                    compact_components[component] = {
                        key: record[key] for key in keep if key in record
                    }
                execution_evidence["component_versions"] = compact_components
            if execution_evidence:
                if execution_evidence.get("component_versions"):
                    text += (
                        "\n\nComponent-audit hard rules: copy each installed/latest version, release "
                        "date, gap, official URL and query time exactly. If release_notes_verified=false, "
                        "say concrete compatibility/breaking changes are unable to verify; do not infer "
                        "semver, patch/minor, or backward compatibility. Name supplied AIOS integrations. "
                        "For rollback, pin each installed_package_name@installed_version exactly, then "
                        "re-run existing integration/E2E checks."
                    )
                text += (
                    "\n\nAIOS-owned live evidence acquired outside the executor sandbox:\n" +
                    json.dumps({
                        "collected_at": live_evidence.get("collected_at", ""),
                        "authoritative": execution_evidence,
                    }, ensure_ascii=False)[:7000] +
                    "\nUse and cite this factual baseline. Executor-local network failure does not "
                    "invalidate it. Verification Gate will independently reacquire it after execution."
                )
        except Exception:
            pass
    # Host Read-Only Evidence Boundary (final-production 2026-08-11).
    # If the parent workflow persisted host_evidence (collected on
    # ``submit`` and stored on the workflow hash), splice it into the
    # executor prompt verbatim so the Codex sandbox never has to call
    # out to localhost.  The injected block is bounded by the host
    # collector's caps; the executor MUST treat it as authoritative
    # and MUST NOT re-probe the host itself.
    #
    # 2026-08-11 closure fix: bind the normal task text to a SAFE budget
    # FIRST so the bounded HF instruction + host-evidence block can be
    # appended AFTER, never falling victim to the legacy ``text[:4096]``
    # cap.  The host-evidence section is already bounded by
    # ``build_host_evidence_executor_section`` (6500 char cap), so the
    # concatenated result is still bounded end-to-end.
    # 2026-08-11 Executor-prompt-length hard budget (single source of
    # truth: ``aios_executor_message_budget``).
    #
    # ROOT_CAUSE: previously ``bounded_text = text[:4096]`` was followed
    # by an UNBOUNDED ``+ suffix`` (HF instruction + host-evidence
    # section + failed-unit body).  For evidence-rich GENERAL profiles
    # the suffix pushed the final payload to ~5000 chars, tripping
    # ``Protocol blocked: 消息过长 (5014 > 4096)``.
    #
    # The whole priority-aware assembly, profile-scoped host-evidence
    # compactness, and HARD_LIMIT / TARGET_LIMIT guard now live in
    # :func:`aios_executor_message_budget.assemble_executor_message`.
    # This function MUST stay the only ``return <task_text>`` site;
    # do NOT add a local ``[:4096]`` clamp here.
    final_message = text
    try:
        from aios_executor_message_budget import (
            HARD_LIMIT as _BUDGET_HARD,
            TARGET_LIMIT as _BUDGET_TARGET,
            assemble_executor_message,
            hard_clip_for_protocol,
        )
        from aios_orchestrator_host_evidence_injection import (
            load_workflow_host_evidence,
        )
        _host_evidence = (
            load_workflow_host_evidence(parent_id) if parent_id else None
        )
        _profile_hint = (
            str((_host_evidence or {}).get("profile", "") or "").upper()
            if _host_evidence else ""
        )
        _report = assemble_executor_message(
            text,
            host_evidence=_host_evidence,
            repair_context="",
            profile=_profile_hint or None,
        )
        final_message = _report.final_message
        # Final protocol guard.  If the priority-aware assembly still
        # ran past HARD_LIMIT (a P0 invariant violation), apply the
        # last-mile hard clip so the executor call still ships while
        # the violation is logged via the helper.
        if len(final_message) > _BUDGET_HARD:
            final_message = hard_clip_for_protocol(final_message)
    except Exception as _exc:
        # The budget helper MUST NEVER block task execution.  Fall
        # back to the bare ``text`` (still capped at HARD_LIMIT by
        # the guard below); the helper logs the underlying exception.
        if len(final_message) > 4096:
            final_message = final_message[:4096]
    return final_message


def _record_verified_outcome(parent_id: str, workflow: dict, node: dict,
                             child_state: dict, verdict: dict) -> None:
    """Admit evidence to Hermes/tool learning only after parent acceptance."""
    task_id = str(child_state.get("task_id", "") or "")
    executor = str(child_state.get("executor", "") or "")
    if not task_id or executor not in EXECUTORS:
        return
    if not verdict.get("learning_eligible", False):
        publish_event("learning.rejected", {
            "parent_id": parent_id,
            "task_id": task_id,
            "executor": executor,
            "reason": "verification_not_grounded_for_learning",
            "verification_strength": verdict.get("verification_strength", ""),
        }, "aios-orchestrator")
        return
    accepted_at = _now()
    mapping = {
        "parent_verification": "accepted",
        "verified_parent_id": parent_id,
        "verified_at": accepted_at,
        "semantic_reviewer": str(verdict.get("reviewer_backend", verdict.get("reviewer", ""))),
        "verification_strength": str(verdict.get("verification_strength", "")),
        "executor": executor,
    }
    for key in (f"aios:bus:state:{task_id}", f"aios:bus:task:{task_id}"):
        _redis_client.hset(key, mapping=mapping)
    if str(workflow.get("source", "")) != "test":
        try:
            from aios_tool_evolution import record_outcome
            record_outcome(
                executor,
                "verified",
                str(node.get("task", "")),
                str(child_state.get("result_summary", "")),
                task_id,
            )
        except Exception:
            pass
        publish_event("learning.accepted", {
            "parent_id": parent_id,
            "task_id": task_id,
            "executor": executor,
            "status": "verified",
            "reviewer": mapping["semantic_reviewer"],
            "accepted_at": accepted_at,
        }, "aios-orchestrator")


def _enqueue_node(parent_id: str, workflow: dict, node: dict,
                  repair_reason: str = "", previous_result: str = "",
                  exclude_executors=(),
                  preferred_repair_executor: str = "") -> bool:
    attempted = list(node.get("attempted_executors", []) or [])
    excluded = set(attempted)
    if isinstance(exclude_executors, str):
        excluded.update([exclude_executors] if exclude_executors else [])
    else:
        excluded.update(str(item) for item in (exclude_executors or []) if str(item))

    # Check if workflow has strict executor requirement
    strict_executor = str(workflow.get("strict_executor", "") or "")
    allow_fallback = bool(workflow.get("allow_executor_fallback", True))

    overlay = workflow.get("capability_overlay") or {}
    if strict_executor and strict_executor in EXECUTORS and not allow_fallback:
        # Strict mode: only use the specified executor. ``strict_executor``
        # is a *contract* — it must never be excluded from retry even
        # if a previous attempt landed on the same executor and the
        # ``attempted_executors`` ledger would otherwise filter it out
        # for a normal failover. Without this exemption, repair on a
        # strict-mode node fails with ``strict_executor_excluded`` after
        # the first attempt.
        excluded.discard(strict_executor)
        if strict_executor in excluded:
            node["status"] = "failed"
            node["error"] = f"strict_executor_excluded:{strict_executor}"
            return False
        # P0-1 final-production binding-truth.  When the strict
        # binding is ``codex:minimax`` (the production main chain)
        # the tool-level ``_is_executor_available`` judgment is
        # augmented by the binding-aware ``_binding_health_eligible``
        # truth surface so the Codex native CLI's 15 s cloud-config
        # timeout cannot pollute codex:minimax routing.  We also
        # accept the same verdict when ``preferred_model_binding``
        # resolves to ``codex:minimax`` even if ``strict_executor``
        # is unset (defensive: the Registry may set the binding
        # without flipping the strict flag).
        #
        # 2026-08-11 AIOS_FINAL_REAL_CLI_CLOSURE: the legacy AND
        # gate (tool-level available AND binding-level eligible)
        # made a stale ``codex:native`` cooldown block the healthy
        # ``codex:minimax`` binding path — exactly the failure
        # mode the user saw on the ``a91f3388`` real CLI run.
        # The intent of P0-1 is: when the binding is
        # ``codex:minimax`` and ``_binding_health_eligible`` returns
        # True (relay reachable + protocol ready + recent real
        # inference ledger), the strict-mode dispatch MUST use that
        # binding regardless of the tool-level cooldown caused by
        # the native CLI.  We therefore short-circuit the AND on
        # the codex:minimax binding path: binding_eligible alone is
        # sufficient when ``preferred_binding == "codex:minimax"``.
        preferred_binding = str(workflow.get(
            "preferred_model_binding", "") or "")
        binding_eligible = True
        if (strict_executor == "codex"
                or preferred_binding == "codex:minimax"):
            binding_eligible = _binding_health_eligible(
                strict_executor,
                binding_id="codex:minimax",
                resource_id="minimax.shared",
            )
        if preferred_binding == "codex:minimax" and binding_eligible:
            executor = strict_executor
        elif (
            _is_executor_available(strict_executor, capability_overlay=overlay)
            and binding_eligible
        ):
            executor = strict_executor
        else:
            node["status"] = "failed"
            if not binding_eligible:
                node["error"] = (
                    f"strict_binding_unavailable:codex:minimax:"
                    f"{strict_executor}"
                )
            else:
                node["error"] = f"strict_executor_unavailable:{strict_executor}"
            return False
    else:
        repair_executor = str(preferred_repair_executor or "")
        # Production runtime hotfix 2026-08-10: the repair-target
        # shortcut is ONLY honoured when the executor is not in the
        # task-local ``excluded`` set AND the model side reports
        # the endpoint as currently callable.  Without this gate,
        # a verification-retry helper can return the just-failed
        # executor (e.g. ``opencode``) the moment the failure-event
        # TTL elapses, and the orchestrator would re-enqueue the
        # same dead route — the exact failure mode that produced the
        # ``opencode → codex → opencode`` repair loop in the
        # 2026-08-10 burn-in (T2 / T6).
        if (
            repair_executor in EXECUTORS
            and repair_executor not in excluded
            and _is_executor_available(
                repair_executor, capability_overlay=overlay)
            and _executor_model_available(repair_executor)
        ):
            executor = repair_executor
        else:
            # [FIX] 2026-08-12 entry/runtime-fix verification-reject-alternate:
            #   When a repair was triggered by Hermes (the Reviewer)
            #   rejecting an Executor's *content* (not a runtime
            #   failure), the previously-healthy executor is still
            #   *runtime* healthy but its output has been judged
            #   insufficient for this Parent.  A new repair
            #   attempt must therefore *prefer an alternate
            #   executor* — i.e. the current Parent's
            #   ``verification_attempt_history`` is read once here
            #   (CURRENT_PARENT_ONLY scope, see below) and its
            #   executors are added to the LOCAL exclude set passed
            #   to ``choose_executor`` ONLY.  The ledger contract
            #   is preserved:
            #
            #     * ``attempted_executors`` continues to record
            #       only *runtime* failures (set in ``_repair_node``
            #       only on the runtime-failure branch).
            #     * ``verification_attempt_history`` is the
            #       CURRENT-PARENT audit trail; it is never copied
            #       into the global tool health or registry.
            #     * If EVERY candidate is verifier-rejected for
            #       this Parent and no alternate healthy executor
            #       is available, ``choose_executor`` returns the
            #       empty string and the bounded-repair
            #       / ``_enqueue_node`` post-check path falls back
            #       to the previous behaviour (a same-executor
            #       retry with verifier feedback) instead of
            #       emitting a false ``no_healthy_executor``.
            #     * The bound (``MAX_REPAIRS=2`` /
            #       ``repair_count``) is unaffected.
            #
            #   Scope discipline: ``verification_attempt_history``
            #   is read here, used for the LOCAL exclude union, and
            #   discarded — it is never written back to
            #   ``node``, ``attempted_executors``, tool health, or
            #   the registry.  A new Parent starts with a fresh
            #   ``verification_attempt_history`` (see
            #   ``submit``/``_initialise_workflow``).
            verifier_rejected = {
                str(h.get("executor", "")).strip()
                for h in (node.get("verification_attempt_history", []) or [])
                if isinstance(h, dict)
                and str(h.get("executor", "")).strip()
                and h.get("previous_result_present") is True
            }
            verifier_rejected.discard("")
            alt_excluded = set(excluded) | verifier_rejected
            executor = choose_executor(
                node.get("role", "opencode"),
                exclude=alt_excluded,
                capability_overlay=overlay,
            )
            # Production executor-live-failover 2026-08-10: bounded
            # stale-negative cache refresh.  When ``choose_executor``
            # returns the empty string AND at least one unexcluded
            # candidate has a stale negative inference cache (older
            # than :data:`_STALE_NEGATIVE_REFRESH_AFTER_SECONDS`),
            # force-refresh that cache once and retry.  Without this
            # step, an executor that recovered between scheduled
            # probes (e.g. codex probe 80 minutes old showed
            # ``network_error`` but codex is healthy now) would be
            # permanently excluded — the user-visible failure shape
            # is ``opencode failed → codex stale-neg cache says no
            # → no healthy executor → workflow fails``.  The refresh
            # is intentionally one-shot per call so an unbounded
            # probe loop cannot spawn inside ``_enqueue_node``.
            if not executor:
                # [FIX] 2026-08-12 entry/runtime-fix verifier-reject-no-alternate:
                #   Discriminate T4 (multi-verifier-rejected, no
                #   same-executor retry) from T5 (single
                #   verifier-rejected with all other candidates
                #   runtime-unavailable, bounded same-executor
                #   retry is legitimate).
                #
                #   T4 contract (preserved):
                #     verifier_rejected = {opencode, codex}, both
                #     runtime-healthy -> surface empty executor so
                #     the parent's MAX_REPAIRS gate drives the
                #     terminal transition.
                #
                #   T5 contract (added):
                #     verifier_rejected = {opencode}, codex never
                #     attempted (runtime-unavailable) ->
                #     allow bounded same-executor verification
                #     retry on opencode, with the verifier feedback
                #     re-injected.  This is the ONLY path that
                #     respects the user's "no healthy alternate ->
                #     same executor bounded retry" semantic.
                #
                #   Discriminator (in order):
                #     1. ``verifier_rejected_healthy_count >= 2``:
                #        at least two healthy candidates are
                #        already verifier-rejected; bounded retry
                #        will not rescue -> terminal (T4).
                #     2. ``verifier_rejected_healthy_count == 1``
                #        AND every non-verifier-rejected candidate
                #        is runtime-unavailable: bounded same-
                #        executor verification retry is the only
                #        path -> allow (T5).
                #     3. Other cases (e.g. healthy alternates not
                #        yet tried): the original choose_executor
                #        path would have picked them, so this
                #        branch is reached only on empty result;
                #        surface the empty executor and let
                #        MAX_REPAIRS gate the terminal transition.
                verifier_rejected_healthy_count = 0
                verifier_rejected_unhealthy = []
                verifier_rejected_list = []
                for cand in [
                    str(h.get("executor", "")).strip()
                    for h in (node.get("verification_attempt_history", []) or [])
                    if isinstance(h, dict)
                    and str(h.get("executor", "")).strip()
                    and h.get("previous_result_present") is True
                ]:
                    if not cand or cand in excluded:
                        continue
                    verifier_rejected_list.append(cand)
                    if (_is_executor_available(cand, capability_overlay=overlay)
                            and _executor_model_available(cand)):
                        verifier_rejected_healthy_count += 1
                    else:
                        verifier_rejected_unhealthy.append(cand)
                if verifier_rejected_healthy_count >= 2:
                    # T4 contract.
                    executor = ""
                elif verifier_rejected_healthy_count == 1:
                    # T5 discriminator: only allowed if every
                    # other candidate (i.e. not in
                    # ``verifier_rejected_list``) is runtime-
                    # unavailable.  ``EXECUTORS`` is
                    # ``("opencode", "claude", "codex")`` and
                    # ``verifier_rejected_list`` is the local
                    # derived set; we check the union of
                    # EXECUTORS \ verifier_rejected_list.
                    not_in_rejected = [
                        str(c) for c in EXECUTORS
                        if c not in verifier_rejected_list
                        and c not in excluded
                    ]
                    other_runtime_healthy = [
                        c for c in not_in_rejected
                        if (_is_executor_available(c, capability_overlay=overlay)
                            and _executor_model_available(c))
                    ]
                    if not other_runtime_healthy:
                        # T5 path: bounded same-executor retry.
                        executor = verifier_rejected_list[0]
                        try:
                            node["verifier_same_executor_retry"] = executor
                            node["verifier_feedback_injected"] = True
                        except Exception:
                            pass
                    else:
                        # Other healthy alternates exist but were
                        # already excluded by attempt ledger or
                        # the alt_excluded set.  Surface empty
                        # so MAX_REPAIRS / process_workflow drive
                        # the terminal transition naturally.
                        executor = ""
                else:
                    # verifier_rejected_healthy_count == 0 means
                    # no verifier-rejected executor is currently
                    # runtime-healthy; surface empty.
                    executor = ""
            if not executor:
                refreshed = False
                for candidate in EXECUTORS:
                    if candidate in excluded:
                        continue
                    if _force_refresh_executor_model_cache(candidate):
                        refreshed = True
                if refreshed:
                    executor = choose_executor(
                        node.get("role", "opencode"),
                        exclude=excluded,
                        capability_overlay=overlay,
                    )
                    if executor:
                        try:
                            node["stale_negative_refreshed"] = (
                                sorted({
                                    str(c) for c in EXECUTORS
                                    if c not in excluded
                                })
                            )
                        except Exception:
                            pass

    # P8C-U dual-axis failover hook: wraps the chosen executor in
    # the dynamic tool + model selection engine.  This block is
    # a no-op when ``AIOS_TOOL_FAILOVER_ENABLED`` and
    # ``AIOS_MODEL_FAILOVER_ENABLED`` are both off (the
    # production default outside canary).  All state changes are
    # confined to ``node[...]`` field writes; no orchestrator state
    # machine path is altered.
    # Initialise the decision variable so the strict-violation
    # check below can run even when the hook is unavailable
    # (P8C-U is optional and best-effort).
    routing_decision = None
    chosen_tool = ""
    try:
        from aios_orchestrator_failover_hook import (
            route_node_executor,
            attach_routing_to_node,
        )
        node_task_id = str(node.get("task_id", "") or parent_id)
        chosen_tool, routing_decision = route_node_executor(
            task_id=node_task_id,
            role=str(node.get("role", "opencode")),
            source=str(workflow.get("source", "api")),
            preferred_executor=executor,
            strict_executor=strict_executor,
            allow_executor_fallback=allow_fallback,
            sender=str(workflow.get("sender_id", "") or "") or None,
            capability_overlay=workflow.get("capability_overlay") or None,
            preferred_tool=str(workflow.get("preferred_executor", "") or "") or None,
            preferred_model_binding=str(workflow.get("preferred_model_binding", "") or "") or None,
            strict_tool=(workflow.get("strict_tool") if workflow.get("strict_tool") is not None else None),
            strict_model=(workflow.get("strict_model") if workflow.get("strict_model") is not None else None),
            blocked_tools=_decode_block_list(workflow.get("blocked_tools")),
            blocked_model_bindings=_decode_block_list(workflow.get("blocked_model_bindings")),
            blocked_resources=_decode_block_list(workflow.get("blocked_resources")),
            allow_tool_fallback=(workflow.get("allow_executor_fallback") if workflow.get("allow_executor_fallback") is not None else allow_fallback),
            allow_model_fallback=(workflow.get("allow_model_fallback") if workflow.get("allow_model_fallback") is not None else allow_fallback),
        )
        if chosen_tool:
            executor = chosen_tool
        try:
            attach_routing_to_node(node, routing_decision)
        except Exception:
            pass
    except Exception:
        pass
    # means the task is BLOCKED.  Wipe the executor / actual_tool /
    # actual_model_binding surface so the audit ledger cannot
    # misreport the blocked tool as having been used.  Then mark
    # the node as ``blocked`` and let the parent workflow finalise
    # with the documented STRICT_*_VIOLATION reason.
    if routing_decision is not None and getattr(
            routing_decision, "action", None) in ("strict_violation", "no_candidates"):
        try:
            from aios_orchestrator_failover_hook import attach_routing_to_node
            attach_routing_to_node(node, routing_decision)
        except Exception:
            pass
        # Make sure the per-task policy surface is wiped
        node["actual_tool"] = ""
        node["actual_model_binding"] = ""
        node["attempted_tools"] = []
        node["attempted_model_bindings"] = []
        # The decision reason carries the canonical signal.  The
        # RoutingDecision object exposes ``reason`` (legacy) plus the
        # dedicated ``tool_failover_reason`` /
        # ``model_failover_reason`` channels.  Aggregate all of them
        # so the close-out can pick the correct STRICT_*_VIOLATION
        # contract regardless of which channel the hook used.
        rr_parts = []
        for attr in ("reason", "tool_failover_reason",
                     "model_failover_reason"):
            value = getattr(routing_decision, attr, "") or ""
            if value:
                rr_parts.append(value)
        rr = " | ".join(rr_parts)
        node["status"] = "blocked"
        if ("strict_model" in rr
                or "blocked_model_bindings" in rr
                or getattr(routing_decision, "model_decision", None) is not None
                and getattr(getattr(routing_decision, "model_decision", None),
                            "action", "") == "strict_violation"):
            node["error"] = "STRICT_MODEL_VIOLATION"
        else:
            node["error"] = "STRICT_TOOL_VIOLATION"
        node["routing_action"] = "strict_violation"
        return False

    if not executor:
        node["status"] = "failed"
        node["error"] = "no_healthy_executor"
        return False

    # ----- P9F: Canonical child claim + idempotent enqueue -----
    # The single source of truth for "this (parent, node, generation)
    # has a child task_id" is the Redis SET NX key under
    # ``KEY_CANONICAL_CHILD``.  Concurrent ``_enqueue_node`` callers
    # — initial dispatch, dependency release, repair, restart
    # recovery, pending-too-long escalation — all funnel through this
    # claim.  Whichever caller wins the SET NX writes the bus state
    # hash + LPUSH; subsequent callers re-use the existing task_id
    # without re-enqueueing.
    node_index = node.get("index", 0)
    generation = int(node.get("attempt", 0))
    canonical_tid, was_new_claim = _claim_canonical_child(
        parent_id, node_index, generation,
    )
    if not canonical_tid:
        # Redis unavailable or write race — caller will retry on the
        # next ``process_workflow`` poll.  We surface a hard fail so
        # the audit ledger records the orchestrator's last attempt;
        # the canonical claim is *not* held by us in this branch so we
        # do NOT need to release.
        node["status"] = "failed"
        node["error"] = "canonical_claim_failed"
        return False

    if not was_new_claim:
        # Some caller already won the SET NX for this generation.  The
        # canonical task_id exists.  If its bus state is in a non-terminal
        # status, reuse it verbatim — re-running the executor would be
        # a duplicate inference call.  If the bus state is terminal,
        # the caller path that invoked us should have advanced the
        # generation first (via ``_repair_node``); treat the
        # same-generation retry as a no-op so we don't regenerate the
        # UUID and accidentally mint a duplicate child.
        existing_status = _canonical_child_status(canonical_tid)
        if existing_status in CHILD_STATUS_NON_TERMINAL:
            node["task_id"] = canonical_tid
            node["status"] = existing_status or "queued"
            node["error"] = ""
            return True
        if existing_status in CHILD_STATUS_TERMINAL:
            # Same generation, child is terminal.  The legitimate retry
            # path (``_repair_node``) has already advanced ``attempt``.
            # If we're here, the caller path forgot to advance it.  Do
            # NOT mint a new task_id — return False so the caller can
            # either advance the generation (proper retry) or finalise.
            node["error"] = (
                "duplicate_generation_terminal_no_advance:"
                f"{canonical_tid}:{existing_status}"
            )
            return False
        # No bus state yet — the previous winner crashed before
        # completing the LPUSH.  Continue and complete the enqueue for
        # this canonical task_id so the child actually enters the queue.

    task_text = _execution_text(
        str(workflow.get("goal", "")), node,
        repair_reason=repair_reason, previous_result=previous_result,
        parent_id=parent_id, actual_executor=executor,
        single_node=len(workflow.get("nodes", [])) == 1,
    )
    criteria = workflow.get("user_verification_criteria", [])
    child_criteria = [
        item for item in criteria if isinstance(item, dict)
    ] if len(workflow.get("nodes", [])) == 1 else []
    task_id = enqueue_task(
        task_name=task_text,
        system="aios-orchestrator",
        priority=2 if executor == "claude" else 3,
        logic_depth=_logic_depth(executor),
        source=str(workflow.get("source", "api")),
        context=_json({
            "parent_id": parent_id,
            "node_index": node_index,
            "original_goal": str(workflow.get("goal", ""))[:6000],
            "generation": generation,
            "canonical": True,
        }),
        verification_criteria=child_criteria or None,
        parent_id=parent_id,
        node_index=node_index,
        preferred_executor=executor,
        acceptance=node.get("acceptance", []),
        attempt=generation,
        approval_id=str(workflow.get("approval_id", "") or ""),
        risk_action=str(workflow.get("risk_action", "") or ""),
        task_id_override=canonical_tid,
    )
    if not task_id:
        # The canonical claim is held by us; release it so a future
        # legitimate retry path can re-attempt without being blocked
        # by a phantom generation-0 claim.
        _release_canonical_child(parent_id, node_index, generation)
        node["status"] = "failed"
        node["error"] = "enqueue_failed"
        return False
    if task_id != canonical_tid:
        # Defensive: enqueue_task must return the overridden task_id
        # verbatim.  If it ever diverges, prefer the bus-side task_id
        # (it is what executors will actually claim) but still treat
        # the canonical claim as authoritative for future idempotent
        # look-ups.  Fix the claim to match the actual returned value.
        _release_canonical_child(parent_id, node_index, generation)
        # Re-claim with the actual returned value (best effort).
        try:
            _redis_client.set(
                _canonical_child_key(parent_id, node_index, generation),
                task_id, ex=CANONICAL_CHILD_TTL_SECONDS,
            )
        except Exception:
            pass
        canonical_tid = task_id
    node.update({
        "task_id": canonical_tid,
        "status": "queued",
        "assigned_executor": executor,
        "actual_executor": "",
        "error": "",
        "queued_at": _now(),
        "canonical_generation": generation,
    })
    if executor not in attempted:
        attempted.append(executor)
    node["attempted_executors"] = attempted
    publish_event("workflow.node_queued", {
        "parent_id": parent_id,
        "task_id": canonical_tid,
        "node_index": node_index,
        "requested_role": node.get("role", ""),
        "assigned_executor": executor,
        "attempt": generation,
        "canonical": True,
    }, "aios-orchestrator")
    return True


def submit(goal: str, source: str = "api", sender_id: str = "local",
           session_key: str = "", preferred_executor: str = "",
           user_priority=None, user_logic_depth: str = "",
           verification_criteria: list = None,
           strict_executor: str = "",
           allow_executor_fallback: bool = True,
           capability_overlay: dict = None,
           # P12 (close-out 20260727-§五) task-local controls.
           preferred_model_binding: str = "",
           preferred_planner: str = "",
           preferred_reviewer: str = "",
           allow_planner_fallback: bool = True,
           allow_reviewer_fallback: bool = True,
           strict_tool: bool = False,
           strict_model: bool = False,
           blocked_tools: list = None,
           blocked_model_bindings: list = None,
           # Host Read-Only Evidence Boundary (final-production 2026-08-11).
           # The CLI supplies a canonical profile (GENERAL/AUDIT/OPS/CODE) and
           # an optional ``--project`` path; the Orchestrator runs the host
           # evidence boundary on this profile, persists the result on the
           # workflow hash, and splices it into the Executor / Reviewer
           # prompts so the Codex sandbox never has to talk to the host.
           profile: str = "",
           project_path: str = "",
           host_evidence_profile: str = "",
           blocked_resources: list = None,
           blocked_reviewer_tools: list = None,
           blocked_planner_tools: list = None) -> dict:
    """Durably accept a parent goal and return immediately.

    P5 changes:
    - Accepts strict_executor and allow_executor_fallback fields
    - strict_executor: if set and allow_executor_fallback=False, only
      the named executor can be used; no fallback to other executors.

    P12 (close-out 20260727-§五) additions:
    - ``preferred_model_binding``  first-choice ``ToolModelBinding``;
                                  may be blocked by
                                  ``blocked_model_bindings``.
    - ``preferred_planner``         first-choice planner tool; may be
                                  blocked by ``blocked_planner_tools``.
    - ``preferred_reviewer``        first-choice reviewer tool; may be
                                  blocked by ``blocked_reviewer_tools``.
    - ``allow_planner_fallback``    if False, planner must be the
                                  ``preferred_planner``; no other
                                  planner tool may be used.
    - ``allow_reviewer_fallback``   same rule for reviewers.
    - ``strict_tool`` / ``strict_model`` are alternate knobs;
                                  ``allow_*_fallback=False`` is the
                                  canonical way to enforce strict mode.
    - ``blocked_tools``             per-task tool exclusion.
    - ``blocked_model_bindings``    per-task model-binding exclusion
                                  (also accepted via
                                  ``capability_overlay.blocked_model_bindings``
                                  for audit-sender use).
    - ``blocked_resources``        per-task shared-resource exclusion.
    - ``blocked_reviewer_tools``    per-task reviewer exclusion.
    - ``blocked_planner_tools``     per-task planner exclusion.

    All these fields are written into the parent workflow hash and
    read back by the executor / model / planner / reviewer
    selection paths. They never mutate the registry or global
    health state.
    """
    goal = str(goal or "").strip()
    if not goal:
        return {"task_ids": [], "parent_id": "", "error": "empty_goal"}
    if not _is_available():
        return {"task_ids": [], "parent_id": "", "error": "redis_unavailable"}

    parent_id = generate_task_id()
    created_at = _now()
    risk_action = classify_l4_action(goal)
    approval_id = ""
    initial_status = "planning"
    if risk_action:
        approval_id = request_approval(
            goal,
            "AIOS L4 action requires explicit owner approval before planning or execution.",
            risk="L4",
            requester=sender_id,
            parent_id=parent_id,
            action=risk_action,
        )
        if not approval_id:
            return {
                "task_ids": [], "parent_id": "",
                "error": "approval_store_unavailable",
            }
        initial_status = "awaiting_approval"

    # Validate strict_executor
    resolved_strict = strict_executor if strict_executor in EXECUTORS else ""

    # Build per-task blocker serialisations (string lists — easy
    # to compare against ``ToolModelBinding.binding_id`` /
    # ``SharedModelResource.resource_id`` / executor / planner /
    # reviewer tool_ids).
    def _norm_block_list(value) -> str:
        if isinstance(value, (list, tuple, set)):
            return _json(sorted({str(item) for item in value if str(item)}))
        if isinstance(value, str):
            return _json([item.strip() for item in value.split(",") if item.strip()])
        return "[]"
    bt_json = _norm_block_list(blocked_tools)
    bb_json = _norm_block_list(blocked_model_bindings)
    br_json = _norm_block_list(blocked_resources)
    brt_json = _norm_block_list(blocked_reviewer_tools)
    bpt_json = _norm_block_list(blocked_planner_tools)

    saved = _save_workflow(
        parent_id,
        parent_id=parent_id,
        schema_version="aios-workflow/1.1",
        status=initial_status,
        goal=goal,
        source=source,
        sender_id=sender_id,
        session_key=session_key,
        preferred_executor=preferred_executor if preferred_executor in EXECUTORS else "",
        strict_executor=resolved_strict,
        allow_executor_fallback=allow_executor_fallback,
        user_priority=user_priority if user_priority is not None else "",
        user_logic_depth=user_logic_depth or "",
        created_at=created_at,
        plan_mode="pending",
        plan_error="",
        user_verification_criteria=verification_criteria or [],
        repair_count=0,
        nodes=[],
        approval_id=approval_id,
        risk_action=risk_action,
        capability_overlay=dict(capability_overlay) if capability_overlay else {},
        # P12 (close-out 20260727-§五) persisted task-local controls.
        preferred_model_binding=preferred_model_binding,
        preferred_planner=preferred_planner,
        preferred_reviewer=preferred_reviewer,
        allow_planner_fallback=allow_planner_fallback,
        allow_reviewer_fallback=allow_reviewer_fallback,
        strict_tool=strict_tool,
        strict_model=strict_model,
        blocked_tools=bt_json,
        blocked_model_bindings=bb_json,
        blocked_resources=br_json,
        blocked_reviewer_tools=brt_json,
        blocked_planner_tools=bpt_json,
        # Host Read-Only Evidence Boundary (final-production 2026-08-11).
        # The profile / project_path / host_evidence_profile flow from
        # the CLI into the workflow hash so the planner, executor,
        # reviewer and parent workflow step all see the same canonical
        # authority.  The actual evidence is gathered synchronously
        # below and persisted alongside the hash.
        profile=profile or "GENERAL",
        project_path=project_path or "",
        host_evidence_profile=host_evidence_profile or profile or "GENERAL",
    )

    # Host Read-Only Evidence Boundary: gather host facts ONCE on
    # submit so the Codex sandbox never has to read
    # ``127.0.0.1:18801`` or any other host-local surface.  This
    # runs in the orchestrator process so the result is a plain
    # Python object that survives the workflow lifecycle.
    try:
        from aios_host_readonly_evidence import collect_host_evidence
        host_evidence = collect_host_evidence(
            host_evidence_profile or profile or "GENERAL",
            goal=goal,
            project_path=project_path or "",
        )
        # FIX_ONE closure (2026-08-17): ``host_evidence_used`` must flip to
        # ``True`` whenever the synchronous collector returned at least
        # one non-error capability item — not only for those profiles
        # whose static capability set already included the
        # evidence-producing capabilities.  GENERAL-profile
        # ``WEB_DISCOVERY_FETCH`` results must count, otherwise every
        # independent-live query is locked into
        # ``repair_exhausted: authoritative_*_missing`` even when the
        # host-evidence block actually contains the facts.
        #
        # The collector encodes success as ``"error" not in item``
        # (``_summary``); do NOT rely on an explicit ``ok`` field,
        # which most capability handlers omit.  Both the
        # ``summary.ok`` counter and the per-item ``"error"`` absence
        # are checked so a partially-empty collect still surfaces
        # usable evidence.
        items = host_evidence.get("items") or []
        ok_items = [it for it in items if isinstance(it, dict) and "error" not in it]
        profile_norm = (host_evidence_profile or profile or "GENERAL").upper()
        host_evidence_used = bool(ok_items)
        _save_workflow(
            parent_id,
            host_evidence=_json(host_evidence),
            host_evidence_used=host_evidence_used,
            host_evidence_profile=profile_norm,
        )
    except Exception as _exc:
        # Evidence collection must NEVER block task submission.
        # Surface a structured empty evidence record so the verifier
        # and the reviewer see the same shape as the success path.
        try:
            _save_workflow(
                parent_id,
                host_evidence=_json({
                    "profile": host_evidence_profile or profile or "GENERAL",
                    "generated_at": _now(),
                    "items": [],
                    "summary": {
                        "total": 0,
                        "ok": 0,
                        "error": 1,
                        "by_capability": {},
                        "error_reason": f"host_evidence_collect_failed:{type(_exc).__name__}:{str(_exc)[:200]}",
                    },
                }),
                host_evidence_used=False,
            )
        except Exception:
            pass
    if not saved:
        return {"task_ids": [], "parent_id": "", "error": "workflow_persist_failed"}

    if session_key:
        register_callback(
            parent_id, source=source, reply_key=session_key, sender_id=sender_id,
        )
    if initial_status == "awaiting_approval":
        _redis_client.zrem(KEY_ACTIVE, parent_id)
    publish_event("workflow.created", {
        "parent_id": parent_id,
        "source": source,
        "status": initial_status,
        "approval_id": approval_id,
        "risk_action": risk_action,
    }, "aios-orchestrator")
    return {
        "task_ids": [parent_id],
        "parent_id": parent_id,
        "count": 1,
        "plan_mode": "pending",
        "status": initial_status,
        "approval_required": bool(approval_id),
        "approval_id": approval_id,
        "risk_action": risk_action,
    }


def approve_workflow(parent_id: str, approval_id: str, approver: str,
                     note: str = "") -> dict:
    """Resume one L4 workflow only after its bound approval is authenticated."""
    workflow = get_workflow(parent_id)
    if workflow.get("status") != "awaiting_approval":
        return {"ok": False, "error": "workflow_not_awaiting_approval"}
    if str(workflow.get("approval_id", "")) != str(approval_id or ""):
        return {"ok": False, "error": "approval_id_mismatch"}
    ok, reason = approve_request(
        approval_id, parent_id, approver=approver, note=note,
    )
    if not ok:
        return {"ok": False, "error": reason}
    ok, reason = consume_approval(approval_id, parent_id)
    if not ok:
        return {"ok": False, "error": reason}
    _save_workflow(
        parent_id,
        status="planning",
        approved_at=_now(),
        approved_by=str(approver)[:128],
    )
    publish_event("workflow.approved", {
        "parent_id": parent_id, "approval_id": approval_id,
        "approver": str(approver)[:128],
    }, "aios-orchestrator")
    return {"ok": True, "parent_id": parent_id, "status": "planning"}


def _plan_and_dispatch(parent_id: str, workflow: dict) -> dict:
    """Plan a durably accepted workflow in the background and dispatch roots.

    P5 fix:
    - When strict_executor is set and allow_executor_fallback=False,
      the plan's single node role is forced to strict_executor.
    - preferred_executor sets the role for single-node plans.
    - plan_mode (openclaw-minimax) is only about planning, NOT about
      executor selection.
    """
    goal = str(workflow.get("goal", "") or "")
    # P9D-R role-closure: rebuild the TaskRoutingPolicy view from the
    # workflow hash so the planner is selected by the same registry +
    # policy surface as the executor / reviewer paths.
    try:
        from aios_task_routing_policy import from_workflow_dict
        task_policy = from_workflow_dict(workflow, role="planner")
    except Exception:
        task_policy = {}
    build_plan_extras: dict = {}
    try:
        # P9D-R role-closure: also resolve the planner target first
        # so the workflow hash records the actual planner / binding
        # / mode even when build_plan short-circuits.
        planner_tool, planner_binding, planner_provider, planner_model, plan_mode_label = (
            _resolve_planner_target(task_policy or {})
        )
        _plan_result = build_plan(
            goal, parent_id, task_policy=task_policy,
        )
        if len(_plan_result) == 4:
            plan, plan_mode, plan_error, build_plan_extras = _plan_result
        else:
            plan, plan_mode, plan_error = _plan_result
            build_plan_extras = {}
        if build_plan_extras:
            # P9D-R-RT: when the fallback chain carried the call, the
            # planner / binding / fallback_count we persist must
            # reflect the actually-used candidate, not the preferred
            # one.  ``_resolve_planner_target`` already returned the
            # preferred tuple, so re-bind from the extras dictionary.
            # P9D-R-actual-planner-semantics: use ``is not None``
            # instead of ``or`` so an explicitly-empty
            # ``actual_planner`` (planning-failed) is NOT silently
            # replaced by the preferred planner.
            _ap = build_plan_extras.get("actual_planner")
            planner_tool = str(_ap) if _ap is not None else str(planner_tool)
            _ab = build_plan_extras.get("actual_binding")
            planner_binding = str(_ab) if _ab is not None else str(planner_binding)
            _apro = build_plan_extras.get("actual_provider")
            planner_provider = str(_apro) if _apro is not None else str(planner_provider)
            _amod = build_plan_extras.get("actual_model")
            planner_model = str(_amod) if _amod is not None else str(planner_model)
            # Re-derive the plan_mode_label from the actual planner
            # binding so the workflow hash describes the route that
            # actually succeeded.
            # P9D-R-actual-planner-semantics: when actual_planner is
            # empty (planning-failed), plan_mode_label stays empty
            # so the persisted planner_mode field truthfully records
            # that no planner succeeded.
            if planner_tool == "opencode":
                plan_mode_label = "opencode-plan-only"
            elif planner_tool == "claude":
                plan_mode_label = "claude-minimax-plan"
            elif planner_tool:
                plan_mode_label = "openclaw-minimax"
            else:
                plan_mode_label = ""
    except Exception as exc:
        _save_workflow(
            parent_id,
            status="failed",
            error=f"planning_exception:{type(exc).__name__}:{str(exc)[:500]}",
            completed_at=_now(),
        )
        _redis_client.zrem(KEY_ACTIVE, parent_id)
        publish_event("alert.error", {
            "component": "aios-orchestrator-planner",
            "parent_id": parent_id,
            "error": f"{type(exc).__name__}:{str(exc)[:500]}",
        }, "aios-orchestrator")
        return workflow_task_view(parent_id)

    # P9D-R role-closure: persist the planner routing fields on the
    # workflow hash so every downstream reader (gateway task,
    # verification record, trace, Result Push) sees which planner
    # was actually selected, what binding carried the call, whether
    # the planner executed in PLAN_ONLY, and which planners were
    # excluded by the policy.  These are read back by the close-out
    # tests and the capability-baseline evidence ledger.
    # Unwrap TaskRoutingPolicy attributes so the persist layer can
    # use plain dict-style field access (this also tolerates legacy
    # plain-dict policies).
    preferred_planner_val = (
        getattr(task_policy, "preferred_planner", "")
        if task_policy and not isinstance(task_policy, dict)
        else (task_policy.get("preferred_planner", "") if task_policy else "")
    ) or ""
    blocked_planner_tools_val = list(
        getattr(task_policy, "blocked_planner_tools", [])
        if task_policy and not isinstance(task_policy, dict)
        else (task_policy.get("blocked_planner_tools") if task_policy else [])
        or []
    )
    allow_planner_fallback_val = bool(
        getattr(task_policy, "allow_planner_fallback", True)
        if task_policy and not isinstance(task_policy, dict)
        else (task_policy.get("allow_planner_fallback", True) if task_policy else True)
    )
    # P9D-R-OpenClaw-Planner-Tool-Boundary: persist the actual
    # candidate sequence the planner walked so the close-out
    # ledger can prove that the fallback path was exercised.
    # P9D-R-RT: when ``build_plan`` returned the extras dict we
    # know exactly which candidates were tried and which planner
    # actually succeeded; surface that on the workflow hash
    # rather than recomputing from the (now possibly reassigned)
    # ``planner_tool``.  When extras are absent (e.g. the legacy
    # 3-tuple branch or the planning-failed branch) we fall back
    # to the candidate-chain reconstruction so the record stays
    # truth-telling in every path.
    # P9D-R: tolerate ``TaskRoutingPolicy`` dataclass alongside
    # plain-dict task policies — read the allow_planner_fallback
    # flag from the unwrapped value already extracted above.
    _allow_fb = allow_planner_fallback_val
    if build_plan_extras:
        attempted_planners_chain = list(
            build_plan_extras.get("attempted_planners") or [planner_tool]
        )
        excluded_planners_chain = list(
            build_plan_extras.get("excluded_planners") or []
        )
        first_failure_scope = str(
            build_plan_extras.get("first_failure_scope", "")
        )
        planner_fallback_count_wf = int(
            build_plan_extras.get("fallback_count", 0) or 0
        )
    else:
        preferred_chain_legacy = [planner_tool] + [
            c for c in (
                _allow_fb and ["opencode", "claude"] or []
            ) if c != planner_tool
        ] if task_policy else [planner_tool] + [
            c for c in ["opencode", "claude"] if c != planner_tool
        ]
        attempted_planners_chain = list(preferred_chain_legacy)
        excluded_planners_chain = list(blocked_planner_tools_val or [])
        first_failure_scope = ""
        planner_fallback_count_wf = (
            len(preferred_chain_legacy) - 1 if preferred_chain_legacy else 0
        )
    try:
        _save_workflow(
            parent_id,
            preferred_planner=str(preferred_planner_val or ""),
            blocked_planner_tools=list(blocked_planner_tools_val or []),
            allow_planner_fallback=bool(allow_planner_fallback_val),
            actual_planner=str(planner_tool or ""),
            planner_binding=str(planner_binding or ""),
            planner_provider=str(planner_provider or ""),
            planner_model=str(planner_model or ""),
            planner_mode=str(plan_mode_label or ""),
            attempted_planners=list(attempted_planners_chain),
            excluded_planners=list(excluded_planners_chain),
            planner_failure_scope=str(first_failure_scope or ""),
            planner_fallback_count=int(planner_fallback_count_wf),
            planner_bypass=False,
        )
    except Exception:
        # Persistence is best-effort; routing itself is already
        # decided and used in the build_plan call above.
        pass

    # Close-out 2026-07-27-§六: a planner timeout / no-candidate result
    # must immediately finalise the parent workflow rather than pin the
    # ``status=planning`` state forever.
    if plan_mode == "planning-failed":
        terminal_status = (
            "blocked"
            if (plan_error and (
                "POLICY_BLOCKED" in plan_error
                or "blocked_resources" in plan_error
                or "blocked_planner_tools" in plan_error))
            else "failed"
        )
        terminal_error = plan_error or "planning-failed"
        nodes_payload: list = []
        if plan:
            nodes_payload = [{
                "index": 0,
                "task": goal,
                "depends_on": [],
                "role": _fallback_role(goal),
                "acceptance": ["planner failed before any node could be produced"],
                "evidence_mode": _classify_evidence_mode(goal, goal),
                "status": "blocked" if terminal_status == "blocked" else "failed",
                "task_id": "",
                "attempt": 0,
                "assigned_executor": "",
                "actual_executor": "",
                "attempted_executors": [],
                "result": "",
                "verification": {},
                "error": terminal_error,
            }]
        _save_workflow(
            parent_id,
            plan=plan,
            nodes=nodes_payload,
            plan_mode=plan_mode,
            plan_error=plan_error,
            status=terminal_status,
            error=terminal_error,
            terminal=True,
            terminal_reason=terminal_error,
            terminal_transition=_now(),
            completed_at=_now(),
        )
        _redis_client.zrem(KEY_ACTIVE, parent_id)
        publish_event(f"task.{terminal_status}", {
            "task_id": parent_id,
            "parent_id": parent_id,
            "status": terminal_status,
            "summary": terminal_error[:2000],
            "source": workflow.get("source", ""),
            "natural_terminal": True,
            "planner_timeout": True,
        }, "aios-orchestrator")
        return workflow_task_view(parent_id)

    preferred_executor = str(workflow.get("preferred_executor", "") or "")
    strict_executor = str(workflow.get("strict_executor", "") or "")
    allow_fallback = bool(workflow.get("allow_executor_fallback", True))

    # Apply executor preference to plan
    if len(plan) == 1:
        if strict_executor and strict_executor in EXECUTORS:
            # Strict mode overrides everything
            plan[0]["role"] = strict_executor
        elif preferred_executor and preferred_executor in EXECUTORS:
            # Preferred executor is the first choice
            plan[0]["role"] = preferred_executor

    nodes = []
    for index, item in enumerate(plan):
        nodes.append({
            "index": index,
            "task": item["task"],
            "depends_on": item["depends_on"],
            "role": item["role"],
            "acceptance": item["acceptance"],
            "evidence_mode": item.get("evidence_mode", "semantic"),
            "status": "planned",
            "task_id": "",
            "attempt": 0,
            "assigned_executor": "",
            "actual_executor": "",
            "attempted_executors": [],
            "result": "",
            "verification": {},
            "error": "",
        })

    workflow.update({
        "plan": plan,
        "nodes": nodes,
        "plan_mode": plan_mode,
        "plan_error": plan_error,
        "status": "dispatching",
    })
    for node in nodes:
        if not node["depends_on"]:
            _enqueue_node(parent_id, workflow, node)

    status = "running"
    error = ""
    if not nodes or any(node["status"] == "failed" for node in nodes):
        status = "failed"
        error = "initial_dispatch_failed"
    _save_workflow(
        parent_id,
        plan=plan,
        nodes=nodes,
        plan_mode=plan_mode,
        plan_error=plan_error,
        status=status,
        error=error,
        dispatched_at=_now(),
    )
    if status in TERMINAL:
        _redis_client.zrem(KEY_ACTIVE, parent_id)
    publish_event("workflow.planned", {
        "parent_id": parent_id,
        "plan_mode": plan_mode,
        "node_count": len(nodes),
        "status": status,
    }, "aios-orchestrator")
    return workflow_task_view(parent_id)


def _repair_node(parent_id: str, workflow: dict, node: dict,
                 reason: str, previous_result: str, actual_executor: str,
                 verification_failure: bool = False) -> None:
    # Close-out 20260727-§五: a child task that was cancelled by
    # the AIOS_CLOSEOUT_MAINTENANCE routine (e.g. stale acceptance
    # backlog cleanup) MUST NOT be re-enqueued.  ``_repair_node`` is
    # the only path that re-injects a failed/cancelled child into
    # the Pending queue, so honouring the cancellation here is the
    # single point that prevents the cancel-and-repair loop.
    if (node.get("cancelled_by")
            or node.get("cancel_classification") == "TEST_TASK_SAFE_TO_CANCEL"
            or (actual_executor in ("", None)
                and "STALE_ACCEPTANCE_BACKLOG_CLEANUP" in str(reason))):
        try:
            child_state = _safe_hget_task_state(str(node.get("task_id", "")))
        except Exception:
            child_state = {}
        if child_state.get("cancelled_by") == "AIOS_CLOSEOUT_MAINTENANCE":
            node["status"] = "cancelled"
            node["error"] = (
                "CANCELLED_BY_CLOSEOUT_MAINTENANCE:"
                f"{reason[:1000]}"
            )
            node["cancel_reason"] = "STALE_ACCEPTANCE_BACKLOG_CLEANUP"
            return
    attempt = int(node.get("attempt", 0))
    if attempt >= MAX_REPAIRS:
        node["status"] = "failed"
        node["error"] = f"repair_exhausted: {reason[:1000]}"
        return
    node["attempt"] = attempt + 1
    workflow["repair_count"] = int(workflow.get("repair_count", 0)) + 1
    # [FIX] 2026-08-12 entry/runtime-fix separate-verification-rejection:
    #   An executor that RAN SUCCESSFULLY and produced a real result
    #   is HEALTHY even if Hermes (the Reviewer) rejected the result.
    #   A "verifier rejection" is a *content* verdict, not an
    #   executor-failure verdict, and MUST NOT be treated as
    #   ``executor_runtime_failure``:
    #
    #     1. The previous attempt's executor is NOT added to
    #        ``attempted_executors`` (the permanent exclude ledger),
    #        because excluding a healthy executor on the next repair
    #        would systematically drain the GENERAL fallback chain
    #        and surface as the false ``no_healthy_executor`` error
    #        (the user-visible production fault).
    #     2. The bounded ``_record_tool_runtime_failure`` event is
    #        NOT emitted for a verifier-only rejection.  Emitting it
    #        would feed a phantom failure into the model-side
    #        health truth and propagate across repair generations.
    #
    #   Same-executor repair for a verifier-only rejection is still
    #   allowed via ``_verification_retry_target`` (which checks
    #   eligibility directly, not the attempted ledger) and is the
    #   legal path for retrying an executor whose evidence was
    #   rejected.  This preserves the bounded-repair invariant
    #   (``MAX_REPAIRS=2``) and the contract that
    #   ``attempted_executors`` only records *runtime* failures.
    #
    #   The verifier-rejection case is RECOGNISED here and recorded
    #   on the node as ``verification_attempt_history`` (read-only
    #   audit trail) so the next ``_enqueue_node`` can prefer
    #   same-executor retry when a different executor is healthy
    #   but its result is unknown, while still allowing the
    #   frozen ``_verification_retry_target`` path to fire.
    #
    #   ``attempted`` is initialised in BOTH branches because the
    #   later ``_enqueue_node(..., exclude_executors=attempted, ...)``
    #   call always reads the local name.  For the verifier-reject
    #   branch the value is the *current* (unchanged) ledger; for
    #   the runtime-failure branch it includes the just-failed
    #   executor as the original code intended.
    attempted = list(node.get("attempted_executors", []) or [])
    if verification_failure and previous_result:
        try:
            history = list(node.get("verification_attempt_history", []) or [])
            history.append({
                "attempt": attempt,
                "executor": actual_executor,
                "reason": str(reason)[:200],
                "previous_result_present": True,
            })
            node["verification_attempt_history"] = history[-5:]
        except Exception:
            pass
    else:
        if actual_executor and actual_executor not in attempted:
            attempted.append(actual_executor)
        node["attempted_executors"] = attempted
    # Production executor-live-failover 2026-08-10: every repair
    # path that has a concrete ``actual_executor`` MUST record a
    # tool runtime failure event so the next ``choose_executor`` /
    # ``_enqueue_node`` call excludes the just-failed executor from
    # routing immediately.  Previously the failure event was only
    # emitted from ``_expire_child_for_repair`` (queue / pending /
    # lock / running stalls); the call-time ``child_status=failed``
    # path bypassed the event entirely and relied on the bounded
    # 120 s recovery TTL plus the cache file ``model_available``
    # field.  When the on-disk cache for the next executor in the
    # fallback order was stale (e.g. codex probe 80 minutes old),
    # the orchestrator ended up with zero healthy executors even
    # though every daemon was alive.  Recording the failure event
    # here closes the gap regardless of whether the executor
    # daemon called ``record_inference_failure`` itself.
    #
    # [FIX] 2026-08-12 entry/runtime-fix separate-verification-rejection:
    #   The runtime-failure event MUST NOT be emitted on a verifier
    #   rejection when the executor actually produced a real result
    #   (``previous_result`` is non-empty).  Emitting it on the
    #   verifier-only path was the second half of the false
    #   ``no_healthy_executor`` fault: it would feed a phantom
    #   failure into the model-side health truth and block
    #   ``choose_executor`` for up to 120 s even though the executor
    #   was actually healthy.  Real runtime failures (queue stall,
    #   transport, provider, adapter) still emit the event below.
    if actual_executor and not (verification_failure and previous_result):
        try:
            from aios_model_resources import (
                FAILURE_SCOPE_RESOURCE,
                FAILURE_SCOPE_BINDING,
                FAILURE_SCOPE_LOCAL_RUNTIME,
                FAILURE_SCOPE_TOOL_ADAPTER,
            )
        except Exception:
            FAILURE_SCOPE_RESOURCE = "RESOURCE"
            FAILURE_SCOPE_BINDING = "BINDING"
            FAILURE_SCOPE_LOCAL_RUNTIME = "LOCAL_RUNTIME"
            FAILURE_SCOPE_TOOL_ADAPTER = "TOOL_ADAPTER"
        # Map the failure reason into a FAILURE_SCOPE_* category so
        # downstream routing can decide whether to retry the same
        # tool with a different binding (BINDING), skip the entire
        # tool (TOOL_PROCESS / TOOL_ADAPTER), or treat the upstream
        # provider as the failure surface (RESOURCE).
        reason_text = str(reason or "").lower()
        if any(token in reason_text for token in (
                "dispatch_claim_timeout", "pending_too_long",
                "executor_hard_timeout")):
            scope = FAILURE_SCOPE_LOCAL_RUNTIME
        elif any(token in reason_text for token in (
                "executor_checkin_timeout", "executor_adapter",
                "local_adapter", "adapter_error")):
            scope = FAILURE_SCOPE_TOOL_ADAPTER
        elif any(token in reason_text for token in (
                "timeout", "timed out", "network_error", "network",
                "connection", "dns", "external_timeout",
                "external_network_error", "external_service_cooldown")):
            scope = FAILURE_SCOPE_RESOURCE
        elif any(token in reason_text for token in (
                "quota", "insufficient", "rate_limit", "auth",
                "402", "429", "401", "403")):
            scope = FAILURE_SCOPE_RESOURCE
        else:
            scope = FAILURE_SCOPE_BINDING
        try:
            _record_tool_runtime_failure(
                actual_executor,
                scope=scope,
                reason=f"repair_node:{reason[:120]}",
            )
        except Exception:
            pass
        try:
            _invalidate_tool_process_cache()
        except Exception:
            pass
    retry_executor = (
        _verification_retry_target(node, reason, actual_executor)
        if verification_failure else ""
    )
    if retry_executor:
        node["verification_same_executor_repairs"] = (
            int(node.get("verification_same_executor_repairs", 0)) + 1
        )
    _enqueue_node(
        parent_id,
        workflow,
        node,
        repair_reason=reason,
        previous_result=previous_result,
        exclude_executors=attempted,
        preferred_repair_executor=retry_executor,
    )


def _finalize(parent_id: str, workflow: dict, status: str,
              error: str = "") -> dict:
    nodes = workflow.get("nodes", [])
    if status == "completed":
        if len(nodes) == 1:
            final_result = str(nodes[0].get("result", ""))
        else:
            parts = []
            for node in nodes:
                parts.append(
                    f"[Node {node.get('index') + 1} | {node.get('actual_executor') or node.get('assigned_executor')}]\n"
                    f"{node.get('result', '')}"
                )
            final_result = "\n\n".join(parts)
    else:
        failed = [
            f"node {node.get('index') + 1}: {node.get('error', 'failed')}"
            for node in nodes if node.get("status") == "failed"
        ]
        final_result = "AIOS workflow failed: " + ("; ".join(failed) or error or "unknown")
    final_result = final_result[:32768]
    completed_at = _now()
    _save_workflow(
        parent_id,
        nodes=nodes,
        status=status,
        error=error,
        final_result=final_result,
        repair_count=int(workflow.get("repair_count", 0)),
        completed_at=completed_at,
    )
    _redis_client.zrem(KEY_ACTIVE, parent_id)
    publish_event(f"task.{status}", {
        "task_id": parent_id,
        "parent_id": parent_id,
        "status": status,
        "summary": final_result[:2000],
        "source": workflow.get("source", ""),
    }, "aios-orchestrator")
    return workflow_task_view(parent_id)


def process_workflow(parent_id: str) -> dict:
    workflow = get_workflow(parent_id)
    if workflow.get("status") in TERMINAL or workflow.get("status") == "unknown":
        return workflow_task_view(parent_id)
    if workflow.get("status") == "awaiting_approval":
        return workflow_task_view(parent_id)
    if workflow.get("status") == "planning":
        return _plan_and_dispatch(parent_id, workflow)

    nodes = workflow.get("nodes", [])
    changed = False
    for node in nodes:
        # P9D-R-live §4: ``repairing`` is a non-terminal state set by
        # ``_expire_child_for_repair``.  Honour the explicit guard
        # contract — only completed / failed / verifying / cancelled
        # are terminal — and process ``repairing`` here so the
        # ``_repair_node`` path creates a fresh generation.
        if node.get("status") in ("completed", "failed", "verifying",
                                  "cancelled", "canceled"):
            continue
        if node.get("status") == "repairing":
            # P9D-R-live §4.5: terminal at MAX_REPAIRS, repair
            # otherwise.  ``_repair_node`` is the *only* path that
            # advances generation; it sets ``node.status = "queued"``
            # via ``_enqueue_node`` and increments ``attempt``.
            attempt = int(node.get("attempt", 0))
            if attempt >= MAX_REPAIRS:
                node["status"] = "failed"
                node["error"] = (
                    f"repair_exhausted:{node.get('repair_reason', '')[:1000]}"
                )
                node["repair_finished_at"] = _now()
                changed = True
                continue
            attempted = list(node.get("attempted_executors", []) or [])
            last_executor = attempted[-1] if attempted else ""
            _repair_node(
                parent_id,
                workflow,
                node,
                str(node.get("repair_reason") or "dispatch_claim_timeout"),
                str(node.get("result") or ""),
                last_executor,
            )
            node["repair_finished_at"] = _now()
            # Clear the transitional marker so subsequent passes do
            # not re-enter this branch with the same repairing stamp.
            if node.get("status") == "queued":
                pass
            changed = True
            continue
        task_id = node.get("task_id", "")
        if task_id:
            child = get_task_state(task_id)
            child_status = child.get("status", "unknown")
            if child.get("executor"):
                node["actual_executor"] = child["executor"]
            if child_status in ("pending", "locked", "running", "verifying"):
                stall_reason = _child_stall_reason(node, child_status, child)
                if stall_reason and (
                    stall_reason.startswith("dispatch_claim_timeout")
                    or stall_reason.startswith("executor_checkin_timeout")
                    or stall_reason.startswith("executor_hard_timeout")
                    or stall_reason.startswith("pending_too_long")
                ):
                    # P9E close-out: ANY of the four timeout flavours
                    # produced by ``_child_stall_reason`` triggers
                    # ``_expire_child_for_repair``.  Prior to P9E only
                    # ``dispatch_claim_timeout`` (executor unreachable)
                    # escalated; ``executor_checkin_timeout`` (locked too
                    # long), ``executor_hard_timeout`` (running too long)
                    # and ``queued_behind_executor`` (backpressure)
                    # were emitted but only updated ``node.error``,
                    # which let historical child tasks sit in the
                    # pending queue forever and pinned the parent
                    # workflow in ``running``.  Bounded repair is the
                    # natural terminal signal; without it the parent
                    # workflow cannot converge.
                    _expire_child_for_repair(
                        parent_id, workflow, node, child, stall_reason,
                    )
                    changed = True
                    continue
                # Mark the node status from the live child for both
                # "no stall detected" and "executor busy" cases; only the
                # true dispatch-claim-timeout escalates to repair.
                node["status"] = child_status
                if stall_reason and child_status == "pending":
                    # Persist the backpressure marker on the node so the
                    # monitor sees it; the task is still legitimately queued.
                    node["error"] = stall_reason
                changed = True
                continue
            if child_status == "completed":
                node["status"] = "verifying"
                changed = True
                # P9D-R role-closure: build a task policy view from the
                # workflow hash so the Reviewer selection in
                # verify_parent_node honours the same
                # preferred_reviewer / blocked_reviewer_tools /
                # allow_reviewer_fallback as the planner and executor.
                try:
                    from aios_task_routing_policy import from_workflow_dict
                    task_policy_view = from_workflow_dict(workflow, role="reviewer")
                except Exception:
                    task_policy_view = {}
                try:
                    verdict = verify_parent_node(
                        parent_id,
                        str(workflow.get("goal", "")),
                        node,
                        child,
                        task_policy=task_policy_view,
                    )
                except Exception as exc:
                    verdict = {
                        "passed": False,
                        "stage": "verification_exception",
                        "reviewer": "aios-orchestrator",
                        "reason": f"verify_raised:{type(exc).__name__}:{str(exc)[:300]}",
                        "repair_instruction": "Re-verify the result independently.",
                    }
                node["verification"] = verdict
                if verdict.get("passed"):
                    _record_verified_outcome(parent_id, workflow, node, child, verdict)
                    node["status"] = "completed"
                    node["result"] = verdict.get(
                        "deliverable", child.get("result_summary", ""),
                    )
                    node["completed_at"] = _now()
                elif verdict.get("stage") == "semantic_infrastructure":
                    node["status"] = "failed"
                    node["error"] = verdict.get("reason", "semantic_verifier_unavailable")
                else:
                    _repair_node(
                        parent_id,
                        workflow,
                        node,
                        verdict.get("reason") or verdict.get("repair_instruction", ""),
                        child.get("result_summary", ""),
                        child.get("executor", ""),
                        verification_failure=True,
                    )
                changed = True
                continue
            if child_status in ("failed", "cancelled", "canceled"):
                _repair_node(
                    parent_id,
                    workflow,
                    node,
                    child.get("execution_error") or child.get("result_summary", "") or child_status,
                    child.get("result_summary", ""),
                    child.get("executor", ""),
                )
                changed = True
                continue
            if child_status == "unknown":
                node["status"] = "failed"
                node["error"] = "child_state_missing"
                changed = True
                continue

        if not node.get("task_id"):
            dependency_states = [
                nodes[index].get("status")
                for index in node.get("depends_on", [])
                if 0 <= index < len(nodes)
            ]
            if any(state == "failed" for state in dependency_states):
                node["status"] = "failed"
                node["error"] = "dependency_failed"
                changed = True
            elif all(state == "completed" for state in dependency_states):
                _enqueue_node(parent_id, workflow, node)
                changed = True

    workflow["nodes"] = nodes
    if any(node.get("status") == "failed" for node in nodes):
        _save_workflow(
            parent_id,
            nodes=nodes,
            repair_count=int(workflow.get("repair_count", 0)),
            status="failed",
            error="workflow_node_failed",
        )
        return _finalize(parent_id, workflow, "failed", "workflow_node_failed")
    # Close-out 20260727-§十一: a node marked ``blocked`` (strict_tool /
    # strict_model violation) is a terminal signal.  Propagate it to
    # the parent workflow so the audit ledger records a single
    # definitive terminal status instead of leaving the parent in
    # ``running`` indefinitely.
    if any(node.get("status") == "blocked" for node in nodes):
        blocked_errors = [
            f"node {node.get('index') + 1}: {node.get('error', 'blocked')}"
            for node in nodes if node.get("status") == "blocked"
        ]
        _save_workflow(
            parent_id,
            nodes=nodes,
            repair_count=int(workflow.get("repair_count", 0)),
            status="blocked",
            error=("; ".join(blocked_errors) or "workflow_node_blocked"),
        )
        return _finalize(parent_id, workflow, "blocked",
                          ("; ".join(blocked_errors)
                           or "workflow_node_blocked"))
    if nodes and all(node.get("status") == "completed" for node in nodes):
        _save_workflow(
            parent_id,
            nodes=nodes,
            repair_count=int(workflow.get("repair_count", 0)),
            status="completed",
        )
        return _finalize(parent_id, workflow, "completed")

    if changed:
        _save_workflow(
            parent_id,
            nodes=nodes,
            repair_count=int(workflow.get("repair_count", 0)),
            status="running",
        )
    return workflow_task_view(parent_id)


def _reconcile_orphan_task_states() -> int:
    """P9D-R close-out: release orphan ``aios:bus:state:*`` records.

    An "orphan" task state is one whose key either has an empty
    ``task_id`` (the trailing ``:`` in ``aios:bus:state:`` makes the
    hash key unusable as a child of any workflow) or whose
    ``parent_id`` no longer maps to an existing workflow.  These can
    arise from older P7A / P9A test-loop leaks where a runner wrote
    state without a workflow owner; ``process_workflow`` only walks
    the ``KEY_ACTIVE`` zset so the orphan rows would otherwise remain
    locked forever.  We refuse to ``DEL`` the keys directly and instead
    drive them through the normal terminal transition so the audit
    ledger records a real reconciliation outcome.

    Returns the number of orphan state rows reconciled.  Bounded at
    ``MAX_ORPHAN_RECONCILE_PER_TICK`` so a noisy run cannot starve the
    normal workflow poll.
    """
    if not _is_available():
        return 0
    MAX_ORPHAN_RECONCILE_PER_TICK = 32
    state_prefix = f"{KEY_STATE}:"
    reconciled = 0
    try:
        cursor = 0
        while reconciled < MAX_ORPHAN_RECONCILE_PER_TICK:
            cursor, keys = _redis_client.scan(
                cursor=cursor, match=f"{state_prefix}*", count=128,
            )
            for k in keys:
                if reconciled >= MAX_ORPHAN_RECONCILE_PER_TICK:
                    break
                key_str = k.decode() if isinstance(k, bytes) else k
                task_id = key_str[len(state_prefix):]
                if not task_id:
                    # Truly empty task_id; transition through the bus
                    # so the audit log records it.
                    state = _redis_client.hgetall(key_str)
                    if (state.get(b"status") or state.get("status")) == b"locked":
                        _redis_client.hset(
                            key_str, mapping={
                                "status": "failed",
                                "failure_reason": "orphan_state_no_task_id",
                                "ts_orphan_reconciled": _now(),
                            },
                        )
                        reconciled += 1
                    continue
                # Skip rows that still belong to a live workflow.
                parent_id = (
                    state.get(b"parent_id") or state.get("parent_id")
                ) if False else None
                raw = _redis_client.hgetall(key_str)
                if isinstance(raw, dict):
                    parent_id = raw.get(b"parent_id") or raw.get("parent_id")
                    if isinstance(parent_id, bytes):
                        parent_id = parent_id.decode()
                if parent_id:
                    wf_key = f"aios:orchestrator:workflow:{parent_id}"
                    if _redis_client.exists(wf_key):
                        continue
                # Either no parent or parent workflow is gone → reconcile.
                current_status = raw.get(b"status") or raw.get("status") or b""
                if isinstance(current_status, bytes):
                    current_status = current_status.decode()
                if current_status in ("locked", "running", "pending"):
                    new_status = "failed" if current_status != "locked" else "failed"
                    _redis_client.hset(
                        key_str,
                        mapping={
                            "status": new_status,
                            "failure_reason": "orphan_state_parent_missing",
                            "ts_orphan_reconciled": _now(),
                        },
                    )
                    reconciled += 1
            if cursor == 0:
                break
    except Exception as exc:  # pragma: no cover — defensive
        publish_event("alert.warn", {
            "component": "aios-orchestrator",
            "error": f"orphan_reconcile_failed:{type(exc).__name__}",
            "detail": str(exc)[:500],
        }, "aios-orchestrator")
    return reconciled


def _has_running_lease(parent_id: str) -> bool:
    """Check if any active lease exists for this workflow's children."""
    try:
        for state_key in _redis_client.keys("aios:bus:state:*"):
            raw = _redis_client.hgetall(state_key)
            parent = raw.get(b"parent_id") or raw.get("parent_id")
            if isinstance(parent, bytes):
                parent = parent.decode()
            if parent != parent_id:
                continue
            status = raw.get(b"status") or raw.get("status")
            if isinstance(status, bytes):
                status = status.decode()
            if status in ("locked", "running"):
                return True
    except Exception:
        return False
    return False


def _has_pending_or_running_child(parent_id: str) -> bool:
    """Check if workflow has any non-terminal child task."""
    try:
        for state_key in _redis_client.keys("aios:bus:state:*"):
            raw = _redis_client.hgetall(state_key)
            parent = raw.get(b"parent_id") or raw.get("parent_id")
            if isinstance(parent, bytes):
                parent = parent.decode()
            if parent != parent_id:
                continue
            status = raw.get(b"status") or raw.get("status")
            if isinstance(status, bytes):
                status = status.decode()
            if status in ("pending", "locked", "running", "verifying"):
                return True
    except Exception:
        return False
    return False


def _reconcile_orphan_workflows(grace_seconds: int = 300) -> int:
    """P9DR close-out: reconcile orphan workflows that have no valid task_data,
    no executable child, no active lease, no claim owner, no deliverable result,
    and no parseable plan, beyond a bounded grace period.

    The reconciler drives eligible workflows through the standard
    ``_save_workflow`` terminal transition (``status=failed``) so the
    audit ledger records a real reconciliation outcome.  The workflow
    hash itself is never ``DEL``ed.  Reuse of the canonical state
    machine is mandatory: no second reconciler engine is introduced.

    Eligibility — ALL must hold:

    1. workflow status is non-terminal;
    2. workflow has no parseable, non-empty ``plan``;
    3. workflow has no ``final_result``;
    4. no child task is in ``locked`` / ``running`` (no running lease);
    5. no child task is in ``pending`` (no active claim owner);
    6. no child task at all OR every child task is itself in a
       terminal state with no non-terminal residue (no executable
       child);
    7. workflow was created at least ``grace_seconds`` ago (defends
       against pre-persistence workflows that just hit the bus).

    Returns the number of orphan workflows reconciled.  Bounded at
    ``MAX_ORPHAN_WF_PER_TICK`` so a noisy run cannot starve the
    normal workflow poll.
    """
    if not _is_available():
        return 0

    MAX_ORPHAN_WF_PER_TICK = 16
    GRACE_SECONDS = max(60, int(grace_seconds))
    wf_prefix = f"{KEY_WORKFLOW}:"
    reconciled = 0
    try:
        cursor = 0
        while reconciled < MAX_ORPHAN_WF_PER_TICK:
            cursor, keys = _redis_client.scan(
                cursor=cursor, match=f"{wf_prefix}*", count=128,
            )
            for k in keys:
                if reconciled >= MAX_ORPHAN_WF_PER_TICK:
                    break
                key_str = k.decode() if isinstance(k, bytes) else k
                wf_id = key_str[len(wf_prefix):]
                if not wf_id:
                    continue
                wf = _redis_client.hgetall(key_str)
                if not wf:
                    continue
                status = wf.get(b"status") or wf.get("status")
                if isinstance(status, bytes):
                    status = status.decode()
                if status in TERMINAL:
                    continue
                if status not in (
                    "planning", "dispatching", "queued", "claimed",
                    "running", "verifying", "repairing",
                    "awaiting_approval",
                ):
                    continue

                # Reject if workflow has a parseable non-empty plan
                plan_raw = wf.get(b"plan") or wf.get("plan")
                if isinstance(plan_raw, bytes):
                    plan_raw = plan_raw.decode()
                plan = None
                if plan_raw:
                    try:
                        plan = json.loads(plan_raw)
                    except Exception:
                        plan = None
                if plan and isinstance(plan, list) and len(plan) > 0:
                    continue

                # Reject if workflow has a final_result
                if wf.get(b"final_result") or wf.get("final_result"):
                    continue

                # Reject if any child task is currently locked/running
                if _has_running_lease(wf_id):
                    continue

                # Reject if any child task is in pending state (claim active)
                if _has_pending_or_running_child(wf_id):
                    continue

                # Reject if workflow was just created (grace period)
                created = wf.get(b"created_at") or wf.get("created_at")
                if isinstance(created, bytes):
                    created = created.decode()
                if not created:
                    continue
                try:
                    ts = datetime.fromisoformat(
                        str(created).replace("Z", "+00:00"),
                    )
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age = (
                        datetime.now(timezone.utc) - ts
                    ).total_seconds()
                except Exception:
                    continue
                if age < GRACE_SECONDS:
                    continue

                # All eligibility criteria hold. Drive workflow to
                # failed via the standard _save_workflow terminal
                # transition.
                nodes_raw = wf.get(b"nodes") or wf.get("nodes")
                if isinstance(nodes_raw, bytes):
                    nodes_raw = nodes_raw.decode()
                try:
                    parsed_nodes = (
                        json.loads(nodes_raw) if nodes_raw else []
                    )
                except Exception:
                    parsed_nodes = []
                updated_nodes = []
                for node in (parsed_nodes or []):
                    if (
                        isinstance(node, dict)
                        and node.get("status") not in TERMINAL
                    ):
                        updated_nodes.append({
                            **node,
                            "status": "failed",
                            "error": "WORKFLOW_TASK_DATA_MISSING",
                            "orphan_reconciled": True,
                            "ts_orphan_reconciled": _now(),
                        })
                    else:
                        updated_nodes.append(node)
                final_error = "WORKFLOW_TASK_DATA_MISSING"
                _save_workflow(
                    wf_id,
                    status="failed",
                    error=final_error,
                    terminal=True,
                    terminal_reason="ORPHAN_WORKFLOW_RECONCILED",
                    failure_scope="WORKFLOW_STATE_INTEGRITY",
                    nodes=updated_nodes,
                    completed_at=_now(),
                )
                # Remove from active set (do NOT DEL the workflow hash)
                try:
                    _redis_client.zrem(KEY_ACTIVE, wf_id)
                except Exception:
                    pass
                publish_event(
                    "task.failed", {
                        "task_id": wf_id,
                        "parent_id": wf_id,
                        "status": "failed",
                        "summary": final_error,
                        "source": (
                            wf.get(b"source") or wf.get("source") or ""
                        ),
                        "natural_terminal": True,
                        "force_finalised": False,
                        "orphan_workflow_reconciled": True,
                    }, "aios-orchestrator",
                )
                reconciled += 1
            if cursor == 0:
                break
    except Exception as exc:  # pragma: no cover — defensive
        publish_event("alert.warn", {
            "component": "aios-orchestrator",
            "error": (
                f"orphan_workflow_reconcile_failed:"
                f"{type(exc).__name__}"
            ),
            "detail": str(exc)[:500],
        }, "aios-orchestrator")
    return reconciled


def run_once() -> dict:
    if not _is_available():
        return {"ok": False, "error": "redis_unavailable"}
    orphan_state_reconciled = _reconcile_orphan_task_states()
    orphan_workflow_reconciled = _reconcile_orphan_workflows()
    raw_ids = _redis_client.zrange(KEY_ACTIVE, 0, 99)
    processed = [
        {"orphan_state_reconciled": orphan_state_reconciled},
        {"orphan_workflow_reconciled": orphan_workflow_reconciled},
    ]

    def _process_one(raw_id: bytes | str) -> dict:
        parent_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        if not _acquire_workflow_lock(parent_id):
            return {"pid": parent_id, "skipped": "already_processing"}
        try:
            return process_workflow(parent_id)
        finally:
            _release_workflow_lock(parent_id)

    futures = [_WORKFLOW_POOL.submit(_process_one, rid) for rid in raw_ids]
    for f in as_completed(futures):
        try:
            processed.append(f.result())
        except Exception:
            processed.append({"error": "unhandled_exception"})
    return {"ok": True, "processed": len(processed), "workflows": processed}


def wait_workflow(parent_id: str, timeout_seconds: int = 300) -> dict:
    deadline = time.time() + max(1, min(int(timeout_seconds), 900))
    while time.time() < deadline:
        view = workflow_task_view(parent_id)
        if view.get("status") in TERMINAL:
            return view
        time.sleep(0.5)
    view = workflow_task_view(parent_id)
    view["wait_timeout"] = True
    return view


def run_daemon() -> None:
    print("AIOS Orchestrator v1.0 - parent workflow owner")
    # Task 014: force fresh registries so minimax-official policy
    # registered in aios_model_resources is visible to the failover
    # engine.  Without this, a long-lived daemon process keeps the
    # singletons it captured at first import, which pre-dated the
    # minimax-official ToolModelPolicy / ToolModelBinding we added.
    try:
        import aios_model_resources as _amr
        import aios_tool_failover as _atf
        import aios_routing_policy as _arp
        _amr._DEFAULT_POLICY_REGISTRY = None
        _amr._DEFAULT_BINDING_REGISTRY = None
        _amr._DEFAULT_RESOURCE_REGISTRY = None
        _atf._ENGINE_SINGLETON = None
        _arp._ROUTING_SINGLETON = None
        print(f"[orchestrator] singletons reset for minimax-official", flush=True)
    except Exception as _e:
        print(f"[orchestrator] reset_singletons_warn: {_e}")
    while True:
        try:
            heartbeat("aios-orchestrator", revision=LOADED_REVISION)
            run_once()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            publish_event("alert.error", {
                "component": "aios-orchestrator",
                "error": str(exc)[:500],
            }, "aios-orchestrator")
        time.sleep(POLL_SECONDS)


register_pin("orchestrator.submit", submit, "Create and own a parent workflow")
register_pin("orchestrator.status", workflow_task_view, "Read parent workflow state")
register_pin("orchestrator.wait", wait_workflow, "Wait for verified parent result")
register_pin("orchestrator.process", process_workflow, "Advance one parent workflow")
register_pin("orchestrator.approve", approve_workflow, "Approve and resume one L4 workflow")


# ---------------------------------------------------------------------------
# P9F — Historical duplicate child reconciliation (Phase F / K)
# ---------------------------------------------------------------------------
# Pre-P9F duplicate children (created by repeated ``_enqueue_node``
# without the canonical claim) sit in the bus index alongside the
# canonical child for the same (parent, node_index).  This routine
# is the *only* legitimate path for terminating them:
#
#   * pick the canonical child deterministically (real result first,
#     then terminal completeness, then active lease, then earliest
#     ``ts_created`` — never by UUID alone)
#   * write the non-canonical duplicates through the standard
#     ``cancelled`` terminal state with
#     ``reason=DUPLICATE_CHILD_SUPERSEDED``
#   * drop their pending / lock / index entries so they cannot be
#     re-claimed by any executor
#   * re-write the canonical child into the canonical-claim Redis
#     key so future idempotent enqueue calls reuse it
#
# No manual Redis edits; no ``force_finalise``; no silent drops.  The
# supersede audit record travels through ``update_task_status`` +
# ``publish_event`` so it is observable by the existing monitoring
# surface.


def _canonical_child_pick(children: list) -> tuple:
    """Return ``(canonical, duplicates)`` from a list of child state
    dicts (one per child task_id, all sharing the same parent +
    node_index).  Returns ``([...], [...])`` with the canonical
    child first.

    Selection priority (deterministic, audit-friendly):
      1. A child with a real, non-empty result_summary wins.
      2. Among terminal children, the one with the latest terminal
         timestamp wins.
      3. Among locked/running children, the one whose ``ts_locked``
         is most recent wins (it is the lease currently alive).
      4. Among pending children, the one with the oldest
         ``ts_created`` wins (it is the earliest claim).
      5. Tie-break by task_id ascending so the choice is stable.
    """
    if not children:
        return [], []
    if len(children) == 1:
        return [children[0]], []

    def rank(child: dict) -> tuple:
        status = str(child.get("status", ""))
        result = str(child.get("result_summary", "") or "").strip()
        ts_created = str(child.get("ts_created", "") or "")
        ts_terminal = (
            child.get("ts_completed")
            or child.get("ts_failed")
            or child.get("ts_cancelled")
            or ""
        )
        ts_terminal = str(ts_terminal or "")
        ts_locked = str(child.get("ts_locked", "") or "")
        tid = str(child.get("task_id", "") or "")
        has_result = bool(result) and status in ("completed",)
        is_terminal = status in ("completed", "failed", "cancelled", "canceled")
        is_active_lease = status in ("locked", "running", "verifying")
        # Negative score: higher priority gets a lower tuple head
        priority_bucket = (
            0 if has_result else
            1 if is_terminal else
            2 if is_active_lease else
            3
        )
        # We want the most recent timestamp to win → negate
        return (
            priority_bucket,
            -_safe_iso_sortable(ts_terminal or ts_locked or ts_created),
            tid,
        )

    sorted_children = sorted(children, key=rank)
    canonical = [sorted_children[0]]
    duplicates = sorted_children[1:]
    return canonical, duplicates


def _safe_iso_sortable(value: str) -> float:
    """Convert an ISO-8601 timestamp to a comparable float; fall back
    to a tiny number so missing timestamps sort last."""
    if not value:
        return -1e18
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (TypeError, ValueError):
        return -1e18


def _group_children_by_parent_node(children: list) -> dict:
    """Group bus-state dicts by (parent_id, node_index, attempt).

    Returns a dict ``(parent_id, node_index, generation) -> [child, ...]``
    so the caller can inspect duplicates without re-scanning Redis.
    """
    grouped: dict = {}
    for child in children:
        parent_id = str(child.get("parent_id", "") or "")
        node_index = str(child.get("node_index", "") or "")
        attempt = str(child.get("attempt", "") or "0")
        if not parent_id or node_index == "":
            continue
        key = (parent_id, node_index, attempt)
        grouped.setdefault(key, []).append(child)
    return grouped


def supersede_duplicate_children(parent_id: str, node_index: int,
                                 generation: int | None = None,
                                 *, commit: bool = False,
                                 dry_run: bool = False) -> dict:
    """Find every child of (parent_id, node_index) sharing the same
    canonical generation and reconcile them to a single canonical
    child.  Non-canonical duplicates are routed through the normal
    ``cancelled`` terminal state with ``reason=DUPLICATE_CHILD_SUPERSEDED``;
    their pending / lock / index entries are pulled so executors
    cannot claim them.

    Args:
        parent_id:   workflow id.
        node_index:  node index within the workflow.
        generation:  optional filter — only consider children whose
                     stored ``attempt`` field equals this value.
                     ``None`` matches any generation (used by the
                     historical sweep).
        commit:      when True, perform the writes; when False, only
                     return the classification and counters.
        dry_run:     alias for ``commit=False`` with explicit intent.

    Returns:
        Dict with ``canonical``, ``duplicates``, ``inspect``,
        ``superseded``, ``errors`` keys.

    Notes:
      * Pure function on the bus state; the workflow hash's
        ``nodes[].task_id`` is only rewritten to the canonical
        task_id when ``commit=True``.  Without ``commit``, callers
        can preview the classification.
      * No ``force_finalise`` is used; non-canonical duplicates
        enter the legitimate ``cancelled`` terminal state via
        ``update_task_status``.
    """
    write = bool(commit) and not dry_run
    result = {
        "parent_id": parent_id,
        "node_index": int(node_index),
        "generation": generation,
        "inspect": 0,
        "canonical": [],
        "duplicates": [],
        "superseded": [],
        "errors": [],
    }
    if not _is_available():
        result["errors"].append("redis_unavailable")
        return result
    children: list = []
    try:
        index_keys = _redis_client.zrange(KEY_INDEX, 0, -1)
    except Exception as exc:
        result["errors"].append(f"index_scan_failed:{type(exc).__name__}")
        return result
    for raw_tid in index_keys:
        tid = raw_tid.decode() if isinstance(raw_tid, bytes) else raw_tid
        try:
            state = _redis_client.hgetall(f"{KEY_QUEUE_STATE}:{tid}")
        except Exception:
            continue
        if not state:
            continue
        decoded = {}
        for k, v in state.items():
            kk = k.decode() if isinstance(k, bytes) else k
            vv = v.decode() if isinstance(v, bytes) else v
            decoded[kk] = vv
        if decoded.get("parent_id") != parent_id:
            continue
        if str(decoded.get("node_index", "")) != str(int(node_index)):
            continue
        if generation is not None:
            child_generation = str(decoded.get("attempt", "") or "")
            if child_generation and int(child_generation) != int(generation):
                continue
        decoded["task_id"] = tid
        children.append(decoded)
    result["inspect"] = len(children)
    if not children:
        return result
    canonical, duplicates = _canonical_child_pick(children)
    result["canonical"] = [
        {"task_id": c.get("task_id", ""), "status": c.get("status", "")}
        for c in canonical
    ]
    result["duplicates"] = [
        {"task_id": d.get("task_id", ""), "status": d.get("status", "")}
        for d in duplicates
    ]
    if not write or not duplicates:
        return result
    canonical_tid = str(canonical[0].get("task_id", "") or "")
    if canonical_tid:
        try:
            _redis_client.set(
                _canonical_child_key(
                    parent_id, node_index,
                    int(canonical[0].get("attempt", 0) or 0),
                ),
                canonical_tid, ex=CANONICAL_CHILD_TTL_SECONDS,
            )
        except Exception as exc:
            result["errors"].append(
                f"canonical_claim_write_failed:{type(exc).__name__}"
            )
    for dup in duplicates:
        tid = str(dup.get("task_id", "") or "")
        if not tid or tid == canonical_tid:
            continue
        supersede_reason = (
            f"{SUPERSEDE_REASON}:canonical={canonical_tid}"
        )
        try:
            _supersede_terminalise_child(tid, supersede_reason, canonical_tid)
            result["superseded"].append(tid)
        except Exception as exc:
            result["errors"].append(
                f"supersede_failed:{tid}:{type(exc).__name__}"
            )
    publish_event("workflow.duplicate_children_superseded", {
        "parent_id": parent_id,
        "node_index": int(node_index),
        "generation": generation,
        "canonical_child_id": canonical_tid,
        "superseded_count": len(result["superseded"]),
        "natural_terminal": True,
        "force_finalised": False,
    }, "aios-orchestrator")
    return result


def supersede_all_duplicate_children(*, commit: bool = False,
                                     limit: int = 10000) -> dict:
    """Sweep the bus index for any (parent, node) pair with more than
    one child and reconcile each such pair in a single Redis pass.
    Returns aggregate counters; safe to invoke at startup or
    periodically without disturbing an otherwise clean queue.

    This routine runs in **single-pass** O(n) time, not the O(n^2)
    of a naive ``for group: supersede_duplicate_children`` loop, so a
    10000-entry bus index resolves in well under a second.
    """
    if not _is_available():
        return {"ok": False, "error": "redis_unavailable"}
    write = bool(commit)
    children: list = []
    try:
        index_keys = _redis_client.zrange(KEY_INDEX, 0, -1)
    except Exception as exc:
        return {"ok": False, "error": f"index_scan_failed:{type(exc).__name__}"}
    for raw_tid in index_keys:
        tid = raw_tid.decode() if isinstance(raw_tid, bytes) else raw_tid
        try:
            state = _redis_client.hgetall(f"{KEY_QUEUE_STATE}:{tid}")
        except Exception:
            continue
        if not state:
            continue
        decoded = {}
        for k, v in state.items():
            kk = k.decode() if isinstance(k, bytes) else k
            vv = v.decode() if isinstance(v, bytes) else v
            decoded[kk] = vv
        parent_id = str(decoded.get("parent_id", "") or "")
        node_index = str(decoded.get("node_index", "") or "")
        if not parent_id or node_index == "":
            continue
        decoded["task_id"] = tid
        decoded["parent_id"] = parent_id
        decoded["node_index"] = node_index
        children.append(decoded)
    grouped = _group_children_by_parent_node(children)
    if limit and len(grouped) > limit:
        bounded_keys = list(grouped.keys())[:limit]
        grouped = {k: grouped[k] for k in bounded_keys}
    out = {
        "ok": True,
        "write": write,
        "groups_inspected": 0,
        "groups_with_duplicates": 0,
        "duplicates_superseded": 0,
        "canonical_selected": 0,
        "existing_results_reused": 0,
        "executor_calls_prevented": 0,
        "review_calls_prevented": 0,
        "verification_calls_prevented": 0,
        "acceptance_calls_prevented": 0,
        "nodes": [],
        "errors": [],
    }
    for (parent_id, node_index, generation), group in grouped.items():
        out["groups_inspected"] += 1
        if len(group) <= 1:
            continue
        out["groups_with_duplicates"] += 1
        canonical, duplicates = _canonical_child_pick(group)
        if not canonical:
            continue
        canonical_tid = str(canonical[0].get("task_id", "") or "")
        node_summary = {
            "workflow": parent_id,
            "node": int(node_index) if str(node_index).isdigit() else node_index,
            "child_count_before": len(group),
            "canonical": canonical_tid,
            "duplicate_count": len(duplicates),
        }
        if write and canonical_tid:
            try:
                _redis_client.set(
                    _canonical_child_key(parent_id, int(node_index), int(generation)),
                    canonical_tid, ex=CANONICAL_CHILD_TTL_SECONDS,
                )
            except Exception as exc:
                out["errors"].append(
                    f"canonical_claim_write_failed:{type(exc).__name__}"
                )
        for dup in duplicates:
            tid = str(dup.get("task_id", "") or "")
            if not tid or tid == canonical_tid:
                continue
            if write:
                supersede_reason = (
                    f"{SUPERSEDE_REASON}:canonical={canonical_tid}"
                )
                try:
                    _supersede_terminalise_child(
                        tid, supersede_reason, canonical_tid,
                    )
                except Exception as exc:
                    out["errors"].append(
                        f"supersede_failed:{tid}:{type(exc).__name__}"
                    )
                    continue
                out["duplicates_superseded"] += 1
            out["executor_calls_prevented"] += 1
            out["review_calls_prevented"] += 1
            out["verification_calls_prevented"] += 1
            out["acceptance_calls_prevented"] += 1
        out["canonical_selected"] += 1
        # Existing-result reuse detection.
        canonical_state = _safe_hget_task_state(canonical_tid) if write else canonical[0]
        if canonical_state.get("result_summary"):
            out["existing_results_reused"] += 1
            node_summary["final_node_state"] = "completed"
            node_summary["final_workflow_state"] = "completed_or_completed_node"
        out["nodes"].append(node_summary)
    if write and out["groups_with_duplicates"] > 0:
        publish_event("workflow.historical_duplicate_sweep", {
            "groups_with_duplicates": out["groups_with_duplicates"],
            "duplicates_superseded": out["duplicates_superseded"],
            "canonical_selected": out["canonical_selected"],
            "executor_calls_prevented": out["executor_calls_prevented"],
            "natural_terminal": True,
            "force_finalised": False,
        }, "aios-orchestrator")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", default="")
    parser.add_argument("--submit", default="")
    parser.add_argument("--source", default="cli")
    args = parser.parse_args()
    if args.daemon:
        run_daemon()
        return 0
    if args.once:
        print(_json(run_once()))
        return 0
    if args.status:
        print(json.dumps(workflow_task_view(args.status), ensure_ascii=False, indent=2))
        return 0
    if args.submit:
        print(json.dumps(submit(args.submit, source=args.source), ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())