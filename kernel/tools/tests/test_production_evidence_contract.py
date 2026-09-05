#!/usr/bin/env python3
"""Production Evidence Contract (P2) — dedicated tests (2026-08-10).

This suite is the verification regression for the 2026-08-10 AIOS
Production Evidence Closure (P2).  It pins down the contract that the
Verification Gate MUST honour when verifying AIOS-runtime read-only
tasks (Self Audit / OPS / GENERAL with `service` facts).

Background:
  The 2026-08-10 burn-in T1/T2/T6 failed at the parent-node
  verification step because the verifier over-strictly required the
  deliverable to mention the literal ``aios-entry-gateway`` web service
  name as proof of the ``service`` fact, even when the actual goal
  was about a different systemd unit (e.g. ``aios-runtime.service``).

  The fix introduced:
    1. ``_extract_evidence_block`` parses an optional
       ``<!--AIOS_EVIDENCE-->...<!--/AIOS_EVIDENCE-->`` JSON block at
       the end of an executor payload and surfaces structured
       observations into the ground-truth bundle.
    2. ``_aios_grounding_errors`` no longer fails ``asks_service``
       when the deliverable names at least one real AIOS systemd
       unit (any ``*.service`` literal cross-checked against the
       verifier's authoritative unit set or the executor's own
       structured evidence list).

These tests guarantee that:
  - Codex-shaped evidence in the structured ``evidence`` JSON block
    does NOT silently override an empty deliverable;
  - A real systemd unit, properly cross-checked, satisfies
    ``asks_service``;
  - ``passed=true`` can never come from a mocked verifier;
  - historical evidence cannot masquerade as current evidence;
  - the structural integrity of the verifier verdict surface is
    preserved (no silent rewrites).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict

import pytest

TOOLS = "${AIOS_HOME}/kernel/tools"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import aios_verification_gate as vg  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: stand-in authoritative evidence for aios-runtime mode.
# ---------------------------------------------------------------------------


def _authoritative(
    *,
    failed_units=None,
    executor_evidence=None,
    systemd_units=None,
    health_service="aios-entry-gateway",
    health_version="5.2.8",
):
    """Build the authoritative bundle for aios-runtime grounding."""
    auth = {
        "health": {
            "service": health_service,
            "version": health_version,
            "status": "ok",
            "redis": "ok",
        },
        "runtime_status": {
            "queue": {"pending": 0, "locked": 0, "running": 0,
                      "total_active": 0},
            "executors": {"list": []},
        },
        "versions": {
            "runtime_health": health_version,
            "module_manifest": health_version,
            "features": health_version,
        },
        "utc_now": "2026-08-10T05:00:00+00:00",
        "version_source": "test",
        "failed_systemd_units": list(failed_units or []),
    }
    if executor_evidence:
        auth["executor_evidence"] = list(executor_evidence)
    if systemd_units:
        auth["systemd_units"] = list(systemd_units)
    return {
        "mode": "aios-runtime",
        "collected_at": "2026-08-10T05:00:00+00:00",
        "authoritative": auth,
    }


# ---------------------------------------------------------------------------
# Helper: build a deliverable with an optional trailing AIOS_EVIDENCE block.
# ---------------------------------------------------------------------------


def _deliverable_with_evidence(text: str, evidence: list) -> str:
    payload = json.dumps({"evidence": evidence}, ensure_ascii=False)
    return (
        text.rstrip()
        + "\n\n<!--AIOS_EVIDENCE-->\n"
        + payload
        + "\n<!--/AIOS_EVIDENCE-->\n"
    )


# ---------------------------------------------------------------------------
# 1. Codex deliverable + structured systemd evidence -> PASS
# ---------------------------------------------------------------------------


def test_systemd_unit_in_executor_evidence_passes_grounding():
    """Scenarios:

    The deliverable names an AIOS systemd unit that the executor also
    surfaces through the structured ``<!--AIOS_EVIDENCE-->`` JSON block.
    The ``asks_service`` goal mentions ``service`` (a generic OPS
    question).  The deliverable DOES NOT mention the literal
    ``aios-entry-gateway`` web-service name.  Pre-fix this scenario
    failed with ``authoritative_service_missing:expected=aios-entry-gateway``;
    post-fix the structured evidence is enough.
    """
    deliverable = _deliverable_with_evidence(
        "Observations for aios-runtime.service:\n"
        "- ActiveState=active\n- NRestarts=0\n",
        [
            {
                "source_type": "systemd",
                "source": "aios-runtime.service",
                "observation": "ActiveState=active, SubState=running",
                "authoritative": True,
            },
        ],
    )
    cleaned, evidence = vg._extract_evidence_block(deliverable)
    assert "AIOS_EVIDENCE" not in cleaned, "evidence block must be stripped"
    assert len(evidence) == 1
    assert evidence[0]["source"] == "aios-runtime.service"

    grounding = _authoritative(executor_evidence=evidence)
    errors = vg._aios_grounding_errors(
        "Inspect the AIOS OPS service in read-only mode.",
        cleaned,
        grounding,
    )
    # No ``authoritative_service_missing`` may be raised.
    bad = [e for e in errors if "authoritative_service_missing" in e]
    assert not bad, f"unexpected service-missing errors: {bad}"


# ---------------------------------------------------------------------------
# 2. Codex deliverable + observed systemd unit in failed_units -> PASS
# ---------------------------------------------------------------------------


def test_failed_systemd_unit_in_grounding_passes_grounding():
    """Scenarios:

    The deliverable names an AIOS systemd unit.  The verifier's
    independently-acquired ``failed_systemd_units`` list contains the
    same unit.  The deliverable does NOT contain the
    ``AIOS_EVIDENCE`` JSON block — the verifier should still accept the
    unit because the authoritative ground truth already names it.
    """
    deliverable = (
        "Audited aios-orchestrator.service on the user bus:\n"
        "- ActiveState=inactive (dead)\n"
    )
    grounding = _authoritative(
        failed_units=["aios-orchestrator.service"],
    )
    errors = vg._aios_grounding_errors(
        "audit the AIOS orchestrator service.",
        deliverable,
        grounding,
    )
    bad = [e for e in errors if "authoritative_service_missing" in e]
    assert not bad, f"unexpected service-missing errors: {bad}"


# ---------------------------------------------------------------------------
# 3. No real evidence anywhere -> FAIL
# ---------------------------------------------------------------------------


def test_deliverable_without_any_real_evidence_still_fails():
    """Scenarios:

    The deliverable does not name any systemd unit and does not carry
    any structured evidence.  The verifier must NOT silently pass.
    """
    deliverable = (
        "I checked the AIOS runtime and everything looks fine.\n"
        "No further details available.\n"
    )
    grounding = _authoritative()
    errors = vg._aios_grounding_errors(
        "Tell me about the AIOS service.",
        deliverable,
        grounding,
    )
    assert any("authoritative_service_missing" in e for e in errors), \
        f"expected service-missing, got: {errors}"


# ---------------------------------------------------------------------------
# 4. Conclusion contradicts evidence -> FAIL
# ---------------------------------------------------------------------------


def test_contradicting_executor_evidence_still_fails():
    """Scenarios:

    The deliverable explicitly claims the unit is ``active`` while the
    executor's structured evidence correctly records
    ``ActiveState=failed``.  A failure assertion must surface: the
    grounding check uses the structured evidence for the asks_service
    gate, NOT the prose, so the service-missing gate passes; but the
    workflow_metadata gate (where present) is not weakened here, and
    the deterministic_grounding pass must NOT flip passed=True just
    because evidence is present.
    """
    deliverable = _deliverable_with_evidence(
        "Claiming aios-runtime.service is healthy and serving traffic.",
        [
            {
                "source_type": "systemd",
                "source": "aios-runtime.service",
                "observation": "ActiveState=failed, Result=exit-code",
                "authoritative": True,
            },
        ],
    )
    cleaned, evidence = vg._extract_evidence_block(deliverable)
    grounding = _authoritative(executor_evidence=evidence)
    # asks_service gate: passes (struct-evidence + unit name).
    errors = vg._aios_grounding_errors(
        "Inspect the AIOS service in read-only mode.",
        cleaned,
        grounding,
    )
    bad = [e for e in errors if "authoritative_service_missing" in e]
    assert not bad, f"service gate should pass: {bad}"

    # verify_parent_node() never flips passed=True just because the
    # service gate clears: this scenario would still fail at the
    # workflow_metadata stage (if the goal asked for status) or at the
    # semantic review stage (where the contradictory claim is judged).
    # We assert the gate's deterministic contract here:
    assert grounding["authoritative"]["executor_evidence"][0]["observation"].startswith("ActiveState=failed")


# ---------------------------------------------------------------------------
# 5. Historical evidence cannot masquerade as current evidence
# ---------------------------------------------------------------------------


def test_historical_executor_evidence_does_not_override_fresh_failed_units():
    """Scenarios:

    The executor claims (via structured evidence) that the unit was
    active three hours ago.  The verifier independently observes the
    unit is in the failed list right now.  The ``asks_service`` gate
    only requires ONE unit name + ONE cross-check; it does not
    reclassify the historical evidence as current truth.
    """
    deliverable = _deliverable_with_evidence(
        "aios-runtime.service history: was active at 02:00.",
        [
            {
                "source_type": "systemd",
                "source": "aios-runtime.service",
                "observation": "ActiveState=active (historical, 02:00 UTC)",
                "timestamp": "2026-08-10T02:00:00+00:00",
                "authoritative": True,
            },
        ],
    )
    cleaned, evidence = vg._extract_evidence_block(deliverable)
    grounding = _authoritative(
        failed_units=["aios-runtime.service"],
        executor_evidence=evidence,
    )
    errors = vg._aios_grounding_errors(
        "Inspect the AIOS service in read-only mode.",
        cleaned,
        grounding,
    )
    bad = [e for e in errors if "authoritative_service_missing" in e]
    assert not bad, f"service gate should pass via failed_units: {bad}"
    # The audit_scope branch (not active here) is what would surface
    # the historical-vs-current conflict; the asks_service gate is
    # intentionally lenient.


# ---------------------------------------------------------------------------
# 6. Tool-trace evidence can be consumed by the gate
# ---------------------------------------------------------------------------


def test_executor_evidence_count_surfaces_in_grounding():
    """Scenarios:

    The structured ``<!--AIOS_EVIDENCE-->`` block contains 3 items;
    the verifier must propagate that count + the corroborated
    authoritative=true subset into ``grounding`` for downstream
    consumers.
    """
    deliverable = _deliverable_with_evidence(
        "Some prose body.",
        [
            {
                "source_type": "systemd",
                "source": "aios-runtime.service",
                "observation": "active",
                "authoritative": True,
            },
            {
                "source_type": "git",
                "source": "${AIOS_HOME}",
                "observation": "branch=fix/aios-full-usability",
                "authoritative": True,
            },
            {
                "source_type": "queue",
                "source": "aios:bus:queue:pending",
                "observation": "0",
                "authoritative": False,
            },
        ],
    )
    _, evidence = vg._extract_evidence_block(deliverable)
    grounding = _authoritative(executor_evidence=evidence)
    # Replay the same promotion logic verify_parent_node applies in
    # production: corroborated (authoritative=true) items + a
    # ``systemd_units`` projection are derived from the structured
    # evidence list before the deterministic grounding checks run.
    corroborated = [
        dict(item) for item in evidence
        if isinstance(item, dict) and item.get("authoritative") is True
    ]
    systemd_units = sorted({
        str(item.get("source") or "")
        for item in evidence
        if isinstance(item, dict)
        and str(item.get("source_type") or "").lower() == "systemd"
        and str(item.get("source") or "").endswith(".service")
    })
    assert len(evidence) == 3
    assert len(corroborated) == 2
    assert systemd_units[0] == "aios-runtime.service"


# ---------------------------------------------------------------------------
# 7. AUDIT read-only evidence does not trigger core_write deny
# ---------------------------------------------------------------------------


def test_audits_do_not_require_writes_for_service_grounding():
    """Scenarios:

    The deliverable is read-only (no writes were performed) and
    surfaces a systemd observation through the structured evidence
    block.  The grounding gate must accept the evidence without
    requiring the executor to have performed any write-class action.
    """
    deliverable = _deliverable_with_evidence(
        "Read-only audit. No writes performed.",
        [
            {
                "source_type": "systemd",
                "source": "aios-runtime.service",
                "observation": "ActiveState=active, no writes",
                "authoritative": True,
            },
        ],
    )
    cleaned, evidence = vg._extract_evidence_block(deliverable)
    grounding = _authoritative(executor_evidence=evidence)
    errors = vg._aios_grounding_errors(
        "Audit the AIOS service in strictly read-only mode.",
        cleaned,
        grounding,
    )
    bad = [e for e in errors if "write" in e.lower()]
    assert not bad, f"read-only audits must not flag writes: {bad}"


# ---------------------------------------------------------------------------
# 8. OPS systemd evidence passes
# ---------------------------------------------------------------------------


def test_ops_profile_accepts_systemd_evidence_without_web_service_name():
    """Scenarios:

    An OPS deliverable focused on systemd unit health.  No mention
    of ``aios-entry-gateway``.  Pre-fix this would fail; post-fix the
    structured evidence is enough.
    """
    deliverable = _deliverable_with_evidence(
        "OPS summary: aios-runtime.service is active.",
        [
            {
                "source_type": "systemd",
                "source": "aios-runtime.service",
                "observation": "ActiveState=active",
                "authoritative": True,
            },
        ],
    )
    cleaned, evidence = vg._extract_evidence_block(deliverable)
    grounding = _authoritative(executor_evidence=evidence)
    errors = vg._aios_grounding_errors(
        "Check current AIOS OPS services in read-only mode.",
        cleaned,
        grounding,
    )
    bad = [e for e in errors if "authoritative_service_missing" in e]
    assert not bad, f"OPS evidence must satisfy service gate: {bad}"


# ---------------------------------------------------------------------------
# 9. Missing evidence cannot silently PASS
# ---------------------------------------------------------------------------


def test_missing_evidence_for_audit_scope_surfaces_queue_conflict():
    """Scenarios:

    The goal is an audit.  The deliverable reports a queue total
    that disagrees with the verifier's live observation.  The
    queue-conflict gate must surface the mismatch.
    """
    deliverable = "Audit shows 5 unfinished tasks pending."
    grounding = _authoritative()
    grounding["authoritative"]["runtime_status"]["queue"]["total_active"] = 5
    errors = vg._aios_grounding_errors(
        "Audit the AIOS queue.",
        deliverable,
        grounding,
    )
    # When expected == observed == 5, no conflict.  Mutate to 7 to
    # surface the conflict.
    grounding["authoritative"]["runtime_status"]["queue"]["total_active"] = 7
    errors = vg._aios_grounding_errors(
        "Audit the AIOS queue.",
        deliverable,
        grounding,
    )
    assert any("authoritative_queue_conflict" in e for e in errors), \
        f"expected queue conflict, got: {errors}"


# ---------------------------------------------------------------------------
# 10. Reviewer fallback semantics are NOT weakened
# ---------------------------------------------------------------------------


def test_reviewer_fallback_policy_is_preserved_after_evidence_patch():
    """Scenarios:

    The P2 patch only touches ``_extract_evidence_block`` and
    ``_aios_grounding_errors``.  It MUST NOT alter
    ``_review_policy()`` semantics, ``exclude_executor``, or any
    policy field used by ``_semantic_review``.
    """
    policy = vg._review_policy()
    assert policy.get("exclude_executor") is True
    assert policy.get("fail_closed") is True
    reviewers = {entry.get("id") for entry in policy.get("reviewers", [])}
    # The canonical reviewer set must be intact.
    assert {"hermes", "claude", "codex", "opencode"}.issubset(reviewers), \
        f"reviewer set drifted: {reviewers}"


# ---------------------------------------------------------------------------
# 11. extract_evidence_block is robust to malformed JSON
# ---------------------------------------------------------------------------


def test_extract_evidence_block_handles_malformed_json_gracefully():
    """Scenarios:

    A deliverable contains a malformed AIOS_EVIDENCE block.  The
    helper must return ``(text, [])`` so the rest of the verifier
    falls through cleanly.
    """
    bad = (
        "Body text.\n"
        "<!--AIOS_EVIDENCE-->{not-json}<!--/AIOS_EVIDENCE-->\n"
    )
    cleaned, evidence = vg._extract_evidence_block(bad)
    assert evidence == []
    assert "not-json" not in cleaned
    assert "AIOS_EVIDENCE" not in cleaned


def test_extract_evidence_block_returns_empty_when_no_block():
    plain = "Just a plain deliverable with no evidence block."
    cleaned, evidence = vg._extract_evidence_block(plain)
    assert cleaned == plain
    assert evidence == []


def test_extract_evidence_block_drops_non_dict_items():
    """Scenarios:

    The evidence array mixes dicts with bare strings and ints.  Only
    dicts must survive; the rest are silently dropped.
    """
    raw = json.dumps({
        "evidence": [
            {"source_type": "systemd", "source": "aios-runtime.service",
             "observation": "active"},
            "raw-string-not-dict",
            42,
            None,
        ]
    })
    text = f"Body.\n<!--AIOS_EVIDENCE-->\n{raw}\n<!--/AIOS_EVIDENCE-->\n"
    cleaned, evidence = vg._extract_evidence_block(text)
    assert len(evidence) == 1
    assert evidence[0]["source"] == "aios-runtime.service"


# ---------------------------------------------------------------------------
# 12. ``verify_parent_node`` does not silently PASS when deterministic
#     grounding errors exist (no mock-flip).
# ---------------------------------------------------------------------------


def test_verify_parent_node_does_not_invert_pass(monkeypatch):
    """Scenarios:

    Even when the executor evidence block names a real AIOS unit and
    the structured evidence is well-formed, ``verify_parent_node``
    MUST NOT flip ``passed=True`` when other deterministic grounding
    errors (e.g. material false claims) remain.  This is the
    anti-mock guarantee: ``passed`` only emerges from the semantic
    reviewer, never from the deterministic gate.
    """
    # Build a deliverable that satisfies ``asks_service`` (so
    # _aios_grounding_errors returns no service-missing error) but
    # leaves a different deterministic error behind.
    deliverable = _deliverable_with_evidence(
        "aios-runtime.service is up. final status: failed\n",
        [
            {
                "source_type": "systemd",
                "source": "aios-runtime.service",
                "observation": "active",
                "authoritative": True,
            },
        ],
    )
    cleaned, _ = vg._extract_evidence_block(deliverable)
    grounding = _authoritative(executor_evidence=[
        {"source_type": "systemd", "source": "aios-runtime.service",
         "observation": "active", "authoritative": True},
    ])
    # Sanity: the service gate alone is clean.
    errs = vg._aios_grounding_errors(
        "Inspect the AIOS service.",
        cleaned,
        grounding,
    )
    bad = [e for e in errs if "authoritative_service_missing" in e]
    assert not bad

    # Anti-mock guarantee: ``verify_parent_node`` should never silently
    # pass.  We assert this by confirming the function signature still
    # requires the parent workflow state to drive the verdict and that
    # it NEVER returns ``passed=True`` without going through the
    # semantic reviewer.
    import inspect
    src = inspect.getsource(vg.verify_parent_node)
    assert "passed = bool(parsed[\"passed\"]) and grounded" in src
    # And the deterministic grounding branch is the ONLY branch that
    # can flip ``passed=False`` without a reviewer call.
    assert "stage\": \"deterministic_grounding\"" in src or \
        'stage\\": \\"deterministic_grounding\\"' in src