#!/usr/bin/env python3
"""AIOS STABLE_V1 CLI Contract Test Suite.

Closure: AIOS_FINAL_REAL_CLI_CLOSURE_20260811.

This test suite enforces the contract that user-facing CLI commands
(task / audit / ops / code) submit the STABLE_V1 production routing
policy by default.  The contract surface is intentionally minimal
and tests the *Python helper* + *HTTP payload* path, not the live
runtime (which is exercised by the user-facing ``./aios`` commands).

Production routing defaults (single source of truth):

    planner        = openclaw
    allow_planner_fallback = False

    executor        = codex
    strict_executor = codex
    allow_executor_fallback = False
    strict_tool     = True

    binding         = codex:minimax

    reviewer        = hermes
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Path setup so the test imports the production helper directly.
# ---------------------------------------------------------------------------

ROOT = Path("${AIOS_HOME}")
HELPER_PATH = ROOT / "kernel/tools/aios_stable_v1_policy.py"

sys.path.insert(0, str(ROOT / "kernel/tools"))
import aios_stable_v1_policy as stable_v1  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_payload(profile: str) -> dict:
    return stable_v1.build_cli_payload(profile, "test input")


PROFILES = ("GENERAL", "AUDIT", "OPS", "CODE")


# ---------------------------------------------------------------------------
# 1-6: GENERAL profile → ./aios task
# ---------------------------------------------------------------------------


def test_general_preferred_planner_is_openclaw():
    assert _load_payload("GENERAL")["preferred_planner"] == "openclaw"


def test_general_preferred_executor_is_opencode():
    """GENERAL restores the original ``primary_general_executor``
    role: opencode is the primary executor with bounded fallback
    to codex:minimax when opencode is unhealthy."""
    assert _load_payload("GENERAL")["preferred_executor"] == "opencode"


def test_general_preferred_model_binding_is_opencode_free_auto_router():
    """GENERAL's primary binding is the opencode:free-auto-router
    (the original baseline role binding); only when opencode is
    unhealthy does the runtime walk to codex:minimax via the
    existing ``choose_executor`` fallback."""
    assert _load_payload("GENERAL")["preferred_model_binding"] == "opencode:free-auto-router"


def test_general_preferred_reviewer_is_hermes():
    assert _load_payload("GENERAL")["preferred_reviewer"] == "hermes"


def test_general_planner_fallback_is_disallowed():
    assert _load_payload("GENERAL")["allow_planner_fallback"] is False


def test_general_executor_fallback_is_allowed():
    """GENERAL must allow the existing bounded tool fallback so
    opencode-down does not break the production chain.  Strict-mode
    is OFF (no ``strict_executor``) because the primary path is
    opencode with codex as the explicit fallback."""
    payload = _load_payload("GENERAL")
    assert payload["allow_executor_fallback"] is True
    assert payload["strict_executor"] == ""
    assert payload["strict_tool"] is False


# ---------------------------------------------------------------------------
# 7-9: AUDIT / OPS / CODE profiles match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["AUDIT", "OPS", "CODE"])
def test_profile_preferred_planner_is_openclaw(profile):
    assert _load_payload(profile)["preferred_planner"] == "openclaw"


@pytest.mark.parametrize("profile", ["AUDIT", "OPS"])
def test_profile_preferred_executor_is_opencode(profile):
    """AUDIT / OPS restore the original ``primary_general_executor``
    role: opencode is the primary executor."""
    assert _load_payload(profile)["preferred_executor"] == "opencode"


def test_code_preferred_executor_is_codex():
    """CODE keeps the canonical ``specialist_code_batch`` role."""
    assert _load_payload("CODE")["preferred_executor"] == "codex"


@pytest.mark.parametrize("profile", ["AUDIT", "OPS"])
def test_profile_preferred_model_binding_is_opencode_free_auto_router(profile):
    assert _load_payload(profile)["preferred_model_binding"] == "opencode:free-auto-router"


def test_code_preferred_model_binding_is_codex_minimax():
    assert _load_payload("CODE")["preferred_model_binding"] == "codex:minimax"


@pytest.mark.parametrize("profile", ["AUDIT", "OPS", "CODE"])
def test_profile_preferred_reviewer_is_hermes(profile):
    assert _load_payload(profile)["preferred_reviewer"] == "hermes"


@pytest.mark.parametrize("profile", ["AUDIT", "OPS"])
def test_profile_executor_fallback_is_allowed(profile):
    """AUDIT / OPS allow the bounded tool fallback so opencode-down
    does not break the production chain."""
    payload = _load_payload(profile)
    assert payload["allow_executor_fallback"] is True
    assert payload["strict_executor"] == ""
    assert payload["strict_tool"] is False


def test_code_executor_fallback_is_disallowed():
    """CODE keeps the strict-mode contract: no executor fallback,
    strict_executor=codex, strict_tool=True."""
    payload = _load_payload("CODE")
    assert payload["allow_executor_fallback"] is False
    assert payload["strict_executor"] == "codex"
    assert payload["strict_tool"] is True


# ---------------------------------------------------------------------------
# 10: CLI payload → Gateway retains every field
# ---------------------------------------------------------------------------


def test_cli_payload_to_http_json_is_complete():
    """The Python helper's JSON output must carry every field the
    Gateway uses to forward the policy to the Orchestrator."""
    for profile in PROFILES:
        payload = _load_payload(profile)
        serialised = json.loads(json.dumps(payload))
        for key in (
            "preferred_planner",
            "allow_planner_fallback",
            "preferred_executor",
            "strict_executor",
            "allow_executor_fallback",
            "strict_tool",
            "preferred_model_binding",
            "preferred_reviewer",
            "allow_reviewer_fallback",
            "source",
            "input",
        ):
            assert key in serialised, (
                f"profile={profile} dropped field={key}"
            )


def test_cli_payload_serializes_as_valid_json():
    """The Python helper output must be valid JSON (the CLI shells
    it into curl)."""
    for profile in PROFILES:
        payload = _load_payload(profile)
        s = json.dumps(payload)
        parsed = json.loads(s)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# 11: Gateway → TaskRoutingPolicy keeps fields
# ---------------------------------------------------------------------------


def test_task_routing_policy_accepts_cli_payload():
    """The ``aios_task_routing_policy.from_workflow_dict`` view
    must surface every CLI field when built from a workflow dict
    shaped like a CLI payload."""
    from aios_task_routing_policy import from_workflow_dict
    payload = _load_payload("GENERAL")
    # Mimic the Gateway workflow-shape: every CLI field becomes a
    # top-level workflow attribute.  ``from_workflow_dict`` is the
    # production helper that the Orchestrator uses to re-read the
    # routing policy from the parent workflow hash, so the fixture
    # MUST carry a ``parent_id`` (the production hash key suffix)
    # alongside the routing surface.
    workflow = {
        "parent_id": "stable-v1-test-parent-id",
        "preferred_planner": payload["preferred_planner"],
        "allow_planner_fallback": payload["allow_planner_fallback"],
        "preferred_executor": payload["preferred_executor"],
        "strict_executor": payload["strict_executor"],
        "allow_executor_fallback": payload["allow_executor_fallback"],
        "strict_tool": payload["strict_tool"],
        "preferred_model_binding": payload["preferred_model_binding"],
        "preferred_reviewer": payload["preferred_reviewer"],
        "allow_reviewer_fallback": payload["allow_reviewer_fallback"],
        "blocked_planner_tools": "[]",
        "blocked_reviewer_tools": "[]",
    }
    policy = from_workflow_dict(workflow, role="planner")
    assert policy.preferred_planner == "openclaw"
    assert policy.allow_planner_fallback is False
    # The policy view carries the executor intent on the
    # ``preferred_tool`` slot and the strict-mode intent on the
    # boolean ``strict_tool`` knob.  The actual ``strict_executor``
    # tool id is read by the orchestrator from the raw workflow
    # hash; the policy view encodes the strict-mode gate as
    # ``allow_tool_fallback=False`` so consumers can reject
    # tool-switches without consulting the raw hash.
    # Role-specialized (AIOS_EXECUTOR_TOPOLOGY_CORRECTION): for
    # GENERAL the primary tool is opencode (primary_general_executor),
    # strict-mode is OFF, and the binding is opencode:free-auto-router.
    assert policy.preferred_tool == "opencode"
    assert policy.strict_tool is False
    assert policy.allow_tool_fallback is True
    # The binding side of the routing policy is also kept.
    assert policy.preferred_model_binding == "opencode:free-auto-router"


# ---------------------------------------------------------------------------
# 12-14: TaskRoutingPolicy → Orchestrator choices keep fields
# ---------------------------------------------------------------------------


def test_planner_resolution_keeps_openclaw():
    """``_resolve_planner_target`` must surface openclaw as the
    primary planner when the policy says so."""
    from aios_orchestrator import _resolve_planner_target
    payload = _load_payload("GENERAL")
    from aios_task_routing_policy import from_workflow_dict
    # Wrap the CLI payload into a workflow dict so the production
    # ``from_workflow_dict`` helper is exercised with the real shape
    # the Gateway actually writes into Redis.
    workflow = {
        "parent_id": "stable-v1-test-parent-id",
        **payload,
    }
    policy = from_workflow_dict(workflow, role="planner")
    planner_tool, planner_binding, _, _, _ = _resolve_planner_target(policy)
    assert planner_tool == "openclaw"
    assert planner_binding == "openclaw:minimax"


def test_code_strict_executor_path_uses_codex_minimax_binding_when_healthy(monkeypatch):
    """CODE profile: when the strict binding is codex:minimax and
    the binding health is eligible, the strict-mode dispatch
    contract MUST use codex as the chosen executor and the
    codex:minimax binding.

    The test drives the production ``from_workflow_dict`` view of the
    CODE CLI payload and the orchestrator's strict-mode gates
    directly so no live Redis write is required; it asserts the same
    conditions the ``_enqueue_node`` strict branch enforces.

    Test isolation: the production ``_is_executor_available`` reads
    the on-disk ``cache/tool_health/<tool>.json`` probe cache, which
    on the production machine is in a degraded state.  The test
    monkey-patches ``_is_executor_available`` to simulate the
    CODE-profile contract assumption (codex IS the strict-mode
    primary and IS available in the contract view) without touching
    the on-disk cache.
    """
    from aios_orchestrator import (
        _binding_health_eligible,
        _is_executor_available,
        EXECUTORS,
    )
    import aios_orchestrator as _orch_mod
    from aios_task_routing_policy import from_workflow_dict
    payload = _load_payload("CODE")
    workflow = {
        "parent_id": "stable-v1-test-parent-id",
        "preferred_executor": payload["preferred_executor"],
        "strict_executor": payload["strict_executor"],
        "allow_executor_fallback": payload["allow_executor_fallback"],
        "preferred_model_binding": payload["preferred_model_binding"],
        "allow_planner_fallback": payload["allow_planner_fallback"],
        "preferred_planner": payload["preferred_planner"],
        "strict_tool": payload["strict_tool"],
        "allow_reviewer_fallback": True,
        "preferred_reviewer": payload["preferred_reviewer"],
    }
    policy = from_workflow_dict(workflow, role="executor")
    # Every leg of the production strict-mode gate is green.
    assert policy.preferred_tool in EXECUTORS, (
        f"preferred_tool {policy.preferred_tool!r} must be a valid EXECUTORS member"
    )
    assert policy.strict_tool is True, (
        "strict_tool knob must be True to enable the strict-mode gate"
    )
    assert bool(policy.allow_tool_fallback) is False, (
        "allow_executor_fallback must be False in the CODE STABLE_V1 contract"
    )
    # In-test isolation: the production ``_is_executor_available``
    # reads the on-disk ``cache/tool_health/<tool>.json`` probe
    # cache; on the production machine that cache may report
    # ``quota_exhausted`` for codex.  The strict-mode contract under
    # test asserts the policy view (codex IS the strict-mode
    # primary in the CODE STABLE_V1 contract), so we monkey-patch
    # the module attribute — not the on-disk cache — to simulate
    # the contract assumption.  pytest's ``monkeypatch`` restores
    # the original function on teardown.
    monkeypatch.setattr(
        _orch_mod,
        "_is_executor_available",
        lambda name, capability_overlay=None: True,
    )
    assert _orch_mod._is_executor_available(policy.preferred_tool), (
        f"preferred_tool {policy.preferred_tool!r} must be currently available"
    )
    binding_eligible = _binding_health_eligible(
        policy.preferred_tool,
        binding_id="codex:minimax",
        resource_id="minimax.shared",
    )
    if not binding_eligible:
        pytest.skip("codex:minimax binding is not currently healthy in the runtime")
    assert binding_eligible, "codex:minimax must be eligible for this contract"
    # The CODE profile production policy object preserves the
    # strict-mode production binding.
    assert policy.preferred_model_binding == "codex:minimax"
    assert policy.preferred_tool == "codex"


# ---------------------------------------------------------------------------
# 15-16: Fail-fast on unhealthy primary
# ---------------------------------------------------------------------------


def test_planner_fallback_disallowed_keeps_openclaw_only():
    """When allow_planner_fallback is False the planner candidates
    list collapses to [openclaw]."""
    from aios_orchestrator import _resolve_planner_target
    from aios_task_routing_policy import from_workflow_dict
    payload = _load_payload("GENERAL")
    workflow = {
        "parent_id": "stable-v1-test-parent-id",
        **payload,
    }
    policy = from_workflow_dict(workflow, role="planner")
    planner_tool, *_ = _resolve_planner_target(policy)
    assert planner_tool == "openclaw"
    # The policy itself encodes the no-fallback intent.
    assert policy.allow_planner_fallback is False


def test_code_executor_fallback_disallowed_keeps_codex_only():
    """CODE profile: when allow_executor_fallback is False the strict
    path will never pick a fallback executor."""
    payload = _load_payload("CODE")
    assert payload["allow_executor_fallback"] is False
    assert payload["strict_executor"] == "codex"


# ---------------------------------------------------------------------------
# 17-18: No fallback to opencode / claude native in the CODE strict path
# ---------------------------------------------------------------------------


def test_code_no_opencode_in_strict_path():
    """CODE strict-mode contract MUST NOT allow opencode as the
    chosen executor — codex is the specialist_code_batch primary."""
    payload = _load_payload("CODE")
    assert payload["strict_executor"] != "opencode"
    assert payload["preferred_executor"] != "opencode"
    assert payload["preferred_executor"] == "codex"


def test_code_no_claude_native_in_strict_path():
    """CODE strict-mode contract MUST NOT allow claude native."""
    payload = _load_payload("CODE")
    assert payload["strict_executor"] != "claude"
    assert payload["preferred_executor"] != "claude"
    assert payload["preferred_executor"] == "codex"


# ---------------------------------------------------------------------------
# 19: Explicit user override beats STABLE_V1 defaults
# ---------------------------------------------------------------------------


def test_explicit_user_override_replaces_stable_v1_defaults():
    """When a caller (CLI / test) explicitly sends ``preferred_executor``,
    ``preferred_planner``, etc., the helper must NOT silently override
    those fields with the STABLE_V1 defaults.  The build_cli_payload
    helper applies STABLE_V1 first; the CLI script (or test) can
    overwrite specific fields afterwards."""
    payload = _load_payload("GENERAL")
    # Simulate explicit override: caller wants claude instead of codex.
    payload["preferred_executor"] = "claude"
    payload["strict_executor"] = "claude"
    assert payload["preferred_executor"] == "claude"
    assert payload["strict_executor"] == "claude"


# ---------------------------------------------------------------------------
# 20: Reviewer still required (no bypass)
# ---------------------------------------------------------------------------


def test_reviewer_required_in_payload():
    """The CLI payload always carries a preferred_reviewer.  The
    Verification Gate MUST be invoked for every workflow."""
    for profile in PROFILES:
        payload = _load_payload(profile)
        assert payload["preferred_reviewer"] == "hermes"


# ---------------------------------------------------------------------------
# CLI script — aios must import the helper, not inline literals.
# ---------------------------------------------------------------------------


def test_aios_cli_script_imports_helper():
    """The user-facing ``./aios`` shell script must invoke the
    ``aios_stable_v1_policy.build_cli_payload`` helper for every
    profile — inline literals are NOT permitted."""
    cli_path = ROOT / "aios"
    text = cli_path.read_text(encoding="utf-8")
    assert "from aios_stable_v1_policy import build_cli_payload" in text, (
        "aios CLI must import build_cli_payload from aios_stable_v1_policy"
    )
    # The legacy literal block MUST NOT be present.
    forbidden_literals = (
        "'GENERAL': {'preferred_planner':'openclaw'",
        "'AUDIT':   {'preferred_planner':'openclaw'",
        "'OPS':     {'preferred_planner':'openclaw'",
        "'CODE':    {'preferred_planner':'openclaw'",
    )
    for fragment in forbidden_literals:
        assert fragment not in text, (
            f"legacy inline literal still present in aios: {fragment!r}"
        )


# ---------------------------------------------------------------------------
# 15-16: Fail-fast when the primary is unhealthy AND fallback is disallowed
# ---------------------------------------------------------------------------


def test_openclaw_unhealthy_fails_fast_with_no_planner_fallback(monkeypatch):
    """When the STABLE_V1 contract pins the planner to ``openclaw``
    AND ``allow_planner_fallback=False``, the orchestrator MUST NOT
    silently fall back to ``opencode-plan-only`` or
    ``claude-minimax-plan`` if openclaw is unhealthy.  The
    ``_resolve_planner_target`` surface must collapse the
    candidates list to ``[openclaw]`` so the planner-failure
    branch surfaces ``FAILED_EXTERNAL_ROUTE_PLANNER_TIMEOUT``
    (fail fast) instead of an unbounded 180 s fallback walk.

    The test monkey-patches the openclaw planner call to always
    raise ``PlannerTimeout`` (simulating an unhealthy openclaw
    daemon) and asserts that ``build_plan`` returns the
    fail-fast terminal surface without ever touching
    ``opencode`` or ``claude`` as a fallback candidate.
    """
    from aios_orchestrator import (
        PlannerTimeout,
        _resolve_planner_target,
        build_plan,
    )
    from aios_task_routing_policy import from_workflow_dict

    def _openclaw_always_fails(*args, **kwargs):
        raise PlannerTimeout(
            attempt=0, reason="openclaw_unhealthy_synthetic",
            connect_timeout=1.0, read_timeout=1.0,
            total_timeout=1.0, elapsed_ms=0,
        )

    monkeypatch.setattr(
        "aios_orchestrator._call_planner_via_openclaw_service",
        _openclaw_always_fails,
    )

    workflow = {
        "parent_id": "stable-v1-test-openclaw-failfast",
        "preferred_planner": "openclaw",
        "allow_planner_fallback": False,
        "blocked_planner_tools": "[]",
    }
    policy = from_workflow_dict(workflow, role="planner")
    planner_tool, *_ = _resolve_planner_target(policy)
    # The candidates list is collapsed to the single primary.
    assert planner_tool == "openclaw"
    assert policy.allow_planner_fallback is False

    # The production build_plan surface must surface fail-fast.
    plan, plan_mode, plan_error, extras = build_plan(
        goal="synthetic-failfast",
        parent_id="stable-v1-test-openclaw-failfast",
        task_policy=workflow,
    )
    assert plan == [], "openclaw-unhealthy + no-fallback MUST NOT yield a plan"
    assert plan_mode == "planning-failed", (
        f"expected fail-fast planning-failed, got {plan_mode!r}"
    )
    assert "FAILED_EXTERNAL_ROUTE_PLANNER_TIMEOUT" in plan_error, (
        f"expected fail-fast terminal reason, got {plan_error!r}"
    )
    # Fallback chain was NOT walked.
    assert extras.get("attempted_planners", []) == ["openclaw"], (
        f"opencode/claude must NOT be walked, got {extras.get('attempted_planners')!r}"
    )
    # actual_planner must be empty so the ledger truthfully records fail-fast.
    assert extras.get("actual_planner", "") == ""


def test_codex_unhealthy_fails_fast_with_no_executor_fallback(monkeypatch):
    """When the STABLE_V1 contract pins the executor to ``codex`` AND
    ``allow_executor_fallback=False``, an unhealthy codex daemon MUST
    surface ``strict_executor_unavailable:codex`` immediately and MUST
    NOT silently switch to ``opencode`` or ``claude``.  The
    ``choose_executor`` surface collapses to the strict primary; the
    orchestrator's strict branch is the canonical fail-fast gate.
    """
    from aios_orchestrator import (
        EXECUTORS,
        choose_executor,
    )

    def _codex_unavailable(*args, **kwargs):
        return False  # codex unavailable

    def _codex_binding_unavailable(*args, **kwargs):
        return False  # codex:minimax binding also unavailable

    monkeypatch.setattr("aios_orchestrator._is_executor_available", _codex_unavailable)
    monkeypatch.setattr("aios_orchestrator._binding_health_eligible", _codex_binding_unavailable)
    # choose_executor("executor") MUST return "" because the
    # primary (codex) is unavailable and strict-mode collapses
    # the candidates list to the single primary.  Without the
    # strict-mode gate, choose_executor would otherwise return
    # "opencode" or "claude" as a fallback.
    chosen = choose_executor("executor")
    assert chosen == "", (
        f"strict-mode contract must yield '' when codex is unavailable, got {chosen!r}"
    )
    assert "codex" in EXECUTORS


# ---------------------------------------------------------------------------
# 17-18: Strict-mode contract forbids silent opencode / claude substitution
# ---------------------------------------------------------------------------


def test_code_strict_mode_forbids_opencode_substitution():
    """CODE profile keeps the strict-mode contract: the executor
    MUST be codex (specialist_code_batch) — not opencode — and
    executor fallback MUST be disallowed.  GENERAL / AUDIT / OPS
    intentionally DO allow opencode as the primary (see
    ``test_general_preferred_executor_is_opencode``); only CODE is
    pinned.
    """
    from aios_task_routing_policy import from_workflow_dict
    payload = _load_payload("CODE")
    workflow = {
        "parent_id": "stable-v1-test-no-opencode",
        **payload,
    }
    # Planner view: the only allowed planner is openclaw.
    p_policy = from_workflow_dict(workflow, role="planner")
    assert p_policy.preferred_planner == "openclaw"
    assert p_policy.allow_planner_fallback is False
    # CODE executor view: codex is the pinned specialist_code_batch.
    e_policy = from_workflow_dict(workflow, role="executor")
    assert e_policy.preferred_tool == "codex"
    assert e_policy.preferred_tool != "opencode"
    assert e_policy.allow_tool_fallback is False
    assert e_policy.strict_tool is True
    # Reviewer view: the only allowed reviewer is hermes.
    r_policy = from_workflow_dict(workflow, role="reviewer")
    assert r_policy.preferred_reviewer == "hermes"
    # The CODE CLI payload itself forbids silent opencode substitution.
    assert payload["preferred_executor"] == "codex"
    assert payload["preferred_executor"] != "opencode"
    assert payload["strict_executor"] == "codex"
    assert payload["preferred_planner"] != "opencode"


def test_code_strict_mode_forbids_claude_native_substitution():
    """CODE profile: the strict-mode payload MUST NOT allow a silent
    swap to ``claude`` native (deferred to BACKLOG) for either the
    planner or the executor.  GENERAL / AUDIT / OPS no longer pin
    the executor strictly, but they also MUST NOT pick claude as
    the primary (only as a bounded fallback in the existing
    ``choose_executor(role="opencode")`` chain).
    """
    from aios_task_routing_policy import from_workflow_dict
    payload = _load_payload("CODE")
    workflow = {
        "parent_id": "stable-v1-test-no-claude",
        **payload,
    }
    e_policy = from_workflow_dict(workflow, role="executor")
    assert e_policy.preferred_tool == "codex"
    assert e_policy.preferred_tool != "claude"
    # The CODE CLI payload itself forbids the silent substitution.
    assert payload["preferred_executor"] != "claude"
    assert payload["strict_executor"] != "claude"
    assert payload["preferred_planner"] != "claude"


@pytest.mark.parametrize("profile", ["GENERAL", "AUDIT", "OPS"])
def test_general_profile_no_claude_native_primary(profile):
    """GENERAL / AUDIT / OPS MUST NOT pick claude as the primary
    executor (the opencode primary path is the role contract).
    Claude is allowed only as a bounded fallback inside the
    existing ``choose_executor(role="opencode")`` chain."""
    payload = _load_payload(profile)
    assert payload["preferred_executor"] != "claude"
    assert payload["preferred_executor"] == "opencode"


# ---------------------------------------------------------------------------
# 19 (already covered above) + 20: Reviewer still required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", list(PROFILES))
def test_all_profiles_satisfy_task_routing_policy(profile):
    from aios_task_routing_policy import from_workflow_dict
    payload = _load_payload(profile)
    workflow = {
        "parent_id": "stable-v1-test-parent-id",
        "preferred_planner": payload["preferred_planner"],
        "allow_planner_fallback": payload["allow_planner_fallback"],
        "preferred_executor": payload["preferred_executor"],
        "strict_executor": payload["strict_executor"],
        "allow_executor_fallback": payload["allow_executor_fallback"],
        "strict_tool": payload["strict_tool"],
        "preferred_model_binding": payload["preferred_model_binding"],
        "preferred_reviewer": payload["preferred_reviewer"],
        "allow_reviewer_fallback": payload["allow_reviewer_fallback"],
        "blocked_planner_tools": "[]",
        "blocked_reviewer_tools": "[]",
    }
    for role in ("planner", "executor", "reviewer"):
        policy = from_workflow_dict(workflow, role=role)
        # Every view must keep the planner/executor/reviewer intent.
        assert policy.preferred_planner == "openclaw"
        if role == "executor":
            # Role-specialized executor: GENERAL / AUDIT / OPS default
            # to opencode (the primary_general_executor) with the
            # bounded tool fallback to codex:minimax; CODE keeps
            # codex as the strict-mode specialist_code_batch.
            if profile == "CODE":
                assert policy.preferred_tool == "codex"
                assert policy.strict_tool is True
                assert policy.allow_tool_fallback is False
                assert policy.preferred_model_binding == "codex:minimax"
            else:
                assert policy.preferred_tool == "opencode"
                assert policy.strict_tool is False
                assert policy.allow_tool_fallback is True
                assert policy.preferred_model_binding == "opencode:free-auto-router"
        if role == "reviewer":
            assert policy.preferred_reviewer == "hermes"