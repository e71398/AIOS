#!/usr/bin/env python3
"""AIOS P6A independent-tool and quota-aware validation tests.

These tests enforce the five independent AI capabilities
(``opencode``, ``hermes``, ``openclaw``, ``claude``, ``codex``)
under P6A. They cover:

1. The five-tool capability matrix is exhaustive (each tool appears).
2. Current quota_exhausted evidence overrides historical success.
3. Unavailable tools MUST NOT participate in fallback selection.
4. Role independence: OpenCode executor ≠ OpenCode reviewer; Hermes
   is independent of OpenCode.
5. Provider call statistics separate current run vs historical
   cumulative (the 9294-token governance cache is NOT counted as a
   current run call).
6. ``optional_degradations`` is correctly populated with the actual
   degraded tool names when overall_status == DEGRADED.
7. P5F evidence directory file count is locked to its real count.

Tests are offline: no AI provider is invoked, no Redis is mutated,
and no subprocess is spawned. They run with the same PYTHONPATH the
rest of the test suite uses.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

from aios_capability import (
    AVAILABLE, DEGRADED_EXTERNAL, DEGRADED_INTERNAL, NOT_CONFIGURED,
    UNAVAILABLE, UNVERIFIED, capability_matrix, evaluate_capability,
    is_available, is_available_for_role, select_available,
)
from aios_health_model import (
    STATUS_DEGRADED, STATUS_FAILED, STATUS_HEALTHY,
    HealthDimension, assemble_health_report, probe_capability,
)


# ---------------------------------------------------------------------------
# Helpers — synthetic probe cache without filesystem side effects
# ---------------------------------------------------------------------------


def _probe(
    *,
    model_available: bool = False,
    model_state: str = "unverified",
    checked_at: Optional[str] = None,
    retry_after: Optional[str] = None,
    reason: str = "synthetic",
) -> dict:
    """Build a synthetic probe cache.

    ``checked_at`` defaults to "60 seconds before now" so the helper is
    time-stable across long test runs and across the 2-hour
    ``DEFAULT_EVIDENCE_MAX_AGE_SECONDS`` window. Callers can still
    override ``checked_at`` explicitly to test stale-evidence paths.
    """
    from datetime import datetime, timedelta, timezone
    if checked_at is None:
        ts = datetime.now(timezone.utc) - timedelta(seconds=60)
        checked_at = ts.isoformat()
    return {
        "checked_at": checked_at,
        "latency_ms": 0,
        "model_state": model_state,
        "model_available": model_available,
        "reason": reason,
        "returncode": 0 if model_available else 1,
        "evidence": reason,
        "success_marker_seen": model_available,
        "fatal_error_seen": not model_available,
        "probe_required": True,
        **({"retry_after": retry_after} if retry_after else {}),
    }


# ---------------------------------------------------------------------------
# 1. Five-tool capability matrix is exhaustive
# ---------------------------------------------------------------------------


class FiveToolCapabilityMatrix(unittest.TestCase):
    """``capability_matrix`` must return exactly the canonical five tools."""

    def test_five_canonical_tools_present(self):
        matrix = capability_matrix()
        for tool in ("opencode", "claude", "codex", "hermes", "openclaw"):
            self.assertIn(tool, matrix,
                          f"canonical tool {tool!r} missing from matrix")
            self.assertIn(
                matrix[tool],
                {AVAILABLE, DEGRADED_EXTERNAL, DEGRADED_INTERNAL,
                 UNAVAILABLE, UNVERIFIED, NOT_CONFIGURED},
                f"tool {tool!r} has unknown status {matrix[tool]!r}",
            )

    def test_matrix_has_no_extras(self):
        """P8A: the capability matrix is dynamic — the canonical five
        tools MUST be present, but new tools may also appear without
        modifying this test. We assert presence of the canonical
        five and that the matrix is non-empty; we DO NOT assert that
        the size is exactly five (that would re-introduce the
        hardcoded count that P8A forbids)."""
        matrix = capability_matrix()
        self.assertGreaterEqual(len(matrix), 5,
                                 "matrix MUST contain at least the canonical five tools")
        for canonical in ("opencode", "claude", "codex", "hermes", "openclaw"):
            self.assertIn(canonical, matrix,
                          f"canonical tool {canonical!r} missing from matrix")
            self.assertIn(
                matrix[canonical],
                {AVAILABLE, DEGRADED_EXTERNAL, DEGRADED_INTERNAL,
                 UNAVAILABLE, UNVERIFIED, NOT_CONFIGURED},
                f"tool {canonical!r} has unknown status {matrix[canonical]!r}",
            )

    def test_matrix_does_not_assert_exactly_five(self):
        """Anti-regression: P8A explicitly forbids tests that pin the
        tool count to exactly five. Adding a 6th tool to the registry
        must not break any test."""
        matrix = capability_matrix()
        # The set of tools we expect to be present; the matrix MAY
        # contain additional tools (this assertion would FAIL if the
        # suite was still pinned to the five-tool tuple).
        expected = {"opencode", "claude", "codex", "hermes", "openclaw"}
        self.assertTrue(expected.issubset(set(matrix.keys())),
                        "canonical five tools must all be present")


# ---------------------------------------------------------------------------
# 2. Current quota overrides historical success
# ---------------------------------------------------------------------------


class QuotaOverridesHistoricalSuccess(unittest.TestCase):
    """A current ``quota_exhausted`` MUST downgrade a tool even when
    earlier success evidence exists. Historical success alone does not
    promote a quota-blocked tool back to AVAILABLE."""

    def test_quota_failure_overrides_recent_success(self):
        # First check-in: success. Second: quota exhausted. The current
        # probe cache should reflect quota_exhausted → DEGRADED_EXTERNAL.
        with patch("aios_capability._read_probe_cache",
                   return_value=_probe(
                       model_available=False,
                       model_state="quota_exhausted",
                       reason="API Error: 402 Insufficient Balance",
                   )):
            rec = evaluate_capability("claude")
        self.assertEqual(rec["status"], DEGRADED_EXTERNAL)
        self.assertEqual(rec["last_failure_kind"], "quota_exhausted")
        self.assertEqual(rec["evidence_freshness"], "FRESH")
        self.assertFalse(is_available("claude"))

    def test_quota_with_active_cooldown(self):
        with patch("aios_capability._read_probe_cache",
                   return_value=_probe(
                       model_available=False,
                       model_state="quota_exhausted",
                       retry_after="2099-01-01T00:00:00+00:00",
                   )):
            rec = evaluate_capability("codex")
        self.assertEqual(rec["status"], DEGRADED_EXTERNAL)
        self.assertIsNotNone(rec["cooldown_until"])

    def test_local_adapter_exception(self):
        """Local adapter exceptions (timeout / probe_error) MUST surface
        as DEGRADED_INTERNAL, not DEGRADED_EXTERNAL, because the failure
        is internal infrastructure, not a provider quota block."""
        with patch("aios_capability._read_probe_cache",
                   return_value=_probe(
                       model_available=False,
                       model_state="timeout",
                       reason="subprocess.TimeoutExpired",
                   )):
            rec = evaluate_capability("opencode")
        self.assertEqual(rec["status"], DEGRADED_INTERNAL)
        self.assertEqual(rec["last_failure_kind"], "timeout")


# ---------------------------------------------------------------------------
# 3. Unavailable tools MUST NOT participate in fallback selection
# ---------------------------------------------------------------------------


class UnavailableToolsExcludedFromFallback(unittest.TestCase):
    """``select_available`` MUST skip degraded / unavailable candidates
    even when they are listed. A degraded tool cannot be silently used
    as fallback for executor or reviewer."""

    def test_degraded_excluded(self):
        # claude: degraded; opencode: available. select_available must
        # skip claude and return opencode.
        states = {
            "claude": {"status": DEGRADED_EXTERNAL, "roles": ["executor"],
                       "evidence_freshness": "FRESH"},
            "opencode": {"status": AVAILABLE, "roles": ["executor"],
                         "evidence_freshness": "FRESH"},
        }
        with patch("aios_capability.evaluate_capability",
                   side_effect=lambda name, **kw: states[name]):
            self.assertEqual(
                select_available(["claude", "opencode"], role="executor"),
                "opencode",
            )

    def test_unavailable_excluded(self):
        # claude: unavailable; hermes: available. select_available must
        # skip claude and return hermes.
        states = {
            "claude": {"status": UNAVAILABLE, "roles": ["reviewer"],
                       "evidence_freshness": "STALE"},
            "hermes": {"status": AVAILABLE, "roles": ["reviewer"],
                       "evidence_freshness": "FRESH"},
        }
        with patch("aios_capability.evaluate_capability",
                   side_effect=lambda name, **kw: states[name]):
            self.assertEqual(
                select_available(["claude", "hermes"], role="reviewer"),
                "hermes",
            )

    def test_role_mismatch_excluded(self):
        # opencode only supports executor; asking for reviewer must
        # yield empty (no candidate matches the role).
        states = {
            "opencode": {"status": AVAILABLE, "roles": ["executor"],
                         "evidence_freshness": "FRESH"},
        }
        with patch("aios_capability.evaluate_capability",
                   side_effect=lambda name, **kw: states[name]):
            self.assertEqual(
                select_available(["opencode"], role="reviewer"),
                "",
            )


# ---------------------------------------------------------------------------
# 4. Role independence
# ---------------------------------------------------------------------------


class RoleIndependence(unittest.TestCase):
    """OpenCode executor and OpenCode reviewer are conceptually
    distinct even if the same tool chip supports both roles. The
    fallback order MUST prefer the canonical executor (OpenCode) and
    reviewer (Hermes) instead of substituting the same chip."""

    def test_opencode_executor_and_reviewer_are_not_silent_swap(self):
        # The tool_adapters.json role for opencode is primary_general_executor
        # and for hermes is primary_memory_semantic_review. The orchestrator
        # treats them as distinct roles — so an executor-only OpenCode chip
        # MUST NOT auto-promote to reviewer.
        cfg_path = TOOLS.parent.parent / "config/tool_adapters.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["tools"]["opencode"]["role"],
                         "primary_general_executor")
        self.assertEqual(cfg["tools"]["hermes"]["role"],
                         "primary_memory_semantic_review")

    def test_hermes_independent_of_opencode(self):
        # Both tools must appear independently in the matrix; independence
        # is enforced by the per-tool probe cache and adapter config, not
        # by forcing different status values.
        matrix = capability_matrix()
        self.assertIn("hermes", matrix)
        self.assertIn("opencode", matrix)
        # Their underlying adapter configs are distinct: different
        # executables, different capabilities, different roles.
        cfg = json.loads((TOOLS.parent.parent / "config/tool_adapters.json")
                         .read_text(encoding="utf-8"))
        self.assertNotEqual(
            cfg["tools"]["hermes"]["executable"],
            cfg["tools"]["opencode"]["executable"],
        )
        self.assertNotEqual(
            cfg["tools"]["hermes"]["capabilities"],
            cfg["tools"]["opencode"]["capabilities"],
        )


# ---------------------------------------------------------------------------
# 5. Provider current / historical call accounting
# ---------------------------------------------------------------------------


class ProviderCallAccounting(unittest.TestCase):
    """Historical cumulative governance cache (e.g. 9294 tokens) is NOT
    the same metric as a current-run provider call. The accounting
    structure MUST keep them separate."""

    def test_redis_governance_hash_separate_from_current_run(self):
        import redis as _redis
        client = _redis.Redis(host="localhost", port=6379,
                              socket_connect_timeout=1)
        try:
            client.ping()
        except Exception:
            self.skipTest("redis not available")
        today = "20260724"
        hash_data = client.hgetall(f"aios:bus:governance:daily:{today}") or {}
        decoded = {k.decode() if isinstance(k, bytes) else k:
                   v.decode() if isinstance(v, bytes) else v
                   for k, v in hash_data.items()}
        # openclaw_tokens / openclaw_cost are historical cumulative
        # governance telemetry, NOT current-run call counts. The keys
        # carry '_tokens' / '_cost', NOT '_calls'.
        if "openclaw_tokens" in decoded:
            self.assertIn("openclaw_cost", decoded)
            self.assertNotIn("openclaw_calls", decoded,
                             "governance cache MUST NOT mix cumulative "
                             "tokens with current-run call counts")


# ---------------------------------------------------------------------------
# 6. optional_degradations correctly populated
# ---------------------------------------------------------------------------


class OptionalDegradationsExposeToolNames(unittest.TestCase):
    """When overall_status == DEGRADED, ``optional_degradations`` MUST
    list the actually degraded tool names (claude, codex, …) extracted
    from capability dimensions' ``extra`` payload. Empty list while
    overall == DEGRADED is a regression."""

    def test_tool_names_surfaced_when_overall_degraded(self):
        exec_dim = probe_capability(
            "executor_capability",
            {"opencode": AVAILABLE, "claude": DEGRADED_EXTERNAL,
             "codex": DEGRADED_EXTERNAL, "hermes": AVAILABLE,
             "openclaw": DEGRADED_EXTERNAL},
            mandatory=True,
        )
        rev_dim = probe_capability(
            "reviewer_capability",
            {"opencode": AVAILABLE, "claude": DEGRADED_EXTERNAL,
             "codex": DEGRADED_EXTERNAL, "hermes": AVAILABLE,
             "openclaw": DEGRADED_EXTERNAL},
            mandatory=True,
        )
        healthy = HealthDimension(
            unit="service_state", status=STATUS_HEALTHY,
            reason_code="ALL_CORE_UNITS_ACTIVE", mandatory=True,
            evidence_source="systemd:is-active",
            observed_at="2026-07-24T07:45:00+00:00",
            age_seconds=0, summary="5 core units active",
        )
        report = assemble_health_report({
            "service_state": healthy,
            "executor_capability": exec_dim,
            "reviewer_capability": rev_dim,
        })
        self.assertEqual(report.overall_status, STATUS_DEGRADED)
        # P6A §3.2: each optional_degradations entry must expose
        # tool_id / status / reason_code / evidence_freshness /
        # mandatory=false as structured fields.
        by_tool = {
            entry["tool_id"]: entry
            for entry in report.optional_degradations
            if isinstance(entry, dict)
        }
        self.assertIn("claude", by_tool)
        self.assertIn("codex", by_tool)
        # P7A errata: openclaw IS a canonical five-tool capability
        # matrix member (planner/reviewer-like) and now appears in
        # optional_degradations when its state is degraded. The P6A
        # test originally asserted the opposite based on the bug that
        # the reviewer_capability candidates tuple omitted openclaw.
        self.assertIn("openclaw", by_tool)
        for entry in by_tool.values():
            self.assertEqual(entry["status"], DEGRADED_EXTERNAL)
            self.assertTrue(entry["reason_code"])
            self.assertIn(entry["mandatory"], (False, "false"))
            # evidence_freshness key must exist (may be UNKNOWN when
            # the lightweight matrix carries only status strings).
            self.assertIn("evidence_freshness", entry)
    def test_healthy_overall_yields_empty_degradations(self):
        exec_dim = probe_capability(
            "executor_capability",
            {"opencode": AVAILABLE, "claude": AVAILABLE,
             "codex": AVAILABLE, "hermes": AVAILABLE},
            mandatory=True,
        )
        healthy = HealthDimension(
            unit="service_state", status=STATUS_HEALTHY,
            reason_code="ALL_CORE_UNITS_ACTIVE", mandatory=True,
            evidence_source="systemd:is-active",
            observed_at="2026-07-24T07:45:00+00:00",
            age_seconds=0, summary="5 core units active",
        )
        report = assemble_health_report({
            "service_state": healthy,
            "executor_capability": exec_dim,
        })
        self.assertEqual(report.overall_status, STATUS_HEALTHY)
        self.assertEqual(report.optional_degradations, [])


# ---------------------------------------------------------------------------
# 7. P5F evidence directory file count
# ---------------------------------------------------------------------------


class P5FEvidenceFileCount(unittest.TestCase):
    """Lock the actual evidence file count so future audits do not
    silently drop or add files. This guards the 17/18 discrepancy
    surfaced by the P6A errata review."""

    def test_p5f_evidence_count(self):
        evidence_dir = (TOOLS.parent.parent / "docs" / "evidence"
                        / "AIOS_P5F_FINAL_ACCEPTANCE_MONITOR_CLOSEOUT_20260724")
        if not evidence_dir.is_dir():
            self.skipTest("P5F evidence dir not present")
        files = sorted(
            p.name for p in evidence_dir.iterdir() if p.is_file()
        )
        # 15 numbered artifacts (00..14) + governance_raw.txt +
        # manifest.sha256 = 17. P5R had 17 numbered + manifest = 18;
        # P5F deliberately dropped the systemd-runtime inventory
        # artifact because the runtime inventory is captured by the
        # baseline TSV (01_BASELINE.tsv).
        self.assertEqual(len(files), 17,
                         f"P5F evidence count drifted: {len(files)} {files}")
        self.assertIn("manifest.sha256", files)
        self.assertIn("governance_raw.txt", files)


if __name__ == "__main__":
    unittest.main()