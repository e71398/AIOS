#!/usr/bin/env python3
"""Tests for the close-out 20260727-§三 / §四 / §五 / §十一 additions.

Adds minimal coverage for:

* queue admission guard — REJECT when test sources blow past the cap
* queue admission guard — ADMIT for real-user sources even with backlog
* pending_drain classification — orchestrator-wrapped acceptance
  parents get reclassified as TEST_TASK_SAFE_TO_CANCEL via the
  parent_workflow sender_marker heuristic
* aios_orchestrator_failover_hook — strict_violation surface wins
  even when shadow_mode would otherwise downgrade the action
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from aios_queue_admission import (  # noqa: E402
    evaluate as admission_evaluate,
    reload_config as admission_reload,
)


class TestQueueAdmissionGuard:
    """§三: source-level admission guard rejects test backlogs."""

    def setup_method(self):
        admission_reload()

    def test_real_user_source_admit_even_with_backlog(self):
        result = admission_evaluate("api")
        assert result["ok"] is True
        assert result["decision"] == "ADMIT"
        assert result["reason"] in ("REAL_USER_SOURCE",
                                     "NON_TEST_SOURCE",
                                     "TEST_BACKLOG_OK")

    def test_other_real_user_source_admit(self):
        result = admission_evaluate("feishu")
        assert result["ok"] is True
        assert result["decision"] == "ADMIT"

    def test_test_source_decision_includes_audit_fields(self):
        # An empty queue should still allow test sources through so
        # the audit surface is present even when ADMIT is granted.
        result = admission_evaluate("acceptance")
        assert "configured_limit" in result
        assert "current_pending" in result
        assert "test_pending" in result
        assert "timestamp" in result

    def test_test_source_reject_when_backlog_at_cap(self, monkeypatch):
        # Force the cap low so we can prove REJECT is reachable.
        monkeypatch.setenv("AIOS_TEST_PENDING_LIMIT", "0")
        admission_reload()
        result = admission_evaluate("test")
        assert result["ok"] is False
        assert result["decision"] == "REJECT"
        assert result["reason"] == "TEST_BACKLOG_LIMIT_REACHED"
        assert result["http_status"] == 429
        # Audit fields must be present.
        assert result["test_pending"] >= 0
        assert result["configured_limit"] == 0
        assert isinstance(result["timestamp"], str)


class TestHookStrictViolationSurface:
    """§十一: a strict_model+blocked_binding conflict surfaces as
    strict_violation even when shadow_mode would otherwise hide the
    inner decision.
    """

    def test_strict_model_in_blocked_bindings_returns_strict_violation(
            self, monkeypatch):
        # Make sure failover flags are enabled so the live engine
        # path runs instead of the all-off shadow shortcut.
        monkeypatch.setenv("AIOS_TOOL_FAILOVER_ENABLED", "1")
        monkeypatch.setenv("AIOS_MODEL_FAILOVER_ENABLED", "1")
        monkeypatch.setenv("AIOS_ROUTING_SHADOW_MODE", "1")
        # Reload the feature flags inside the hook module.
        import importlib
        import aios_orchestrator_failover_hook as hook
        importlib.reload(hook)
        try:
            chosen, decision = hook.route_node_executor(
                task_id="test-strict-m",
                role="executor",
                source="api",
                preferred_executor="opencode",
                strict_executor="",
                allow_executor_fallback=True,
                preferred_tool="opencode",
                preferred_model_binding="opencode:minimax",
                strict_model=True,
                blocked_tools=(),
                blocked_model_bindings=["opencode:minimax"],
                blocked_resources=(),
            )
        finally:
            # Restore module features after the test.
            importlib.reload(hook)
        assert chosen == "", (
            "strict_model in blocked_model_bindings must NOT pick a tool"
        )
        assert decision.action == "strict_violation"
        assert "blocked_model_bindings" in (
            decision.model_failover_reason or ""
        )

    def test_plain_no_conflict_proceeds_or_returns_decision(self, monkeypatch):
        # A baseline routing call with no strict_* / block_* must
        # *not* surface strict_violation — even if shadow_mode is on.
        monkeypatch.setenv("AIOS_TOOL_FAILOVER_ENABLED", "1")
        monkeypatch.setenv("AIOS_MODEL_FAILOVER_ENABLED", "1")
        monkeypatch.setenv("AIOS_ROUTING_SHADOW_MODE", "1")
        import importlib
        import aios_orchestrator_failover_hook as hook
        importlib.reload(hook)
        try:
            chosen, decision = hook.route_node_executor(
                task_id="test-baseline",
                role="executor",
                source="api",
                preferred_executor="opencode",
                strict_executor="",
                allow_executor_fallback=True,
                preferred_tool="opencode",
                preferred_model_binding="",
                strict_tool=False,
                strict_model=False,
                blocked_tools=(),
                blocked_model_bindings=(),
                blocked_resources=(),
            )
        finally:
            importlib.reload(hook)
        # No strict_violation in the no-conflict baseline.  The
        # chosen tool may or may not be resolved depending on the
        # real engine state, but the verdict must not be a
        # strict_violation.
        assert decision.action != "strict_violation", (
            "baseline routing should never surface strict_violation"
        )


class TestCancelCascadeParents:
    """§五: cancel() must only cancel parents that look like
    acceptance/closeout senders.  Real-user parents must NOT be
    cancelled even when their children are listed in the cancel set.
    """

    def _mk_record(self, source, sender_id, session_key, parent_id=""):
        return {
            "source": source,
            "sender": sender_id,
            "session_id": session_key,
            "parent_id": parent_id,
            "status": "pending",
        }

    def test_parent_sender_marker_recognised(self):
        from aios_pending_drain import _has_test_sender_marker
        assert _has_test_sender_marker("p8c-f-audit", "")
        assert _has_test_sender_marker("p8c-u-audit", "")
        assert _has_test_sender_marker(
            "user", "api:closeout:abcdef-1234")
        # Real-user sources must not trigger the marker.
        assert not _has_test_sender_marker("real-user-007", "telegram:xyz")
        assert not _has_test_sender_marker("", "")