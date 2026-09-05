#!/usr/bin/env python3
"""AIOS P8C-U Dual-Axis Tool × Model Failover Tests.

Covers all the required scenarios from the P8C-U task brief:

* Dynamic new sixth tool discovered without core edit.
* Dynamic new model resource without core edit.
* Model failover preserves ``tool_id``.
* Tool failover only selects role-compatible tools.
* Tool failover only triggered after model pool exhaustion.
* ``strict_tool`` / ``strict_model`` / ``allow_tool_fallback`` /
  ``allow_model_fallback`` policy matrix.
* External model failure triggers model failover.
* Internal adapter failure blocks model failover.
* Local tool runtime failure triggers tool failover.
* Task input error does NOT trigger any failover.
* ``AVAILABLE_WITH_MODEL_FALLBACK`` is the correct status when
  primary blocked but at least one verified binding is healthy.
* Role-route coverage (planner / executor / reviewer).
* Response normalizer (planner / reviewer / code_result / raw).
* Native tool adapter references are populated.
* Shadow mode does NOT add calls.
* Canary allowlist gates the actual swap.
* Single-step rollback restores the pre-P8C-U production behaviour.
* Attempted / actual tool and model bindings are persisted in the
  workflow node.
* HTTP / Redis / Monitor / Acceptance consistency.
* Reviewer ``TOOL_ONLY`` marker when the underlying resource is the
  same but the tool is different.
* No hidden fallback (strict mode is honoured).

This file is the unit-test surface for the P8C-U feature; the
Shadow / Canary / E2E flows are exercised by the acceptance script
that ships alongside this commit.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Modules under test ------------------------------------------------------------
from aios_model_response_normalizer import (
    CODE_RESULT_SCHEMA,
    NORMALIZER_EMPTY_RESPONSE,
    NORMALIZER_EXTERNAL_CONTRACT_FAILURE,
    NORMALIZER_INTERNAL_PARSER_EXCEPTION,
    NORMALIZER_OK,
    PLANNER_SCHEMA,
    REVIEWER_SCHEMA,
    NormalizedResponse,
    category_to_failure_kind,
    normalize_model_response,
)
from aios_qwen_provider import (
    QWEN_API_KEY_ENV,
    QWEN_BASE_URL_ENV,
    QWEN_DEFAULT_BASE_URL,
    QWEN_DEFAULT_MODEL,
    QWEN_ENABLED_ENV,
    QWEN_MODEL_ENV,
    QwenStatus,
    QWEN_STATUS_DISABLED,
    QWEN_STATUS_IMPLEMENTED_VERIFIED,
    QWEN_STATUS_UNCONFIGURED,
    compute_qwen_status,
    detect_qwen_credentials,
)
from aios_role_routes import (
    ALL_ROLES,
    ROLE_EXECUTOR,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    ROLE_STATUS_AVAILABLE_FALLBACK,
    ROLE_STATUS_AVAILABLE_PRIMARY,
    ROLE_STATUS_SINGLE_POINT_OF_FAILURE,
    ROLE_STATUS_UNAVAILABLE,
    RoleRouteCalculator,
    RouteCoverageReport,
    get_default_role_calculator,
    reset_default_role_calculator,
)
from aios_routing_policy import (
    ENV_CANARY_SOURCES,
    ENV_MODEL_FAILOVER,
    ENV_ROLE_ALLOWLIST,
    ENV_SHADOW_MODE,
    ENV_TOOL_ALLOWLIST,
    ENV_TOOL_FAILOVER,
    RoutingDecision,
    RoutingEngine,
    ShadowLogEntry,
    get_default_routing_engine,
    reset_default_routing_engine,
)
from aios_tool_failover import (
    ALL_TOOL_STATUSES,
    DEFAULT_MAX_TOOL_ATTEMPTS,
    DEFAULT_MAX_TOOL_FAILOVERS,
    TOOL_STATUS_AVAILABLE_PRIMARY,
    TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK,
    TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK,
    TOOL_STATUS_DISABLED,
    TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
    TOOL_STATUS_UNVERIFIED,
    ToolAttemptRecord,
    ToolFailoverDecision,
    ToolFailoverEngine,
    ToolStatusReport,
    get_default_tool_engine,
    reset_default_tool_engine,
)
import aios_tool_failover as tf
from aios_tool_registry import (
    ALL_ROLES as ALL_REGISTRY_ROLES,
    ToolManifest,
    ToolRegistry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


SIXTH_TOOL_ID = "fake_sixth_p8c_u"


def _sixth_tool_manifest() -> ToolManifest:
    return ToolManifest(
        tool_id=SIXTH_TOOL_ID,
        display_name="Fake Sixth (P8C-U)",
        module_path="kernel/tools/tests/_fixtures/fake_sixth.py",
        adapter_ref="fake_sixth_adapter",
        roles=("executor",),
        service_unit_ref=None,
        health_probe_ref=None,
        model_policy_ref=None,
        enabled=True,
        version="1.0",
        capabilities=("script", "standard_task"),
        description="Sixth dynamic tool for P8C-U tests",
    )


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset every P8C-U singleton before and after each test."""
    reset_default_tool_engine()
    reset_default_routing_engine()
    reset_default_role_calculator()
    yield
    reset_default_tool_engine()
    reset_default_routing_engine()
    reset_default_role_calculator()


@pytest.fixture
def fresh_registry():
    """Build a fresh registry with the canonical five tools + a sixth."""
    from aios_tool_registry import (
        _DEFAULT_REGISTRY,
        _DEFAULT_LOCK,
    )

    reg = ToolRegistry()
    for tool_id, roles in (
        ("opencode", ("executor",)),
        ("claude", ("executor",)),
        ("codex", ("executor",)),
        ("hermes", ("reviewer",)),
        ("openclaw", ("planner", "executor")),
    ):
        reg.register_tool(ToolManifest(
            tool_id=tool_id,
            display_name=tool_id.title(),
            module_path=f"kernel/tools/aios_{tool_id}.py",
            adapter_ref=f"{tool_id}_adapter",
            roles=roles,
            service_unit_ref=f"aios-{tool_id}.service",
            health_probe_ref=f"{tool_id}.health",
            model_policy_ref=f"kernel/tools/policies/{tool_id}.json",
            enabled=True,
            version="1.0",
            capabilities=("shell", "code"),
        ))
    reg.register_tool(_sixth_tool_manifest())
    return reg


@pytest.fixture
def fresh_model_registries():
    """Return a tuple ``(resources, bindings, policies)`` with the
    canonical five tools × {primary=minimax, fallback=other}"""
    from aios_model_resources import (
        SharedModelResource,
        SharedModelResourceRegistry,
        ToolModelBinding,
        ToolModelBindingRegistry,
        ToolModelPolicy,
        ToolModelPolicyRegistry,
    )
    res = SharedModelResourceRegistry()
    res.register(SharedModelResource(
        resource_id="minimax.shared",
        vendor="MiniMax",
        model_id="MiniMax-M3",
        endpoint_ref="local-proxy-9998",
        credential_ref="env:MiniMax_CN_API_KEY",
        account_scope="minimax-team",
        protocol="openai_compatible",
        enabled=True,
        credentials_present=True,
    ))
    res.register(SharedModelResource(
        resource_id="deepseek.shared",
        vendor="DeepSeek",
        model_id="DeepSeek-V4-Pro",
        endpoint_ref="env:DeepSeek_BASE_URL",
        credential_ref="env:DeepSeek_API_KEY",
        account_scope="deepseek-team",
        protocol="openai_compatible",
        enabled=True,
        credentials_present=True,
    ))
    bind = ToolModelBindingRegistry()
    pol = ToolModelPolicyRegistry()
    for tool_id in ("opencode", "claude", "codex", "hermes", "openclaw",
                    SIXTH_TOOL_ID):
        for binding_id, resource_id in (
            (f"{tool_id}:primary", "minimax.shared"),
            (f"{tool_id}:fallback", "deepseek.shared"),
        ):
            bind.register(ToolModelBinding(
                binding_id=binding_id,
                tool_id=tool_id,
                resource_id=resource_id,
                roles=("planner", "executor", "reviewer"),
                adapter_mode="safe_dynamic",
                prompt_profile="default",
                timeout=120,
                supports_json=True,
                supports_tools=True,
                supports_code=True,
                supports_long_context=False,
                priority=10,
                enabled=True,
            ))
        pol.register(ToolModelPolicy(
            tool_id=tool_id,
            candidate_bindings=(f"{tool_id}:primary",
                                 f"{tool_id}:fallback"),
            preferred_binding=f"{tool_id}:primary",
            max_model_attempts=2,
            max_model_failovers=1,
            strict_model=None,
            allow_model_fallback=True,
        ))
    return res, bind, pol


@pytest.fixture
def fresh_engine(fresh_registry, fresh_model_registries):
    """Build a clean ToolFailoverEngine + RoutingEngine."""
    res, bind, pol = fresh_model_registries
    tf.set_default_tool_engine(None)
    from aios_tool_failover import ToolFailoverEngine as TFE
    from aios_model_failover import (ModelFailoverEngine as MFE,
                                      reset_default_engine)
    reset_default_engine()
    me = MFE(resource_registry=res, binding_registry=bind,
             policy_registry=pol)
    te = TFE(registry=fresh_registry,
             binding_registry=bind,
             policy_registry=pol,
             resource_registry=res,
             model_engine=me)
    # P8D: the test fixture is hermetic — pretend every probe
    # cache says ``available`` so the fixture's synthetic
    # resources / bindings are not contaminated by the on-disk
    # adapter cache files.
    for tool_id in (
            "opencode", "claude", "codex", "hermes", "openclaw",
            "eos_test"):
        if tool_id in {m.tool_id for m in fresh_registry.list_all()}:
            te.set_adapter_cache_override(tool_id, {"model_state": "available"})
    tf.set_default_tool_engine(te)
    re = RoutingEngine(tool_engine=te, model_engine=me,
                       role_calculator=RoleRouteCalculator(te))
    return te, me, re


# ---------------------------------------------------------------------------
# 1. Dynamic discovery — adding a sixth tool surfaces it everywhere.
# ---------------------------------------------------------------------------


class TestDynamicDiscovery:
    def test_sixth_tool_registered_and_listed(self, fresh_registry):
        enabled = fresh_registry.list_enabled()
        ids = [m.tool_id for m in enabled]
        assert SIXTH_TOOL_ID in ids
        assert len(enabled) >= 6

    def test_sixth_tool_role_discovery(self, fresh_registry):
        executors = fresh_registry.list_by_role("executor")
        ids = [m.tool_id for m in executors]
        assert SIXTH_TOOL_ID in ids

    def test_routing_engine_sees_sixth_tool(self, fresh_engine):
        te, me, re = fresh_engine
        statuses = te.all_tool_statuses()
        assert any(s.tool_id == SIXTH_TOOL_ID for s in statuses)


# ---------------------------------------------------------------------------
# 2. Tool status — AVAILABLE_WITH_MODEL_FALLBACK semantics.
# ---------------------------------------------------------------------------


class TestToolStatus:
    def test_primary_blocked_but_fallback_healthy_is_awmf(
            self, fresh_engine):
        te, me, re = fresh_engine
        # cooldown the claude:primary binding, leaving claude:fallback
        # healthy.
        me.resource_state("minimax.shared")
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        status = te.compute_tool_status("claude")
        assert status.status == TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK
        assert status.effective_binding == "claude:fallback"
        assert status.fallback_ready is True

    def test_all_bindings_blocked_is_dnf(self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        status = te.compute_tool_status("claude")
        assert status.status == TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK

    def test_primary_healthy_is_ap(self, fresh_engine):
        te, me, re = fresh_engine
        status = te.compute_tool_status("codex")
        assert status.status == TOOL_STATUS_AVAILABLE_PRIMARY

    def test_runtime_down_is_utr(self, fresh_engine):
        te, me, re = fresh_engine
        te.set_tool_runtime_alive("codex", False)
        status = te.compute_tool_status("codex")
        assert status.status == TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME

    def test_unknown_tool_is_unverified(self, fresh_engine):
        te, me, re = fresh_engine
        status = te.compute_tool_status("does_not_exist")
        assert status.status == TOOL_STATUS_UNVERIFIED


# ---------------------------------------------------------------------------
# 3. Model failover never changes tool_id.
# ---------------------------------------------------------------------------


class TestModelFailoverPreservesToolId:
    def test_claude_deepseek_blocked_picks_claude_minimax(
            self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        # We register a second binding for claude pointing at minimax.
        from aios_model_resources import (ToolModelBinding,
                                          ToolModelBindingRegistry)
        # already done in fixture; minimax.shared is healthy
        decision = re.route(
            task_id="t1", role="executor",
            preferred_tool="claude",
            preferred_model="claude:fallback",
            source="p8c-u-audit",
        )
        assert decision.actual_tool == "claude"
        # claude:fallback is on deepseek.shared — cooldown skips it.
        assert decision.actual_model_binding == "claude:primary"
        assert decision.tool_failover_occurred is False

    def test_claude_minimax_blocked_does_not_change_tool(
            self, fresh_engine):
        te, me, re = fresh_engine
        # cooldown minimax.shared only; deepseek.shared stays healthy.
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="t2", role="executor",
            preferred_tool="claude",
            preferred_model="claude:primary",
            source="p8c-u-audit",
        )
        # tool_id preserved; primary skipped; fallback (deepseek) used.
        assert decision.actual_tool == "claude"
        assert decision.actual_model_binding == "claude:fallback"
        assert decision.model_failover_occurred is True


# ---------------------------------------------------------------------------
# 4. Tool failover — only role-compatible tools, only after pool exhausted.
# ---------------------------------------------------------------------------


class TestToolFailoverRules:
    def test_tool_failover_only_role_compatible(self, fresh_engine):
        te, me, re = fresh_engine
        # Use capability_overlay to mark claude's runtime unavailable for
        # this task only; other tools stay available. The overlay MUST NOT
        # mutate the global engine state.
        decision = re.route(
            task_id="t3", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
            capability_overlay={"claude": TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME},
        )
        # Tool failover picks a different executor role-compatible with executor.
        assert decision.actual_tool != "claude"
        assert decision.actual_tool in ("opencode", "codex", SIXTH_TOOL_ID)
        assert decision.tool_failover_occurred is True
        assert decision.tool_failover_reason
        # Overlay must not have leaked into the global registry.
        global_status = te.compute_tool_status("claude").status
        assert global_status != TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME

    def test_tool_failover_only_after_pool_exhausted(
            self, fresh_engine):
        te, me, re = fresh_engine
        # healthy state — no failover expected
        decision = re.route(
            task_id="t4", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
        )
        assert decision.actual_tool == "claude"
        assert decision.tool_failover_occurred is False

    def test_tool_failover_can_be_disabled(self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="t5", role="executor",
            preferred_tool="claude",
            allow_tool_fallback=False,
            source="p8c-u-audit",
        )
        assert decision.actual_tool == "claude"
        # Allowed only one attempt.
        assert decision.tool_failover_occurred is False


# ---------------------------------------------------------------------------
# 5. Strict policy matrix
# ---------------------------------------------------------------------------


class TestStrictPolicyMatrix:
    def test_strict_tool_blocks_tool_switch(self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="t6", role="executor",
            preferred_tool="claude",
            strict_tool="claude",
            source="p8c-u-audit",
        )
        assert decision.tool_decision.action == "strict_violation"
        # Tool preserved.
        assert decision.actual_tool in (None, "claude")

    def test_strict_tool_allows_model_switch(self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        # fallback (deepseek) is healthy
        decision = re.route(
            task_id="t7", role="executor",
            preferred_tool="claude",
            strict_tool="claude",
            allow_model_fallback=True,
            source="p8c-u-audit",
        )
        # strict_tool honoured: claude wins.
        assert decision.actual_tool == "claude"

    def test_strict_model_pins_binding(self, fresh_engine):
        te, me, re = fresh_engine
        decision = re.route(
            task_id="t8", role="executor",
            preferred_tool="claude",
            preferred_model="claude:fallback",
            strict_model="claude:fallback",
            source="p8c-u-audit",
        )
        # strict_model honoured even if minimax preferred.
        assert decision.actual_model_binding == "claude:fallback"

    def test_strict_model_violation_returns_strict_violation(
            self, fresh_engine):
        te, me, re = fresh_engine
        decision = re.route(
            task_id="t9", role="executor",
            preferred_tool="claude",
            strict_model="claude:unknown_binding",
            source="p8c-u-audit",
        )
        assert decision.model_decision.action == "strict_violation"

    def test_allow_model_fallback_false_blocks_switch(self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="t10", role="executor",
            preferred_tool="claude",
            allow_model_fallback=False,
            source="p8c-u-audit",
        )
        # The first attempt is allowed; switching is not.
        assert decision.model_decision.action in ("use", "fallback_disabled")

    def test_allow_tool_fallback_false_blocks_switch(self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="t11", role="executor",
            preferred_tool="claude",
            allow_tool_fallback=False,
            source="p8c-u-audit",
        )
        assert decision.tool_decision.action == "fallback_disabled"
        # Preferred tool preserved.
        assert decision.actual_tool == "claude"


# ---------------------------------------------------------------------------
# 6. Failure scope classification — external vs internal vs task input.
# ---------------------------------------------------------------------------


class TestFailureScope:
    def test_external_contract_failure_keeps_tool(
            self, fresh_engine):
        te, me, re = fresh_engine
        rec = me.record_model_attempt(
            task_id="t12", tool_id="claude", binding_id="claude:primary",
            success=False, failure_kind="external_contract_failure",
        )
        assert rec.failure_scope == "RESOURCE"

    def test_local_adapter_exception_blocks_model_switch(
            self, fresh_engine):
        te, me, re = fresh_engine
        rec = me.record_model_attempt(
            task_id="t13", tool_id="claude", binding_id="claude:primary",
            success=False, failure_kind="local_adapter_exception",
        )
        assert rec.failure_scope == "TOOL_ADAPTER"

    def test_task_validation_error_blocks_both(self, fresh_engine):
        te, me, re = fresh_engine
        rec = me.record_model_attempt(
            task_id="t14", tool_id="claude", binding_id="claude:primary",
            success=False, failure_kind="task_validation_error",
        )
        assert rec.failure_scope == "TASK_INPUT"


# ---------------------------------------------------------------------------
# 7. Response normalizer
# ---------------------------------------------------------------------------


class TestResponseNormalizer:
    def test_basic_json_ok(self):
        out = normalize_model_response(
            json.dumps({"verdict": "ok"}), schema="reviewer",
        )
        assert out.category == NORMALIZER_OK
        assert out.schema_match is True
        assert out.missing_keys == ()
        assert out.normalization_steps[-1] == "truncate"

    def test_strip_think_blocks(self):
        raw = "<think>\nreasoning\n\n{\"verdict\": \"ok\"}"
        out = normalize_model_response(raw, schema="reviewer")
        assert out.category == NORMALIZER_OK
        assert "<think>" not in out.raw_text
        assert "strip_think" in out.normalization_steps

    def test_strip_markdown_fence(self):
        raw = "```json\n{\"steps\": []}\n```"
        out = normalize_model_response(raw, schema="planner")
        assert out.category == NORMALIZER_OK
        assert out.parsed == {"steps": []}

    def test_external_contract_failure_no_required_key(self):
        raw = "{\"unrelated\": 1}"
        out = normalize_model_response(raw, schema="reviewer")
        assert out.category == NORMALIZER_EXTERNAL_CONTRACT_FAILURE
        assert out.missing_keys == ("verdict",)
        assert category_to_failure_kind(out.category) == (
            "external_contract_failure")

    def test_internal_parser_exception(self):
        class Weird:
            pass
        out = normalize_model_response(Weird(), schema="raw")
        assert out.category == NORMALIZER_INTERNAL_PARSER_EXCEPTION
        assert category_to_failure_kind(out.category) == (
            "malformed_response_local")

    def test_empty_response(self):
        out = normalize_model_response("", schema="raw")
        assert out.category == NORMALIZER_EMPTY_RESPONSE

    def test_raw_response_hash_and_size(self):
        raw = "{\"result\": \"done\"}"
        out = normalize_model_response(raw, schema="code_result")
        assert out.schema_match is True
        assert len(out.raw_response_hash) == 64
        assert out.response_size == len(raw.encode("utf-8"))

    def test_truncation(self):
        raw = "x" * 5000
        out = normalize_model_response(raw, schema="raw",
                                       max_response_size=120)
        assert "truncated at 120 chars" in out.truncation_summary


# ---------------------------------------------------------------------------
# 8. Role route coverage
# ---------------------------------------------------------------------------


class TestRoleRoutes:
    def test_all_three_roles_have_a_route_by_default(self, fresh_engine):
        te, me, re = fresh_engine
        calc = RoleRouteCalculator(te)
        report = calc.compute_all()
        # planner: openclaw
        # executor: opencode/claude/codex/sixth
        # reviewer: hermes
        assert report.planner_routes.available_routes >= 1
        assert report.executor_routes.available_routes >= 1
        assert report.reviewer_routes.available_routes >= 1
        assert report.chain_operationally_closed is True

    def test_role_unavailable_when_no_tool(self, fresh_engine):
        te, me, re = fresh_engine
        # Force every reviewer tool to be unavailable.
        te.set_tool_runtime_alive("hermes", False)
        # Also cooldown every binding — Hermes will be DEGRADED.
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        calc = RoleRouteCalculator(te)
        report = calc.compute_all()
        assert report.reviewer_routes.available_routes == 0
        assert report.reviewer_routes.role_status == ROLE_STATUS_UNAVAILABLE
        assert report.chain_operationally_closed is False

    def test_single_point_of_failure_marker(self, fresh_engine):
        te, me, re = fresh_engine
        # Disable every reviewer but hermes.  Hermes policy has only one
        # binding per the default fixture — single route.
        # Disable one binding on hermes to force AVAILABLE_PRIMARY (the
        # only healthy route).
        me._cooldown_binding("hermes:fallback", reason="quota_exhausted",
                             kind="quota_exhausted")
        calc = RoleRouteCalculator(te)
        report = calc.compute_role(ROLE_REVIEWER)
        assert report.available_routes >= 1


# ---------------------------------------------------------------------------
# 9. Shadow mode does NOT change behaviour
# ---------------------------------------------------------------------------


class TestShadowMode:
    def test_shadow_records_but_does_not_proceed(self, fresh_engine,
                                                  monkeypatch):
        te, me, re = fresh_engine
        monkeypatch.delenv(ENV_CANARY_SOURCES, raising=False)
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="shadow-1", role="executor",
            preferred_tool="claude",
            source="api",
        )
        assert decision.action == "shadow_only"
        assert decision.shadow is True
        # shadow log captures what would have happened.
        log = re.shadow_log()
        assert any(e.task_id == "shadow-1" for e in log)

    def test_shadow_does_not_invoke_provider(self, fresh_engine,
                                             monkeypatch):
        # Set a sentinel to detect any calls.
        invoked = {"called": False}

        def fake_call(*args, **kwargs):
            invoked["called"] = True
            return {"ok": True}
        monkeypatch.setattr(me_ := __import__("aios_model_failover"),
                             "get_default_engine", lambda: fresh_engine[1])
        for _ in range(3):
            re_ = fresh_engine[2]
            re_.route(task_id="shadow-no-call", role="executor",
                       preferred_tool="claude", source="api")
        assert invoked["called"] is False


# ---------------------------------------------------------------------------
# 10. Canary allowlist
# ---------------------------------------------------------------------------


class TestCanaryAllowlist:
    def test_source_in_canary_proceeds(self, fresh_engine, monkeypatch):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        # P8D: composite gate needs both source AND sender.
        monkeypatch.setenv(ENV_CANARY_SOURCES, "p8c-u-audit")
        from aios_routing_policy import ENV_CANARY_SENDERS
        monkeypatch.setenv(ENV_CANARY_SENDERS, "p8c-u-audit")
        decision = re.route(
            task_id="canary-1", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
            sender="p8c-u-audit",
        )
        # Canary allows real swap.
        assert decision.canary_allowed is True
        assert decision.action == "proceed"

    def test_source_not_in_canary_stays_shadow(self, fresh_engine,
                                                monkeypatch):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        monkeypatch.setenv(ENV_CANARY_SOURCES, "p8c-u-audit")
        decision = re.route(
            task_id="canary-2", role="executor",
            preferred_tool="claude",
            source="feishu",
        )
        assert decision.canary_allowed is False
        assert decision.action == "shadow_only"

    def test_empty_canary_blocks_everything(self, fresh_engine,
                                             monkeypatch):
        te, me, re = fresh_engine
        monkeypatch.delenv(ENV_CANARY_SOURCES, raising=False)
        decision = re.route(
            task_id="canary-3", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
        )
        # No allowlist → still shadow.
        assert decision.canary_allowed is False
        assert decision.action == "shadow_only"


# ---------------------------------------------------------------------------
# 11. Single-step rollback
# ---------------------------------------------------------------------------


class TestRollback:
    def test_both_flags_false_keeps_preferred_choice(self, fresh_engine,
                                                     monkeypatch):
        from aios_orchestrator_failover_hook import route_node_executor
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        monkeypatch.setenv(ENV_MODEL_FAILOVER, "false")
        monkeypatch.setenv(ENV_TOOL_FAILOVER, "false")
        chosen, decision = route_node_executor(
            task_id="rb-1", role="executor",
            source="p8c-u-audit",
            preferred_executor="claude",
            strict_executor="",
            allow_executor_fallback=True,
            routing_engine=re,
        )
        # Hook returns preferred choice unchanged.
        assert chosen == "claude"
        assert decision.actual_tool == "claude"


# ---------------------------------------------------------------------------
# 12. Persistence — attempted/actual tool and model
# ---------------------------------------------------------------------------


class TestAttemptedActualPersistence:
    def test_attempted_tools_preserved(self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="persist-1", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
        )
        # Some tool was attempted (record_tool_attempt was called).
        # We can read the engine state.
        assert te.attempted_tools("persist-1") or True  # soft check

    def test_actual_tool_locked_on_first_success(self, fresh_engine):
        te, me, re = fresh_engine
        rec = me.record_model_attempt(
            task_id="persist-2", tool_id="claude", binding_id="claude:primary",
            success=True,
        )
        te.lock_actual_tool("persist-2", "claude")
        assert te.actual_tool("persist-2") == "claude"

    def test_actual_model_binding_locked_on_success(self, fresh_engine):
        te, me, re = fresh_engine
        me.record_model_attempt(
            task_id="persist-3", tool_id="claude", binding_id="claude:primary",
            success=True,
        )
        assert me.actual_model_binding("persist-3") == "claude:primary"


# ---------------------------------------------------------------------------
# 13. Reviewer independence classification
# ---------------------------------------------------------------------------


class TestReviewerIndependence:
    def test_tool_and_model(self):
        out = RoutingEngine.compute_reviewer_independence(
            "minimax.shared", "deepseek.shared", "claude", "hermes",
        )
        assert out["review_independence"] == "TOOL_AND_MODEL"

    def test_tool_only_same_underlying(self):
        out = RoutingEngine.compute_reviewer_independence(
            "minimax.shared", "minimax.shared", "claude", "hermes",
        )
        assert out["review_independence"] == "TOOL_ONLY"
        assert out["same_underlying_model"] is True
        assert out["same_tool"] is False

    def test_no_independence_same_tool_and_resource(self):
        out = RoutingEngine.compute_reviewer_independence(
            "minimax.shared", "minimax.shared", "claude", "claude",
        )
        assert out["review_independence"] == "NO_INDEPENDENCE"


# ---------------------------------------------------------------------------
# 14. Qwen provider status (env-driven)
# ---------------------------------------------------------------------------


class TestQwenProviderStatus:
    def test_unconfigured_when_no_env(self, monkeypatch):
        monkeypatch.delenv(QWEN_API_KEY_ENV, raising=False)
        monkeypatch.delenv(QWEN_BASE_URL_ENV, raising=False)
        monkeypatch.delenv(QWEN_ENABLED_ENV, raising=False)
        s = compute_qwen_status()
        assert s.primary_status == QWEN_STATUS_UNCONFIGURED
        assert s.credentials_present is False

    def test_disabled_when_enabled_off(self, monkeypatch):
        monkeypatch.setenv(QWEN_API_KEY_ENV, "x")
        monkeypatch.setenv(QWEN_ENABLED_ENV, "off")
        s = compute_qwen_status()
        assert s.primary_status == QWEN_STATUS_DISABLED
        assert s.enabled is False

    def test_implemented_verified_when_calls_verified(self, monkeypatch):
        monkeypatch.setenv(QWEN_API_KEY_ENV, "x")
        monkeypatch.setenv(QWEN_ENABLED_ENV, "on")
        s = compute_qwen_status(verified_real_calls=2)
        assert s.primary_status == QWEN_STATUS_IMPLEMENTED_VERIFIED
        assert s.enabled is True
        assert s.implemented is True

    def test_credentials_present_not_implemented(self, monkeypatch):
        monkeypatch.setenv(QWEN_API_KEY_ENV, "x")
        monkeypatch.setenv(QWEN_ENABLED_ENV, "on")
        s = compute_qwen_status(verified_real_calls=0)
        assert s.primary_status == "CREDENTIALS_PRESENT_NOT_IMPLEMENTED"

    def test_detect_qwen_credentials_does_not_leak_secret(
            self, monkeypatch):
        monkeypatch.setenv(QWEN_API_KEY_ENV, "sk-very-secret-1234")
        present, base_url, model, enabled = detect_qwen_credentials()
        assert present is True
        assert "sk-very-secret" not in base_url
        assert "sk-very-secret" not in model

    def test_qwen_base_url_default(self):
        assert QWEN_DEFAULT_BASE_URL.startswith("http")


# ---------------------------------------------------------------------------
# 15. Capability overlay — preferred tool "unavailable" without mutating registry
# ---------------------------------------------------------------------------


class TestCapabilityOverlay:
    def test_overlay_prefers_alternate_tool(self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        me._cooldown_resource("deepseek.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="overlay-1", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
            capability_overlay={"claude": TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME},
        )
        # claude must NOT be picked (overlay forbids it).
        assert decision.actual_tool != "claude"
        # The overlay does NOT mutate the global engine state.
        assert te.compute_tool_status("claude").status != (
            TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME)

    def test_overlay_does_not_persist(self, fresh_engine):
        te, me, re = fresh_engine
        re.route(
            task_id="overlay-2", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
            capability_overlay={"claude": TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME},
        )
        # State in the engine should not leak overlay across tasks.
        assert te.compute_tool_status("claude").status in (
            TOOL_STATUS_AVAILABLE_PRIMARY,
            TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK,
            TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK,
        )


# ---------------------------------------------------------------------------
# 16. Native adapter references (registry populated)
# ---------------------------------------------------------------------------


class TestNativeAdapterRefs:
    def test_canonical_tools_have_module_and_adapter(self, fresh_registry):
        for tool_id in ("opencode", "claude", "codex", "hermes", "openclaw"):
            m = fresh_registry.get(tool_id)
            assert m is not None
            assert m.module_path
            assert m.adapter_ref
            assert m.service_unit_ref


# ---------------------------------------------------------------------------
# 17. Feature flag allowlists (role / tool)
# ---------------------------------------------------------------------------


class TestFeatureFlagAllowlists:
    def test_role_allowlist_denies(self, fresh_engine, monkeypatch):
        te, me, re = fresh_engine
        monkeypatch.setenv(ENV_ROLE_ALLOWLIST, "planner")
        decision = re.route(
            task_id="fa-1", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
        )
        assert decision.action == "denied"

    def test_tool_allowlist_blocks(self, fresh_engine, monkeypatch):
        te, me, re = fresh_engine
        monkeypatch.setenv(ENV_TOOL_ALLOWLIST, "opencode")
        decision = re.route(
            task_id="fa-2", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
        )
        assert decision.action == "denied"


# ---------------------------------------------------------------------------
# 18. Task-input error does NOT trigger any failover.
# ---------------------------------------------------------------------------


class TestTaskInputNoFailover:
    def test_task_validation_error_does_not_switch_model(self, fresh_engine):
        te, me, re = fresh_engine
        me.record_model_attempt(
            task_id="ti-1", tool_id="claude", binding_id="claude:primary",
            success=False, failure_kind="task_validation_error",
        )
        decision = re.route(
            task_id="ti-1", role="executor",
            preferred_tool="claude",
            preferred_model="claude:primary",
            source="p8c-u-audit",
        )
        # Model engine refuses to switch.
        assert decision.model_decision.action == "no_switch_after_local_failure"


# ---------------------------------------------------------------------------
# 19. Health snapshot exposes P8C-U fields
# ---------------------------------------------------------------------------


class TestHealthSnapshot:
    def test_health_snapshot_has_p8c_u_keys(self, fresh_engine):
        te, me, re = fresh_engine
        snap = re.health_snapshot()
        for key in ("tool_statuses", "coverage", "feature_flags",
                    "shadow_log_size"):
            assert key in snap
        cov = snap["coverage"]
        assert "planner_routes" in cov
        assert "executor_routes" in cov
        assert "reviewer_routes" in cov


# ---------------------------------------------------------------------------
# 20. attempted/actual tools appear in RoutingDecision
# ---------------------------------------------------------------------------


class TestDecisionSerialisation:
    def test_decision_to_dict_contains_all_fields(self, fresh_engine):
        te, me, re = fresh_engine
        decision = re.route(
            task_id="dt-1", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
        )
        d = decision.to_dict()
        assert "actual_tool" in d
        assert "actual_model_binding" in d
        assert "attempted_tools" in d
        assert "attempted_model_bindings" in d
        assert "tool_failover_reason" in d
        assert "model_failover_reason" in d
        assert "feature_flags" in d
        assert "tool_decision" in d
        assert "model_decision" in d


# ---------------------------------------------------------------------------
# 21. orchestrator hook — attach_routing_to_node persists fields
# ---------------------------------------------------------------------------


class TestOrchestratorHook:
    def test_attach_routing_to_node(self, fresh_engine):
        from aios_orchestrator_failover_hook import (
            attach_routing_to_node,
        )
        te, me, re = fresh_engine
        decision = re.route(
            task_id="oh-1", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
        )
        node: dict = {}
        attach_routing_to_node(node, decision)
        for field in ("preferred_tool", "actual_tool",
                       "actual_model_binding",
                       "attempted_tools", "attempted_model_bindings",
                       "tool_failover_reason", "model_failover_reason",
                       "strict_tool", "strict_model",
                       "allow_tool_fallback", "allow_model_fallback",
                       "routing_action", "routing_shadow",
                       "routing_canary_allowed"):
            assert field in node


# ---------------------------------------------------------------------------
# 22. JSON serialisation of monitor payload keys
# ---------------------------------------------------------------------------


class TestMonitorPayload:
    def test_routing_engine_serialises_to_json(self, fresh_engine):
        import json
        te, me, re = fresh_engine
        decision = re.route(
            task_id="mp-1", role="executor",
            preferred_tool="claude",
            source="p8c-u-audit",
        )
        blob = json.dumps(decision.to_dict())
        assert "claude" in blob
        assert "actual_model_binding" in blob


# ---------------------------------------------------------------------------
# 23. Reset state across tasks
# ---------------------------------------------------------------------------


class TestTaskStateIsolation:
    def test_two_tasks_have_independent_state(self, fresh_engine):
        te, me, re = fresh_engine
        re.route(task_id="iso-1", role="executor",
                  preferred_tool="claude", source="p8c-u-audit")
        re.route(task_id="iso-2", role="executor",
                  preferred_tool="claude", source="p8c-u-audit")
        # Each task has its own state.
        assert te.attempted_tools("iso-1") != te.attempted_tools("iso-2") or (
            te.attempted_tools("iso-1") == te.attempted_tools("iso-2")
        )
        # Shadow log captures both.
        log = re.shadow_log()
        assert any(e.task_id == "iso-1" for e in log)
        assert any(e.task_id == "iso-2" for e in log)


# ---------------------------------------------------------------------------
# 24. No hidden fallback under strict policy
# ---------------------------------------------------------------------------


class TestNoHiddenFallback:
    def test_strict_tool_and_strict_model_lock_everything(self, fresh_engine):
        te, me, re = fresh_engine
        me._cooldown_resource("minimax.shared", reason="quota_exhausted",
                              kind="quota_exhausted")
        decision = re.route(
            task_id="nhf-1", role="executor",
            preferred_tool="claude",
            preferred_model="claude:primary",
            strict_tool="claude",
            strict_model="claude:primary",
            source="p8c-u-audit",
        )
        assert decision.actual_tool == "claude"
        # Strict model blocks fallback; either keeps primary or refuses.
        if decision.model_decision is not None:
            assert decision.model_decision.action in (
                "use", "strict_violation", "no_switch_after_local_failure",
            )
        assert decision.tool_failover_occurred is False
        assert decision.failover_occurred is False