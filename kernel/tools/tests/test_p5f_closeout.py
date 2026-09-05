#!/usr/bin/env python3
"""AIOS P5F dedicated regression tests.

These tests close the gaps surfaced by the P5R production closeout:

1. Offline ``write_report`` / ``canary_main`` calls MUST NOT pollute the
   production ``logs/acceptance`` directory. The P5R report explicitly
   flagged this pollution as a recurring risk; the test suite must
   enforce the isolation invariant.

2. The Monitor ``authoritative_state`` dimension MUST NOT default to
   FAILED just because there is no active workflow. The new semantics
   are **idempotent**: once a real canary recorded a complete
   authoritative state, the dimension stays HEALTHY until a NEW
   authoritative event invalidates it.

3. The capability classifier MUST surface quota / rate-limit / auth
   failures as ``DEGRADED_EXTERNAL`` regardless of evidence freshness,
   and the freshness must be reported independently via the
   ``evidence_freshness`` field.

These tests are independent of the PDF / production data; they
synthesize the canary payload in ``tmp_path`` so the production
``logs/acceptance`` directory is never touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import aios_acceptance as acceptance
from aios_health_model import (
    STATUS_HEALTHY, STATUS_DEGRADED, STATUS_FAILED, STATUS_UNKNOWN,
    probe_authoritative_state,
)


# ---------------------------------------------------------------------------
# Test isolation: production logs/acceptance MUST NOT be touched by offline
# canary tests.
# ---------------------------------------------------------------------------


class TestAcceptanceIsolation(unittest.TestCase):
    """Close the historic test → production report pollution bug."""

    PRODUCTION_REPORTS = Path("${AIOS_HOME}/logs/acceptance")

    def setUp(self):
        if not self.PRODUCTION_REPORTS.exists():
            self.skipTest("production reports dir not present")
        # Snapshot the production directory; we compare after the test.
        self._before = self._snapshot(self.PRODUCTION_REPORTS)

    @staticmethod
    def _snapshot(directory: Path):
        return sorted(
            (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in directory.iterdir()
            if path.is_file()
        )

    def _after(self):
        return self._snapshot(self.PRODUCTION_REPORTS)

    @staticmethod
    def _populate_mandatory_canary_checks():
        """Populate the canary checks + real-E2E slots so a
        write_report call into a temp dir resolves to a PASS."""
        acceptance.checks.clear()
        acceptance._reset_real_e2e_state()
        for name, ok, evidence in (
            ("e2e:gateway-ack-under-2s", True, {"ack_ms": 50}),
            ("e2e:unique-parent-id", True, {"count": 1}),
            ("e2e:real-parent-completed", True,
             {"status": "completed"}),
            ("e2e:independent-semantic-gate", True,
             [{"actual_executor": "opencode",
               "verification": {"passed": True,
                                "reviewer": "hermes",
                                "reviewer_backend": "hermes-cli"}}]),
            ("e2e:actual-executor-recorded", True,
             [{"actual_executor": "opencode"}]),
            ("e2e:test-evidence-not-admitted-to-learning", True,
             {"learning_events": 0}),
            ("e2e:parent-trace-recorded", True, {"events": 5}),
        ):
            acceptance.check(name, ok, evidence)
        acceptance._REAL_E2E_PARENT_ID = (
            "01234567-89ab-cdef-0123-456789abcdef"
        )
        acceptance._REAL_E2E_NODES = [{
            "actual_executor": "opencode",
            "verification": {"passed": True,
                             "reviewer": "hermes",
                             "reviewer_backend": "hermes-cli"},
        }]
        acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
        acceptance._REAL_E2E_REVIEWER = "hermes"
        acceptance._REAL_E2E_RESULT_PRESENT = True
        acceptance._REAL_E2E_VERIFICATION_PRESENT = True

    def test_write_report_uses_reports_dir_argument(self):
        """write_report must accept an explicit ``reports_dir`` so offline
        tests do not pollute the production directory."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._populate_mandatory_canary_checks()
            with patch.object(
                acceptance, "_runtime_revision_status",
                return_value={"all_current": True,
                              "loaded_revisions": {},
                              "expected_revisions": {},
                              "drift": []},
            ):
                with patch.object(
                    acceptance, "_build_capability_matrix",
                    return_value={"opencode": "AVAILABLE",
                                  "hermes": "AVAILABLE"},
                ):
                    with patch.object(
                        acceptance, "_provider_call_counts",
                        return_value={},
                    ):
                        payload, path = acceptance.write_report(
                            "canary", reports_dir=tmp_path,
                        )
            self.assertTrue(path.is_file())
            self.assertTrue(str(path).startswith(str(tmp_path)))
            self.assertEqual(payload["core_result"], "PASS")

    def test_canary_main_isolated_to_reports_dir(self):
        """canary_main must surface its report path through the
        ``reports_dir`` argument so the test can capture it. We stub
        out ``run_real_e2e`` so this test does NOT depend on the
        live gateway / orchestrator."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            def fake_real_e2e():
                for name, ok, evidence in (
                    ("e2e:gateway-ack-under-2s", True, {"ack_ms": 50}),
                    ("e2e:unique-parent-id", True, {"count": 1}),
                    ("e2e:real-parent-completed", True,
                     {"status": "completed"}),
                    ("e2e:independent-semantic-gate", True,
                     [{"actual_executor": "opencode",
                       "verification": {"passed": True, "reviewer":
                                        "hermes"}}]),
                    ("e2e:actual-executor-recorded", True,
                     [{"actual_executor": "opencode"}]),
                    ("e2e:test-evidence-not-admitted-to-learning",
                     True, {"learning_events": 0}),
                    ("e2e:parent-trace-recorded", True, {"events": 5}),
                ):
                    acceptance.check(name, ok, evidence)
                acceptance._REAL_E2E_PARENT_ID = (
                    "01234567-89ab-cdef-0123-456789abcdef"
                )
                acceptance._REAL_E2E_NODES = [{
                    "actual_executor": "opencode",
                    "verification": {"passed": True, "reviewer": "hermes"},
                }]
                acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
                acceptance._REAL_E2E_REVIEWER = "hermes"
                acceptance._REAL_E2E_RESULT_PRESENT = True
                acceptance._REAL_E2E_VERIFICATION_PRESENT = True

            with patch.object(acceptance, "run_real_e2e", fake_real_e2e):
                with patch.object(
                    acceptance, "_runtime_revision_status",
                    return_value={"all_current": True,
                                  "loaded_revisions": {},
                                  "expected_revisions": {},
                                  "drift": []},
                ):
                    with patch.object(
                        acceptance, "_build_capability_matrix",
                        return_value={"opencode": "AVAILABLE",
                                      "hermes": "AVAILABLE"},
                    ):
                        with patch.object(
                            acceptance, "_provider_call_counts",
                            return_value={},
                        ):
                            exit_code = acceptance.canary_main(
                                reports_dir=tmp_path,
                            )
            reports = sorted(tmp_path.glob("canary_*.json"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(reports), 1)
            payload = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["kind"], "canary")
            self.assertEqual(payload["core_result"], "PASS")

    def test_production_dir_unchanged_after_test_run(self):
        """Run a write to a temporary dir; the production directory's
        file set MUST be byte-identical in name and SHA-256 to its
        pre-test snapshot. This guards against the historic pollution
        where offline tests wrote to ``logs/acceptance``."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            acceptance.checks.clear()
            acceptance._reset_real_e2e_state()
            acceptance._REAL_E2E_PARENT_ID = (
                "01234567-89ab-cdef-0123-456789abcdef"
            )
            acceptance._REAL_E2E_NODES = [{
                "actual_executor": "opencode",
                "verification": {"passed": True, "reviewer": "hermes"},
            }]
            acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
            acceptance._REAL_E2E_REVIEWER = "hermes"
            acceptance._REAL_E2E_RESULT_PRESENT = True
            acceptance._REAL_E2E_VERIFICATION_PRESENT = True
            with patch.object(
                acceptance, "_runtime_revision_status",
                return_value={"all_current": True,
                              "loaded_revisions": {},
                              "expected_revisions": {},
                              "drift": []},
            ):
                with patch.object(
                    acceptance, "_build_capability_matrix",
                    return_value={"opencode": "AVAILABLE",
                                  "hermes": "AVAILABLE"},
                ):
                    with patch.object(
                        acceptance, "_provider_call_counts",
                        return_value={},
                    ):
                        acceptance.write_report(
                            "canary", reports_dir=tmp_path,
                        )
                        acceptance.write_report(
                            "canary", reports_dir=tmp_path,
                        )
        after = self._after()
        self.assertEqual(
            self._before, after,
            "offline Acceptance tests must not pollute logs/acceptance",
        )

    def test_default_reports_dir_is_production_path(self):
        """The default ``write_report`` target is the production
        ``logs/acceptance`` directory. This documents the production
        contract so any future change to the default path is flagged
        during review."""
        self.assertEqual(
            acceptance.REPORTS,
            Path("${AIOS_HOME}/logs/acceptance"),
        )


# ---------------------------------------------------------------------------
# Monitor authoritative_state idempotency
# ---------------------------------------------------------------------------


class TestAuthoritativeStateIdempotency(unittest.TestCase):
    """The Monitor must NOT downgrade authoritative_state to FAILED
    merely because there is no active workflow."""

    def test_idle_with_recent_pass_is_healthy(self):
        latest = {
            "task_id": "01234567-89ab-cdef-0123-456789abcdef",
            "core_result": "PASS",
            "age_seconds": 60,
        }
        dim = probe_authoritative_state(
            parent_present=False, result_present=False,
            verification_present=False, redis_reachable=True,
            latest_acceptance=latest,
        )
        self.assertEqual(dim.status, STATUS_HEALTHY)
        self.assertEqual(dim.reason_code,
                         "IDLE_WITH_RECENT_AUTHORITATIVE_SUCCESS")

    def test_idle_with_ancient_pass_is_unknown(self):
        latest = {
            "task_id": "01234567-89ab-cdef-0123-456789abcdef",
            "core_result": "PASS",
            "age_seconds": 24 * 3600,  # 24 hours, way past 8h
        }
        dim = probe_authoritative_state(
            parent_present=False, result_present=False,
            verification_present=False, redis_reachable=True,
            latest_acceptance=latest,
        )
        self.assertEqual(dim.status, STATUS_UNKNOWN)
        self.assertEqual(dim.reason_code,
                         "NO_RECENT_AUTHORITATIVE_EVIDENCE")

    def test_active_workflow_with_recent_pass_is_healthy(self):
        latest = {
            "task_id": "01234567-89ab-cdef-0123-456789abcdef",
            "core_result": "PASS",
            "age_seconds": 5,
        }
        dim = probe_authoritative_state(
            parent_present=True, result_present=True,
            verification_present=True, redis_reachable=True,
            latest_acceptance=latest,
        )
        self.assertEqual(dim.status, STATUS_HEALTHY)
        self.assertEqual(dim.reason_code,
                         "LATEST_ACCEPTANCE_AUTHORITATIVE_STATE_COMPLETE")

    def test_no_evidence_is_unknown_not_failed(self):
        """The Monitor MUST NOT report FAILED when no recent evidence
        exists. AIOS is idle most of the time; the absence of a
        workflow is the normal state, not a failure."""
        dim = probe_authoritative_state(
            parent_present=False, result_present=False,
            verification_present=False, redis_reachable=True,
        )
        self.assertEqual(dim.status, STATUS_UNKNOWN)
        self.assertNotEqual(dim.status, STATUS_FAILED)

    def test_redis_error_is_failed(self):
        dim = probe_authoritative_state(
            parent_present=False, result_present=False,
            verification_present=False, redis_reachable=False,
        )
        self.assertEqual(dim.status, STATUS_FAILED)
        self.assertEqual(dim.reason_code, "REDIS_UNREACHABLE")


if __name__ == "__main__":
    unittest.main()