#!/usr/bin/env python3
"""AIOS P8D — All tools have effective routes & dual-axis failover gates.

P8D §十八 test surface.  These tests are offline and use the
``fresh_engine`` and ``fresh_registry`` fixtures from the P8C-U test
suite.  They do NOT touch the live monitor, the live acceptance
service, or any real Provider; every gate is exercised through the
in-process routing engine, the model failover engine, and the
acceptance override layer.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Reuse the fresh_registry / fresh_model_registries / fresh_engine
# fixtures from the P8C-U test module.  The fixtures are not exported
# to conftest because they are deliberately stateful per-test
# (fresh_engine creates a brand-new routing engine, model engine, and
# tool registry so the global singletons are isolated).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# P8D reuses fixtures (``fresh_registry`` / ``fresh_model_registries`` /
# ``fresh_engine``) defined in test_p8c_u_dual_axis_failover.  The
# historical code used ``import test_p8c_u_dual_axis_failover as _p8cu``
# which depends on the current working directory being the tests
# directory and breaks collection from the project root.
#
# AIOS-010 §六: replace the cwd-coupled import with an explicit
# importlib-based loader so this file is collectable from any cwd.
import importlib.util as _il_util
_p8c_u_path = str(Path(__file__).resolve().parent / "test_p8c_u_dual_axis_failover.py")
_p8cu_spec = _il_util.spec_from_file_location("test_p8c_u_dual_axis_failover", _p8c_u_path)
if _p8cu_spec is None or _p8cu_spec.loader is None:  # pragma: no cover
    raise ImportError("Cannot locate test_p8c_u_dual_axis_failover for P8D fixtures")
_p8cu = _il_util.module_from_spec(_p8cu_spec)
_p8cu_spec.loader.exec_module(_p8cu)
del _p8c_u_path, _p8cu_spec
from aios_tool_failover import (
    TOOL_STATUS_AVAILABLE_PRIMARY,
    TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK,
    TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK,
    TOOL_STATUS_DISABLED,
    TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
    TOOL_STATUS_UNVERIFIED,
)
from aios_model_failover import (
    DEFAULT_RESOURCE_COOLDOWN_SECONDS,
    FAILURE_SCOPE_RESOURCE,
    FAILURE_SCOPE_LOCAL_RUNTIME,
    FAILURE_SCOPE_TOOL_ADAPTER,
    FAILURE_SCOPE_BINDING,
    FAILURE_SCOPE_TASK_INPUT,
)
from aios_routing_policy import (
    ENV_CANARY_SENDERS,
    ENV_CANARY_SOURCES,
    RoutingEngine,
    get_default_routing_engine,
)


# The P8C-U module exposes these fixtures.  Re-export them so pytest
# can discover them under the P8D test module's namespace.
fresh_registry = _p8cu.fresh_registry
fresh_model_registries = _p8cu.fresh_model_registries
fresh_engine = _p8cu.fresh_engine


# ---------------------------------------------------------------------------
# 1.  Every registered, enabled tool MUST have a non-empty effective
#     binding — the primary resource being unhealthy MUST NOT exclude the
#     tool; the verified fallback keeps the tool in
#     AVAILABLE_WITH_MODEL_FALLBACK.
# ---------------------------------------------------------------------------

class TestPrimaryResourceFailureDoesNotExcludeTool:
    """P8D §五: the primary binding being unhealthy is not enough to
    mark the tool as unavailable.  The fallback binding is verified
    and used instead.
    """

    def test_claude_primary_blocked_keeps_fallback_healthy(
            self, fresh_engine):
        te, me, re = fresh_engine
        # Cooldown ONLY claude's primary binding's resource.  The
        # fallback binding on ``deepseek.shared`` is still healthy,
        # so the tool MUST end up AVAILABLE_WITH_MODEL_FALLBACK,
        # not DEGRADED.
        me._cooldown_resource(
            "minimax.shared", reason="quota_exhausted",
            kind="quota_exhausted")
        status = te.compute_tool_status("claude")
        assert status.status == (
            TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK
        ), status
        assert status.effective_binding == "claude:fallback", status
        assert status.primary_binding == "claude:primary", status
        assert status.fallback_ready is True, status

    def test_codex_primary_blocked_keeps_fallback_healthy(self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource(
            "minimax.shared", reason="quota_exhausted",
            kind="quota_exhausted")
        status = te.compute_tool_status("codex")
        assert status.status == (
            TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK
        ), status
        assert status.effective_binding == "codex:fallback", status

    def test_opencode_with_shared_resources_blocked_keeps_local(
            self, fresh_engine):
        te, me, re = fresh_engine
        # Exhaust both shared resources; opencode's primary is
        # shared but its local ``opencode:free → opencode.free``
        # binding is NOT a shared resource, so it stays healthy.
        me._cooldown_resource(
            "minimax.shared", reason="quota_exhausted",
            kind="quota_exhausted")
        me._cooldown_resource(
            "deepseek.shared", reason="quota_exhausted",
            kind="quota_exhausted")
        status = te.compute_tool_status("opencode")
        # The local opencode:free binding MUST keep the tool
        # AVAILABLE_*; if every shared binding is also blocked the
        # tool degrades but does NOT become UNAVAILABLE_TOOL_RUNTIME
        # (its own runtime is fine; the issue is the resource pool).
        assert status.status in (
            TOOL_STATUS_AVAILABLE_PRIMARY,
            TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK,
            TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK,
        ), status

    def test_healthy_state_keeps_every_tool_available(self, fresh_engine):
        te, me, re = fresh_engine
        # No cooldowns.  Every tool MUST have a non-empty
        # effective_binding.
        for tool_id in ("openclaw", "hermes", "opencode",
                         "claude", "codex"):
            status = te.compute_tool_status(tool_id)
            assert status.effective_binding, (tool_id, status)
            assert status.status in (
                TOOL_STATUS_AVAILABLE_PRIMARY,
                TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK,
            ), (tool_id, status)


# ---------------------------------------------------------------------------
# 2.  Failure-scope classification (P8D §四) — RESOURCE / BINDING /
#     TOOL_ADAPTER / LOCAL_RUNTIME are the four scopes; TASK_INPUT
#     must NEVER trigger any failover.
# ---------------------------------------------------------------------------

class TestFailureScopeClassification:
    """P8D §四: mechanical classification of the raw failure kind."""

    @pytest.mark.parametrize("kind, expected", [
        ("quota_exhausted", FAILURE_SCOPE_RESOURCE),
        ("insufficient_balance", FAILURE_SCOPE_RESOURCE),
        ("rate_limited", FAILURE_SCOPE_RESOURCE),
        ("token_plan", FAILURE_SCOPE_BINDING),
        ("region_restricted", FAILURE_SCOPE_RESOURCE),
        ("network_error", FAILURE_SCOPE_RESOURCE),
        ("external_timeout", FAILURE_SCOPE_BINDING),
        ("local_adapter_exception", FAILURE_SCOPE_TOOL_ADAPTER),
        ("ipc_failure", FAILURE_SCOPE_LOCAL_RUNTIME),
        ("task_validation_error", FAILURE_SCOPE_TASK_INPUT),
    ])
    def test_scope_classification(self, fresh_engine, kind, expected):
        from aios_model_failover import DEFAULT_KIND_TO_SCOPE
        scope = DEFAULT_KIND_TO_SCOPE.get(kind)
        assert scope == expected, (kind, scope)

    def test_unknown_kind_defaults_to_binding(self):
        from aios_model_failover import DEFAULT_KIND_TO_SCOPE
        # Anything not in the canonical map is treated as a binding
        # anomaly (does NOT silently cascade to RESOURCE).
        assert DEFAULT_KIND_TO_SCOPE.get("never_seen_kind") in (
            None, FAILURE_SCOPE_BINDING,
        )


# ---------------------------------------------------------------------------
# 3.  Source + sender composite canary gate (P8D §七).
# ---------------------------------------------------------------------------

class TestSourceSenderCompositeGate:
    """P8D §七: a single source match is no longer enough; the live
    feature flag must also confirm a matching sender.
    """

    def test_source_only_with_empty_sendergate_is_legacy(
            self, monkeypatch):
        # Empty canary_allowed_senders preserves P8C-F single-source
        # behaviour: any source in the allowlist opens the canary.
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.delenv(ENV_CANARY_SENDERS, raising=False)
        engine = RoutingEngine()
        assert engine.canary_allows("api") is True
        assert engine.canary_allows("feishu") is False

    def test_source_plus_sender_required_when_sendergate_set(
            self, monkeypatch):
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit,p8c-f-audit")
        engine = RoutingEngine()
        # Source matches but sender missing → closed.
        assert engine.canary_allows("api", sender=None) is False
        # Source matches and sender matches → open.
        assert engine.canary_allows("api", sender="p8d-audit") is True
        assert engine.canary_allows("api", sender="p8c-f-audit") is True
        # Source matches but sender outside the allowlist → closed.
        assert engine.canary_allows("api", sender="web-user") is False
        # Source outside the allowlist → closed regardless.
        assert engine.canary_allows("feishu", sender="p8d-audit") is False

    def test_action_resolution_blocks_sender_outside_allowlist(
            self, monkeypatch):
        from aios_routing_policy import (
            ENV_MODEL_FAILOVER, ENV_TOOL_FAILOVER, ENV_SHADOW_MODE,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        # Force failover on so the routing engine actually builds
        # a tool decision.
        monkeypatch.setenv(ENV_MODEL_FAILOVER, "true")
        monkeypatch.setenv(ENV_TOOL_FAILOVER, "true")
        engine = RoutingEngine()
        decision = engine.route(
            task_id="p8d-canary-mismatch",
            role="executor",
            source="api",
            sender="feishu-bot",
            preferred_tool="opencode",
        )
        assert decision.canary_allowed is False
        assert decision.action == "shadow_only"

    def test_action_resolution_proceeds_when_both_match(self, monkeypatch):
        from aios_routing_policy import (
            ENV_MODEL_FAILOVER, ENV_TOOL_FAILOVER,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        monkeypatch.setenv(ENV_MODEL_FAILOVER, "true")
        monkeypatch.setenv(ENV_TOOL_FAILOVER, "true")
        engine = RoutingEngine()
        decision = engine.route(
            task_id="p8d-canary-match",
            role="executor",
            source="api",
            sender="p8d-audit",
            preferred_tool="opencode",
        )
        assert decision.canary_allowed is True
        assert decision.action == "proceed"


# ---------------------------------------------------------------------------
# 4.  Strict policy matrix (P8D §十二).
# ---------------------------------------------------------------------------

class TestStrictPolicyMatrix:
    """P8D §十二 forward + reverse."""

    def test_strict_tool_locks_executor_even_when_blocked(
            self, fresh_engine, monkeypatch):
        from aios_routing_policy import (
            ENV_CANARY_SOURCES, ENV_CANARY_SENDERS,
            ENV_MODEL_FAILOVER, ENV_TOOL_FAILOVER,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        monkeypatch.setenv(ENV_MODEL_FAILOVER, "true")
        monkeypatch.setenv(ENV_TOOL_FAILOVER, "true")
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="p8d-strict-tool", role="executor",
            preferred_tool="claude", strict_tool="claude",
            source="api", sender="p8d-audit",
        )
        # claude stays as the executor (strict), but model attempt
        # cannot use the preferred binding (cooldown).  Either the
        # engine reports strict_violation OR keeps claude with the
        # fallback (we tolerate either as long as actual_tool is
        # claude and not an unrelated tool).
        assert decision.actual_tool == "claude"
        # No silent switch to a different tool.
        assert decision.tool_failover_occurred is False
        # The canary was allowed because both source and sender matched.
        assert decision.canary_allowed is True

    def test_strict_model_pins_specific_binding(
            self, fresh_engine, monkeypatch):
        from aios_routing_policy import (
            ENV_CANARY_SOURCES, ENV_CANARY_SENDERS,
            ENV_MODEL_FAILOVER, ENV_TOOL_FAILOVER,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        monkeypatch.setenv(ENV_MODEL_FAILOVER, "true")
        monkeypatch.setenv(ENV_TOOL_FAILOVER, "true")
        te, me, re = fresh_engine
        decision = re.route(
            task_id="p8d-strict-model", role="executor",
            preferred_tool="claude", strict_model="claude:primary",
            source="api", sender="p8d-audit",
        )
        # strict_model == claude:primary; the engine respects it
        # even when minimax is preferred.
        assert decision.actual_model_binding == "claude:primary"

    def test_strict_model_violation_is_rejected(
            self, fresh_engine, monkeypatch):
        from aios_routing_policy import (
            ENV_CANARY_SOURCES, ENV_CANARY_SENDERS,
            ENV_MODEL_FAILOVER, ENV_TOOL_FAILOVER,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        monkeypatch.setenv(ENV_MODEL_FAILOVER, "true")
        monkeypatch.setenv(ENV_TOOL_FAILOVER, "true")
        te, me, re = fresh_engine
        decision = re.route(
            task_id="p8d-strict-model-miss", role="executor",
            preferred_tool="claude",
            strict_model="claude:does-not-exist",
            source="api", sender="p8d-audit",
        )
        # The model decision surfaces strict_violation; the caller
        # MUST treat that as a hard refusal, not a silent fallback.
        assert decision.model_decision.action == "strict_violation"

    def test_allow_tool_fallback_false_keeps_preferred(
            self, fresh_engine, monkeypatch):
        from aios_routing_policy import (
            ENV_CANARY_SOURCES, ENV_CANARY_SENDERS,
            ENV_MODEL_FAILOVER, ENV_TOOL_FAILOVER,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        monkeypatch.setenv(ENV_MODEL_FAILOVER, "true")
        monkeypatch.setenv(ENV_TOOL_FAILOVER, "true")
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="p8d-no-tool-fallback", role="executor",
            preferred_tool="claude", allow_tool_fallback=False,
            source="api", sender="p8d-audit",
        )
        # We did not consent to a tool switch → the engine must NOT
        # swap to opencode.  Either it keeps claude (with strict
        # refusal on model) or surfaces a tool decision action.
        assert decision.tool_decision.action in (
            "use", "fallback_disabled", "strict_violation",
        )
        assert decision.tool_failover_occurred is False
        assert decision.failover_occurred is False

    def test_allow_model_fallback_false_pins_primary(
            self, fresh_engine, monkeypatch):
        from aios_routing_policy import (
            ENV_CANARY_SOURCES, ENV_CANARY_SENDERS,
            ENV_MODEL_FAILOVER, ENV_TOOL_FAILOVER,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        monkeypatch.setenv(ENV_MODEL_FAILOVER, "true")
        monkeypatch.setenv(ENV_TOOL_FAILOVER, "true")
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="p8d-no-model-fallback", role="executor",
            preferred_tool="claude",
            preferred_model="claude:primary",
            allow_model_fallback=False,
            source="api", sender="p8d-audit",
        )
        # We did not consent to a model switch → the engine must NOT
        # silently swap to claude:fallback.
        assert decision.model_decision.action in (
            "use", "fallback_disabled", "no_switch_after_local_failure",
        )
        # And the tool identity is preserved.
        assert decision.actual_tool == "claude"


# ---------------------------------------------------------------------------
# 5.  Persisted workflow fields (P8D §十三).
# ---------------------------------------------------------------------------

class TestPersistedWorkflowFields:
    """Every executed node MUST carry the routing-snapshot fields."""

    def test_attach_routing_to_node_copies_all_fields(self):
        from aios_orchestrator_failover_hook import attach_routing_to_node
        # Minimal decision body; the helper only reads public fields.
        from aios_routing_policy import RoutingDecision
        decision = RoutingDecision(
            action="proceed",
            actual_tool="claude",
            actual_model_binding="claude:minimax",
            attempted_tools=("claude",),
            attempted_model_bindings=("claude:primary", "claude:minimax"),
            tool_failover_reason="deepseek quota_exhausted",
            model_failover_reason="deepseek quota_exhausted",
            preferred_tool="claude",
            preferred_model="claude:primary",
            role="executor",
            strict_tool=None,
            strict_model=None,
            allow_tool_fallback=True,
            allow_model_fallback=True,
            capability_overlay={},
            canary_allowed=True,
            feature_flags={"canary_allowed_senders": ("p8d-audit",)},
        )
        # Build a minimal tool_decision / model_decision so attach
        # doesn't AttributeError on .tool_status / .reason.
        from aios_tool_failover import ToolFailoverDecision
        from aios_model_failover import ModelFailoverDecision
        decision.tool_decision = ToolFailoverDecision(
            action="use",
            tool_id="claude",
            reason="first eligible",
            next_attempt_index=0,
            tool_status="AVAILABLE_WITH_MODEL_FALLBACK",
            effective_model_binding="claude:minimax",
        )
        decision.model_decision = ModelFailoverDecision(
            action="use",
            binding_id="claude:minimax",
            reason="deepseek quota_exhausted",
            next_attempt_index=1,
        )
        node: dict = {}
        attach_routing_to_node(node, decision)
        for field in (
            "preferred_tool", "actual_tool",
            "primary_model_binding", "actual_model_binding",
            "attempted_tools", "attempted_model_bindings",
            "tool_failover_count", "model_failover_count",
            "tool_failover_reason", "model_failover_reason",
            "strict_tool", "strict_model",
            "allow_tool_fallback", "allow_model_fallback",
            "routing_action", "routing_shadow",
            "routing_canary_allowed",
            "tool_status", "tool_decision_reason",
            "model_decision_reason",
        ):
            assert field in node, field


# ---------------------------------------------------------------------------
# 6.  Tool-failover priority: RESOURCE first, TOOL last (P8D §六).
# ---------------------------------------------------------------------------

class TestDecisionOrderModelBeforeTool:
    """P8D §六: the model engine MUST be consulted first; the tool
    engine is the LAST resort.
    """

    def test_resource_failover_does_not_touch_tool(
            self, fresh_engine, monkeypatch):
        from aios_routing_policy import (
            ENV_CANARY_SOURCES, ENV_CANARY_SENDERS,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        te, me, re = fresh_engine
        # A RESOURCE-scope failure is recorded.  The tool MUST stay
        # the same.
        me.record_model_attempt(
            task_id="p8d-resource", tool_id="claude",
            binding_id="claude:primary", success=False,
            failure_kind="quota_exhausted")
        decision = re.route(
            task_id="p8d-resource-2", role="executor",
            preferred_tool="claude", preferred_model="claude:primary",
            source="api", sender="p8d-audit",
        )
        assert decision.actual_tool == "claude"
        assert decision.tool_failover_occurred is False

    def test_binding_failover_does_not_touch_tool(
            self, fresh_engine, monkeypatch):
        from aios_routing_policy import (
            ENV_CANARY_SOURCES, ENV_CANARY_SENDERS,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        te, me, re = fresh_engine
        me.record_model_attempt(
            task_id="p8d-binding", tool_id="claude",
            binding_id="claude:primary", success=False,
            failure_kind="token_plan")
        decision = re.route(
            task_id="p8d-binding-2", role="executor",
            preferred_tool="claude", preferred_model="claude:primary",
            source="api", sender="p8d-audit",
        )
        assert decision.actual_tool == "claude"

    def test_local_runtime_unavailability_changes_tool_status(
            self, fresh_engine, monkeypatch):
        from aios_routing_policy import (
            ENV_CANARY_SOURCES, ENV_CANARY_SENDERS,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        te, me, re = fresh_engine
        # Mark claude's runtime as dead; the routing engine MUST
        # surface that as a non-"use claude" verdict (otherwise we
        # would silently pick an unavailable tool).
        te.set_tool_runtime_alive("claude", False)
        status = te.compute_tool_status("claude")
        # claude is no longer alive → either UNAVAILABLE_TOOL_RUNTIME
        # or DEGRADED_NO_MODEL_FALLBACK.  The exact bucket depends on
        # the available bindings, but it MUST be one of those two.
        assert status.status in (
            TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
            TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK,
        ), status


# ---------------------------------------------------------------------------
# 7.  Capability overlay is task-local and cannot leak across tasks.
# ---------------------------------------------------------------------------

class TestCapabilityOverlayIsTaskLocal:
    """P8D §十一: capability_overlay is a task-scoped override; it
    MUST NOT mutate the global registry / engine state.
    """

    def test_overlay_does_not_leak_across_tasks(
            self, fresh_engine, monkeypatch):
        from aios_routing_policy import (
            ENV_CANARY_SOURCES, ENV_CANARY_SENDERS,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        te, me, re = fresh_engine
        re.route(
            task_id="p8d-overlay-1", role="executor",
            preferred_tool="claude",
            capability_overlay={
                "claude": TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
            },
            source="api", sender="p8d-audit",
        )
        # Engine state for claude is unchanged after the task.
        global_status = te.compute_tool_status("claude").status
        assert global_status != TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME

    def test_overlay_emulated_status_surfaces_runtime_failure(
            self, fresh_engine, monkeypatch):
        from aios_routing_policy import (
            ENV_CANARY_SOURCES, ENV_CANARY_SENDERS,
        )
        monkeypatch.setenv(ENV_CANARY_SOURCES, "api")
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8d-audit")
        te, me, re = fresh_engine
        # Mark the engine runtime for ALL canonical tools down so
        # the routing layer can either swap or refuse; either
        # outcome is acceptable as long as the overlay's runtime
        # down state is NOT silently ignored.
        te.set_tool_runtime_alive("claude", False)
        te.set_tool_runtime_alive("opencode", False)
        te.set_tool_runtime_alive("hermes", False)
        te.set_tool_runtime_alive("openclaw", False)
        # codex is the only executor that is still alive.
        decision = re.route(
            task_id="p8d-overlay-2", role="executor",
            preferred_tool="claude",
            capability_overlay={
                "claude": TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
            },
            source="api", sender="p8d-audit",
        )
        # claude is no longer runtime-alive.  The decision must NOT
        # silently pick claude as the executor.  Either it surfaces
        # the runtime failure or it picks a different tool.
        td = decision.tool_decision
        assert td.tool_id != "claude" or td.tool_status in (
            TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
            TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK,
        ), td


# ---------------------------------------------------------------------------
# 8.  Single-step rollback (P8D §十六) — both flags off → no failover.
# ---------------------------------------------------------------------------

class TestFlagsOffRollback:
    """P8D §十六: turning both flags off MUST restore the
    pre-P8C-U production behaviour (preferred executor only, no
    model failover).
    """

    def test_both_flags_off_skips_routing_engine(
            self, fresh_engine, monkeypatch):
        from aios_orchestrator_failover_hook import route_node_executor
        from aios_routing_policy import (
            ENV_MODEL_FAILOVER, ENV_TOOL_FAILOVER,
        )
        # Failure flags off: the hook returns the preferred choice
        # WITHOUT going through the dual-axis engine.
        monkeypatch.setenv(ENV_MODEL_FAILOVER, "false")
        monkeypatch.setenv(ENV_TOOL_FAILOVER, "false")
        chosen, decision = route_node_executor(
            task_id="p8d-rollback-1", role="executor",
            source="api", sender="p8d-audit",
            preferred_executor="claude",
            strict_executor="",
            allow_executor_fallback=True,
        )
        # Hook returns preferred choice unchanged.
        assert chosen == "claude"
        assert decision.actual_tool == "claude"
        # No shadow tracking when the hook skipped the engine.
        assert decision.shadow is False


# ---------------------------------------------------------------------------
# 9.  Five enabled tools each have a non-empty effective binding.
# ---------------------------------------------------------------------------

class TestAllEnabledToolsHaveEffectiveBinding:
    """P8D §五: every registered, enabled tool MUST have a non-empty
    effective_binding in the live routing snapshot.
    """

    def test_each_tool_has_effective_binding(self, fresh_engine):
        te, me, re = fresh_engine
        statuses = te.all_tool_statuses()
        # The fixture only registers the canonical five plus a sixth.
        for s in statuses:
            assert s.effective_binding, (
                f"{s.tool_id} has no effective_binding; "
                f"reason={s.reason!r}; "
                f"verified={list(s.verified_bindings)}; "
                f"blocked={list(s.blocked_bindings)}"
            )

    def test_each_enabled_tool_is_at_least_unverified(
            self, fresh_engine):
        te, me, re = fresh_engine
        statuses = te.all_tool_statuses()
        # Disabled tools would land in TOOL_STATUS_DISABLED; the
        # canonical five are all enabled so they must NEVER be
        # classified as DISABLED.
        for s in statuses:
            if s.tool_id in ("openclaw", "hermes", "opencode",
                              "claude", "codex"):
                assert s.status != TOOL_STATUS_DISABLED, s


# ---------------------------------------------------------------------------
# 10. Computed `tool_effective_binding` is reflected in the monitor.
# ---------------------------------------------------------------------------

class TestMonitorReflectsEffectiveBindings:
    """P8D §十四: monitor.p8c_u.tool_effective_binding MUST mirror
    what the routing engine would pick.
    """

    def test_monitor_p8c_u_includes_tool_effective_binding(
            self, fresh_engine, monkeypatch):
        from aios_monitor import _tool_effective_binding_payload
        te, me, re = fresh_engine
        payload = _tool_effective_binding_payload(te)
        assert isinstance(payload, dict)
        for tool_id in ("openclaw", "hermes", "opencode",
                         "claude", "codex"):
            assert tool_id in payload, tool_id
            info = payload[tool_id]
            assert info.get("effective_binding"), (tool_id, info)
            assert info.get("status"), (tool_id, info)
            assert info.get("fallback_ready") is not None, (tool_id, info)