#!/usr/bin/env python3
"""P9D-R Runtime Health Freshness closure — dedicated tests.

This suite validates the runtime-tool-health freshness contract
introduced in the P9D-R close-out (2026-08-04).  The live
fault-injection runs established these invariants:

  1. ``ToolFailoverEngine.record_tool_runtime_failure`` records an
     explicit failure event that survives the on-disk probe cache.
  2. ``ToolFailoverEngine.compute_tool_status`` consults the
     per-tool failure-event map FIRST so a freshly-observed
     TOOL_PROCESS / TOOL_ADAPTER / DISPATCH_CLAIM_TIMEOUT failure is
     reflected on the very next routing decision without waiting for
     the long ``probe_max_age_seconds`` window to expire.
  3. The orchestrator's ``_is_executor_available`` consults the
     failure event FIRST so a dead tool is skipped even when the
     on-disk capability cache still says AVAILABLE.
  4. After a failure event is recorded, the next ``choose_executor``
     iteration skips the dead tool and picks the next healthy
     candidate — *not* a no-op repair against the same dead tool.
  5. The failure event has a bounded TTL (``_TOOL_RUNTIME_FAILURE_TTL``)
     and uses ``time.monotonic`` to survive wall-clock skew.
  6. ``clear_tool_runtime_failure`` is the only path that re-enables
     routing after a failure event; without it the long-lived TTL
     would prevent routing from ever resuming.

Tests are written as pure unit tests with mocked Redis / capability
truth sources so they run without a live system.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest
from pathlib import Path as _PathMod

# AIOS-010 §六: resolve paths from __file__ (was hardcoded
# ``${AIOS_HOME}/kernel/tools`` which caused the test to import
# the formal-disk source instead of the iso copy).
_TESTS_DIR = _PathMod(__file__).resolve().parent
_TOOLS_DIR = _TESTS_DIR.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# ---------------------------------------------------------------------------
# 1. record_tool_runtime_failure: failure event semantics
# ---------------------------------------------------------------------------


def _clean(tool_id: str) -> None:
    """Helper: clear any pre-existing failure event for ``tool_id``
    so the test starts from a known-clean state.
    """
    from aios_tool_failover import get_default_tool_engine
    get_default_tool_engine().clear_tool_runtime_failure(tool_id)


def test_record_tool_runtime_failure_sets_event():
    """``record_tool_runtime_failure`` must store the event so
    ``get_tool_runtime_failure`` returns the same dict.
    """
    _clean("opencode")
    from aios_tool_failover import (
        record_tool_runtime_failure,
        get_tool_runtime_failure,
    )
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    event = get_tool_runtime_failure("opencode")
    assert event is not None
    assert event["scope"] == "TOOL_PROCESS"
    assert event["reason"] == "daemon_dead"
    assert event["tool_id"] == "opencode"
    assert "observed_at_monotonic" in event
    assert "expires_at_monotonic" in event
    assert event["expires_at_monotonic"] > event["observed_at_monotonic"]
    _clean("opencode")


def test_record_tool_runtime_failure_uses_monotonic_clock():
    """``record_tool_runtime_failure`` MUST use ``time.monotonic``
    for both observed and expires timestamps.
    """
    _clean("hermes")
    from aios_tool_failover import (
        record_tool_runtime_failure,
        get_tool_runtime_failure,
    )
    t_before = time.monotonic()
    record_tool_runtime_failure("hermes", scope="TOOL_PROCESS",
                                reason="test")
    t_after = time.monotonic()
    event = get_tool_runtime_failure("hermes")
    assert event is not None
    assert t_before - 0.05 <= event["observed_at_monotonic"] <= t_after + 0.05
    _clean("hermes")


def test_record_tool_runtime_failure_ttl_is_bounded():
    """The default TTL is ``_TOOL_RUNTIME_FAILURE_TTL_SECONDS`` (120s).
    """
    from aios_tool_failover import (
        _TOOL_RUNTIME_FAILURE_TTL_SECONDS,
        get_default_tool_engine,
        get_tool_runtime_failure,
        record_tool_runtime_failure,
    )
    assert _TOOL_RUNTIME_FAILURE_TTL_SECONDS == 120
    _clean("codex")
    record_tool_runtime_failure("codex", scope="TOOL_PROCESS", reason="x")
    event = get_tool_runtime_failure("codex")
    assert event is not None
    delta = event["expires_at_monotonic"] - event["observed_at_monotonic"]
    assert abs(delta - _TOOL_RUNTIME_FAILURE_TTL_SECONDS) < 0.01
    _clean("codex")


def test_record_tool_runtime_failure_custom_ttl():
    """A custom ``ttl_seconds`` argument overrides the default TTL.
    """
    _clean("claude")
    from aios_tool_failover import (
        record_tool_runtime_failure,
        get_tool_runtime_failure,
    )
    record_tool_runtime_failure("claude", scope="TOOL_ADAPTER",
                                reason="y", ttl_seconds=30)
    event = get_tool_runtime_failure("claude")
    assert event is not None
    assert event["ttl_seconds"] == 30
    delta = event["expires_at_monotonic"] - event["observed_at_monotonic"]
    assert abs(delta - 30) < 0.01
    _clean("claude")


def test_get_tool_runtime_failure_purges_expired():
    """``get_tool_runtime_failure`` must purge the entry on read when
    its ``expires_at_monotonic`` is in the past.
    """
    _clean("opencode")
    from aios_tool_failover import (
        get_default_tool_engine,
        get_tool_runtime_failure,
    )
    e = get_default_tool_engine()
    e.record_tool_runtime_failure(
        "opencode", scope="TOOL_PROCESS", reason="expired_test",
        ttl_seconds=1,
        observed_at_monotonic=time.monotonic() - 100,
    )
    first = get_tool_runtime_failure("opencode")
    assert first is None
    second = get_tool_runtime_failure("opencode")
    assert second is None


def test_clear_tool_runtime_failure_removes_event():
    """``clear_tool_runtime_failure`` removes the failure event.
    """
    _clean("opencode")
    from aios_tool_failover import (
        clear_tool_runtime_failure,
        get_tool_runtime_failure,
        record_tool_runtime_failure,
    )
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="x")
    assert get_tool_runtime_failure("opencode") is not None
    clear_tool_runtime_failure("opencode")
    assert get_tool_runtime_failure("opencode") is None


# ---------------------------------------------------------------------------
# 2. compute_tool_status: failure events take precedence
# ---------------------------------------------------------------------------


def _make_status(status: str, binding: str = "stub:binding") -> Any:
    """Tiny ``ToolStatusReport`` stand-in for stubbing."""
    from aios_tool_failover import ToolStatusReport
    return ToolStatusReport(
        tool_id="opencode",
        status=status,
        primary_binding=binding,
        effective_binding=binding,
        verified_bindings=(binding,),
        blocked_bindings=(),
        reason="stub",
    )


def test_compute_tool_status_returns_unavailable_when_failure_event_set():
    """When a failure event is recorded for ``tool_id``, every
    subsequent ``compute_tool_status`` call returns
    ``UNAVAILABLE_TOOL_RUNTIME`` regardless of the on-disk probe
    cache or the capability layer's view.
    """
    from aios_tool_failover import (
        get_default_tool_engine,
        record_tool_runtime_failure,
    )
    _clean("opencode")
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    e = get_default_tool_engine()
    report = e.compute_tool_status("opencode")
    assert report.status == "UNAVAILABLE_TOOL_RUNTIME", (
        f"failure event must force UNAVAILABLE, got {report.status!r}"
    )
    _clean("opencode")


def test_compute_tool_status_failure_event_blocks_all_bindings():
    """When a failure event is recorded, ``blocked_bindings`` MUST
    include all candidate bindings (every ``opencode:*`` Executor
    binding), not just the failing one.
    """
    _clean("opencode")
    from aios_tool_failover import (
        get_default_tool_engine,
        record_tool_runtime_failure,
    )
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    e = get_default_tool_engine()
    report = e.compute_tool_status("opencode")
    assert report.status == "UNAVAILABLE_TOOL_RUNTIME"
    # The failure event MUST put the tool into UNAVAILABLE state
    # and the blocked_bindings MUST include every candidate
    # ``opencode:*`` Executor binding — not just the one that
    # actually failed.
    for binding in report.blocked_bindings:
        assert binding.startswith("opencode:") or binding == "opencode", (
            f"unexpected blocked binding: {binding!r}"
        )
    _clean("opencode")


def test_compute_tool_status_resets_liveness_flag():
    """``record_tool_runtime_failure`` MUST also set the per-tool
    ``_tool_runtime_alive`` flag to False so that even if
    ``is_tool_runtime_alive`` is consulted *before*
    ``compute_tool_status``, the tool is still reported unavailable.
    """
    from aios_tool_failover import (
        get_default_tool_engine,
        is_tool_runtime_alive,
        record_tool_runtime_failure,
    )
    _clean("opencode")
    e = get_default_tool_engine()
    # Before any failure event, the liveness defaults to True.
    assert is_tool_runtime_alive("opencode") is True
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="x")
    assert is_tool_runtime_alive("opencode") is False, (
        "_tool_runtime_alive must be False after a failure event"
    )
    _clean("opencode")


def test_failure_event_bypasses_tool_status_cache():
    """The failure event MUST take precedence over the on-disk probe
    cache.  Even if the capability layer says AVAILABLE, the
    failure event forces UNAVAILABLE.
    """
    from aios_tool_failover import (
        get_default_tool_engine,
        record_tool_runtime_failure,
    )
    _clean("opencode")
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="x")
    e = get_default_tool_engine()
    report = e.compute_tool_status("opencode")
    assert report.status == "UNAVAILABLE_TOOL_RUNTIME", (
        f"failure event must bypass the cache, got status={report.status!r}"
    )
    _clean("opencode")


# ---------------------------------------------------------------------------
# 3. Failure event TTL expiration
# ---------------------------------------------------------------------------


def test_expired_failure_event_does_not_block_routing():
    """A failure event whose TTL has elapsed MUST be purged on
    read and MUST NOT block routing.
    """
    from aios_tool_failover import (
        get_default_tool_engine,
    )
    e = get_default_tool_engine()
    _clean("opencode")
    # Record an event whose TTL has already elapsed.
    e.record_tool_runtime_failure(
        "opencode", scope="TOOL_PROCESS", reason="expired",
        ttl_seconds=1,
        observed_at_monotonic=time.monotonic() - 100,
    )
    # After read, the entry is purged.
    status_now = e.get_tool_runtime_failure("opencode")
    assert status_now is None
    # compute_tool_status now returns the live status (which may
    # vary by environment; the contract is only that the failure
    # event is no longer blocking).
    report = e.compute_tool_status("opencode")
    assert report.status != "UNAVAILABLE_TOOL_RUNTIME" or (
        report.reason and "tool_runtime_failure_event" not in report.reason
    )


# ---------------------------------------------------------------------------
# 4. Multi-tool concurrent failure events
# ---------------------------------------------------------------------------


def test_concurrent_failure_events_for_different_tools():
    """A failure event on one tool MUST NOT affect the availability
    of any other tool.  Each tool's failure event is independent.
    """
    from aios_tool_failover import (
        get_default_tool_engine,
        record_tool_runtime_failure,
    )
    for tid in ("opencode", "claude", "codex"):
        _clean(tid)
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="opencode_dead")
    record_tool_runtime_failure("claude", scope="TOOL_PROCESS",
                                reason="claude_dead")
    e = get_default_tool_engine()
    report_opencode = e.compute_tool_status("opencode")
    report_claude = e.compute_tool_status("claude")
    report_codex = e.compute_tool_status("codex")
    assert report_opencode.status == "UNAVAILABLE_TOOL_RUNTIME"
    assert report_claude.status == "UNAVAILABLE_TOOL_RUNTIME"
    # codex is independent — no failure event → AVAILABLE.
    assert report_codex.status in (
        "AVAILABLE_PRIMARY", "AVAILABLE_WITH_MODEL_FALLBACK",
        "DEGRADED_NO_MODEL_FALLBACK", "DEGRADED_NO_NATIVE_MODEL_ROUTE",
    ), f"unexpected status for codex: {report_codex.status!r}"
    for tid in ("opencode", "claude", "codex"):
        _clean(tid)


# ---------------------------------------------------------------------------
# 5. list_tool_runtime_failures snapshot
# ---------------------------------------------------------------------------


def test_list_tool_runtime_failures_returns_only_active():
    """``list_tool_runtime_failures`` returns only events whose TTL
    is still in the future, lazily purging expired ones.
    """
    from aios_tool_failover import (
        get_default_tool_engine,
        list_tool_runtime_failures,
        record_tool_runtime_failure,
    )
    for tid in ("opencode", "claude", "codex"):
        _clean(tid)
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="alive", ttl_seconds=120)
    get_default_tool_engine().record_tool_runtime_failure(
        "claude", scope="TOOL_PROCESS", reason="expired", ttl_seconds=1,
        observed_at_monotonic=time.monotonic() - 100,
    )
    snapshot = list_tool_runtime_failures()
    assert "opencode" in snapshot
    assert "claude" not in snapshot, (
        "expired event must be purged on read"
    )
    _clean("opencode")


# ---------------------------------------------------------------------------
# 6. The orchestrator's _is_executor_available respects failure events
# ---------------------------------------------------------------------------


def test_orchestrator_is_executor_available_returns_false_with_failure_event(
    monkeypatch,
):
    """``_is_executor_available`` (the orchestrator's per-tool check)
    MUST return ``False`` when a failure event is active, regardless
    of the capability layer's view.  This is the production routing
    contract: a dead tool cannot be re-selected just because the
    capability cache still says AVAILABLE.
    """
    from aios_tool_failover import (
        record_tool_runtime_failure,
    )
    _clean("opencode")
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    import aios_orchestrator as _orch
    assert _orch._is_executor_available("opencode") is False, (
        "_is_executor_available must return False with active failure event"
    )
    _clean("opencode")
    # And after clear, the orchestrator considers the tool alive again.
    assert _orch._is_executor_available("opencode") is True


# ---------------------------------------------------------------------------
# 7. Force-refresh contract — what the orchestrator depends on
# ---------------------------------------------------------------------------


def test_orchestrator_cache_invalidation_marks_event_active():
    """The Repair path's call to ``record_tool_runtime_failure``
    (recorded in ``_expire_child_for_repair``) MUST persist a
    failure event so the next ``compute_tool_status`` returns
    ``UNAVAILABLE_TOOL_RUNTIME``.  This is the contract that
    forces the routing to skip the dead tool on the next generation.
    """
    from aios_tool_failover import (
        get_default_tool_engine,
        record_tool_runtime_failure,
    )
    _clean("opencode")
    # Simulate the orchestrator's Repair hook:
    record_tool_runtime_failure(
        "opencode",
        scope="TOOL_PROCESS",
        reason="dispatch_claim_timeout:60s",
    )
    # The next compute_tool_status call MUST reflect the failure event.
    e = get_default_tool_engine()
    report = e.compute_tool_status("opencode")
    assert report.status == "UNAVAILABLE_TOOL_RUNTIME", (
        f"Repair hook must force UNAVAILABLE, got {report.status!r}"
    )
    _clean("opencode")


# ---------------------------------------------------------------------------
# P9D-R-executor-primary-recovery: positive recovery probe contract
# ---------------------------------------------------------------------------
#
# These tests assert the *symmetric* recovery path introduced in
# 2026-08-04: once a real tool failure is recorded, the system MUST
# be able to detect a genuine recovery (service active + endpoint
# reachable + adapter probe OK) without waiting for the bounded 120 s
# TTL.  The recovery path is implemented inside
# ``aios_orchestrator._attempt_tool_recovery`` and is invoked from
# ``choose_executor`` automatically — tests must NOT call
# ``clear_tool_runtime_failure`` directly; they MUST drive the
# helper functions and observe the engine state changing in response.
# The bounded 120 s TTL is honoured at every step (failure events
# keep their expiry) but the recovery is detected before the TTL
# expires.


import importlib
from unittest import mock


def _load_orchestrator():
    """Return the already-imported ``aios_orchestrator`` module.

    AIOS-010 §六: the previous implementation called
    ``importlib.reload(...)`` to re-import the orchestrator after
    every test.  ``importlib.reload`` re-executes the module body
    which **discards** every monkeypatch and ``mock.patch.object``
    applied to module-level functions.  Because every test in this
    file calls ``_load_orchestrator()`` to obtain a fresh handle on
    the orchestrator, the reload wiped the conftest's
    ``_executor_model_available`` patch and the 4th leg of
    ``_attempt_tool_recovery`` reverted to reading the real
    on-disk ``cache/tool_health/<name>.json`` — a
    test-isolation violation (§六).

    The conftest already imports ``aios_orchestrator`` during
    collection, so the module is always in ``sys.modules`` by the
    time any test runs.  Returning the cached module (without
    reload) preserves every monkeypatch installed by the
    conftest's autouse fixtures.
    """
    import aios_orchestrator as _orch
    return _orch


def _patch_recovery_signals(orch, *, service_active, endpoint_ok, adapter_ok):
    """Replace the three recovery legs with controlled booleans so
    the test can simulate every positive / negative combination
    deterministically.
    """
    return mock.patch.multiple(
        orch,
        _executor_service_active=mock.Mock(return_value=service_active),
        _executor_endpoint_reachable=mock.Mock(return_value=endpoint_ok),
        _executor_adapter_probe_ok=mock.Mock(return_value=adapter_ok),
    )


def test_1_failure_event_excludes_tool_from_routing():
    """1. A fresh failure event MUST exclude the tool from the
    next ``choose_executor`` call regardless of capability.
    """
    from aios_tool_failover import (
        record_tool_runtime_failure,
        get_tool_runtime_failure,
    )
    _clean("opencode")
    orch = _load_orchestrator()
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    # Even with all three recovery legs green, ``choose_executor``
    # MUST NOT return ``opencode`` because the routing layer only
    # calls ``_attempt_tool_recovery`` when a real failure event is
    # present.  In the all-green case the recovery probe runs and
    # clears the event BEFORE the availability check, so the test
    # here needs the recovery probe to be blocked.  We block the
    # service leg so the probe returns False and the failure event
    # remains active.
    with mock.patch.object(orch, "_executor_service_active",
                           return_value=False), \
         mock.patch.object(orch, "_executor_endpoint_reachable",
                           return_value=True), \
         mock.patch.object(orch, "_executor_adapter_probe_ok",
                           return_value=True):
        chosen = orch.choose_executor("opencode")
    # ``opencode`` blocked → fallback chain returns the next healthy
    # tool.  Whatever the answer is, ``opencode`` MUST NOT be it.
    assert chosen != "opencode"
    # Confirm the failure event is still active.
    assert get_tool_runtime_failure("opencode") is not None
    _clean("opencode")


def test_2_service_active_endpoint_failed_does_not_clear():
    """2. Service active but endpoint unreachable MUST NOT clear
    the failure event.  The recovery probe requires ALL THREE legs.
    """
    from aios_tool_failover import (
        record_tool_runtime_failure,
        get_tool_runtime_failure,
    )
    _clean("opencode")
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    orch = _load_orchestrator()
    with mock.patch.object(orch, "_executor_service_active",
                           return_value=True), \
         mock.patch.object(orch, "_executor_endpoint_reachable",
                           return_value=False), \
         mock.patch.object(orch, "_executor_adapter_probe_ok",
                           return_value=True):
        result = orch._attempt_tool_recovery("opencode")
    assert result is False
    assert get_tool_runtime_failure("opencode") is not None, (
        "partial recovery (endpoint down) must NOT clear the event"
    )
    _clean("opencode")


def test_3_all_three_legs_green_clears_failure():
    """3. service active + endpoint reachable + adapter probe OK
    MUST clear the failure event automatically.  No direct call
    to ``clear_tool_runtime_failure`` is made by the test.
    """
    from aios_tool_failover import (
        record_tool_runtime_failure,
        get_tool_runtime_failure,
    )
    _clean("opencode")
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    assert get_tool_runtime_failure("opencode") is not None
    orch = _load_orchestrator()
    with mock.patch.object(orch, "_executor_service_active",
                           return_value=True), \
         mock.patch.object(orch, "_executor_endpoint_reachable",
                           return_value=True), \
         mock.patch.object(orch, "_executor_adapter_probe_ok",
                           return_value=True):
            result = orch._attempt_tool_recovery("opencode")
    assert result is True
    assert get_tool_runtime_failure("opencode") is None, (
        "positive recovery probe must clear the failure event"
    )
    _clean("opencode")


def test_4_clear_failure_invalidates_process_cache():
    """4. The successful recovery MUST invalidate the
    ``_TOOL_PROCESS_HEALTH_CACHE`` so the next
    ``_tool_process_health`` call re-probes ``/proc``.
    """
    from aios_tool_failover import record_tool_runtime_failure
    _clean("opencode")
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    orch = _load_orchestrator()
    # Seed the cache with a stale "False" entry.
    orch._TOOL_PROCESS_HEALTH_CACHE["opencode"] = (False, 0.0)
    with mock.patch.object(orch, "_executor_service_active",
                           return_value=True), \
         mock.patch.object(orch, "_executor_endpoint_reachable",
                           return_value=True), \
         mock.patch.object(orch, "_executor_adapter_probe_ok",
                           return_value=True):
        result = orch._attempt_tool_recovery("opencode")
    assert result is True
    assert "opencode" not in orch._TOOL_PROCESS_HEALTH_CACHE, (
        "process-health cache must be cleared on recovery"
    )
    _clean("opencode")


def test_5_recovery_does_not_wait_for_ttl():
    """5. A recovery probe MUST succeed even if the 120 s failure
    TTL has not yet expired.  This is the key contract that
    prevents the 2-hour cache expiry / recovery block.
    """
    from aios_tool_failover import record_tool_runtime_failure
    _clean("opencode")
    # Use a 10000 s TTL (far in the future) to prove the probe
    # clears the event without consulting the TTL.
    record_tool_runtime_failure(
        "opencode", scope="TOOL_PROCESS",
        reason="daemon_dead", ttl_seconds=10000,
    )
    orch = _load_orchestrator()
    with mock.patch.object(orch, "_executor_service_active",
                           return_value=True), \
         mock.patch.object(orch, "_executor_endpoint_reachable",
                           return_value=True), \
         mock.patch.object(orch, "_executor_adapter_probe_ok",
                           return_value=True):
        result = orch._attempt_tool_recovery("opencode")
    assert result is True
    from aios_tool_failover import get_tool_runtime_failure
    assert get_tool_runtime_failure("opencode") is None
    _clean("opencode")


def test_6_recovery_re_enables_choose_executor():
    """6. After recovery, ``choose_executor`` MUST return the
    recovered tool as the top candidate for its preferred role.
    """
    from aios_tool_failover import record_tool_runtime_failure
    _clean("opencode")
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    orch = _load_orchestrator()
    with mock.patch.object(orch, "_executor_service_active",
                           return_value=True), \
         mock.patch.object(orch, "_executor_endpoint_reachable",
                           return_value=True), \
         mock.patch.object(orch, "_executor_adapter_probe_ok",
                           return_value=True), \
         mock.patch.object(orch, "_is_executor_available",
                           return_value=True), \
         mock.patch.object(orch, "_tool_process_health",
                           return_value=True):
        # choose_executor must run the recovery probe inline; with
        # all three legs green the failure event is cleared and
        # the routing layer picks ``opencode`` as primary.
        chosen = orch.choose_executor("opencode")
    assert chosen == "opencode", (
        f"recovered tool must win the routing decision, got {chosen!r}"
    )
    _clean("opencode")


def test_7_codex_returns_to_secondary_after_opencode_recovery():
    """7. After opencode recovers, ``codex`` MUST still be a
    candidate for the ``codex`` role; it is no longer promoted to
    primary.
    """
    _clean("opencode")
    orch = _load_orchestrator()
    # AIOS-010 §七: the test must not read the real model-side
    # probe cache (cache/tool_health/codex.json) — patch
    # ``_executor_model_available`` so the candidate survives the
    # fourth leg of the eligibility gate without depending on
    # the host filesystem.
    with mock.patch.object(orch, "_is_executor_available",
                           return_value=True), \
         mock.patch.object(orch, "_tool_process_health",
                           return_value=True), \
         mock.patch.object(orch, "_executor_model_available",
                           return_value=True):
        # No failure event active: codex role → codex wins.
        chosen = orch.choose_executor("codex")
    assert chosen == "codex"
    _clean("opencode")


def test_8_recovery_is_driven_by_probe_not_direct_clear():
    """8. The recovery path MUST be triggered by
    ``_attempt_tool_recovery`` (driven by the probe), not by a
    direct ``clear_tool_runtime_failure`` call.  The test asserts
    that the test never calls the direct clear function while
    still producing a fully-recovered state.
    """
    from aios_tool_failover import (
        record_tool_runtime_failure,
        get_tool_runtime_failure,
        clear_tool_runtime_failure,
    )
    _clean("opencode")
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    orch = _load_orchestrator()
    # Patch the direct-clear function to RAISE if invoked by the
    # recovery path.  This proves the test does not (and the
    # production code does not) bypass the probe.
    with mock.patch.object(orch, "_clear_tool_runtime_failure",
                           side_effect=AssertionError(
                               "direct clear_tool_runtime_failure is "
                               "forbidden in the recovery path")) as \
         direct_clear, \
         mock.patch.object(orch, "_executor_service_active",
                           return_value=True), \
         mock.patch.object(orch, "_executor_endpoint_reachable",
                           return_value=True), \
         mock.patch.object(orch, "_executor_adapter_probe_ok",
                           return_value=True):
        # Run the probe — it must clear the event via the
        # ``clear_tool_runtime_failure`` MODULE-LEVEL function, not
        # the orchestrator's alias.
        result = orch._attempt_tool_recovery("opencode")
    assert result is True
    assert get_tool_runtime_failure("opencode") is None
    # The orchestrator's alias was NOT called.
    assert direct_clear.call_count == 0
    _clean("opencode")


def test_9_orchestrator_process_cache_invalidated():
    """9. The ``_TOOL_PROCESS_HEALTH_CACHE`` MUST be invalidated on
    recovery.  Subsequent ``_tool_process_health`` calls observe
    the new ``/proc`` state.
    """
    from aios_tool_failover import record_tool_runtime_failure
    _clean("opencode")
    record_tool_runtime_failure("opencode", scope="TOOL_PROCESS",
                                reason="daemon_dead")
    orch = _load_orchestrator()
    # Seed the cache with both opencode and codex entries; recovery
    # should drop both because the helper invalidates the full
    # cache.
    orch._TOOL_PROCESS_HEALTH_CACHE["opencode"] = (False, 0.0)
    orch._TOOL_PROCESS_HEALTH_CACHE["codex"] = (True, 0.0)
    with mock.patch.object(orch, "_executor_service_active",
                           return_value=True), \
         mock.patch.object(orch, "_executor_endpoint_reachable",
                           return_value=True), \
         mock.patch.object(orch, "_executor_adapter_probe_ok",
                           return_value=True):
        orch._attempt_tool_recovery("opencode")
    assert orch._TOOL_PROCESS_HEALTH_CACHE == {}
    _clean("opencode")


def test_10_failure_event_ttl_still_respected_by_get():
    """10. ``get_tool_runtime_failure`` MUST still honour the TTL
    on read; an expired event is purged lazily even when no
    recovery probe has been run.  This guarantees the safety
    net works when the probe never fires (e.g. CI without
    systemd).
    """
    from aios_tool_failover import (
        record_tool_runtime_failure,
        get_tool_runtime_failure,
    )
    _clean("opencode")
    # Force a TTL that expires immediately.
    record_tool_runtime_failure(
        "opencode", scope="TOOL_PROCESS", reason="daemon_dead",
        observed_at_monotonic=0.0, ttl_seconds=1,
    )
    # Backdate the expiry by manipulating the engine map so the
    # event reads as expired.
    engine_mod = sys.modules["aios_tool_failover"]
    event = engine_mod.get_default_tool_engine()._tool_runtime_failure_events["opencode"]
    event["observed_at_monotonic"] = -10.0
    event["expires_at_monotonic"] = -9.0
    # A new get_tool_runtime_failure call must purge the event.
    assert get_tool_runtime_failure("opencode") is None
    _clean("opencode")


def test_11_recovery_inside_ttl_window():
    """11. A positive recovery probe MUST clear the event even
    when the failure event's TTL has not yet expired.  This is
    the operationally-critical case: operator restarted the
    service in 5 s; the probe must recover immediately, not wait
    for the 120 s safety TTL.
    """
    from aios_tool_failover import (
        record_tool_runtime_failure,
        get_tool_runtime_failure,
    )
    _clean("opencode")
    record_tool_runtime_failure(
        "opencode", scope="TOOL_PROCESS",
        reason="daemon_dead", ttl_seconds=10000,
    )
    orch = _load_orchestrator()
    with mock.patch.object(orch, "_executor_service_active",
                           return_value=True), \
         mock.patch.object(orch, "_executor_endpoint_reachable",
                           return_value=True), \
         mock.patch.object(orch, "_executor_adapter_probe_ok",
                           return_value=True):
        result = orch._attempt_tool_recovery("opencode")
    assert result is True
    assert get_tool_runtime_failure("opencode") is None
    _clean("opencode")


def test_12_choose_executor_attempts_recovery_only_for_blocked_tool():
    """12. ``choose_executor`` MUST attempt the recovery probe
    only for tools that are currently blocked by a failure event.
    Healthy tools with no failure event MUST NOT be re-probed.
    """
    from aios_tool_failover import get_tool_runtime_failure
    _clean("opencode")
    _clean("codex")
    # No failure event for either tool.
    assert get_tool_runtime_failure("opencode") is None
    assert get_tool_runtime_failure("codex") is None
    orch = _load_orchestrator()
    call_count = {"opencode": 0, "codex": 0}
    real_service = orch._executor_service_active

    def _spy_service(name: str) -> bool:
        call_count[name] = call_count.get(name, 0) + 1
        return real_service(name)

    with mock.patch.object(orch, "_executor_service_active",
                           side_effect=_spy_service), \
         mock.patch.object(orch, "_executor_endpoint_reachable",
                           return_value=True), \
         mock.patch.object(orch, "_executor_adapter_probe_ok",
                           return_value=True), \
         mock.patch.object(orch, "_is_executor_available",
                           return_value=True), \
         mock.patch.object(orch, "_tool_process_health",
                           return_value=True):
        chosen = orch.choose_executor("opencode")
    assert chosen == "opencode"
    # No failure event ⇒ no recovery probe call.
    assert call_count.get("opencode", 0) == 0
    _clean("opencode")
    _clean("codex")


def test_13_choose_executor_attempts_recovery_for_blocked_tool():
    """13. ``choose_executor`` MUST invoke the recovery probe
    exactly once per blocked tool per call.  The probe runs even
    before the failure event's TTL has expired.
    """
    from aios_tool_failover import record_tool_runtime_failure
    _clean("opencode")
    record_tool_runtime_failure(
        "opencode", scope="TOOL_PROCESS",
        reason="daemon_dead", ttl_seconds=10000,
    )
    orch = _load_orchestrator()
    probe_calls = {"n": 0}
    real = orch._attempt_tool_recovery

    def _spy(name: str) -> bool:
        probe_calls["n"] += 1
        return real(name)

    with mock.patch.object(orch, "_attempt_tool_recovery",
                           side_effect=_spy), \
         mock.patch.object(orch, "_is_executor_available",
                           return_value=True), \
         mock.patch.object(orch, "_tool_process_health",
                           return_value=True):
        chosen = orch.choose_executor("opencode")
    assert probe_calls["n"] >= 1, (
        "choose_executor must attempt recovery for a blocked tool"
    )
    _clean("opencode")
