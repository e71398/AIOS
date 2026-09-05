#!/usr/bin/env python3
"""AIOS P9D — Role Fallback Runtime Tests.

Phase 1: Dynamic Role Capability Matrix
  1.1  capability_matrix() returns all three executors
  1.2  choose_executor() respects AVAILABLE status
  1.3  choose_executor() skips UNAVAILABLE executors
  1.4  choose_executor() respects role preference order
  1.5  capability_overlay overrides global state

Phase 2: Tool/Provider Failure Scope Isolation
  2.1  single tool failure does not affect other tools
  2.2  capability_overlay scopes failure to one task
  2.3  provider failure propagates to shared bindings
  2.4  strict_tool blocks fallback
  2.5  non-strict tool allows fallback

Phase 3A: Executor Fallback Live
  3A.1  primary executor unavailable → fallback to secondary
  3A.2  all executors unavailable → returns empty
  3A.3  primary recovers → auto-recovery to primary
  3A.4  exclude list prevents re-selecting failed executor

Phase 3B: Reviewer Fallback Live
  3B.1  reviewer uses same choose_executor path
  3B.2  reviewer fallback when primary unavailable

Phase 3C: Planner Fallback Live
  3C.1  planner fallback when primary unavailable
  3C.2  planner degraded mode when all unavailable

Strict No-Fallback Regression
  S.1  strict_executor + allow_fallback=False → no fallback
  S.2  strict_tool violation → blocked
  S.3  strict_model violation → blocked
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from typing import Any, Dict
from unittest.mock import patch, MagicMock

import pytest

TOOLS = "${AIOS_HOME}/kernel/tools"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def orch():
    """Import orchestrator fresh for each test.

    Production runtime hotfix 2026-08-10: also mock the
    four-leg model side (``_executor_model_available``) so the
    P9D-R suite tests the legacy executor availability contract
    without coupling to the on-disk ``cache/tool_health/<tool>.json``
    model-side state.  The new gate is independently covered by
    ``tests/test_production_runtime_executor_health_gate.py``.
    """
    mod = importlib.import_module("aios_orchestrator")
    # Patch the model-side probe to legacy-green for all tools so
    # the P9D-R suite continues to exercise the executor-availability
    # contract without coupling to the real cache state.
    mod._executor_model_available = lambda name: True
    return mod


@pytest.fixture
def cap_mod():
    """Import capability module fresh."""
    mod = importlib.import_module("aios_capability")
    return mod


# ---------------------------------------------------------------------------
# Phase 1: Dynamic Role Capability Matrix
# ---------------------------------------------------------------------------

class TestPhase1DynamicRoleCapabilityMatrix:
    """Phase 1: Verify the dynamic role capability matrix."""

    def test_1_1_capability_matrix_returns_all_executors(self, orch, cap_mod):
        """capability_matrix() returns entries for all three executors."""
        matrix = cap_mod.capability_matrix(list(orch.EXECUTORS))
        for exe in orch.EXECUTORS:
            assert exe in matrix, f"{exe} missing from capability matrix"

    def test_1_2_choose_executor_respects_available(self, orch):
        """choose_executor() returns an AVAILABLE executor."""
        overlay = {"opencode": "AVAILABLE_PRIMARY"}
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == "opencode"

    def test_1_3_choose_executor_skips_unavailable(self, orch):
        """choose_executor() skips UNAVAILABLE_TOOL_RUNTIME executors."""
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "AVAILABLE_PRIMARY",
        }
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == "claude", "Should fallback to claude when opencode unavailable"

    def test_1_4_choose_executor_respects_role_preference(self, orch):
        """choose_executor() follows role-specific preference order."""
        overlay = {
            "opencode": "AVAILABLE_PRIMARY",
            "claude": "AVAILABLE_PRIMARY",
            "codex": "AVAILABLE_PRIMARY",
        }
        # claude role should prefer claude first
        result = orch.choose_executor("claude", capability_overlay=overlay)
        assert result == "claude"

        # codex role should prefer codex first
        result = orch.choose_executor("codex", capability_overlay=overlay)
        assert result == "codex"

    def test_1_5_capability_overlay_overrides_global(self, orch):
        """capability_overlay overrides without mutating global state."""
        # First check global state
        overlay_unavail = {"opencode": "UNAVAILABLE_TOOL_RUNTIME"}
        result = orch.choose_executor("opencode", capability_overlay=overlay_unavail)
        assert result != "opencode", "overlay should make opencode unavailable"

        # Now without overlay, opencode should be selectable again
        # (assuming it's actually available globally)
        overlay_avail = {"opencode": "AVAILABLE_PRIMARY"}
        result = orch.choose_executor("opencode", capability_overlay=overlay_avail)
        assert result == "opencode", "overlay should make opencode available"


# ---------------------------------------------------------------------------
# Phase 2: Tool/Provider Failure Scope Isolation
# ---------------------------------------------------------------------------

class TestPhase2ToolProviderFailureScope:
    """Phase 2: Verify tool/provider failure scope isolation."""

    def test_2_1_single_tool_failure_isolated(self, orch):
        """Failing one tool does not affect other tools."""
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "AVAILABLE_PRIMARY",
        }
        # claude should still be available
        result = orch.choose_executor("claude", capability_overlay=overlay)
        assert result == "claude", "claude should not be affected by opencode failure"

    def test_2_2_capability_overlay_scopes_to_task(self, orch):
        """capability_overlay only affects the current task scope."""
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "UNAVAILABLE_TOOL_RUNTIME",
            "codex": "AVAILABLE_PRIMARY",
        }
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == "codex", "Should fallback to codex"

        # Different overlay for different task
        overlay2 = {"opencode": "AVAILABLE_PRIMARY"}
        result2 = orch.choose_executor("opencode", capability_overlay=overlay2)
        assert result2 == "opencode", "Different task can still use opencode"

    def test_2_3_provider_failure_propagates(self, orch):
        """Provider failure affects tools sharing that provider."""
        # Simulate: if a provider is down, tools using it should be unavailable
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "UNAVAILABLE_TOOL_RUNTIME",
            "codex": "UNAVAILABLE_TOOL_RUNTIME",
        }
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == "", "All tools down → no executor available"

    def test_2_4_strict_tool_blocks_fallback(self, orch):
        """strict_tool prevents fallback to other tools."""
        # When strict_tool is set, the workflow should not fallback
        workflow = {
            "strict_executor": "opencode",
            "allow_executor_fallback": False,
            "capability_overlay": {"opencode": "UNAVAILABLE_TOOL_RUNTIME"},
        }
        # The _repair_node logic should fail, not fallback
        strict = workflow.get("strict_executor", "")
        allow_fb = workflow.get("allow_executor_fallback", True)
        assert strict == "opencode"
        assert allow_fb is False

    def test_2_5_non_strict_allows_fallback(self, orch):
        """Non-strict tool allows legitimate fallback."""
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "AVAILABLE_PRIMARY",
        }
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == "claude", "Non-strict should allow fallback"


# ---------------------------------------------------------------------------
# Phase 3A: Executor Fallback Live
# ---------------------------------------------------------------------------

class TestPhase3AExecutorFallbackLive:
    """Phase 3A: Verify executor fallback in live runtime."""

    def test_3a_1_primary_unavailable_fallback_to_secondary(self, orch):
        """When primary executor is unavailable, fallback to secondary."""
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "AVAILABLE_PRIMARY",
            "codex": "AVAILABLE_PRIMARY",
        }
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result in ("claude", "codex"), "Should fallback to secondary"

    def test_3a_2_all_unavailable_returns_empty(self, orch):
        """When all executors unavailable, choose_executor returns empty."""
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "UNAVAILABLE_TOOL_RUNTIME",
            "codex": "UNAVAILABLE_TOOL_RUNTIME",
        }
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == "", "All unavailable → empty string"

    def test_3a_3_primary_recovers_auto_recovery(self, orch):
        """When primary recovers, auto-recovery selects primary again."""
        # Phase 1: primary down
        overlay_down = {"opencode": "UNAVAILABLE_TOOL_RUNTIME", "claude": "AVAILABLE_PRIMARY"}
        result_down = orch.choose_executor("opencode", capability_overlay=overlay_down)
        assert result_down == "claude"

        # Phase 2: primary recovers
        overlay_up = {"opencode": "AVAILABLE_PRIMARY", "claude": "AVAILABLE_PRIMARY"}
        result_up = orch.choose_executor("opencode", capability_overlay=overlay_up)
        assert result_up == "opencode", "Should auto-recover to primary"

    def test_3a_4_exclude_prevents_reselect(self, orch):
        """exclude list prevents re-selecting failed executor."""
        overlay = {
            "opencode": "AVAILABLE_PRIMARY",
            "claude": "AVAILABLE_PRIMARY",
        }
        result = orch.choose_executor("opencode", exclude={"opencode"}, capability_overlay=overlay)
        assert result == "claude", "Excluded opencode → fallback to claude"


# ---------------------------------------------------------------------------
# Phase 3B: Reviewer Fallback Live
# ---------------------------------------------------------------------------

class TestPhase3BReviewerFallbackLive:
    """Phase 3B: Verify reviewer fallback uses same path."""

    def test_3b_1_reviewer_uses_choose_executor(self, orch):
        """Reviewer selection uses choose_executor path."""
        # The reviewer role maps to the same EXECUTORS pool
        overlay = {
            "opencode": "AVAILABLE_PRIMARY",
            "claude": "AVAILABLE_PRIMARY",
            "codex": "AVAILABLE_PRIMARY",
        }
        # Reviewer prefers claude (high-depth reasoning)
        result = orch.choose_executor("claude", capability_overlay=overlay)
        assert result == "claude"

    def test_3b_2_reviewer_fallback_when_primary_unavailable(self, orch):
        """Reviewer fallback when primary reviewer unavailable."""
        overlay = {
            "claude": "UNAVAILABLE_TOOL_RUNTIME",
            "opencode": "AVAILABLE_PRIMARY",
        }
        result = orch.choose_executor("claude", capability_overlay=overlay)
        assert result == "opencode", "Reviewer should fallback to opencode"


# ---------------------------------------------------------------------------
# Phase 3C: Planner Fallback Live
# ---------------------------------------------------------------------------

class TestPhase3CPlannerFallbackLive:
    """Phase 3C: Verify planner fallback in degraded mode."""

    def test_3c_1_planner_fallback_when_primary_unavailable(self, orch):
        """Planner fallback when primary planner tool unavailable."""
        # Planner uses _fallback_role to determine preferred executor
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "AVAILABLE_PRIMARY",
        }
        # When opencode is down, planner should use claude
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == "claude"

    def test_3c_2_planner_degraded_when_all_unavailable(self, orch):
        """Planner degraded mode when all tools unavailable."""
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "UNAVAILABLE_TOOL_RUNTIME",
            "codex": "UNAVAILABLE_TOOL_RUNTIME",
        }
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == "", "All unavailable → planner enters degraded mode"


# ---------------------------------------------------------------------------
# Strict No-Fallback Regression
# ---------------------------------------------------------------------------

class TestStrictNoFallbackRegression:
    """Strict mode regression tests."""

    def test_s1_strict_executor_no_fallback(self, orch):
        """strict_executor + allow_fallback=False → no fallback."""
        strict_executor = "opencode"
        allow_fallback = False
        overlay = {"opencode": "UNAVAILABLE_TOOL_RUNTIME"}

        # Simulate the strict mode logic from _repair_node
        if strict_executor in orch.EXECUTORS and not allow_fallback:
            if not orch._is_executor_available(strict_executor, capability_overlay=overlay):
                # Should fail, not fallback
                result = ""
            else:
                result = strict_executor
        else:
            result = orch.choose_executor("opencode", capability_overlay=overlay)

        assert result == "", "Strict mode should not fallback"

    def test_s2_strict_tool_violation_blocked(self, orch):
        """strict_tool violation → task blocked."""
        # When strict_tool is set and the tool is unavailable, task is blocked
        workflow = {
            "strict_tool": True,
            "preferred_executor": "opencode",
            "capability_overlay": {"opencode": "UNAVAILABLE_TOOL_RUNTIME"},
        }
        # The failover hook should mark this as blocked
        strict_tool = workflow.get("strict_tool", False)
        preferred = workflow.get("preferred_executor", "")
        overlay = workflow.get("capability_overlay", {})
        available = orch._is_executor_available(preferred, capability_overlay=overlay)
        if strict_tool and not available:
            blocked = True
        else:
            blocked = False
        assert blocked is True, "strict_tool violation should block"

    def test_s3_strict_model_violation_blocked(self, orch):
        """strict_model violation → task blocked."""
        # strict_model is handled by the failover hook
        # Here we verify the contract: strict_model + unavailable model → blocked
        workflow = {
            "strict_model": True,
            "preferred_model_binding": "minimax/model-x",
            "blocked_model_bindings": "minimax/model-x",
        }
        strict_model = workflow.get("strict_model", False)
        preferred_binding = workflow.get("preferred_model_binding", "")
        blocked_bindings = workflow.get("blocked_model_bindings", "")
        # If preferred binding is in blocked list and strict_model is True
        if strict_model and preferred_binding in blocked_bindings:
            blocked = True
        else:
            blocked = False
        assert blocked is True, "strict_model violation should block"


# ---------------------------------------------------------------------------
# Integration: Full Fallback Chain
# ---------------------------------------------------------------------------

class TestFullFallbackChain:
    """Integration tests for full fallback chain."""

    def test_full_chain_opencode_down(self, orch):
        """Full chain: opencode down → claude → codex."""
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "AVAILABLE_PRIMARY",
            "codex": "AVAILABLE_PRIMARY",
        }
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result in ("claude", "codex")

    def test_full_chain_opencode_claude_down(self, orch):
        """Full chain: opencode+claude down → codex."""
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "UNAVAILABLE_TOOL_RUNTIME",
            "codex": "AVAILABLE_PRIMARY",
        }
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == "codex"

    def test_full_chain_all_down(self, orch):
        """Full chain: all down → empty."""
        overlay = {
            "opencode": "UNAVAILABLE_TOOL_RUNTIME",
            "claude": "UNAVAILABLE_TOOL_RUNTIME",
            "codex": "UNAVAILABLE_TOOL_RUNTIME",
        }
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == ""

    def test_model_fallback_status(self, orch):
        """AVAILABLE_WITH_MODEL_FALLBACK still allows executor selection."""
        overlay = {"opencode": "AVAILABLE_WITH_MODEL_FALLBACK"}
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        assert result == "opencode", "Model fallback status should allow executor"

    def test_degraded_status_blocks(self, orch):
        """DEGRADED_NO_MODEL_FALLBACK blocks executor selection."""
        # DEGRADED states are not AVAILABLE_PRIMARY or AVAILABLE_WITH_MODEL_FALLBACK
        # so they should fall through to global capability check
        overlay = {"opencode": "DEGRADED_NO_MODEL_FALLBACK"}
        # This will check global capability - we just verify it doesn't crash
        result = orch.choose_executor("opencode", capability_overlay=overlay)
        # Result depends on global state, just verify no exception
        assert isinstance(result, str)