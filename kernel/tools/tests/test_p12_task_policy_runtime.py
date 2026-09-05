#!/usr/bin/env python3
"""AIOS close-out 20260727-§十七 new tests: task-local routing policy
runtime enforcement.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the kernel/tools dir importable when pytest is run from the
# project root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

import aios_task_routing_policy as trp
import aios_tool_failover
import aios_model_failover
import aios_routing_policy
from aios_role_routes import (
    ROLE_PLANNER,
    ROLE_EXECUTOR,
    ROLE_REVIEWER,
)
from aios_tool_failover import (
    ToolFailoverEngine,
)
from aios_model_failover import (
    ModelFailoverEngine,
)
from aios_model_resources import (
    SharedModelResource,
    SharedModelResourceRegistry,
    ToolModelBinding,
    ToolModelBindingRegistry,
    ToolModelPolicy,
    ToolModelPolicyRegistry,
)
from aios_tool_registry import ToolManifest, ToolRegistry
from aios_routing_policy import RoutingEngine


# ---------------------------------------------------------------------------
# Helpers — build minimal registries with real dataclasses
# ---------------------------------------------------------------------------


def _res(name: str, *, vendor: str = "minimax") -> SharedModelResource:
    return SharedModelResource(
        resource_id=name, vendor=vendor,
        model_id="M3", endpoint_ref="ep", credential_ref="cred",
        account_scope=name, protocol="openai_compatible",
        enabled=True,
    )


def _bind(*, binding_id: str, resource_id: str,
          roles=("executor",), enabled: bool = True) -> ToolModelBinding:
    tool_id = binding_id.split(":", 1)[0]
    return ToolModelBinding(
        binding_id=binding_id,
        tool_id=tool_id,
        resource_id=resource_id,
        roles=tuple(roles),
        adapter_mode="openai_chat",
        prompt_profile="default",
        timeout=60,
        supports_json=True, supports_tools=False,
        supports_code=False, supports_long_context=False,
        priority=100,
        enabled=enabled,
    )


def _pol(*, tool_id: str, candidates) -> ToolModelPolicy:
    return ToolModelPolicy(
        tool_id=tool_id,
        candidate_bindings=tuple(candidates),
        preferred_binding=candidates[0] if candidates else None,
    )


def _manifest(tool_id, roles=("executor",), enabled=True) -> ToolManifest:
    return ToolManifest(
        tool_id=tool_id, display_name=tool_id,
        module_path=f"agents/executors/{tool_id}",
        adapter_ref=f"{tool_id}-adapter",
        roles=tuple(roles),
        service_unit_ref=f"aios-{tool_id}.service",
        health_probe_ref="",
        model_policy_ref=f"{tool_id}.policy",
        enabled=enabled, version="1.0",
        capabilities=("executor",),
    )


def _build_tool_engine():
    resources = SharedModelResourceRegistry()
    resources.register(_res("minimax.shared"))
    bindings = ToolModelBindingRegistry()
    for bid in ("codex:minimax", "opencode:minimax", "openclaw:minimax",
                "hermes:minimax", "claude:minimax"):
        bindings.register(_bind(binding_id=bid, resource_id="minimax.shared"))
    policies = ToolModelPolicyRegistry()
    policies.register(_pol(tool_id="codex", candidates=["codex:minimax"]))
    policies.register(_pol(tool_id="opencode", candidates=["opencode:minimax"]))
    policies.register(_pol(tool_id="openclaw", candidates=["openclaw:minimax"]))
    policies.register(_pol(tool_id="hermes", candidates=["hermes:minimax"]))
    policies.register(_pol(tool_id="claude", candidates=["claude:minimax"]))
    registry = ToolRegistry()
    registry.register_tool(_manifest("codex", ("executor",)))
    registry.register_tool(_manifest("opencode", ("executor",)))
    registry.register_tool(_manifest("openclaw", ("planner",)))
    registry.register_tool(_manifest("hermes", ("reviewer",)))
    registry.register_tool(_manifest("claude", ("executor",)))
    model_engine = ModelFailoverEngine(
        binding_registry=bindings,
        policy_registry=policies,
        resource_registry=resources,
    )
    engine = ToolFailoverEngine(
        registry=registry,
        binding_registry=bindings,
        policy_registry=policies,
        resource_registry=resources,
        model_engine=model_engine,
    )
    # Force the on-disk probe cache to ``available`` so the engine
    # does not honour real prior failures recorded against opencode /
    # claude / etc. in this VM.  This is the documented test entry
    # point for the probe cache (see
    # :func:`ToolFailoverEngine.set_adapter_cache_override`).
    for tid in ("codex", "opencode", "openclaw", "hermes", "claude"):
        engine.set_adapter_cache_override(
            tid, {"model_state": "available", "model_available": True})
    return engine


def _build_model_engine(*, with_ollama: bool = False):
    resources = SharedModelResourceRegistry()
    resources.register(_res("minimax.shared"))
    bindings = ToolModelBindingRegistry()
    bindings.register(_bind(binding_id="codex:minimax", resource_id="minimax.shared"))
    bindings.register(_bind(binding_id="opencode:minimax", resource_id="minimax.shared"))
    bindings.register(_bind(binding_id="hermes:minimax", resource_id="minimax.shared"))
    if with_ollama:
        bindings.register(_bind(binding_id="claude:ollama",
                                resource_id="ollama.local", roles=("executor",)))
    policies = ToolModelPolicyRegistry()
    candidates = ["codex:minimax", "opencode:minimax", "hermes:minimax"]
    if with_ollama:
        candidates.append("claude:ollama")
    policies.register(_pol(tool_id="codex", candidates=candidates))
    return ModelFailoverEngine(
        binding_registry=bindings,
        policy_registry=policies,
        resource_registry=resources,
    )


# ---------------------------------------------------------------------------
# TaskRoutingPolicy dataclass
# ---------------------------------------------------------------------------


class TestTaskRoutingPolicy:
    def test_frozen_dataclass_rejects_mutation(self):
        p = trp.build_task_routing_policy(
            task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            preferred_tool="codex",
        )
        with pytest.raises(Exception):
            p.preferred_tool = "opencode"  # type: ignore[misc]

    def test_blocked_tools_deduped_and_capped(self):
        raw = ["codex", "opencode", "codex", "x" * 200]
        p = trp.build_task_routing_policy(
            task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            blocked_tools=raw,
        )
        assert p.blocked_tools == ("codex", "opencode")

    def test_blocked_resources_propagate_to_is_resource_blocked(self):
        p = trp.build_task_routing_policy(
            task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            blocked_resources=["minimax.shared"],
        )
        assert p.is_resource_blocked("minimax.shared") is True
        assert p.is_resource_blocked("qwen.primary") is False

    def test_invalid_id_dropped(self):
        p = trp.build_task_routing_policy(
            task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            blocked_tools=["x y", "good_tool"],
        )
        assert p.blocked_tools == ("good_tool",)

    def test_from_workflow_dict_recovers_lists(self):
        wf = {
            "parent_id": "abc-123",
            "blocked_tools": '["codex", "opencode"]',
            "blocked_resources": '["minimax.shared"]',
            "preferred_model_binding": "opencode:minimax",
            "blocked_reviewer_tools": '["hermes"]',
            "blocked_planner_tools": '["openclaw"]',
        }
        p = trp.from_workflow_dict(wf, role=ROLE_EXECUTOR)
        assert p.blocked_tools == ("codex", "opencode")
        assert p.blocked_resources == ("minimax.shared",)
        assert p.preferred_model_binding == "opencode:minimax"
        assert p.blocked_reviewer_tools == ("hermes",)
        assert p.blocked_planner_tools == ("openclaw",)

    def test_csv_legacy_form_decoded(self):
        wf = {"parent_id": "abc-123", "blocked_tools": "codex,opencode"}
        p = trp.from_workflow_dict(wf, role=ROLE_EXECUTOR)
        assert p.blocked_tools == ("codex", "opencode")

    def test_role_validation(self):
        with pytest.raises(ValueError):
            trp.build_task_routing_policy(
                task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                role="observer",
            )


# ---------------------------------------------------------------------------
# ToolFailoverEngine.select_tool — task_blocked_tools
# ---------------------------------------------------------------------------


class TestToolFailoverTaskBlocked:
    def test_blocked_tool_is_removed_from_candidates(self):
        engine = _build_tool_engine()
        decision = engine.select_tool(
            task_id="t1", role=ROLE_EXECUTOR,
            preferred_tool="codex", task_blocked_tools=["codex"],
            allow_tool_fallback=True,
        )
        assert decision.action == "use"
        assert decision.tool_id == "opencode"

    def test_strict_tool_with_blocked_returns_blocked_signal(self):
        engine = _build_tool_engine()
        decision = engine.select_tool(
            task_id="t2", role=ROLE_EXECUTOR,
            preferred_tool="codex", strict_tool="codex",
            task_blocked_tools=["codex"], allow_tool_fallback=True,
        )
        assert decision.action in ("no_candidates", "strict_violation")

    def test_all_executor_tools_blocked_returns_no_candidates(self):
        engine = _build_tool_engine()
        decision = engine.select_tool(
            task_id="t3", role=ROLE_EXECUTOR,
            preferred_tool="codex",
            task_blocked_tools=["codex", "opencode", "claude"],
            allow_tool_fallback=True,
        )
        assert decision.action == "no_candidates"
        assert "codex" in decision.reason or "opencode" in decision.reason or "claude" in decision.reason

    def test_fallback_disabled_with_blocked_returns_no_candidates(self):
        engine = _build_tool_engine()
        decision = engine.select_tool(
            task_id="t4", role=ROLE_EXECUTOR,
            preferred_tool="codex", task_blocked_tools=["codex"],
            allow_tool_fallback=False,
        )
        assert decision.action == "no_candidates"

    def test_blocked_tools_do_not_bleed_across_tasks(self):
        engine = _build_tool_engine()
        a = engine.select_tool(
            task_id="taskA", role=ROLE_EXECUTOR,
            preferred_tool="codex", task_blocked_tools=["codex"],
            allow_tool_fallback=True,
        )
        assert a.tool_id == "opencode"
        b = engine.select_tool(
            task_id="taskB", role=ROLE_EXECUTOR,
            preferred_tool="codex", allow_tool_fallback=True,
        )
        assert b.tool_id == "codex"


# ---------------------------------------------------------------------------
# ModelFailoverEngine — task_blocked_resources / task_blocked_bindings
# ---------------------------------------------------------------------------


class TestModelFailoverTaskBlocked:
    def test_blocked_resource_excludes_every_binding(self):
        engine = _build_model_engine()
        decision = engine.select_model_binding(
            task_id="t", tool_id="codex", role="executor",
            preferred_model="codex:minimax",
            task_blocked_resources=["minimax.shared"],
            allow_model_fallback=True,
        )
        assert decision.action in (
            "no_candidates", "max_attempts", "exhausted", "skip_binding",
        )
        assert decision.binding_id not in (
            "codex:minimax", "opencode:minimax", "hermes:minimax")

    def test_blocked_binding_only_excludes_that_binding(self):
        engine = _build_model_engine()
        decision = engine.select_model_binding(
            task_id="t", tool_id="codex", role="executor",
            preferred_model="codex:minimax",
            task_blocked_bindings=["codex:minimax"],
            allow_model_fallback=True,
        )
        assert decision.action == "use"
        assert decision.binding_id == "opencode:minimax"

    def test_blocked_resources_isolated_across_tasks(self):
        engine = _build_model_engine()
        engine.select_model_binding(
            task_id="taskA", tool_id="codex", role="executor",
            preferred_model="codex:minimax",
            task_blocked_resources=["minimax.shared"],
            allow_model_fallback=True,
        )
        b = engine.select_model_binding(
            task_id="taskB", tool_id="codex", role="executor",
            preferred_model="codex:minimax", allow_model_fallback=True,
        )
        assert b.action == "use"
        assert b.binding_id == "codex:minimax"


# ---------------------------------------------------------------------------
# RoutingEngine.route — full chain
# ---------------------------------------------------------------------------


class TestRoutingEngineRoute:
    def test_route_threads_blocked_tools_through(self):
        tool_engine = _build_tool_engine()
        routing = RoutingEngine(
            tool_engine=tool_engine,
            model_engine=tool_engine._model_engine,
        )
        decision = routing.route(
            task_id="t", role="executor",
            preferred_tool="codex", task_blocked_tools=["codex"],
            allow_tool_fallback=True,
            source="test", sender="aios-canary",
        )
        assert decision.action == "proceed"
        assert decision.actual_tool == "opencode"
        assert decision.actual_model_binding == "opencode:minimax"

    def test_route_threads_blocked_resources(self):
        tool_engine = _build_tool_engine()
        routing = RoutingEngine(
            tool_engine=tool_engine,
            model_engine=tool_engine._model_engine,
        )
        decision = routing.route(
            task_id="t", role="executor",
            preferred_tool="codex",
            task_blocked_resources=["minimax.shared"],
            allow_tool_fallback=True, allow_model_fallback=True,
            source="test", sender="aios-canary",
        )
        assert decision.actual_model_binding in (None, "")


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------


class TestStrictModes:
    def test_strict_tool_decision_shape(self):
        engine = _build_tool_engine()
        decision = engine.select_tool(
            task_id="t", role=ROLE_EXECUTOR,
            preferred_tool="opencode", strict_tool="codex",
            allow_tool_fallback=True,
        )
        assert decision.action in ("strict_violation", "use")

    def test_strict_model_violation_signal(self):
        resources = SharedModelResourceRegistry()
        resources.register(_res("minimax.shared"))
        bindings = ToolModelBindingRegistry()
        bindings.register(_bind(binding_id="codex:minimax", resource_id="minimax.shared"))
        policies = ToolModelPolicyRegistry()
        policies.register(_pol(tool_id="codex", candidates=["codex:minimax"]))
        engine = ModelFailoverEngine(
            binding_registry=bindings,
            policy_registry=policies,
            resource_registry=resources,
        )
        decision = engine.select_model_binding(
            task_id="t", tool_id="codex", role="executor",
            strict_model="opencode:minimax", allow_model_fallback=True,
        )
        assert decision.action == "strict_violation"


# ---------------------------------------------------------------------------
# Task isolation
# ---------------------------------------------------------------------------


class TestTaskIsolation:
    def test_blocked_resources_does_not_bleed(self):
        engine = _build_model_engine()
        engine.select_model_binding(
            task_id="A", tool_id="codex", role="executor",
            preferred_model="codex:minimax",
            task_blocked_resources=["minimax.shared"],
            allow_model_fallback=True,
        )
        b = engine.select_model_binding(
            task_id="B", tool_id="codex", role="executor",
            preferred_model="codex:minimax", allow_model_fallback=True,
        )
        assert b.action == "use"
        assert b.binding_id == "codex:minimax"

    def test_blocked_tools_does_not_bleed(self):
        engine = _build_tool_engine()
        engine.select_tool(
            task_id="A", role=ROLE_EXECUTOR,
            preferred_tool="codex", task_blocked_tools=["codex"],
            allow_tool_fallback=True,
        )
        b = engine.select_tool(
            task_id="B", role=ROLE_EXECUTOR,
            preferred_tool="codex", allow_tool_fallback=True,
        )
        assert b.tool_id == "codex"


# ---------------------------------------------------------------------------
# Retry preserves task policy
# ---------------------------------------------------------------------------


class TestRetryPreservesTaskPolicy:
    def test_blocked_tools_remain_blocked_on_retry(self):
        engine = _build_tool_engine()
        d1 = engine.select_tool(
            task_id="t", role=ROLE_EXECUTOR,
            preferred_tool="codex", task_blocked_tools=["codex"],
            allow_tool_fallback=True,
        )
        assert d1.tool_id == "opencode"
        engine.record_tool_attempt(task_id="t", tool_id="opencode")
        d2 = engine.select_tool(
            task_id="t", role=ROLE_EXECUTOR,
            preferred_tool="codex", task_blocked_tools=["codex"],
            allow_tool_fallback=True,
        )
        assert d2.tool_id != "codex"


# ---------------------------------------------------------------------------
# Local model guard
# ---------------------------------------------------------------------------


class TestLocalModelGuard:
    def test_ollama_binding_never_appears_after_resource_block(self):
        engine = _build_model_engine(with_ollama=True)
        decision = engine.select_model_binding(
            task_id="t", tool_id="codex", role="executor",
            preferred_model="codex:minimax",
            task_blocked_resources=["minimax.shared"],
            allow_model_fallback=True,
        )
        assert decision.binding_id != "claude:ollama"
        # Engine surfaces an exhausted-pool signal — either
        # ``no_candidates`` (no candidates at all), ``max_attempts`` /
        # ``exhausted`` (cap reached) or ``skip_binding`` (every
        # candidate blocked by the task overlay).  All three are the
        # documented exhaustion signals and MUST NOT silently fall
        # back to ``claude:ollama``.
        assert decision.action in (
            "no_candidates", "max_attempts", "exhausted", "skip_binding",
        )
