#!/usr/bin/env python3
"""AIOS P5R acceptance truthfulness tests (offline, no provider calls).

These tests close the historic 23-microsecond synthetic-PASS bug by
asserting that ``write_report`` MUST refuse to mark ``core_result`` as
PASS when the real E2E loop did not actually produce:

* a real parent task id (UUID, not a dict repr)
* a real executor
* a real independent reviewer
* a real result
* a real verification
* any check entries at all

They also assert that exceptions inside ``run_real_e2e`` MUST NOT be
swallowed and silently converted into a PASS report.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import aios_acceptance as acceptance


class TestAcceptanceTruthfulness(unittest.TestCase):
    """P5R acceptance truthfulness guards (closes the 23µs synthetic PASS bug)."""

    def setUp(self):
        acceptance.checks.clear()
        acceptance._reset_real_e2e_state()
        # P5F: isolate ``write_report`` / ``canary_main`` writes to a
        # temporary directory so offline tests cannot pollute the
        # production ``logs/acceptance`` directory.
        import tempfile
        self._p5f_reports_dir = tempfile.mkdtemp(prefix="aios-p5f-r-")
        self._p5f_original_reports = acceptance.REPORTS
        acceptance.REPORTS = Path(self._p5f_reports_dir)

    def tearDown(self):
        acceptance.REPORTS = self._p5f_original_reports
        import shutil
        shutil.rmtree(self._p5f_reports_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # The six mandatory truthfulness conditions required by the P5R
    # closeout spec. None of them may pass without the corresponding
    # real signal.
    # ------------------------------------------------------------------

    def test_missing_task_id_cannot_pass(self):
        acceptance.check("e2e:gateway-ack-under-2s", True, {"ack_ms": 50})
        acceptance.check("e2e:unique-parent-id", True, {"count": 1})
        # The evidence dict has ``status=completed`` but no task_id, and
        # the real-E2E slot is empty.
        acceptance.check("e2e:real-parent-completed", True,
                         {"status": "completed"})
        acceptance.check("e2e:independent-semantic-gate", True,
                         [{"actual_executor": "opencode",
                           "verification": {"passed": True,
                                            "reviewer": "hermes",
                                            "reviewer_backend": "hermes-cli"}}])
        acceptance.check("e2e:actual-executor-recorded", True,
                         [{"actual_executor": "opencode"}])
        acceptance.check("e2e:test-evidence-not-admitted-to-learning", True,
                         {"learning_events": 0})
        acceptance.check("e2e:parent-trace-recorded", True, {"events": 5})
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE",
                                            "hermes": "AVAILABLE"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={}):
                    payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "FAIL")
        self.assertIn("real_task_id_missing", payload["mandatory_failures"])
        self.assertIn("REAL_TASK_ID_MISSING", payload["failure_classes"])

    def test_missing_parent_state_cannot_pass(self):
        acceptance.check("e2e:gateway-ack-under-2s", True, {"ack_ms": 50})
        acceptance.check("e2e:unique-parent-id", True, {"count": 1})
        # parent_state is missing (False). The real-E2E slots carry no
        # parent_id either.
        acceptance.check("e2e:real-parent-completed", False,
                         {"status": "running"})
        acceptance.check("e2e:independent-semantic-gate", True,
                         [{"actual_executor": "opencode",
                           "verification": {"passed": True,
                                            "reviewer": "hermes",
                                            "reviewer_backend": "hermes-cli"}}])
        acceptance.check("e2e:actual-executor-recorded", True,
                         [{"actual_executor": "opencode"}])
        acceptance.check("e2e:test-evidence-not-admitted-to-learning", True,
                         {"learning_events": 0})
        acceptance.check("e2e:parent-trace-recorded", True, {"events": 5})
        acceptance._REAL_E2E_PARENT_ID = "01234567-89ab-cdef-0123-456789abcdef"
        acceptance._REAL_E2E_NODES = [{"actual_executor": "opencode",
                                       "verification": {"passed": True,
                                                        "reviewer": "hermes"}}]
        acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
        acceptance._REAL_E2E_REVIEWER = "hermes"
        acceptance._REAL_E2E_RESULT_PRESENT = False
        acceptance._REAL_E2E_VERIFICATION_PRESENT = True
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE",
                                            "hermes": "AVAILABLE"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={}):
                    payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "FAIL")
        self.assertIn("real_parent_completed", payload["mandatory_failures"])
        self.assertIn("result_not_present", payload["mandatory_failures"])

    def test_missing_result_cannot_pass(self):
        acceptance.check("e2e:gateway-ack-under-2s", True, {"ack_ms": 50})
        acceptance.check("e2e:unique-parent-id", True, {"count": 1})
        acceptance.check("e2e:real-parent-completed", True,
                         {"status": "completed"})
        acceptance.check("e2e:independent-semantic-gate", True,
                         [{"actual_executor": "opencode",
                           "verification": {"passed": True,
                                            "reviewer": "hermes",
                                            "reviewer_backend": "hermes-cli"}}])
        acceptance.check("e2e:actual-executor-recorded", True,
                         [{"actual_executor": "opencode"}])
        acceptance.check("e2e:test-evidence-not-admitted-to-learning", True,
                         {"learning_events": 0})
        acceptance.check("e2e:parent-trace-recorded", True, {"events": 5})
        # Parent completed but result_present is False.
        acceptance._REAL_E2E_PARENT_ID = "01234567-89ab-cdef-0123-456789abcdef"
        acceptance._REAL_E2E_NODES = [{"actual_executor": "opencode",
                                       "verification": {"passed": True,
                                                        "reviewer": "hermes"}}]
        acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
        acceptance._REAL_E2E_REVIEWER = "hermes"
        acceptance._REAL_E2E_RESULT_PRESENT = False
        acceptance._REAL_E2E_VERIFICATION_PRESENT = True
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE",
                                            "hermes": "AVAILABLE"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={}):
                    payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "FAIL")
        self.assertIn("result_not_present", payload["mandatory_failures"])
        self.assertIn("RESULT_NOT_PRESENT", payload["failure_classes"])

    def test_missing_verification_cannot_pass(self):
        acceptance.check("e2e:gateway-ack-under-2s", True, {"ack_ms": 50})
        acceptance.check("e2e:unique-parent-id", True, {"count": 1})
        acceptance.check("e2e:real-parent-completed", True,
                         {"status": "completed"})
        acceptance.check("e2e:independent-semantic-gate", False,
                         [{"actual_executor": "opencode",
                           "verification": {"passed": False,
                                            "reviewer": "hermes"}}])
        acceptance.check("e2e:actual-executor-recorded", True,
                         [{"actual_executor": "opencode"}])
        acceptance.check("e2e:test-evidence-not-admitted-to-learning", True,
                         {"learning_events": 0})
        acceptance.check("e2e:parent-trace-recorded", True, {"events": 5})
        acceptance._REAL_E2E_PARENT_ID = "01234567-89ab-cdef-0123-456789abcdef"
        acceptance._REAL_E2E_NODES = [{"actual_executor": "opencode",
                                       "verification": {"passed": False,
                                                        "reviewer": "hermes"}}]
        acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
        acceptance._REAL_E2E_REVIEWER = "hermes"
        acceptance._REAL_E2E_RESULT_PRESENT = True
        acceptance._REAL_E2E_VERIFICATION_PRESENT = False
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE",
                                            "hermes": "AVAILABLE"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={}):
                    payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "FAIL")
        self.assertIn("independent_reviewer", payload["mandatory_failures"])
        self.assertIn("verification_not_present",
                      payload["mandatory_failures"])

    def test_empty_checks_list_cannot_pass(self):
        # The canary did not record any check at all. The guard must
        # refuse to mark PASS regardless of capability status.
        acceptance._REAL_E2E_PARENT_ID = "01234567-89ab-cdef-0123-456789abcdef"
        acceptance._REAL_E2E_NODES = [{"actual_executor": "opencode",
                                       "verification": {"passed": True,
                                                        "reviewer": "hermes"}}]
        acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
        acceptance._REAL_E2E_REVIEWER = "hermes"
        acceptance._REAL_E2E_RESULT_PRESENT = True
        acceptance._REAL_E2E_VERIFICATION_PRESENT = True
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE",
                                            "hermes": "AVAILABLE"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={}):
                    payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "FAIL")
        self.assertIn("no_checks_executed", payload["mandatory_failures"])
        self.assertIn("NO_CHECKS_EXECUTED", payload["failure_classes"])

    def test_swallowed_exception_does_not_pass(self):
        """If ``run_real_e2e`` raises, the report MUST be FAIL with the
        truthfulness slot reset and ``real_task_id_missing`` flagged."""
        def boom():
            # Simulate a partial run: HTTP POST succeeded (parent_id set)
            # but later polling failed and the except block ran. After the
            # except block the slots must be empty.
            acceptance._REAL_E2E_PARENT_ID = ""
            acceptance._REAL_E2E_NODES = []
            acceptance._REAL_E2E_ACTUAL_EXECUTOR = ""
            acceptance._REAL_E2E_REVIEWER = ""
            acceptance._REAL_E2E_RESULT_PRESENT = False
            acceptance._REAL_E2E_VERIFICATION_PRESENT = False
            acceptance.check("e2e:gateway-ack-under-2s", False,
                             "ConnectionError")
            acceptance.check("e2e:unique-parent-id", False, "exception")
            acceptance.check("e2e:real-parent-completed", False, "timeout")
            acceptance.check("e2e:independent-semantic-gate", False, "no nodes")
            acceptance.check("e2e:actual-executor-recorded", False, "no nodes")
            acceptance.check("e2e:test-evidence-not-admitted-to-learning",
                             False, "no run")
            acceptance.check("e2e:parent-trace-recorded", False, "no trace")
            raise RuntimeError("simulated run_real_e2e failure")

        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE",
                                            "hermes": "AVAILABLE"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={}):
                    with patch.object(acceptance, "run_real_e2e", boom):
                        # ``write_report`` itself does not call
                        # ``run_real_e2e``; ``canary_main`` does. We
                        # therefore exercise the canary path.
                        try:
                            acceptance.canary_main()
                        except RuntimeError:
                            pass
                        payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "FAIL")
        self.assertIn("real_task_id_missing", payload["mandatory_failures"])
        self.assertIn("actual_executor_missing", payload["mandatory_failures"])
        self.assertIn("independent_reviewer_missing",
                      payload["mandatory_failures"])

    def test_only_real_completed_state_can_pass(self):
        """All real-E2E slots populated AND capability green AND
        ``_REAL_E2E_RESULT_PRESENT`` True \u21d2 core PASS."""
        for name, ok, evidence in (
            ("e2e:gateway-ack-under-2s", True, {"ack_ms": 50}),
            ("e2e:unique-parent-id", True, {"count": 1}),
            ("e2e:real-parent-completed", True,
             {"status": "completed"}),
            ("e2e:independent-semantic-gate", True,
             [{"actual_executor": "opencode",
               "verification": {"passed": True, "reviewer": "hermes",
                                "reviewer_backend": "hermes-cli"}}]),
            ("e2e:actual-executor-recorded", True,
             [{"actual_executor": "opencode"}]),
            ("e2e:test-evidence-not-admitted-to-learning", True,
             {"learning_events": 0}),
            ("e2e:parent-trace-recorded", True, {"events": 5}),
        ):
            acceptance.check(name, ok, evidence)
        acceptance._REAL_E2E_PARENT_ID = "01234567-89ab-cdef-0123-456789abcdef"
        acceptance._REAL_E2E_NODES = [{"actual_executor": "opencode",
                                       "verification": {"passed": True,
                                                        "reviewer": "hermes",
                                                        "reviewer_backend":
                                                            "hermes-cli"}}]
        acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
        acceptance._REAL_E2E_REVIEWER = "hermes"
        acceptance._REAL_E2E_RESULT_PRESENT = True
        acceptance._REAL_E2E_VERIFICATION_PRESENT = True
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE",
                                            "hermes": "AVAILABLE",
                                            "claude": "DEGRADED_EXTERNAL",
                                            "codex": "DEGRADED_EXTERNAL",
                                            "openclaw": "AVAILABLE"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={}):
                    payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "PASS")
        self.assertEqual(payload["mandatory_failures"], [])
        self.assertEqual(payload["e2e_task_id"],
                         "01234567-89ab-cdef-0123-456789abcdef")
        self.assertEqual(payload["executor"], "opencode")
        self.assertEqual(payload["reviewer"], "hermes")
        self.assertTrue(payload["result_present"])
        self.assertTrue(payload["verification_present"])


class TestCoreE2ESources(unittest.TestCase):
    """The ``_core_e2e_outcome`` and ``_actual_reviewer_and_executor``
    helpers must read from the real-E2E slots in priority order."""

    def setUp(self):
        acceptance.checks.clear()
        acceptance._reset_real_e2e_state()

    def test_task_id_rejects_dict_repr_evidence(self):
        """The dict-repr evidence form (``\"{'status': 'completed'}\"``)
        was the historic source of the synthetic PASS bug. The helper
        must reject it and only accept UUID-shaped strings."""
        acceptance.check("e2e:real-parent-completed", True,
                         {"status": "completed"})
        outcome = acceptance._core_e2e_outcome()
        self.assertEqual(outcome["task_id"], "")

    def test_task_id_accepts_uuid_evidence(self):
        acceptance.check("e2e:real-parent-completed", True,
                         {"status": "completed"})
        # Inject a UUID-shaped string as the evidence body. This path
        # only fires when the real-E2E slot is empty, so it is used by
        # unit tests that want to assert a known UUID.
        acceptance.checks.clear()
        acceptance.check("e2e:real-parent-completed", True,
                         "01234567-89ab-cdef-0123-456789abcdef")
        outcome = acceptance._core_e2e_outcome()
        self.assertEqual(outcome["task_id"],
                         "01234567-89ab-cdef-0123-456789abcdef")

    def test_task_id_prefers_real_e2e_slot(self):
        acceptance.check("e2e:real-parent-completed", True,
                         "ffffffff-ffff-ffff-ffff-ffffffffffff")
        acceptance._REAL_E2E_PARENT_ID = "01234567-89ab-cdef-0123-456789abcdef"
        outcome = acceptance._core_e2e_outcome()
        self.assertEqual(outcome["task_id"],
                         "01234567-89ab-cdef-0123-456789abcdef")

    def test_actual_executor_prefers_real_e2e_slot(self):
        # Evidence suggests claude but the real-E2E slot says opencode.
        acceptance.check("e2e:independent-semantic-gate", True,
                         [{"actual_executor": "claude",
                           "verification": {"passed": True,
                                            "reviewer": "hermes",
                                            "reviewer_backend": "hermes-cli"}}])
        acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
        acceptance._REAL_E2E_REVIEWER = "hermes"
        executor, reviewer = acceptance._actual_reviewer_and_executor()
        self.assertEqual(executor, "opencode")
        self.assertEqual(reviewer, "hermes")


if __name__ == "__main__":
    unittest.main()