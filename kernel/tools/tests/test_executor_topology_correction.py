#!/usr/bin/env python3
"""AIOS Executor Topology Correction — role-specialized tests.

Closure: AIOS_EXECUTOR_TOPOLOGY_CORRECTION.

This test suite enforces the role-specialized executor contract
introduced by the topology correction.  The four production profiles
split into two role groups:

* GENERAL / AUDIT / OPS — restore the original ``primary_general_executor``
  opencode path with bounded tool fallback to codex:minimax.
* CODE — keeps the strict-mode ``specialist_code_batch`` codex path.

Each numbered test maps to a contract bullet the topology correction
spec lists.  Failures here mean the executor topology regressed, NOT
that a single integration was missed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios_stable_v1_policy as stable_v1  # noqa: E402


PROFILES_GENERAL = ("GENERAL", "AUDIT", "OPS")


# ---------------------------------------------------------------------------
# 1-6: profile-aware executor surface
# ---------------------------------------------------------------------------


def test_01_general_preferred_executor_is_opencode():
    assert stable_v1.build_cli_payload("GENERAL", "test")["preferred_executor"] == "opencode"


def test_02_general_preferred_binding_is_opencode_free_auto_router():
    assert (
        stable_v1.build_cli_payload("GENERAL", "test")["preferred_model_binding"]
        == "opencode:free-auto-router"
    )


def test_03_audit_preferred_executor_is_opencode():
    assert stable_v1.build_cli_payload("AUDIT", "test")["preferred_executor"] == "opencode"


def test_04_ops_preferred_executor_is_opencode():
    assert stable_v1.build_cli_payload("OPS", "test")["preferred_executor"] == "opencode"


def test_05_code_preferred_executor_is_codex():
    assert stable_v1.build_cli_payload("CODE", "test")["preferred_executor"] == "codex"


def test_06_code_preferred_binding_is_codex_minimax():
    assert (
        stable_v1.build_cli_payload("CODE", "test")["preferred_model_binding"]
        == "codex:minimax"
    )


# ---------------------------------------------------------------------------
# 7-9: opencode healthy -> GENERAL actual = opencode
#         opencode unhealthy -> GENERAL actual = codex (fallback)
# ---------------------------------------------------------------------------


def test_07_opencode_healthy_general_actual_opencode(monkeypatch):
    """When opencode is healthy, ``choose_executor(role="opencode")``
    must pick opencode as the actual executor.  This is the
    role-specialized runtime path; no production code change needed —
    the existing ``choose_executor`` already walks
    ``(opencode, claude, codex)`` for that role.
    """
    import aios_orchestrator as orch

    def fake_available(name, capability_overlay=None):
        return name == "opencode"

    monkeypatch.setattr(orch, "_is_executor_available", fake_available)
    monkeypatch.setattr(orch, "_executor_model_available", lambda name: True)
    monkeypatch.setattr(orch, "_tool_process_health", lambda name: True)
    monkeypatch.setattr(orch, "_get_tool_runtime_failure", lambda name: None)

    chosen = orch.choose_executor("opencode")
    assert chosen == "opencode"


def test_08_opencode_unhealthy_general_fallback_codex(monkeypatch):
    """When opencode is unhealthy, the bounded fallback walks to
    codex (the existing ``(opencode, claude, codex)`` order).  This
    is what keeps the production chain alive while opencode is
    degraded."""
    import aios_orchestrator as orch

    def fake_available(name, capability_overlay=None):
        return name == "codex"

    monkeypatch.setattr(orch, "_is_executor_available", fake_available)
    monkeypatch.setattr(orch, "_executor_model_available", lambda name: True)
    monkeypatch.setattr(orch, "_tool_process_health", lambda name: True)
    monkeypatch.setattr(orch, "_get_tool_runtime_failure", lambda name: None)

    chosen = orch.choose_executor("opencode")
    assert chosen == "codex"


def test_09_general_fallback_binding_is_codex_minimax():
    """The fallback binding for GENERAL/AUDIT/OPS is codex:minimax
    (matches the orchestrator's codex binding reality)."""
    ep = stable_v1.executor_policy_for_profile("GENERAL")
    assert ep["fallback_executor"] == "codex"
    assert ep["fallback_model_binding"] == "codex:minimax"


# ---------------------------------------------------------------------------
# 10-11: opencode recovers -> next task returns to opencode primary
#          CODE ignores opencode recovery
# ---------------------------------------------------------------------------


def test_10_opencode_recovery_re_enables_opencode(monkeypatch):
    """When opencode recovers (becomes available), ``choose_executor``
    must immediately pick it again.  This proves the topology
    correction auto-returns-to-primary on recovery without any
    user reconfiguration."""
    states = {"opencode_available": True}

    def fake_available(name, capability_overlay=None):
        return name == "opencode" and states["opencode_available"]

    import aios_orchestrator as orch
    monkeypatch.setattr(orch, "_is_executor_available", fake_available)
    monkeypatch.setattr(orch, "_executor_model_available", lambda name: True)
    monkeypatch.setattr(orch, "_tool_process_health", lambda name: True)
    monkeypatch.setattr(orch, "_get_tool_runtime_failure", lambda name: None)

    states["opencode_available"] = False
    assert orch.choose_executor("opencode") == ""
    states["opencode_available"] = True
    assert orch.choose_executor("opencode") == "opencode"


def test_11_code_profile_ignores_opencode_recovery():
    """CODE keeps the specialist_code_batch role: opencode recovery
    MUST NOT change CODE's primary tool to opencode.
    """
    payload = stable_v1.build_cli_payload("CODE", "test")
    assert payload["preferred_executor"] == "codex"
    assert payload["allow_executor_fallback"] is False
    assert payload["strict_executor"] == "codex"


# ---------------------------------------------------------------------------
# 12-13: shared planner / reviewer roles preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", list(PROFILES_GENERAL) + ["CODE"])
def test_12_reviewer_is_hermes_every_profile(profile):
    payload = stable_v1.build_cli_payload(profile, "test")
    assert payload["preferred_reviewer"] == "hermes"


@pytest.mark.parametrize("profile", list(PROFILES_GENERAL) + ["CODE"])
def test_13_planner_is_openclaw_every_profile(profile):
    payload = stable_v1.build_cli_payload(profile, "test")
    assert payload["preferred_planner"] == "openclaw"
    assert payload["allow_planner_fallback"] is False


# ---------------------------------------------------------------------------
# 14-17: frozen artifacts untouched
# ---------------------------------------------------------------------------


def test_14_message_budget_unchanged():
    """c9159e8 (Executor Message Budget) MUST NOT be touched by the
    topology correction."""
    import subprocess
    out = subprocess.check_output(
        ["git", "log", "--oneline", "c9159e8", "-1"],
        cwd="${AIOS_HOME}",
    ).decode()
    assert "executor message budget" in out.lower()


def test_15_host_evidence_unchanged():
    """a2969aa (Host Evidence + Evidence Lock) MUST NOT be touched."""
    import subprocess
    out = subprocess.check_output(
        ["git", "log", "--oneline", "a2969aa", "-1"],
        cwd="${AIOS_HOME}",
    ).decode()
    assert "host evidence" in out.lower() or "evidence" in out.lower()


def test_16_sandbox_unchanged():
    """Sandbox contract: no ``--no-sandbox`` flag introduced in the
    new policy / wiring path."""
    src = Path("${AIOS_HOME}/kernel/tools/aios_stable_v1_policy.py").read_text()
    assert "no-sandbox" not in src
    assert "dangerously-bypass" not in src


def test_17_registry_role_definitions_unchanged():
    """config/ai_registry.json role table is the single source of
    role truth; the topology correction does NOT modify it."""
    reg = json.load(open("${AIOS_HOME}/config/ai_registry.json"))
    by_id = {a["id"]: a for a in reg["agents"]}
    assert by_id["opencode"]["role"] == "primary_general_executor"
    assert by_id["codex"]["role"] == "specialist_code_batch"
    assert by_id["openclaw"]["role"] == "primary_channel_automation"
    assert by_id["hermes"]["role"] == "primary_memory_semantic_review"


# ---------------------------------------------------------------------------
# 18-20: routing architecture guards
# ---------------------------------------------------------------------------


def test_18_no_duplicate_router():
    """The topology correction does NOT add a new router layer; the
    existing ``choose_executor(role="opencode")`` machinery is the
    only fallback path."""
    ep = stable_v1.executor_policy_for_profile("GENERAL")
    # The fallback chain for GENERAL is the EXISTING production order.
    assert ep["fallback_chain"] == ("opencode", "claude", "codex")


def test_19_fallback_bounded():
    """GENERAL/AUDIT/OPS fallback is bounded: the chain has at most
    three tools and CODE profile has empty chain (no fallback at
    all)."""
    for profile in PROFILES_GENERAL:
        ep = stable_v1.executor_policy_for_profile(profile)
        assert len(ep["fallback_chain"]) <= 3
    ep_code = stable_v1.executor_policy_for_profile("CODE")
    assert ep_code["fallback_chain"] == ()


def test_20_no_fallback_loop():
    """The fallback chain MUST NOT include the primary tool at a
    later position (no ``opencode -> codex -> opencode`` loop).
    """
    for profile in PROFILES_GENERAL:
        ep = stable_v1.executor_policy_for_profile(profile)
        primary = ep["preferred_executor"]
        chain = ep["fallback_chain"]
        assert chain[0] == primary
        # Primary may only appear in position 0 — no later entry,
        # otherwise the bounded tool selection loop would re-select
        # the primary after a failure event TTL elapses.
        assert primary not in chain[1:]
