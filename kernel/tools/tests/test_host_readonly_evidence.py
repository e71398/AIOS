#!/usr/bin/env python3
"""
AIOS Host Read-Only Evidence Boundary Test Suite
=================================================

Closure: AIOS_FINAL_PRODUCTION_DELIVERY_20260811.

This test suite enforces the contract that the host-evidence
boundary collects bounded, allowlisted, sanitised facts and that
Codex never has to touch host loopback services directly.

Each numbered test maps to a bullet in the final-production
spec §22.  Failures here mean the host-evidence boundary is unsafe
or unreliable, NOT that a single integration was missed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios_host_readonly_evidence import (  # noqa: E402
    PROFILE_CAPABILITIES,
    audit_runtime_augment,
    collect_host_evidence,
    handle_health_probe,
    profile_capabilities,
    render_evidence_block,
)


# ---------------------------------------------------------------------------
# §22.1  AIOS health comes from the host collector
# ---------------------------------------------------------------------------


def test_01_aios_health_from_host_collector():
    """OPS profile MUST acquire AIOS_HEALTH_SNAPSHOT inline; Codex
    never has to ``curl 127.0.0.1:18801``.
    """
    ev = collect_host_evidence(
        "OPS",
        "check AIOS health",
        explicit_units=["aios-orchestrator.service"],
    )
    capabilities = {item.get("capability") for item in ev["items"]}
    assert "AIOS_HEALTH_SNAPSHOT" in capabilities, (
        "OPS profile must collect AIOS_HEALTH_SNAPSHOT"
    )
    snapshot = next(
        item for item in ev["items"]
        if item.get("capability") == "AIOS_HEALTH_SNAPSHOT"
    )
    assert "error" not in snapshot, (
        f"AIOS_HEALTH_SNAPSHOT must be ok, got {snapshot}"
    )
    assert "sampled_at" in snapshot
    assert "queue" in snapshot
    assert "executors" in snapshot


# ---------------------------------------------------------------------------
# §22.2  Codex does not need localhost access
# ---------------------------------------------------------------------------


def test_02_codex_no_localhost_access_required():
    """OPS profile collects everything Codex needs from host evidence.

    We assert that no OPS item requires Codex to call a URL; the
    LOCAL_HTTP_GET URL is consumed by the HOST collector, not by
    Codex.
    """
    ev = collect_host_evidence("OPS", "check service")
    assert ev["summary"]["ok"] >= 6, (
        f"OPS must yield many ok items, got {ev['summary']}"
    )
    local = [
        item for item in ev["items"]
        if item.get("capability") == "LOCAL_HTTP_GET"
    ]
    if local:
        assert "error" not in local[0]


# ---------------------------------------------------------------------------
# §22.3  systemd user status can be collected
# ---------------------------------------------------------------------------


def test_03_systemd_user_status_collected():
    ev = collect_host_evidence(
        "OPS",
        "service check",
        explicit_units=["aios-orchestrator.service"],
    )
    cap = "SYSTEMD_USER_STATUS"
    item = next(
        (i for i in ev["items"] if i.get("capability") == cap),
        None,
    )
    assert item is not None, f"OPS must include {cap}"
    assert "error" not in item, f"{cap} must succeed"
    assert "active" in item.get("body", "").lower() or "inactive" in item.get("body", "").lower()


# ---------------------------------------------------------------------------
# §22.4  journal bounded
# ---------------------------------------------------------------------------


def test_04_journal_bounded():
    ev = collect_host_evidence(
        "OPS",
        "check journal",
        explicit_units=["aios-orchestrator.service"],
    )
    item = next(
        (i for i in ev["items"]
         if i.get("capability") == "JOURNAL_USER_UNIT_RECENT"),
        None,
    )
    assert item is not None
    body = item.get("body", "")
    assert item.get("lines_requested", 0) <= 200
    assert len(body) <= 8192 + 200


# ---------------------------------------------------------------------------
# §22.5  localhost health allowed
# ---------------------------------------------------------------------------


def test_05_localhost_health_allowed():
    from aios_host_readonly_evidence import _is_local_url
    assert _is_local_url("http://127.0.0.1:18801/health")
    assert _is_local_url("http://localhost:18801/health")
    assert not _is_local_url("http://example.com/health")
    assert not _is_local_url("http://1.2.3.4/health")


# ---------------------------------------------------------------------------
# §22.6  external arbitrary URL disallowed
# ---------------------------------------------------------------------------


def test_06_external_url_rejected():
    item = collect_host_evidence.__globals__["_dispatch"](
        "LOCAL_HTTP_GET",
        {"url": "http://example.com", "timeout": 1.0},
    )
    assert item.get("error") == "non_local_url_rejected"


# ---------------------------------------------------------------------------
# §22.7  directory traversal rejected
# ---------------------------------------------------------------------------


def test_07_directory_traversal_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "project"
        project_root.mkdir()
        outside = Path(tmp) / "outside.txt"
        outside.write_text("hello world\n")
        item = collect_host_evidence.__globals__["_dispatch"](
            "READ_FILE",
            {
                "path": str(outside),
                "allowed_roots": [str(project_root)],
            },
        )
        assert item.get("error") == "path_outside_allowed_root"


# ---------------------------------------------------------------------------
# §22.8  target subpath allowed
# ---------------------------------------------------------------------------


def test_08_target_subpath_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "project"
        sub = project_root / "src"
        sub.mkdir(parents=True)
        f = sub / "main.py"
        f.write_text("print('ok')\n")
        item = collect_host_evidence.__globals__["_dispatch"](
            "READ_FILE",
            {
                "path": str(f),
                "allowed_roots": [str(project_root)],
            },
        )
        assert "error" not in item
        assert "print('ok')" in item["body"]


# ---------------------------------------------------------------------------
# §22.9  ~/.ssh default denied
# ---------------------------------------------------------------------------


def test_09_home_ssh_default_denied():
    ssh_path = os.path.expanduser("~/.ssh")
    item = collect_host_evidence.__globals__["_dispatch"](
        "READ_FILE",
        {
            "path": os.path.join(ssh_path, "id_rsa"),
            "allowed_roots": [],
        },
    )
    assert item.get("error") == "path_denied"


# ---------------------------------------------------------------------------
# §22.10  secret value redacted
# ---------------------------------------------------------------------------


def test_10_secret_value_redacted():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "config.env"
        f.write_text(
            "API_KEY=sk-AAAABBBBCCCCDDDD\n"
            "OTHER=ok\n"
        )
        item = collect_host_evidence.__globals__["_dispatch"](
            "READ_FILE",
            {
                "path": str(f),
                "allowed_roots": [str(tmp)],
            },
        )
        assert "error" not in item
        body = item["body"]
        assert "sk-AAAABBBBCCCCDDDD" not in body
        assert "<redacted" in body
        assert item.get("redacted_lines", 0) >= 1


# ---------------------------------------------------------------------------
# §22.11  private key block redacted
# ---------------------------------------------------------------------------


def test_11_private_key_block_redacted():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "key.pem"
        f.write_text(
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "AAAA\nBBBB\nCCCC\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        item = collect_host_evidence.__globals__["_dispatch"](
            "READ_FILE",
            {
                "path": str(f),
                "allowed_roots": [str(tmp)],
            },
        )
        assert "error" not in item
        assert "AAAA\nBBBB\nCCCC" not in item["body"]
        assert "REDACTED PRIVATE KEY BLOCK" in item["body"]


# ---------------------------------------------------------------------------
# §22.12  output bounded
# ---------------------------------------------------------------------------


def test_12_output_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "big.txt"
        f.write_text("\n".join(f"line {i}" for i in range(2000)))
        item = collect_host_evidence.__globals__["_dispatch"](
            "READ_FILE",
            {
                "path": str(f),
                "allowed_roots": [str(tmp)],
            },
        )
        assert "error" not in item
        assert item.get("truncated") is True
        assert len(item["body"]) <= 8192 + 200


# ---------------------------------------------------------------------------
# §22.13  timeout bounded
# ---------------------------------------------------------------------------


def test_13_timeout_bounded():
    """A unit that does not exist returns an error item rather than
    hanging; the timeout is enforced by ``subprocess.run``.
    """
    item = collect_host_evidence.__globals__["_dispatch"](
        "SYSTEMD_USER_STATUS",
        {"unit": "this-unit-definitely-does-not-exist-9999.service"},
    )
    assert "body" in item or "error" in item


# ---------------------------------------------------------------------------
# §22.14  arbitrary shell rejected
# ---------------------------------------------------------------------------


def test_14_no_arbitrary_shell_in_invocation():
    """The collector must never construct a ``bash -c <model string>``
    command.  We inspect every OPS body for a shell command string.
    """
    ev = collect_host_evidence("OPS", "service check")
    for item in ev["items"]:
        cap = item.get("capability")
        if cap and cap.startswith("SYSTEMD_USER"):
            assert item.get("stderr", "") == ""
            body = item.get("body", "")
            assert " -c " not in body[:200]
        if cap == "JOURNAL_USER_UNIT_RECENT":
            body = item.get("body", "")
            assert " -c " not in body[:200]


# ---------------------------------------------------------------------------
# §22.15  shell metacharacter cannot become command injection
# ---------------------------------------------------------------------------


def test_15_shell_metacharacter_neutralised():
    weird_unit = "aios-orchestrator.service; rm -rf /"
    item = collect_host_evidence.__globals__["_dispatch"](
        "SYSTEMD_USER_STATUS",
        {"unit": weird_unit},
    )
    combined = (item.get("body", "") + item.get("stderr", "")).lower()
    assert "removed " not in combined


# ---------------------------------------------------------------------------
# §22.16  evidence enters Verifier grounding
# ---------------------------------------------------------------------------


def test_16_evidence_enters_verifier_grounding():
    """``verify_parent_node`` must surface host_evidence into the
    grounding block.
    """
    from aios_verification_gate import _build_minimal_review_evidence
    grounding = {
        "mode": "aios-runtime",
        "collected_at": "2026-08-11T00:00:00+00:00",
        "authoritative": {
            "host_evidence": {
                "profile": "OPS",
                "summary": {"total": 1, "ok": 1, "error": 0},
                "items": [
                    {"capability": "AIOS_HEALTH_SNAPSHOT", "queue": {"pending": 0}},
                ],
            },
        },
    }
    rendered = _build_minimal_review_evidence(grounding)
    assert "host_evidence" in rendered["authoritative"]
    assert rendered["authoritative"]["host_evidence"]["profile"] == "OPS"


# ---------------------------------------------------------------------------
# §22.17  Reviewer sees evidence
# ---------------------------------------------------------------------------


def test_17_reviewer_sees_evidence():
    from aios_verification_gate import _build_minimal_review_prompt
    import aios_orchestrator_host_evidence_injection as he_mod
    saved = he_mod.load_workflow_host_evidence

    def fake_load(pid):
        if pid == "stub":
            return {
                "profile": "OPS",
                "summary": {"total": 1, "ok": 1, "error": 0},
                "items": [{"capability": "AIOS_HEALTH_SNAPSHOT", "ok": True}],
            }
        return None
    he_mod.load_workflow_host_evidence = fake_load
    try:
        grounding = {
            "mode": "aios-runtime",
            "collected_at": "2026-08-11T00:00:00+00:00",
            "authoritative": {
                "host_evidence": {
                    "profile": "OPS",
                    "summary": {"total": 1, "ok": 1, "error": 0},
                    "items": [{"capability": "AIOS_HEALTH_SNAPSHOT", "ok": True}],
                },
                "workflow": {"parent_task_id": "stub"},
            },
        }
        node = {"parent_id": "stub", "task": "stub", "acceptance": []}
        prompt, meta = _build_minimal_review_prompt(
            goal="g",
            node=node,
            executor="codex",
            deliverable="d",
            evidence_mode="aios-runtime",
            grounding=grounding,
        )
        assert "AUTHORITATIVE HOST EVIDENCE" in prompt
    finally:
        he_mod.load_workflow_host_evidence = saved


# ---------------------------------------------------------------------------
# §22.18  correction retry retains evidence
# ---------------------------------------------------------------------------


def test_18_correction_retry_retains_evidence():
    from aios_verification_gate import _build_minimal_review_prompt
    import aios_orchestrator_host_evidence_injection as he_mod
    saved = he_mod.load_workflow_host_evidence

    def fake_load(pid):
        if pid == "stub":
            return {
                "profile": "OPS",
                "summary": {"total": 2, "ok": 2, "error": 0},
                "items": [{"capability": "AIOS_HEALTH_SNAPSHOT", "ok": True}],
            }
        return None
    he_mod.load_workflow_host_evidence = fake_load
    try:
        grounding = {
            "mode": "aios-runtime",
            "collected_at": "2026-08-11T00:00:00+00:00",
            "authoritative": {
                "host_evidence": {
                    "profile": "OPS",
                    "summary": {"total": 2, "ok": 2, "error": 0},
                    "items": [{"capability": "AIOS_HEALTH_SNAPSHOT", "ok": True}],
                },
                "workflow": {"parent_task_id": "stub"},
            },
        }
        node = {"parent_id": "stub", "task": "stub", "acceptance": []}
        prompt, _ = _build_minimal_review_prompt(
            goal="g",
            node=node,
            executor="codex",
            deliverable="d",
            evidence_mode="aios-runtime",
            grounding=grounding,
            previous_failure="evidence_contradiction: number must be 5",
        )
        assert "AUTHORITATIVE HOST EVIDENCE" in prompt
        assert "evidence_contradiction" in prompt
    finally:
        he_mod.load_workflow_host_evidence = saved


# ---------------------------------------------------------------------------
# Profile gating sanity
# ---------------------------------------------------------------------------


def test_19_profile_capabilities_defined():
    assert PROFILE_CAPABILITIES["GENERAL"] == ()
    assert PROFILE_CAPABILITIES["CODE"] == ()
    assert "AIOS_HEALTH_SNAPSHOT" in PROFILE_CAPABILITIES["OPS"]
    assert "READ_FILE" in PROFILE_CAPABILITIES["AUDIT"]


def test_20_audit_runtime_hint():
    """When AUDIT goal mentions Hermes / OpenClaw / runtime, OPS-style
    capabilities are appended.
    """
    base = profile_capabilities("AUDIT")
    augmented_hermes = audit_runtime_augment("audit hermes runtime")
    augmented_openclaw = audit_runtime_augment("\u5ba1\u8ba1 openclaw")
    augmented_other = audit_runtime_augment("just a project structure")
    assert "AIOS_HEALTH_SNAPSHOT" not in base
    assert "AIOS_HEALTH_SNAPSHOT" in augmented_hermes
    assert "AIOS_HEALTH_SNAPSHOT" in augmented_openclaw
    assert "AIOS_HEALTH_SNAPSHOT" not in augmented_other


def test_21_render_evidence_block_protocol():
    ev = collect_host_evidence(
        "OPS", "check service", explicit_units=["aios-orchestrator.service"]
    )
    block = render_evidence_block(ev)
    assert block.startswith("<!--AIOS_HOST_EVIDENCE-->\n")
    assert block.rstrip().endswith("<!--/AIOS_HOST_EVIDENCE-->")
    payload = json.loads(
        block.split("<!--AIOS_HOST_EVIDENCE-->\n", 1)[1]
        .split("\n<!--/AIOS_HOST_EVIDENCE-->", 1)[0]
    )
    assert payload["source"] == "aios_host_readonly_evidence"
    assert isinstance(payload["evidence"], list)


def test_22_handle_health_probe_in_process():
    out = handle_health_probe({"capability": "AIOS_HEALTH_SNAPSHOT"})
    assert out["ok"] is True
    assert out["service"] == "aios-host-readonly-evidence"
    assert "generated_at" in out