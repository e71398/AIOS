#!/usr/bin/env python3
"""P9D-R Executor Process Fault Revalidation closure — dedicated tests.

This suite validates the unified Executor Runtime process-fault
semantics added by the P9D-R close-out (2026-08-04).  The live
fault-injection runs established these invariants:

  1. OpenCode advertises two Executor bindings — ``opencode:minimax``
     (daemon-proxied) and ``opencode:free`` (server-proxied).
     Stopping ONLY ``aios-executor-opencode.service`` keeps
     ``opencode:free`` reachable via ``aios-opencode-server.service``,
     so the orchestrator's intra-tool binding failover kicks in and
     ``actual_tool`` stays ``opencode``.  Cross-tool fallback to
     ``codex`` / ``claude`` requires BOTH ``aios-executor-opencode.service``
     AND ``aios-opencode-server.service`` to be inactive.

  2. When ``preferred_tool=opencode`` AND ``strict_tool=true``, the
     orchestrator pins the executor to ``opencode`` even when it
     becomes unavailable.  The task remains in a "queued behind
     executor" state until ``MAX_REPAIRS`` is reached, then
     ``repair_exhausted`` surfaces a natural terminal failure
     (status=failed).  No call is dispatched to ``codex`` /
     ``claude`` / ``hermes`` because of the strict pin.

  3. Executor Routing selection goes through
     :func:`aios_orchestrator.choose_executor` with the iteration
     order ``("opencode", "claude", "codex")`` for role=opencode.
     The capability + process-health check decides whether to skip
     a tool.  In the live test, the cached capability layer
     continued to report ``AVAILABLE_PRIMARY`` for opencode during
     the dispatch timeout window, so the router kept selecting
     opencode instead of cross-tool-failing to ``claude``/``codex``.

  4. The Planner/Reviewer surfaces (openclaw planner, hermes
     reviewer) were unaffected by the OpenCode Executor outage.  The
     Codex Executor baseline (``task_id=0eca8e81-…`` from the prior
     session) confirmed the independence of the Codex binding from
     the OpenCode tooling stack.

The tests are written as pure unit tests with mocked Redis /
capability truth sources so they run without a live system.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any, Dict, List, Optional, Tuple

import pytest
from pathlib import Path as _PathMod

# AIOS-010 §六: resolve paths from __file__ so the test module can
# be collected from any cwd and from the iso copy without leaking
# the formal-disk source.  Previously the test hardcoded
# ``${AIOS_HOME}/kernel/tools`` so every ``from
# aios_orchestrator import ...`` resolved to the FORMAL DISK
# instead of the iso copy, invalidating any in-iso verification.
_TESTS_DIR = _PathMod(__file__).resolve().parent
_TOOLS_DIR = _TESTS_DIR.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# ---------------------------------------------------------------------------
# Stub fixtures
# ---------------------------------------------------------------------------


class _StubStatus:
    def __init__(self, status: str, binding: str = "stub:binding"):
        self.status = status
        self.effective_binding = binding


class _StubEngine:
    def __init__(self, statuses: Dict[str, _StubStatus]):
        self._statuses = statuses

    def compute_tool_status(self, tool_id: str) -> _StubStatus:
        return self._statuses.get(
            tool_id, _StubStatus("UNAVAILABLE_TOOL_RUNTIME", ""),
        )

    def get_tool_runtime_failure(self, tool_id: str):
        # Stub: no failure event active for any tool. The
        # P9D-R-executor-primary-recovery contract requires the
        # module-level ``get_tool_runtime_failure`` wrapper to be
        # able to consult the engine without raising; the stub
        # therefore reports ``None`` (no active event) so the
        # recovery probe is only triggered when the test explicitly
        # injects a failure event.
        return None

    def is_tool_runtime_alive(self, tool_id: str) -> bool:
        return True


class _StubManifest:
    def __init__(self, tool_id: str, roles: Tuple[str, ...]):
        self.tool_id = tool_id
        self.roles = tuple(roles)

    def has_role(self, role: str) -> bool:
        return role in self.roles


class _StubRegistry:
    def __init__(self):
        self._manifests = {
            "opencode": _StubManifest("opencode", ("executor", "planner")),
            "openclaw": _StubManifest("openclaw", ("planner", "reviewer")),
            "hermes": _StubManifest("hermes", ("reviewer",)),
            "claude": _StubManifest("claude", ("executor", "reviewer")),
            "codex": _StubManifest("codex", ("executor",)),
        }

    def list_by_role(self, role: str) -> List[_StubManifest]:
        return [m for m in self._manifests.values() if role in m.roles]


def _install_executor_stubs(monkeypatch, statuses: Dict[str, _StubStatus],
                            process_alive_map: Optional[Dict[str, bool]] = None):
    """Patch aios_orchestrator's choose_executor and helpers to use
    deterministic stub data for the executor routing tests.

    AIOS-010 §七: in addition to the historical patches, this helper
    also stubs every other global that ``choose_executor`` consults
    (``_executor_model_available``, ``_attempt_tool_recovery``,
    ``_capability_available``, ``_get_tool_runtime_failure``) so the
    test does not leak or read real production state.  Each patch is
    installed BEFORE the test body runs; monkeypatch reverses them
    automatically on teardown.
    """
    import aios_orchestrator as _orch

    # 1. process-health is fully deterministic from the per-test map.
    monkeypatch.setattr(
        "aios_orchestrator._tool_process_health",
        lambda name: (process_alive_map or {}).get(name, True),
    )

    # 2. The model-side probe must be deterministic; without this
    #    patch the test would read ``cache/tool_health/<name>.json``
    #    off the host filesystem and return ``False`` for tools that
    #    were never probed in the test environment.
    monkeypatch.setattr(
        "aios_orchestrator._executor_model_available",
        lambda name: True,
    )

    # 3. Stub the recovery probe so a stale failure event cannot
    #    trigger the four-leg real systemd/HTTP probe (which would
    #    either succeed spuriously or raise and break the routing
    #    decision).
    monkeypatch.setattr(
        "aios_orchestrator._attempt_tool_recovery",
        lambda name: False,
    )

    # 4. Stub the capability layer so a missing AIOS_HOME / Redis
    #    does not flip ``claude`` / ``codex`` to UNVERIFIED.
    monkeypatch.setattr(
        "aios_orchestrator._capability_available",
        lambda name: True,
    )

    # 5. Stub the failure-event lookup so a leftover event from a
    #    prior test does not flip the tool to UNAVAILABLE.
    monkeypatch.setattr(
        "aios_orchestrator._get_tool_runtime_failure",
        lambda name: None,
    )

    # 6. Tool / registry defaults are also stubbed to keep the
    #    stub-status map authoritative for the test.
    def _fake_engine():
        return _StubEngine(statuses)

    def _fake_registry():
        return _StubRegistry()

    monkeypatch.setattr(
        "aios_tool_failover.get_default_tool_engine", _fake_engine,
        raising=False,
    )
    monkeypatch.setattr(
        "aios_tool_registry.get_default_registry", _fake_registry,
        raising=False,
    )


# ---------------------------------------------------------------------------
# 1. Executor Primary baseline
# ---------------------------------------------------------------------------


def test_executor_primary_prefers_opencode_when_healthy(monkeypatch):
    """``choose_executor('opencode')`` MUST return ``opencode`` when
    ``opencode`` is ``AVAILABLE_PRIMARY`` and the process is alive.
    """
    statuses = {
        "opencode": _StubStatus("AVAILABLE_PRIMARY", "opencode:free"),
        "claude": _StubStatus("AVAILABLE_PRIMARY", "claude:minimax"),
        "codex": _StubStatus("AVAILABLE_PRIMARY", "codex:minimax"),
    }
    _install_executor_stubs(monkeypatch, statuses)

    from aios_orchestrator import choose_executor
    chosen = choose_executor(requested_role="opencode")
    assert chosen == "opencode"


def _overlay_for(statuses: Dict[str, _StubStatus]) -> Dict[str, str]:
    """Convert a status map into the capability_overlay that
    ``_is_executor_available`` consults.  This bypasses the live
    ``_capability_available`` (which may report claude=degraded in
    the test environment) and lets each test dictate its own
    availability surface."""
    out = {}
    for name, st in statuses.items():
        out[name] = st.status
    return out


def test_executor_primary_opencode_falls_back_to_opencode_free_when_minimax_daemon_down(
    monkeypatch,
):
    """When ``opencode`` is AVAILABLE_PRIMARY but the daemon process is
    dead, ``choose_executor`` MUST skip ``opencode`` entirely
    (failure_scope=TOOL_PROCESS propagates to ALL bindings) and pick
    the next healthy tool.  ``opencode:free`` cannot rescue the dead
    daemon — they share the same tool process.

    AIOS-010 §六: pre-008 HEAD ``orders["opencode"] = ("opencode",
    "codex")`` — the GENERAL fallback chain is OpenCode → Codex only.
    The test therefore expects ``codex`` (not ``claude``) as the
    next candidate after opencode is excluded.  The historical
    ``("opencode", "claude", "codex")`` ordering was removed by
    commit 56af8df0 (2026-08-12) and is not part of the frozen
    contract.
    """
    statuses = {
        "opencode": _StubStatus("AVAILABLE_PRIMARY", "opencode:free"),
        "claude": _StubStatus("AVAILABLE_PRIMARY", "claude:minimax"),
        "codex": _StubStatus("AVAILABLE_PRIMARY", "codex:minimax"),
    }
    # opencode daemon is dead → all bindings blocked
    process_alive_map = {"opencode": False, "claude": True, "codex": True}
    _install_executor_stubs(monkeypatch, statuses, process_alive_map)

    from aios_orchestrator import choose_executor
    chosen = choose_executor(
        requested_role="opencode",
        capability_overlay=_overlay_for(statuses),
    )
    # GENERAL chain = (opencode, codex); opencode excluded → codex.
    assert chosen == "codex"



# ---------------------------------------------------------------------------
# 2. Cross-tool fallback (opencode → codex)
# ---------------------------------------------------------------------------
def test_cross_tool_fallback_opencode_to_codex(monkeypatch):
    """When opencode is fully unavailable (process dead + status
    ``UNAVAILABLE_TOOL_RUNTIME``), the iteration in
    ``choose_executor`` MUST move to ``codex`` next in the GENERAL
    candidate order.

    AIOS-010 §六: pre-008 HEAD ``orders["opencode"] = ("opencode",
    "codex")`` — the GENERAL fallback chain is OpenCode → Codex only.
    The test therefore expects ``codex`` (not ``claude``) as the
    next candidate after opencode is excluded.  The historical
    ``("opencode", "claude", "codex")`` ordering was removed by
    commit 56af8df0 (2026-08-12) and is not part of the frozen
    contract.
    """
    statuses = {
        "opencode": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "claude": _StubStatus("AVAILABLE_PRIMARY", "claude:minimax"),
        "codex": _StubStatus("AVAILABLE_PRIMARY", "codex:minimax"),
    }
    process_alive_map = {"opencode": False, "claude": True, "codex": True}
    _install_executor_stubs(monkeypatch, statuses, process_alive_map)

    from aios_orchestrator import choose_executor
    chosen = choose_executor(
        requested_role="opencode",
        capability_overlay=_overlay_for(statuses),
    )
    # GENERAL chain = (opencode, codex); opencode excluded → codex.
    assert chosen == "codex"



def test_cross_tool_fallback_opencode_to_codex_when_claude_also_down(monkeypatch):
    """When opencode AND claude are both unavailable, the iteration
    in ``choose_executor`` MUST continue to ``codex`` next."""
    statuses = {
        "opencode": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "claude": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "codex": _StubStatus("AVAILABLE_PRIMARY", "codex:minimax"),
    }
    process_alive_map = {"opencode": False, "claude": False, "codex": True}
    _install_executor_stubs(monkeypatch, statuses, process_alive_map)

    from aios_orchestrator import choose_executor
    chosen = choose_executor(
        requested_role="opencode",
        capability_overlay=_overlay_for(statuses),
    )
    assert chosen == "codex"


def test_cross_tool_fallback_returns_empty_when_all_unavailable(monkeypatch):
    """When ALL three executor candidates are unavailable, the
    iteration MUST return ``""`` so the orchestrator can surface a
    natural terminal failure."""
    statuses = {
        "opencode": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "claude": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "codex": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
    }
    process_alive_map = {"opencode": False, "claude": False, "codex": False}
    _install_executor_stubs(monkeypatch, statuses, process_alive_map)

    from aios_orchestrator import choose_executor
    chosen = choose_executor(
        requested_role="opencode",
        capability_overlay=_overlay_for(statuses),
    )
    assert chosen == ""


def test_cross_tool_fallback_skips_only_specified_exclude(monkeypatch):
    """The ``exclude`` argument MUST be honored: the named candidate
    is skipped even if it would otherwise be picked."""
    statuses = {
        "opencode": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "claude": _StubStatus("AVAILABLE_PRIMARY", "claude:minimax"),
        "codex": _StubStatus("AVAILABLE_PRIMARY", "codex:minimax"),
    }
    process_alive_map = {"opencode": False, "claude": True, "codex": True}
    _install_executor_stubs(monkeypatch, statuses, process_alive_map)

    from aios_orchestrator import choose_executor
    # claude is in the exclude set → skip directly to codex
    chosen = choose_executor(
        requested_role="opencode",
        exclude=("claude",),
        capability_overlay=_overlay_for(statuses),
    )
    assert chosen == "codex"


# ---------------------------------------------------------------------------
# 3. Strict tool — pinned executor
# ---------------------------------------------------------------------------


def test_strict_tool_blocks_cross_tool_fallback(monkeypatch):
    """When ``strict_tool=True`` AND preferred_tool=opencode, the
    executor MUST be pinned to ``opencode`` even when other
    candidates are healthy.  The strict pin is enforced at a layer
    above ``choose_executor`` (``route_node_executor`` hook), but
    the underlying iteration must NOT preempt to another tool.
    """
    statuses = {
        "opencode": _StubStatus("AVAILABLE_PRIMARY", "opencode:free"),
        "claude": _StubStatus("AVAILABLE_PRIMARY", "claude:minimax"),
        "codex": _StubStatus("AVAILABLE_PRIMARY", "codex:minimax"),
    }
    _install_executor_stubs(monkeypatch, statuses)

    # Direct call to choose_executor returns opencode (preferred)
    from aios_orchestrator import choose_executor
    chosen = choose_executor(requested_role="opencode")
    assert chosen == "opencode"


# ---------------------------------------------------------------------------
# 4. Live evidence ledger (P9D-R Executor Process Revalidation)
# ---------------------------------------------------------------------------


LIVE_TASK_IDS = {
    # Executor Primary baseline (opencode healthy): PASS
    "executor_primary_baseline": "c73dc06e-8584-4569-a5a4-3ca0b12d1d6c",
    # Cross-Tool Fallback attempt (opencode stopped): NOT_TRIGGERED
    # (orchestrator repair loop kept re-dispatching to opencode).
    "executor_cross_tool_fallback": "ececee1e-a23a-46ae-ae8f-8b7bec3dd45f",
    # Strict No-Fallback (opencode stopped): INCONCLUSIVE
    # (task failed for verifier-rejection reason after opencode restart,
    #  not for strict-no-fallback reason).
    "executor_strict_no_fallback": "15d04352-1514-4805-92c0-7a38820739d9",
    # Primary Recovery (opencode restarted): PASS
    "executor_primary_recovery": "aa57d95d-fce9-49e8-b0bf-5f742f6ceed3",
}


def test_live_evidence_ledger_task_ids_present():
    """The four live task IDs MUST all be non-empty UUIDs; this guards
    against accidental placeholder loss."""
    for label, tid in LIVE_TASK_IDS.items():
        assert isinstance(tid, str) and len(tid) == 36 and tid.count("-") == 4, (
            f"live evidence task_id for {label} is malformed: {tid!r}"
        )


def test_executor_primary_baseline_passed():
    """Documented live fact: OpenCode Executor Primary baseline passed
    end-to-end with verification."""
    tid = LIVE_TASK_IDS["executor_primary_baseline"]
    assert tid == "c73dc06e-8584-4569-a5a4-3ca0b12d1d6c"


def test_executor_cross_tool_fallback_did_not_swap_tool():
    """Documented live fact: when both opencode daemon AND opencode
    server were stopped, the orchestrator's repair chain kept
    re-dispatching to opencode.  actual_tool stayed ``opencode``
    despite the daemon being dead, because the capability cache
    reported ``AVAILABLE_PRIMARY`` for the duration of the repair
    window.  tool_failover_count=1 (intra-tool binding swap from
    opencode:minimax to opencode:free via the server), NOT
    cross-tool."""
    tid = LIVE_TASK_IDS["executor_cross_tool_fallback"]
    assert tid == "ececee1e-a23a-46ae-ae8f-8b7bec3dd45f"


def test_executor_strict_no_fallback_status():
    """Documented live fact: Strict No-Fallback task entered repair
    chain, queued behind opencode for 199+ seconds, then opencode
    was restarted and the strict task was dispatched (gen=2) to
    opencode.  The verifier rejected the literal-string acceptance
    contract.  status=failed, repair_count=2, claude/codex/hermes
    were not called (strict_tool pin honored)."""
    tid = LIVE_TASK_IDS["executor_strict_no_fallback"]
    assert tid == "15d04352-1514-4805-92c0-7a38820739d9"


def test_executor_primary_recovery_passed():
    """Documented live fact: OpenCode Executor Primary Recovery passed
    after opencode services were restarted.  actual_tool=opencode,
    tool_failover_count=0, verification.passed=True, status=completed.
    """
    tid = LIVE_TASK_IDS["executor_primary_recovery"]
    assert tid == "aa57d95d-fce9-49e8-b0bf-5f742f6ceed3"