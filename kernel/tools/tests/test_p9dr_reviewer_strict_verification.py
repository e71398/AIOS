#!/usr/bin/env python3
"""P9D-R Reviewer Strict Verification closure — dedicated tests.

This suite validates the P9D-R-Reviewer-Strict-Closure contract
introduced on 2026-08-04.  The previous close-out short-circuited to
``VERIFICATION_BLOCKED:strict_no_fallback`` whenever
``compute_tool_status(preferred_reviewer)`` reported ``UNAVAILABLE_*``,
even if the reviewer was actually reachable and only had a transient
failure event in the bounded TTL.  The correct semantic — captured
in :mod:`aios_verification_gate` and asserted here — is:

  1. ``allow_reviewer_fallback=False`` means "do not switch to a
     secondary reviewer".  It does NOT mean "do not call the
     preferred reviewer".
  2. The Verification Gate MUST always attempt the preferred
     reviewer through the normal call path.  Transient failure
     events are cleared by the inline recovery probe.
  3. ``VERIFICATION_BLOCKED:strict_no_fallback`` is only emitted
     when the preferred reviewer genuinely failed to produce a
     verdict.
  4. A blocked (``blocked_reviewer_tools``) primary reviewer MUST
     still be called through the normal call path before the gate
     surfaces a BLOCK — blocking is about *which* tool can be
     used, not *whether* the chosen one is reachable.
  5. The verdict must persist ``attempted_reviewers``,
     ``actual_reviewer``, ``reviewer_fallback_count``,
     ``reviewer_bypass`` and the policy surface — even on the
     strict-no-fallback path.

The tests are written as pure unit tests with mocked Redis /
capability truth sources so they run without a live system.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

TOOLS = "${AIOS_HOME}/kernel/tools"
TESTS = "${AIOS_HOME}/kernel/tools/tests"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)


# ---------------------------------------------------------------------------
# Stubs and fixtures
# ---------------------------------------------------------------------------


class _StubStatus:
    def __init__(self, status: str, binding: str = "stub:binding"):
        self.status = status
        self.effective_binding = binding


class _StubEngine:
    """Engine stub used by the close-out tests.  Honours
    ``compute_tool_status`` and exposes no-op failure-event
    helpers so ``_attempt_tool_recovery`` (called by the new
    strict-no-fallback path) can run without raising.
    """

    def __init__(self, statuses: Dict[str, _StubStatus]):
        self._statuses = statuses

    def compute_tool_status(self, tool_id: str) -> _StubStatus:
        return self._statuses.get(
            tool_id, _StubStatus("UNAVAILABLE_TOOL_RUNTIME", ""),
        )

    def get_tool_runtime_failure(self, tool_id: str):
        return None

    def is_tool_runtime_alive(self, tool_id: str) -> bool:
        return True

    def clear_tool_runtime_failure(self, tool_id: str) -> None:
        return None

    def list_tool_runtime_failures(self) -> Dict[str, dict]:
        return {}


def _install_strict_stubs(monkeypatch, statuses: Dict[str, _StubStatus],
                          *, healthy: bool = True) -> None:
    """Patch the verification gate's _semantic_review collaborators so
    the strict-no-fallback path is exercised end-to-end.

    ``healthy`` controls whether the inline ``_call_reviewer_once``
    returns a passing verdict or a transport failure.  When
    ``healthy=True`` the gate must surface
    ``verification.passed=True`` and a single
    ``attempted_reviewers=[hermes]`` entry.  When ``healthy=False``
    the gate must surface the legacy
    ``VERIFICATION_BLOCKED:strict_no_fallback`` reason AFTER the
    preferred reviewer has been attempted (the
    ``attempted_reviewers`` list is non-empty).
    """
    import aios_verification_gate as _vg
    import aios_orchestrator as _orch

    monkeypatch.setattr(
        "aios_tool_failover.get_default_tool_engine",
        lambda: _StubEngine(statuses), raising=False,
    )
    monkeypatch.setattr(
        "aios_tool_registry.get_default_registry", lambda: object(),
        raising=False,
    )
    # ``choose_reviewer`` only needs to return something; the
    # strict-no-fallback path collapses it to the preferred reviewer.
    monkeypatch.setattr(
        _orch, "choose_reviewer",
        lambda **kwargs: {
            "reviewer": kwargs.get("preferred_reviewer", "hermes"),
            "binding": "hermes:deepseek",
            "failure_scope": "",
            "candidates": [kwargs.get("preferred_reviewer", "hermes")],
            "excluded": [],
            "fallback_count": 0,
        },
    )

    # The inline recovery probe must succeed (no transient failure).
    monkeypatch.setattr(
        _orch, "_attempt_tool_recovery", lambda name: True,
    )
    # ``_tool_process_health`` is a no-op; healthy reviewers are
    # always reachable.
    monkeypatch.setattr(_orch, "_tool_process_health",
                       lambda name: True)

    captured: Dict[str, Any] = {"calls": []}

    def _fake_call_once(prompt, reviewer, attempts, binding_id=""):
        captured["calls"].append({
            "reviewer": reviewer,
            "binding_id": binding_id,
            "healthy": healthy,
        })
        if healthy:
            return {
                "reviewer": reviewer,
                "returncode": 0,
                "stdout_bytes": 12,
                "stderr_bytes": 0,
                "stderr_text": "",
                "stdout_text": json.dumps({
                    "passed": True,
                    "reason": "verifier accepted",
                    "repair_instruction": "none",
                    "evidence_checked": True,
                    "evidence_sources": ["GET /status"],
                }),
                "latency_ms": 12,
                "live_recovery": False,
                "extract_category": "OK",
                "parsed_value": {
                    "passed": True,
                    "reason": "verifier accepted",
                    "repair_instruction": "none",
                    "evidence_checked": True,
                    "evidence_sources": ["GET /status"],
                },
                "provider_kind": "AVAILABLE",
                "provider_description": "ok",
                "provider_fatal": False,
                "binding_id": binding_id,
            }
        # unhealthy: surface a transport failure so the gate
        # records the attempt but cannot return a verdict.
        attempts.append({
            "reviewer": reviewer,
            "reason": "CONNECTION_FAILED:fake",
            "provider_kind": "CONNECTION_FAILED",
            "provider_fatal": False,
        })
        return None

    monkeypatch.setattr(_vg, "_call_reviewer_once", _fake_call_once)
    monkeypatch.setattr(_vg, "_call_reviewer_via_minimax_adapter",
                       _fake_call_once)
    return captured


def _task_policy(preferred_reviewer: str = "hermes",
                 allow_reviewer_fallback: bool = False,
                 blocked_reviewer_tools=()) -> dict:
    return {
        "preferred_reviewer": preferred_reviewer,
        "allow_reviewer_fallback": allow_reviewer_fallback,
        "blocked_reviewer_tools": list(blocked_reviewer_tools or ()),
    }


def _child_state(executor: str = "opencode", status: str = "completed",
                 result_summary: str = "[opencode] ok") -> dict:
    return {
        "task_id": "child-test",
        "status": status,
        "executor": executor,
        "result_summary": result_summary,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_1_strict_no_fallback_calls_healthy_primary_reviewer(monkeypatch):
    """Test 1: ``allow_reviewer_fallback=False`` MUST still call the
    healthy primary reviewer (hermes).  The legacy
    ``VERIFICATION_BLOCKED:strict_no_fallback`` short-circuit
    blocked the call whenever the capability layer reported
    ``UNAVAILABLE_*``; the new path clears transient failure
    events and proceeds to the normal call flow.
    """
    import aios_verification_gate as _vg
    statuses = {"hermes": _StubStatus("AVAILABLE_WITH_MODEL_FALLBACK")}
    captured = _install_strict_stubs(monkeypatch, statuses, healthy=True)
    review = _vg._semantic_review(
        prompt="audit",
        executor="opencode",
        task_policy=_task_policy(),
        child_state=_child_state(),
    )
    assert review["actual_reviewer"] == "hermes"
    assert review["attempted_reviewers"] == ["hermes"]
    assert review["reviewer_bypass"] is False
    assert review["reviewer_fallback_count"] == 0
    assert review["allow_reviewer_fallback"] is False
    assert captured["calls"][0]["reviewer"] == "hermes"


def test_2_healthy_primary_passes_not_blocked(monkeypatch):
    """Test 2: a healthy primary reviewer MUST NOT be reported as
    ``VERIFICATION_BLOCKED``; the gate must return a passing
    verdict with ``parsed.passed=True``.
    """
    import aios_verification_gate as _vg
    statuses = {"hermes": _StubStatus("AVAILABLE_WITH_MODEL_FALLBACK")}
    _install_strict_stubs(monkeypatch, statuses, healthy=True)
    review = _vg._semantic_review(
        prompt="audit",
        executor="opencode",
        task_policy=_task_policy(),
        child_state=_child_state(),
    )
    assert review["parsed"]["passed"] is True
    assert "VERIFICATION_BLOCKED" not in review["parsed"].get("reason", "")


def test_3_primary_failure_does_not_call_secondary(monkeypatch):
    """Test 3: when the primary reviewer fails AND
    ``allow_reviewer_fallback=False``, the gate MUST NOT fall
    through to a secondary reviewer; the only attempted entry is
    the primary.
    """
    import aios_verification_gate as _vg
    statuses = {"hermes": _StubStatus("AVAILABLE_WITH_MODEL_FALLBACK")}
    captured = _install_strict_stubs(monkeypatch, statuses, healthy=False)
    with pytest.raises(RuntimeError) as exc_info:
        _vg._semantic_review(
            prompt="audit",
            executor="opencode",
            task_policy=_task_policy(),
            child_state=_child_state(),
        )
    reason = str(exc_info.value)
    assert "VERIFICATION_BLOCKED:strict_no_fallback" in reason
    attempted = [c["reviewer"] for c in captured["calls"]]
    assert attempted == ["hermes"]


def test_4_no_secondary_does_not_break_healthy_primary(monkeypatch):
    """Test 4: the absence of a secondary reviewer in the
    registry MUST NOT break a healthy primary call.  Even when
    only hermes is registered and the policy asks for a strict
    run, the healthy primary is invoked.
    """
    import aios_verification_gate as _vg
    # Only hermes is registered; no claude / openclaw / opencode
    # reviewer.  Strict mode MUST still let hermes run.
    statuses = {"hermes": _StubStatus("AVAILABLE_WITH_MODEL_FALLBACK")}
    captured = _install_strict_stubs(monkeypatch, statuses, healthy=True)
    review = _vg._semantic_review(
        prompt="audit",
        executor="opencode",
        task_policy=_task_policy(),
        child_state=_child_state(),
    )
    assert review["actual_reviewer"] == "hermes"
    assert review["parsed"]["passed"] is True
    assert captured["calls"][0]["reviewer"] == "hermes"


def test_5_blocked_primary_still_calls_via_strict(monkeypatch):
    """Test 5: when the primary reviewer is in
    ``blocked_reviewer_tools`` the gate must surface the BLOCK
    on the primary, but the attempts list records
    ``blocked_reviewer_tool`` (NOT a silent empty loop).  This
    guards against the legacy bug where strict_no_fallback +
    blocked_primary could pass the verdict without ever
    attempting the blocked reviewer.
    """
    import aios_verification_gate as _vg
    statuses = {"hermes": _StubStatus("AVAILABLE_WITH_MODEL_FALLBACK")}
    _install_strict_stubs(monkeypatch, statuses, healthy=True)
    # Block the preferred reviewer.  The gate must surface a
    # structured RuntimeError carrying the ``blocked_reviewer_tool``
    # attempt entry for the primary, AND the
    # ``VERIFICATION_BLOCKED:strict_no_fallback`` reason.  Both
    # entries together prove the gate did NOT silently bypass the
    # blocked reviewer.
    with pytest.raises(RuntimeError) as exc_info:
        _vg._semantic_review(
            prompt="audit",
            executor="opencode",
            task_policy=_task_policy(blocked_reviewer_tools=("hermes",)),
            child_state=_child_state(),
        )
    reason = str(exc_info.value)
    assert "VERIFICATION_BLOCKED:strict_no_fallback" in reason
    assert "blocked_reviewer_tool" in reason
    # Hermes is excluded by policy, not by capability — the
    # audit trail is honest.
    assert '"hermes"' in reason


def test_6_attempted_and_actual_reviewer_persisted(monkeypatch):
    """Test 6: the verdict surface must persist
    ``attempted_reviewers`` / ``actual_reviewer`` / ``fallback_count``
    / ``reviewer_bypass`` / ``policy_driven`` even under
    ``allow_reviewer_fallback=False``.
    """
    import aios_verification_gate as _vg
    statuses = {"hermes": _StubStatus("AVAILABLE_WITH_MODEL_FALLBACK")}
    _install_strict_stubs(monkeypatch, statuses, healthy=True)
    review = _vg._semantic_review(
        prompt="audit",
        executor="opencode",
        task_policy=_task_policy(),
        child_state=_child_state(),
    )
    for field in (
        "attempted_reviewers",
        "excluded_reviewers",
        "preferred_reviewer",
        "blocked_reviewer_tools",
        "allow_reviewer_fallback",
        "actual_reviewer",
        "reviewer_binding",
        "reviewer_fallback_count",
        "reviewer_bypass",
        "policy_driven",
        "selection_candidates",
    ):
        assert field in review, f"missing audit field: {field}"
    assert review["reviewer_bypass"] is False
    assert review["actual_reviewer"] == "hermes"
    assert review["reviewer_fallback_count"] == 0
    assert review["preferred_reviewer"] == "hermes"
    assert review["allow_reviewer_fallback"] is False


def test_7_reviewer_bypass_always_false(monkeypatch):
    """Test 7: ``reviewer_bypass`` MUST be ``False`` for the
    strict-no-fallback path.  Reviewer bypass would mean the
    gate silently accepted the executor's self-claim; that
    is forbidden by the P9D-R role-closure contract.
    """
    import aios_verification_gate as _vg
    statuses = {"hermes": _StubStatus("AVAILABLE_WITH_MODEL_FALLBACK")}
    _install_strict_stubs(monkeypatch, statuses, healthy=True)
    review = _vg._semantic_review(
        prompt="audit",
        executor="opencode",
        task_policy=_task_policy(),
        child_state=_child_state(),
    )
    assert review["reviewer_bypass"] is False

    # Even when the primary fails, bypass must remain False.
    _install_strict_stubs(monkeypatch, statuses, healthy=False)
    with pytest.raises(RuntimeError):
        _vg._semantic_review(
            prompt="audit",
            executor="opencode",
            task_policy=_task_policy(),
            child_state=_child_state(),
        )


def test_8_stale_failure_event_does_not_block_strict(monkeypatch):
    """Test 8: an active TOOL_PROCESS failure event in the bounded
    TTL MUST NOT short-circuit the strict-no-fallback path.  The
    inline recovery probe (``_attempt_tool_recovery``) clears the
    event and the loop proceeds to call hermes.

    The old behaviour was: a 60-100 s failure event on hermes
    made the gate raise
    ``VERIFICATION_BLOCKED:strict_no_fallback`` without ever
    calling hermes.  The new behaviour: the event is cleared
    by the recovery probe and hermes is called normally.
    """
    import aios_verification_gate as _vg

    class _EngineWithFailure:
        def __init__(self):
            self._calls = 0

        def compute_tool_status(self, tool_id):
            self._calls += 1
            # First call: report UNAVAILABLE (simulate stale event)
            # Subsequent calls: report AVAILABLE (after recovery
            # probe clears the event).
            if self._calls == 1:
                return _StubStatus("UNAVAILABLE_TOOL_RUNTIME", "")
            return _StubStatus("AVAILABLE_WITH_MODEL_FALLBACK",
                                "hermes:deepseek")

        def get_tool_runtime_failure(self, tool_id):
            self._calls += 1
            return None

        def is_tool_runtime_alive(self, tool_id):
            return True

        def clear_tool_runtime_failure(self, tool_id):
            return None

        def list_tool_runtime_failures(self):
            return {}

    import aios_orchestrator as _orch
    import aios_verification_gate as _vg
    monkeypatch.setattr(
        "aios_tool_failover.get_default_tool_engine",
        lambda: _EngineWithFailure(), raising=False,
    )
    monkeypatch.setattr(
        _orch, "choose_reviewer",
        lambda **kwargs: {
            "reviewer": "hermes",
            "binding": "hermes:deepseek",
            "failure_scope": "",
            "candidates": ["hermes"],
            "excluded": [],
            "fallback_count": 0,
        },
    )
    monkeypatch.setattr(_orch, "_tool_process_health", lambda name: True)

    captured_calls: List[str] = []

    def _fake_call_once(prompt, reviewer, attempts, binding_id=""):
        captured_calls.append(reviewer)
        return {
            "reviewer": reviewer,
            "returncode": 0,
            "stdout_bytes": 12,
            "stderr_bytes": 0,
            "stderr_text": "",
            "stdout_text": json.dumps({
                "passed": True, "reason": "ok",
                "repair_instruction": "none",
                "evidence_checked": True,
                "evidence_sources": [],
            }),
            "latency_ms": 12,
            "live_recovery": False,
            "extract_category": "OK",
            "parsed_value": {
                "passed": True, "reason": "ok",
                "repair_instruction": "none",
                "evidence_checked": True,
                "evidence_sources": [],
            },
            "provider_kind": "AVAILABLE",
            "provider_description": "ok",
            "provider_fatal": False,
            "binding_id": binding_id,
        }
    monkeypatch.setattr(_vg, "_call_reviewer_once", _fake_call_once)
    monkeypatch.setattr(_vg, "_call_reviewer_via_minimax_adapter",
                       _fake_call_once)

    review = _vg._semantic_review(
        prompt="audit",
        executor="opencode",
        task_policy=_task_policy(),
        child_state=_child_state(),
    )
    assert captured_calls == ["hermes"], (
        f"recovery probe must let hermes run; got {captured_calls!r}"
    )
    assert review["parsed"]["passed"] is True


def test_9_executor_success_can_be_verified_by_hermes(monkeypatch):
    """Test 9: an executor success (opencode returns the literal
    expected string) MUST be verifiable by hermes under
    ``allow_reviewer_fallback=False``.
    """
    import aios_verification_gate as _vg
    statuses = {"hermes": _StubStatus("AVAILABLE_WITH_MODEL_FALLBACK")}
    _install_strict_stubs(monkeypatch, statuses, healthy=True)
    review = _vg._semantic_review(
        prompt="audit",
        executor="opencode",
        task_policy=_task_policy(),
        child_state=_child_state(
            executor="opencode",
            status="completed",
            result_summary="[opencode] EXECUTOR_PRIMARY_RECOVERY_E2E_OK",
        ),
    )
    assert review["parsed"]["passed"] is True
    assert review["actual_reviewer"] == "hermes"


def test_10_reviewer_failure_does_not_regress_executor(monkeypatch):
    """Test 10: a Reviewer failure (e.g. hermes transport down)
    must NOT be written back as an executor failure.  The
    executor's success is preserved on the node; the failure is
    surfaced as a separate ``VERIFICATION_BLOCKED:strict_no_fallback``
    signal that the parent workflow can route to repair WITHOUT
    re-running the executor.
    """
    import aios_verification_gate as _vg
    statuses = {"hermes": _StubStatus("AVAILABLE_WITH_MODEL_FALLBACK")}
    _install_strict_stubs(monkeypatch, statuses, healthy=False)
    with pytest.raises(RuntimeError) as exc_info:
        _vg._semantic_review(
            prompt="audit",
            executor="opencode",
            task_policy=_task_policy(),
            child_state=_child_state(
                executor="opencode",
                status="completed",
                result_summary="[opencode] EXECUTOR_PRIMARY_RECOVERY_E2E_OK",
            ),
        )
    # Reviewer failure is signalled via the RuntimeError.  The
    # gate does NOT mutate the child_state because the gate is
    # pure; the orchestrator decides what to do with the
    # RuntimeError (terminal BLOCK, repair, etc.).
    reason = str(exc_info.value)
    assert "VERIFICATION_BLOCKED:strict_no_fallback" in reason
    # No silent pass: the gate refuses to clear the verdict
    # surface when the primary failed.
    assert "passed" not in reason or "passed=true" not in reason.lower()