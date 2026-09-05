#!/usr/bin/env python3
"""
AIOS Host-Evidence Profile Contract Test Suite
==============================================

Closure: AIOS_FINAL_PRODUCTION_DELIVERY_20260811.

This test suite enforces the contract that the four CLI profiles
(GENERAL / AUDIT / OPS / CODE) drive the host-evidence boundary
deterministically and that:

  * the STABLE_V1 routing surface is NEVER overridden by host-evidence;
  * CODE profile keeps Codex sandbox ENABLED (no host shell bypass);
  * host evidence is bounded, sandbox-safe, and profile-gated.

Each numbered test maps to a bullet in the final-production
spec §23 / §24.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios_stable_v1_policy as stable_v1  # noqa: E402
from aios_host_readonly_evidence import (  # noqa: E402
    PROFILE_CAPABILITIES,
    collect_host_evidence,
)


# ---------------------------------------------------------------------------
# §23.19 GENERAL normal task → no unnecessary host evidence
# ---------------------------------------------------------------------------


def test_19_general_normal_task_has_minimal_evidence():
    """A plain ``./aios task`` with no host keywords MUST NOT pull
    host-only probes.
    """
    ev = collect_host_evidence("GENERAL", "what is the capital of France")
    assert ev["profile"] == "GENERAL"
    assert ev["items"] == []
    assert ev["summary"]["total"] == 0


def test_19_general_host_status_request_bounded():
    """When the user explicitly asks about AIOS / service / status, a
    minimal host-evidence set is collected, but NEVER unbounded.
    """
    ev = collect_host_evidence(
        "GENERAL",
        "\u68c0\u67e5\u5f53\u524d AIOS \u8fd0\u884c\u72b6\u6001",
        explicit_units=["aios-orchestrator.service"],
    )
    assert ev["summary"]["total"] <= 24
    for item in ev["items"]:
        body = item.get("body", "")
        assert len(body) <= 8192 + 256, (
            f"item exceeded bounded output cap: {item.get('capability')}"
        )


# ---------------------------------------------------------------------------
# §23.21 AUDIT project → project evidence
# ---------------------------------------------------------------------------


def test_21_audit_project_collects_project_evidence():
    """``./aios audit /some/project`` MUST collect READ_FILE /
    LIST_DIRECTORY / FILE_METADATA / GIT_* for the project.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "proj"
        project.mkdir()
        (project / "README.md").write_text("# Test\n")
        ev = collect_host_evidence(
            "AUDIT",
            "audit the project structure",
            project_path=str(project),
        )
        caps = {item.get("capability") for item in ev["items"]}
        assert "READ_FILE" in caps
        assert "LIST_DIRECTORY" in caps
        assert "FILE_METADATA" in caps
        assert "GIT_STATUS" in caps
        assert "GIT_LOG" in caps
        assert "GIT_BRANCH" in caps


# ---------------------------------------------------------------------------
# §23.22 AUDIT runtime tool → runtime evidence
# ---------------------------------------------------------------------------


def test_22_audit_runtime_tool_collects_runtime_evidence():
    """An AUDIT goal that mentions Hermes / OpenClaw / runtime terms
    appends OPS-style capabilities.
    """
    ev = collect_host_evidence(
        "AUDIT",
        "audit hermes production runtime",
    )
    caps = {item.get("capability") for item in ev["items"]}
    assert "AIOS_HEALTH_SNAPSHOT" in caps
    assert "SYSTEMD_USER_STATUS" in caps
    assert "LISTENING_PORTS" in caps


# ---------------------------------------------------------------------------
# §23.23 OPS → system evidence
# ---------------------------------------------------------------------------


def test_23_ops_collects_system_evidence():
    """OPS profile MUST collect systemd / journal / ports / AIOS health.
    """
    ev = collect_host_evidence(
        "OPS",
        "\u68c0\u67e5\u670d\u52a1\u95ee\u9898",
        explicit_units=["aios-orchestrator.service"],
    )
    caps = {item.get("capability") for item in ev["items"]}
    for required in (
        "SYSTEMD_USER_STATUS",
        "SYSTEMD_USER_SHOW",
        "SYSTEMD_USER_FAILED",
        "JOURNAL_USER_UNIT_RECENT",
        "PROCESS_LOOKUP",
        "LISTENING_PORTS",
        "LOCAL_HTTP_GET",
        "AIOS_HEALTH_SNAPSHOT",
    ):
        assert required in caps, f"OPS missing {required}, got {caps}"


# ---------------------------------------------------------------------------
# §23.24 CODE → sandbox remains enabled
# ---------------------------------------------------------------------------


def test_24_code_profile_sandbox_safe():
    """CODE profile MUST NOT pull host-only probes; Codex sandbox
    is the canonical execution environment.
    """
    ev = collect_host_evidence("CODE", "fix a bug")
    assert ev["items"] == []


def test_25_code_no_host_arbitrary_execution():
    """CODE profile cannot smuggle in ``bash -c`` shell access via
    host evidence.
    """
    ev = collect_host_evidence(
        "CODE",
        "fix bug with project=/tmp/example-project",
        project_path="${AIOS_HOME}",
    )
    assert ev["items"] == []
    # The CLI payload's host_evidence_profile MUST stay CODE.
    payload = stable_v1.build_cli_payload("CODE", "fix bug")
    assert payload["host_evidence_profile"] == "CODE"


# ---------------------------------------------------------------------------
# §23.26  All profiles retain STABLE_V1 routing
# ---------------------------------------------------------------------------


def test_26_all_profiles_retain_stable_v1_routing():
    """The host-evidence surface MUST NOT touch the STABLE_V1 routing.

    After AIOS_EXECUTOR_TOPOLOGY_CORRECTION the executor surface is
    role-specialized: GENERAL / AUDIT / OPS default to opencode
    primary (with bounded fallback to codex:minimax), CODE keeps
    the strict-mode codex:minimax contract.  Planner and Reviewer
    fields stay shared across profiles.
    """
    for profile in ("GENERAL", "AUDIT", "OPS", "CODE"):
        payload = stable_v1.build_cli_payload(profile, "test")
        assert payload["preferred_planner"] == "openclaw"
        assert payload["allow_planner_fallback"] is False
        assert payload["preferred_reviewer"] == "hermes"
        assert payload["allow_reviewer_fallback"] is True
        if profile == "CODE":
            assert payload["preferred_executor"] == "codex"
            assert payload["strict_executor"] == "codex"
            assert payload["allow_executor_fallback"] is False
            assert payload["strict_tool"] is True
            assert payload["preferred_model_binding"] == "codex:minimax"
        else:
            assert payload["preferred_executor"] == "opencode"
            assert payload["strict_executor"] == ""
            assert payload["allow_executor_fallback"] is True
            assert payload["strict_tool"] is False
            assert payload["preferred_model_binding"] == "opencode:free-auto-router"


# ---------------------------------------------------------------------------
# §24  Sandbox security regression
# ---------------------------------------------------------------------------


def test_sandbox_security_boundary_passes():
    """The Codex sandbox MUST remain ENABLED by default; no global
    bypass is allowed by host-evidence plumbing.
    """
    gateway_src = (Path("${AIOS_HOME}/kernel/tools/aios_entry_gateway.py")
                  .read_text(encoding="utf-8"))
    for forbidden in (
        "dangerously-bypass-approvals-and-sandbox",
        "--no-sandbox",
    ):
        assert forbidden not in gateway_src, (
            f"forbidden flag found in gateway source: {forbidden}"
        )
    adapter_path = Path(
        "${AIOS_HOME}/config/tool_adapters.json"
    )
    if adapter_path.is_file():
        cfg = json.loads(adapter_path.read_text(encoding="utf-8"))
        codex_cfg = cfg.get("tools", {}).get("codex", {})
        cmd = codex_cfg.get("command", "")
        assert "dangerously-bypass-approvals-and-sandbox" not in cmd
        assert "--no-sandbox" not in cmd


def test_no_global_sandbox_bypass_constant():
    """The V1 contract explicitly forbids a global bypass service.
    """
    assert PROFILE_CAPABILITIES["CODE"] == ()
    assert PROFILE_CAPABILITIES["GENERAL"] == ()


def test_host_evidence_does_not_override_routing():
    """Calling ``collect_host_evidence`` does NOT touch the routing
    surface.
    """
    snapshot_before = stable_v1.stable_v1_routing_policy()
    collect_host_evidence("OPS", "check service")
    collect_host_evidence("AUDIT", "audit", project_path="/tmp")
    snapshot_after = stable_v1.stable_v1_routing_policy()
    assert snapshot_before == snapshot_after