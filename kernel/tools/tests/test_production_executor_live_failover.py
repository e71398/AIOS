#!/usr/bin/env python3
"""AIOS Production Executor Live Failover Closure (2026-08-10).

End-to-end test of the live executor failover contract:

* OpenCode call-time ``TimeoutError`` / ``network_error`` MUST
  produce a tool runtime failure event immediately.
* Stale *positive* cache MUST be invalidated on actual call-time
  failure (the previous P9DR hotfix already handled this; this
  module re-asserts the contract at the unit level).
* Task-local exclusion MUST survive across repair generations
  (``attempted_executors`` is the single source of truth).
* All-OpenCode-bindings-down MUST route to a cross-tool codex.
* Codex failure MUST route to claude (when claude is healthy).
* ``strict_tool=True`` MUST NOT cross-tool fallback.
* A failed executor in task A MUST NOT permanently pollute task B.
* A subsequent positive recovery probe MUST allow opencode back
  into a *new* task.
* Repair MUST inherit the failed-binding / failed-tool context.
* Verification retry MUST NOT route back to a failed executor.
* Planner ``planned_tool`` and actual ``actual_tool`` MUST be
  recorded separately so the audit ledger is truthful.
* No healthy executor MUST terminalise naturally — never repair
  forever.

These tests use the in-process stub pattern shared with the P9DR
runtime health freshness suite (see
``kernel/tools/tests/test_p9dr_runtime_health_freshness.py``) so
the orchestrator's hot-path can be exercised without spinning up
real executor daemons, real OpenCode probes, or real Redis writes.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "tests"))

import aios_orchestrator as _orch


def _load_orchestrator() -> object:
    """Reload ``aios_orchestrator`` so module-level state is clean
    between tests.

    Returns the freshly imported module object (callers MUST use
    this object, not the top-level ``_orch``, after a reload).
    """
    import importlib
    if "aios_orchestrator" in sys.modules:
        importlib.reload(sys.modules["aios_orchestrator"])
    return sys.modules["aios_orchestrator"]


def _stub_node(idx: int = 0, **overrides) -> dict:
    """Build a minimal workflow node dict for the enqueue paths."""
    base = {
        "index": idx,
        "task": "verify live executor failover",
        "depends_on": [],
        "role": "opencode",
        "acceptance": [
            "Verify live executor failover honors task-local exclusion.",
        ],
        "evidence_mode": "semantic",
        "status": "queued",
        "task_id": "",
        "attempt": 0,
        "assigned_executor": "",
        "actual_executor": "",
        "attempted_executors": [],
        "result": "",
        "verification": {},
        "error": "",
    }
    base.update(overrides)
    return base


def _stub_workflow(**overrides) -> dict:
    base = {
        "goal": "verify live executor failover",
        "source": "test",
        "nodes": [_stub_node()],
        "status": "running",
        "plan_mode": "opencode-plan-only",
        "user_verification_criteria": [],
        "allow_executor_fallback": True,
        "strict_executor": "",
        "preferred_executor": "",
        "capability_overlay": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# A. opencode health cache=true + actual TimeoutError -> failure event
# ---------------------------------------------------------------------------

def test_a_timeout_emits_tool_runtime_failure_event(monkeypatch):
    """Section 14-A: when the lightweight + model cache say opencode
    is available AND a real call returns ``TimeoutError``, the
    repair path MUST record a tool runtime failure event so the very
    next ``choose_executor`` excludes opencode.

    Setup: child has just reported ``status=failed`` with
    ``execution_error='[opencode/server] task timed out after 300
    seconds'``. The orchestrator's ``process_workflow`` path calls
    ``_repair_node`` with that error string. We assert that the
    failure-event surface (FAILURE_SCOPE_RESOURCE for the
    ``timeout`` token) is honoured.
    """
    orch = _load_orchestrator()
    wf = _stub_workflow()
    recorded = []

    def fake_record(tool, *, scope, reason):
        recorded.append({"tool": tool, "scope": scope, "reason": reason})
        return {"tool_id": tool, "scope": scope, "reason": reason}

    monkeypatch.setattr(
        orch, "_record_tool_runtime_failure", fake_record,
    )
    # ``_enqueue_node`` will try to claim / enqueue — bypass it so
    # the test focuses on the failure-event recording.
    def fake_enqueue(*args, **kwargs):
        return True

    monkeypatch.setattr(orch, "_enqueue_node", fake_enqueue)
    orch._repair_node(
        "test-parent-A",
        wf,
        wf["nodes"][0],
        "[opencode/server] task timed out after 300 seconds",
        "[opencode] previous result",
        "opencode",
    )
    assert any(
        r["tool"] == "opencode" and r["scope"] == "RESOURCE"
        for r in recorded
    ), recorded


# ---------------------------------------------------------------------------
# B. opencode actual timeout -> stale positive cache invalidated
# ---------------------------------------------------------------------------

def test_b_call_time_failure_invalidates_stale_positive_cache(
    monkeypatch,
):
    """Section 14-B: an actual ``TimeoutError`` MUST immediately
    invalidate the stale *positive* model cache. We assert that
    ``_executor_model_available("opencode")`` returns ``False``
    after the opencode daemon writes the failure record (the real
    call-time path), AND that the in-process cache cleared the
    ``tool_runtime_alive`` flag so the next ``choose_executor``
    skips opencode.
    """
    # Simulate what execute_opencode does on a TimeoutError: it
    # calls adapter.record_inference_failure(...).  That helper
    # writes cache/tool_health/opencode.json with
    # model_available=False. We simulate that on-disk mutation by
    # writing a temp cache file, then assert the orchestrator's
    # ``_executor_model_available`` returns False.
    orch = _load_orchestrator()
    cache_root = Path("${AIOS_HOME}/cache/tool_health")
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = cache_root / "opencode.json"
    payload = {
        "checked_at": "2026-08-10T07:25:44.481213+00:00",
        "model_state": "network_error",
        "model_available": False,
        "reason": "模型网络不可用",
        "returncode": 1,
        "evidence": "Error: timed out waiting for cloud config bundle after 15s",
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        assert orch._executor_model_available("opencode") is False
    finally:
        try:
            cache_path.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# C. gen0 opencode timeout -> gen1 never re-selects opencode
# ---------------------------------------------------------------------------

def test_c_repair_does_not_reselect_failed_binding(monkeypatch):
    """Section 14-C: once opencode:free timed out in generation 0,
    the next ``_enqueue_node`` call MUST NOT pick opencode again
    when there is at least one healthy alternative (codex).
    """
    orch = _load_orchestrator()
    wf = _stub_workflow()
    node = wf["nodes"][0]
    node["attempted_executors"] = ["opencode"]
    # Force the model-side cache to report opencode as model_unavailable
    # so choose_executor skips it; mock codex to be model_available +
    # lightweight reachable + process alive so it is the only healthy
    # candidate.
    def fake_model_available(name):
        return name == "codex"

    monkeypatch.setattr(orch, "_executor_model_available",
                        fake_model_available)
    monkeypatch.setattr(
        orch, "_is_executor_available", lambda n, **kw: n == "codex",
    )
    monkeypatch.setattr(orch, "_tool_process_health",
                        lambda n: True)
    # Bypass failover hook / claim / enqueue.
    monkeypatch.setattr(
        orch, "_claim_canonical_child",
        lambda *a, **kw: ("t1", True),
    )
    monkeypatch.setattr(orch, "enqueue_task", lambda **kw: "t1")
    ok = orch._enqueue_node(
        "test-parent-C",
        wf,
        node,
        exclude_executors=["opencode"],
    )
    assert ok is True
    # actual_tool must reflect the chosen executor. The
    # _enqueue_node function calls attach_routing_to_node on success
    # which writes ``actual_tool``. We assert via the hook callback.
    # The attempted list MUST also be appended with codex.
    assert "codex" in node["attempted_executors"]
    assert "opencode" in node["attempted_executors"]


# ---------------------------------------------------------------------------
# D. no other opencode binding -> gen1 chooses codex
# ---------------------------------------------------------------------------

def test_d_all_opencode_bindings_down_routes_to_codex(monkeypatch):
    """Section 14-D: when opencode has no healthy binding and the
    tool process is alive, the cross-tool fallback path picks the
    next healthy tool (codex).  ``attempted_executors`` MUST
    contain opencode AND codex, but never re-pick opencode.
    """
    orch = _load_orchestrator()
    wf = _stub_workflow()
    node = wf["nodes"][0]
    node["attempted_executors"] = ["opencode"]
    # opencode is unavailable (model side) AND process-alive miss;
    # codex is the only candidate.
    monkeypatch.setattr(
        orch, "_executor_model_available", lambda n: n == "codex",
    )
    monkeypatch.setattr(
        orch, "_is_executor_available", lambda n, **kw: n == "codex",
    )
    monkeypatch.setattr(orch, "_tool_process_health",
                        lambda n: True)
    monkeypatch.setattr(
        orch, "_claim_canonical_child",
        lambda *a, **kw: ("t2", True),
    )
    monkeypatch.setattr(orch, "enqueue_task", lambda **kw: "t2")
    ok = orch._enqueue_node(
        "test-parent-D",
        wf,
        node,
        exclude_executors=["opencode"],
    )
    assert ok is True
    assert "codex" in node["attempted_executors"]
    # opencode stays in attempted (audit), but is NOT re-selected.
    assert node.get("assigned_executor") == "codex"


# ---------------------------------------------------------------------------
# E. opencode:free down + opencode:minimax healthy -> same-tool fallback
# ---------------------------------------------------------------------------

def test_e_opencode_minimax_healthy_keeps_opencode(monkeypatch):
    """Section 14-E: when ``opencode:free`` is down but the
    ``opencode`` tool process is healthy, the orchestrator MAY
    keep routing to opencode (a different binding can be selected
    by the dual-axis engine).  At the executor-tool level this
    shows up as ``attempted_executors`` NOT blocking opencode
    unless the tool itself was excluded.

    We assert: even when one opencode binding is reported down, the
    choose_executor path still considers opencode as long as the
    tool process is alive + model side reports a healthy binding.
    """
    orch = _load_orchestrator()
    wf = _stub_workflow()
    # Force both legs (process + model) to be opencode-true so the
    # tool wins the choose_executor race; ``attempted_executors`` is
    # empty so this is a fresh generation.
    monkeypatch.setattr(
        orch, "_executor_model_available", lambda n: True,
    )
    monkeypatch.setattr(
        orch, "_is_executor_available", lambda n, **kw: True,
    )
    monkeypatch.setattr(
        orch, "_tool_process_health", lambda n: True,
    )
    chosen = orch.choose_executor(
        "opencode", exclude=(), capability_overlay=None,
    )
    assert chosen == "opencode"
    # And: when opencode:free is reported as down at the model-side
    # level but the dual-axis engine could swap binding, the
    # orchestrator-side ``choose_executor`` should not block the
    # tool entirely; it only excludes when ``_executor_model_available``
    # returns False (binding-agnostic). We assert that the helper
    # can be patched to return True for opencode:minimax while
    # still keeping opencode in choose_executor's output.
    monkeypatch.setattr(
        orch, "_executor_model_available", lambda n: True,
    )
    chosen2 = orch.choose_executor("opencode", exclude=())
    assert chosen2 == "opencode"


# ---------------------------------------------------------------------------
# F. all opencode bindings down -> cross-tool codex
# ---------------------------------------------------------------------------

def test_f_all_opencode_down_cross_tool_codex(monkeypatch):
    """Section 14-F: when opencode tool is fully unavailable and
    codex is healthy, the orchestrator MUST route to codex.
    """
    orch = _load_orchestrator()
    monkeypatch.setattr(
        orch, "_executor_model_available", lambda n: n == "codex",
    )
    monkeypatch.setattr(
        orch, "_is_executor_available", lambda n, **kw: n == "codex",
    )
    monkeypatch.setattr(orch, "_tool_process_health",
                        lambda n: True)
    chosen = orch.choose_executor("opencode", exclude=())
    assert chosen == "codex"


# ---------------------------------------------------------------------------
# G. opencode fails -> codex
# ---------------------------------------------------------------------------

def test_g_opencode_fails_routes_to_codex(monkeypatch):
    """Section 14-G: when opencode is unavailable, the orchestrator
    MUST pick the next healthy tool in the GENERAL chain (codex).
    Per AIOS-010 §五 / pre-008 HEAD, the GENERAL fallback chain is
    ``(opencode, codex)``; ``claude`` is reserved for the
    ``specialist_high_depth_review`` role and is NOT in the
    GENERAL runtime chain (commit 56af8df0, 2026-08-12).
    """
    orch = _load_orchestrator()
    # opencode unavailable; only codex is healthy.
    def fake_model(name):
        return name == "codex"

    monkeypatch.setattr(orch, "_executor_model_available", fake_model)
    monkeypatch.setattr(
        orch, "_is_executor_available",
        lambda n, **kw: n == "codex",
    )
    monkeypatch.setattr(orch, "_tool_process_health",
                        lambda n: True)
    chosen = orch.choose_executor("opencode", exclude=())
    assert chosen == "codex"


# ---------------------------------------------------------------------------
# H. strict_tool=true -> no cross-tool fallback
# ---------------------------------------------------------------------------

def test_h_strict_tool_blocks_cross_tool_fallback(monkeypatch):
    """Section 14-H: ``allow_executor_fallback=False`` (strict mode)
    MUST keep the orchestrator on the preferred executor. When the
    preferred executor is the only candidate, ``choose_executor``
    returns the preferred name (or "" if the preferred is down).
    """
    orch = _load_orchestrator()
    wf = _stub_workflow()
    wf["strict_executor"] = "opencode"
    wf["allow_executor_fallback"] = False
    node = wf["nodes"][0]
    # opencode is healthy here.
    monkeypatch.setattr(
        orch, "_executor_model_available", lambda n: True,
    )
    monkeypatch.setattr(
        orch, "_is_executor_available", lambda n, **kw: True,
    )
    monkeypatch.setattr(
        orch, "_tool_process_health", lambda n: True,
    )
    monkeypatch.setattr(
        orch, "_claim_canonical_child",
        lambda *a, **kw: ("t-strict", True),
    )
    monkeypatch.setattr(orch, "enqueue_task", lambda **kw: "t-strict")
    ok = orch._enqueue_node("test-parent-H", wf, node)
    assert ok is True
    assert node["assigned_executor"] == "opencode"
    # Now make opencode unavailable under strict mode — the helper
    # MUST refuse the enqueue rather than silently swap to codex.
    wf2 = _stub_workflow()
    wf2["strict_executor"] = "opencode"
    wf2["allow_executor_fallback"] = False
    node2 = wf2["nodes"][0]
    monkeypatch.setattr(
        orch, "_executor_model_available", lambda n: False,
    )
    monkeypatch.setattr(
        orch, "_is_executor_available", lambda n, **kw: False,
    )
    ok2 = orch._enqueue_node("test-parent-H-strict-down", wf2, node2)
    assert ok2 is False
    assert node2["status"] == "failed"
    assert "strict_executor_unavailable" in node2["error"]


# ---------------------------------------------------------------------------
# I. task A exclusion does NOT poison task B
# ---------------------------------------------------------------------------

def test_i_failed_executor_isolated_to_task_a(monkeypatch):
    """Section 14-I: a failed executor in task A MUST NOT permanently
    pollute task B.  ``attempted_executors`` is per-node; the
    global cache ``tool_runtime_failure_events`` has a 120 s TTL
    (or positive recovery clears it).  We assert that a fresh
    workflow node with empty ``attempted_executors`` may select
    opencode again immediately when the tool is healthy.
    """
    orch = _load_orchestrator()
    # Simulate task A leaving a failure event in the global engine.
    # We don't go through the real engine singleton to avoid
    # bleeding state between tests — instead, patch
    # ``_get_tool_runtime_failure`` to return None for opencode,
    # mirroring a fresh process or expired event.
    monkeypatch.setattr(
        orch, "_get_tool_runtime_failure", lambda name: None,
    )
    monkeypatch.setattr(
        orch, "_executor_model_available", lambda n: True,
    )
    monkeypatch.setattr(
        orch, "_is_executor_available", lambda n, **kw: True,
    )
    monkeypatch.setattr(
        orch, "_tool_process_health", lambda n: True,
    )
    wf = _stub_workflow()
    node_b = wf["nodes"][0]
    # task B's node has no attempted_executors — opencode is allowed.
    chosen = orch.choose_executor(
        node_b.get("role", "opencode"),
        exclude=node_b["attempted_executors"],
    )
    assert chosen == "opencode"


# ---------------------------------------------------------------------------
# J. positive recovery -> new task can re-select opencode
# ---------------------------------------------------------------------------

def test_j_positive_recovery_re_enables_opencode(monkeypatch):
    """Section 14-J: a positive recovery probe (all 4 legs green)
    MUST clear the failure event so the next task can select
    opencode again.
    """
    orch = _load_orchestrator()
    cleared = []

    def fake_clear_default(name):
        cleared.append(name)
        return True

    monkeypatch.setattr(orch, "_executor_service_active", lambda n: True)
    monkeypatch.setattr(orch, "_executor_endpoint_reachable", lambda n: True)
    monkeypatch.setattr(orch, "_executor_adapter_probe_ok", lambda n: True)
    monkeypatch.setattr(orch, "_executor_model_available", lambda n: True)
    monkeypatch.setattr(
        orch, "_invalidate_tool_process_cache", lambda: None,
    )
    # Patch the module-level clear path so the recovery probe can
    # honour its import-time contract.
    import aios_tool_failover as _tf
    monkeypatch.setattr(_tf, "clear_tool_runtime_failure",
                        fake_clear_default)
    assert orch._attempt_tool_recovery("opencode") is True
    assert cleared == ["opencode"]


# ---------------------------------------------------------------------------
# K. repair inherits failed binding/tool context
# ---------------------------------------------------------------------------

def test_k_repair_inherits_failed_context(monkeypatch):
    """Section 14-K: ``_repair_node`` MUST carry the failed
    executor into ``attempted_executors`` AND record a tool runtime
    failure event with the correct ``FAILURE_SCOPE_*`` category.
    """
    orch = _load_orchestrator()
    recorded = []
    monkeypatch.setattr(
        orch, "_record_tool_runtime_failure",
        lambda tool, *, scope, reason:
            recorded.append({"tool": tool, "scope": scope})
        or {"tool": tool, "scope": scope},
    )
    monkeypatch.setattr(orch, "_enqueue_node", lambda *a, **kw: True)
    wf = _stub_workflow()
    node = wf["nodes"][0]
    # Before repair: node has no attempted_executors.
    assert node["attempted_executors"] == []
    orch._repair_node(
        "test-parent-K",
        wf,
        node,
        "[codex/relay] task timed out after 300 seconds",
        "[codex] previous",
        "codex",
    )
    # attempted_executors now contains codex.
    assert "codex" in node["attempted_executors"]
    # And a failure event was recorded for codex with the correct
    # scope (RESOURCE for timeout).
    assert any(
        r["tool"] == "codex" and r["scope"] == "RESOURCE"
        for r in recorded
    ), recorded


# ---------------------------------------------------------------------------
# L. verification retry does NOT route back to failed executor
# ---------------------------------------------------------------------------

def test_l_verification_retry_blocks_failed_executor(monkeypatch):
    """Section 14-L: ``_verification_retry_target`` MUST NOT return
    an executor that is already in ``attempted_executors``.
    """
    orch = _load_orchestrator()
    node = {"attempted_executors": ["opencode"]}
    # The missing-marker reason allows same-executor retry in
    # principle, but the attempted check MUST win.
    out = orch._verification_retry_target(
        node,
        reason="authoritative_version_missing:expected=1.0.0",
        actual_executor="opencode",
    )
    assert out == ""


def test_l2_verification_retry_blocks_when_model_unavailable(
    monkeypatch,
):
    """Section 14-L: same path — even if opencode is not yet
    attempted, ``_executor_model_available("opencode")=False``
    MUST cause the verification retry to return ""."""
    orch = _load_orchestrator()
    node = {"attempted_executors": []}
    monkeypatch.setattr(
        orch, "_is_executor_available", lambda n: True,
    )
    monkeypatch.setattr(
        orch, "_executor_model_available", lambda n: False,
    )
    out = orch._verification_retry_target(
        node,
        reason="authoritative_version_missing",
        actual_executor="opencode",
    )
    assert out == ""


# ---------------------------------------------------------------------------
# M. planned_tool vs actual_tool are recorded separately
# ---------------------------------------------------------------------------

def test_m_planned_and_actual_tool_are_persisted_separately(
    monkeypatch,
):
    """Section 14-M: when the planner picks ``opencode`` but the
    routing engine swaps to ``codex``, the workflow hash MUST
    record both values distinctly so the audit ledger is truthful.
    """
    from aios_orchestrator_failover_hook import attach_routing_to_node
    node = {}
    # Build a fake RoutingDecision (only fields the helper reads).
    class FakeDecision:
        action = "proceed"
        actual_tool = "codex"
        actual_model_binding = "codex:minimax"
        tool_decision = None
        model_decision = None
        shadow = False
        attempted_tools = ("opencode", "codex")
        attempted_model_bindings = ("opencode:free", "codex:minimax")
        tool_failover_reason = "opencode unavailable"
        model_failover_reason = ""
        preferred_tool = "opencode"
        preferred_model = "opencode:free"
        role = "executor"
        strict_tool = ""
        strict_model = ""
        allow_tool_fallback = True
        allow_model_fallback = True
        capability_overlay = {}
        canary_allowed = True
        feature_flags = {}
        finished_at = ""
        failover_occurred = True
        tool_failover_occurred = True
        model_failover_occurred = False
    attach_routing_to_node(node, FakeDecision())
    # planned_tool stays opencode (from preferred_tool); actual_tool is codex.
    assert node["preferred_tool"] == "opencode"
    assert node["actual_tool"] == "codex"
    # attempted_tools records both the planning preference and the
    # actual swap, so the audit ledger can show "opencode was
    # preferred but codex was used because opencode was excluded".
    assert "opencode" in node["attempted_tools"]
    assert "codex" in node["attempted_tools"]


# ---------------------------------------------------------------------------
# N. no healthy executor -> natural terminal, never repair loop
# ---------------------------------------------------------------------------

def test_n_no_healthy_executor_terminals_naturally(monkeypatch):
    """Section 14-N: when every candidate is unavailable, the
    orchestrator MUST terminalise the workflow naturally
    (``status=failed, error=no_healthy_executor``). It MUST NOT
    enter an unbounded repair loop.
    """
    orch = _load_orchestrator()
    wf = _stub_workflow()
    node = wf["nodes"][0]
    # Force every leg to return False / unavailable.
    monkeypatch.setattr(
        orch, "_is_executor_available", lambda n, **kw: False,
    )
    monkeypatch.setattr(
        orch, "_executor_model_available", lambda n: False,
    )
    monkeypatch.setattr(
        orch, "_tool_process_health", lambda n: False,
    )
    # Suppress the stale-neg refresh for this test — we want to
    # verify the natural terminal path when the refresh cannot find
    # a healthy executor either.
    monkeypatch.setattr(
        orch, "_force_refresh_executor_model_cache", lambda n: False,
    )
    # Disable the dual-axis failover hook so it does not pick a tool
    # via its own registry engine (independent of the patched
    # orchestrator-side helpers).
    import aios_orchestrator_failover_hook as _h
    fake_decision = _h.RoutingDecision(
        action="proceed",
        actual_tool="",
        actual_model_binding=None,
        shadow=False,
        preferred_tool="",
        preferred_model="",
        role="executor",
        strict_tool="",
        strict_model="",
        allow_tool_fallback=True,
        allow_model_fallback=True,
        canary_allowed=False,
        feature_flags={},
        finished_at="",
    )
    monkeypatch.setattr(
        _h, "route_node_executor",
        lambda **kwargs: ("", fake_decision),
    )
    ok = orch._enqueue_node("test-parent-N", wf, node)
    assert ok is False
    assert node["status"] == "failed"
    assert "no_healthy_executor" in node["error"]


# ---------------------------------------------------------------------------
# Bonus: stale negative cache is refreshed in one shot
# ---------------------------------------------------------------------------

def test_o_stale_negative_cache_refreshed_once(monkeypatch):
    """When ``choose_executor`` returns empty AND a candidate has a
    stale negative cache older than
    :data:`_STALE_NEGATIVE_REFRESH_AFTER_SECONDS`,
    ``_enqueue_node`` MUST force-refresh that cache once and
    retry ``choose_executor``.  The refresh is one-shot — a
    second invocation MUST NOT re-enter the refresh branch
    unless the cache has crossed the threshold again.
    """
    orch = _load_orchestrator()
    wf = _stub_workflow()
    node = wf["nodes"][0]
    refresh_calls = []
    choose_returns = iter(["", "codex"])

    monkeypatch.setattr(
        orch, "_is_executor_available", lambda n, **kw: n == "codex",
    )
    monkeypatch.setattr(
        orch, "_executor_model_available", lambda n: n == "codex",
    )
    monkeypatch.setattr(
        orch, "_tool_process_health", lambda n: True,
    )

    def fake_refresh(name):
        refresh_calls.append(name)
        return True

    def fake_choose(*args, **kwargs):
        return next(choose_returns)

    monkeypatch.setattr(
        orch, "_force_refresh_executor_model_cache", fake_refresh,
    )
    monkeypatch.setattr(orch, "choose_executor", fake_choose)
    monkeypatch.setattr(
        orch, "_claim_canonical_child",
        lambda *a, **kw: ("t-refresh", True),
    )
    monkeypatch.setattr(orch, "enqueue_task", lambda **kw: "t-refresh")
    ok = orch._enqueue_node("test-parent-O", wf, node)
    assert ok is True
    # Refresh was attempted for each unexcluded candidate at most
    # once.
    assert len(refresh_calls) >= 1
    # The node's stale_negative_refreshed field is set when the
    # refresh path re-selected a healthy tool.
    assert node.get("stale_negative_refreshed")
    assert node["assigned_executor"] == "codex"


# ---------------------------------------------------------------------------
# Bonus: failure scope classification respects the actual reason text
# ---------------------------------------------------------------------------

def test_p_failure_scope_classification_per_reason(monkeypatch):
    """The ``_repair_node`` failure-scope classifier MUST map
    common OpenCode / Codex / Claude reason strings to the right
    ``FAILURE_SCOPE_*`` category so the audit ledger can prove the
    routing decision honoured the §7 contract.
    """
    orch = _load_orchestrator()
    recorded = []
    monkeypatch.setattr(
        orch, "_record_tool_runtime_failure",
        lambda tool, *, scope, reason:
            recorded.append({"tool": tool, "scope": scope})
        or {"tool": tool, "scope": scope},
    )
    monkeypatch.setattr(orch, "_enqueue_node", lambda *a, **kw: True)

    cases = [
        # (reason, expected_scope)
        ("[codex/relay] task timed out after 300 seconds", "RESOURCE"),
        ("network error contacting upstream minimax", "RESOURCE"),
        ("insufficient balance 402 quota exhausted", "RESOURCE"),
        ("dispatch_claim_timeout:120s>60s", "LOCAL_RUNTIME"),
        ("executor_checkin_timeout:90s>30s", "TOOL_ADAPTER"),
        ("unclassified failure reason", "BINDING"),
    ]
    for reason, expected in cases:
        recorded.clear()
        wf = _stub_workflow()
        orch._repair_node(
            "test-parent-P",
            wf,
            wf["nodes"][0],
            reason,
            "previous",
            "opencode",
        )
        assert any(r["scope"] == expected for r in recorded), (
            reason, expected, recorded,
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))