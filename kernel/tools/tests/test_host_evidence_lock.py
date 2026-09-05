#!/usr/bin/env python3
"""AIOS Host-Evidence Lock (HF-* anchor) tests.

Pinned 2026-08-11.  No new evidence store, no new service, no
new agent.  The evidence lock is a pure function over
``aios_host_readonly_evidence.collect_host_evidence`` output.

Test cases:

1. ``MainPID=82472`` in evidence; LLM writes ``MainPID=56225``
   → 56225 must NOT appear in the cleaned deliverable.
2. ``MainPID=82472`` must appear (program-generated Verified
   Facts block).
3. HF-* ids are referenced in the verified block.
4. ``service active/inactive`` conflicts are caught.
5. Recommendations survive the filter.
6. End-to-end: the wired ``_executor_payload`` path still routes
   through the lock without breaking the existing test contract.
7. Sandbox flag is absent from any production command surface.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _ops_evidence():
    from aios_host_readonly_evidence import collect_host_evidence
    return collect_host_evidence(
        "OPS", "check service",
        explicit_units=["aios-orchestrator.service"],
    )


def test_01_conflicting_main_pid_is_filtered_out():
    from aios_host_evidence_lock import (
        extract_anchor_facts,
        filter_fact_conflicts,
    )
    evidence = _ops_evidence()
    anchors = extract_anchor_facts(evidence)
    assert anchors, "evidence must contain at least one anchor"
    main_pid = next(
        (a for a in anchors if a["kind"] == "main_pid"), None,
    )
    assert main_pid is not None, "evidence must expose MainPID anchor"
    real = main_pid["value"]
    fake = "0" if real != "0" else "999999"
    deliverable = (
        f"## Analysis\n"
        f"- aios-orchestrator.service Main PID: {fake} (hallucinated)\n"
        f"- aios-orchestrator.service Memory: 33.5M (correct)\n"
    )
    cleaned, conflicts = filter_fact_conflicts(deliverable, anchors)
    # The conflict must be DELETED outright from the cleaned text
    # (no marker, no fabrication).  The line containing the bad
    # value must be gone.
    has_fake_as_primary = any(
        line.strip().startswith(f"- aios-orchestrator.service Main PID: {fake} ")
        for line in cleaned.splitlines()
    )
    assert not has_fake_as_primary, (
        f"fabricated Main PID {fake} must be deleted from cleaned"
    )
    assert any(c["kind"] == "main_pid" for c in conflicts), (
        "the conflict must be reported"
    )


def test_02_verified_block_includes_real_pid():
    from aios_host_evidence_lock import (
        extract_anchor_facts,
        render_verified_facts_block,
    )
    evidence = _ops_evidence()
    anchors = extract_anchor_facts(evidence)
    main_pid = next(a for a in anchors if a["kind"] == "main_pid")
    real = main_pid["value"]
    block = render_verified_facts_block(anchors)
    assert real in block, f"real MainPID={real} must appear in verified block"
    assert "Main PID" in block or "main_pid" in block


def test_03_hf_ids_are_stable():
    from aios_host_evidence_lock import extract_anchor_facts
    evidence = _ops_evidence()
    anchors = extract_anchor_facts(evidence)
    ids = [a["id"] for a in anchors]
    assert all(i.startswith("HF-") for i in ids)
    # Stable: same evidence → same ids in same order.
    anchors2 = extract_anchor_facts(evidence)
    ids2 = [a["id"] for a in anchors2]
    assert ids == ids2


def test_04_service_active_state_conflicts_caught():
    from aios_host_evidence_lock import (
        extract_anchor_facts,
        filter_fact_conflicts,
    )
    evidence = _ops_evidence()
    anchors = extract_anchor_facts(evidence)
    deliverable = (
        "## Analysis\n"
        "- aios-orchestrator.service Active: inactive (failed)\n"
    )
    cleaned, conflicts = filter_fact_conflicts(deliverable, anchors)
    # The fabricated state line must be deleted outright (no
    # marker, no leftover string).
    has_inactive_as_primary = any(
        line.strip().startswith("- aios-orchestrator.service Active: inactive")
        for line in cleaned.splitlines()
    )
    assert not has_inactive_as_primary, (
        "fabricated state must be deleted from cleaned"
    )
    assert any(c["kind"] in ("active", "active_state") for c in conflicts)


def test_05_recommendations_survive():
    from aios_host_evidence_lock import (
        extract_anchor_facts,
        filter_fact_conflicts,
    )
    evidence = _ops_evidence()
    anchors = extract_anchor_facts(evidence)
    deliverable = (
        "## Analysis\n"
        "- aios-orchestrator.service Main PID: 0 (wrong)\n"
        "## Recommendations\n"
        "- audit memory usage\n"
        "- check restart count\n"
    )
    cleaned, conflicts = filter_fact_conflicts(deliverable, anchors)
    assert "audit memory usage" in cleaned
    assert "check restart count" in cleaned


def test_05b_prose_no_failure_contradicts_failed_unit():
    """When the evidence contains a ``failed_unit`` anchor
    (e.g. ``aios-acceptance.service``), the deliverable MUST NOT
    claim ``无失败`` / ``no failed unit`` / ``no failure`` — those
    prose phrases are direct contradictions and must be removed.
    """
    from aios_host_evidence_lock import (
        extract_anchor_facts,
        filter_fact_conflicts,
    )
    evidence = _ops_evidence()
    anchors = extract_anchor_facts(evidence)
    if not any(a.get("kind") == "failed_unit" for a in anchors):
        # Skip if production cache has no failed unit; this
        # is a best-effort unit test that depends on the live
        # system state at run time.
        return
    failed_unit = next(
        a["value"] for a in anchors if a.get("kind") == "failed_unit"
    )
    deliverable = (
        "## Analysis\n"
        "- 没有失败单元，系统稳定\n"
        "## Recommendations\n"
        "- 维持现状\n"
    )
    cleaned, conflicts = filter_fact_conflicts(deliverable, anchors)
    assert "没有失败单元" not in cleaned, (
        "fabricated no-failure claim must be removed"
    )
    assert any(c["kind"] == "failed_unit" for c in conflicts)
    assert any(c.get("expected") == failed_unit for c in conflicts)
    assert "维持现状" in cleaned


def test_06_executor_payload_path_unchanged():
    from aios_verification_gate import _executor_payload
    text = "[codex] hello world"
    assert _executor_payload(text, "codex") == "hello world"
    text2 = "Claude Code: status"
    assert _executor_payload(text2, "claude") == "status"
    text3 = "no prefix"
    assert _executor_payload(text3, "codex") == "no prefix"


def test_07_sandbox_flag_absent_from_production_surface():
    """NO_GLOBAL_SANDBOX_BYPASS contract: no ``--no-sandbox`` /
    ``--dangerously-bypass-approvals-and-sandbox`` flag may appear
    in the production codex invocation surface.
    """
    import re
    surface = Path("${AIOS_HOME}/kernel/tools/aios_codex_client.sh").read_text()
    surface += Path("${AIOS_HOME}/config/tool_adapters.json").read_text()
    for forbidden in (
        "dangerously-bypass-approvals-and-sandbox",
        "--no-sandbox",
    ):
        assert forbidden not in surface, (
            f"forbidden flag {forbidden!r} must not appear in production surface"
        )
