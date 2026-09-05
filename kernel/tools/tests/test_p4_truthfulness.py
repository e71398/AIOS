#!/usr/bin/env python3
"""AIOS P4 truthfulness tests (offline, no provider calls)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

from aios_health_model import (
    STATUS_HEALTHY, STATUS_DEGRADED, STATUS_FAILED, STATUS_UNKNOWN,
    aggregate_status,
    evaluate_runtime_dependency,
    probe_service_state,
    probe_capability,
    probe_authoritative_state,
    probe_latest_e2e_acceptance,
    assemble_health_report,
    HealthDimension,
)

import aios_acceptance as acceptance
import aios_verification_gate as verification_gate


def _dim(status, mandatory=True, reason="OK"):
    return HealthDimension(
        unit="probe", status=status, reason_code=reason, mandatory=mandatory,
        evidence_source="test", observed_at="2026-07-24T00:00:00+00:00",
        age_seconds=0, summary=reason,
    )


def _full_dims(overrides=None):
    dims = {
        "service_state": _dim(STATUS_HEALTHY),
        "endpoint_reachability": _dim(STATUS_HEALTHY),
        "runtime_revision": _dim(STATUS_HEALTHY),
        "executor_capability": _dim(STATUS_HEALTHY),
        "reviewer_capability": _dim(STATUS_HEALTHY),
        "authoritative_state": _dim(STATUS_HEALTHY),
        "latest_e2e_acceptance": _dim(STATUS_HEALTHY),
    }
    if overrides:
        dims.update(overrides)
    return dims


class TestHealthAggregation(unittest.TestCase):
    def test_all_mandatory_pass_no_degradation_is_healthy(self):
        dims = _full_dims()
        self.assertEqual(aggregate_status(dims), STATUS_HEALTHY)
        report = assemble_health_report(dims)
        self.assertEqual(report.overall_status, STATUS_HEALTHY)
        self.assertEqual(report.mandatory_failures, [])
        self.assertEqual(report.optional_degradations, [])

    def test_optional_claude_degraded_is_overall_degraded(self):
        overrides = {
            "claude_optional": _dim(STATUS_DEGRADED, mandatory=False,
                                     reason="DEGRADED_EXTERNAL_402"),
            "codex_optional": _dim(STATUS_DEGRADED, mandatory=False,
                                    reason="DEGRADED_EXTERNAL_PLAN_429"),
        }
        dims = _full_dims(overrides)
        self.assertEqual(aggregate_status(dims), STATUS_DEGRADED)
        report = assemble_health_report(dims)
        self.assertEqual(report.overall_status, STATUS_DEGRADED)
        self.assertEqual(report.mandatory_failures, [])
        self.assertIn("claude_optional", report.optional_degradations)
        self.assertIn("codex_optional", report.optional_degradations)

    def test_gateway_down_is_failed(self):
        dims = _full_dims({
            "service_state": _dim(STATUS_FAILED, reason="GATEWAY_DOWN"),
            "endpoint_reachability": _dim(STATUS_FAILED,
                                          reason="ENDPOINT_UNREACHABLE"),
        })
        self.assertEqual(aggregate_status(dims), STATUS_FAILED)
        report = assemble_health_report(dims)
        self.assertIn("service_state", report.mandatory_failures)
        self.assertIn("endpoint_reachability", report.mandatory_failures)

    def test_redis_down_is_failed(self):
        dims = _full_dims({
            "endpoint_reachability": _dim(STATUS_FAILED,
                                          reason="ENDPOINT_UNREACHABLE_REDIS"),
            "authoritative_state": _dim(STATUS_FAILED,
                                        reason="REDIS_UNREACHABLE"),
        })
        self.assertEqual(aggregate_status(dims), STATUS_FAILED)

    def test_no_executor_is_failed(self):
        capability = {"opencode": "UNAVAILABLE", "hermes": "AVAILABLE",
                      "claude": "DEGRADED_EXTERNAL",
                      "codex": "DEGRADED_EXTERNAL",
                      "openclaw": "UNVERIFIED"}
        dim = probe_capability("executor_capability", capability,
                                mandatory=True)
        self.assertEqual(dim.status, STATUS_FAILED)
        self.assertEqual(dim.reason_code, "NO_EXECUTOR_AVAILABLE")

    def test_no_independent_reviewer_is_failed(self):
        # When every reviewer candidate is unavailable (including
        # opencode fallback) the dimension must surface as FAILED.
        capability = {"opencode": "UNAVAILABLE", "hermes": "UNAVAILABLE",
                      "claude": "UNAVAILABLE", "codex": "UNAVAILABLE",
                      "openclaw": "UNVERIFIED"}
        dim = probe_capability("reviewer_capability", capability,
                                mandatory=True)
        self.assertEqual(dim.status, STATUS_FAILED)
        self.assertEqual(dim.reason_code,
                         "NO_INDEPENDENT_REVIEWER_AVAILABLE")

    def test_reviewer_fallback_is_degraded(self):
        # When the primary reviewer (hermes) is unavailable but a
        # fallback reviewer (opencode) is available, the dimension
        # is DEGRADED, not FAILED.
        capability = {"opencode": "AVAILABLE", "hermes": "UNAVAILABLE",
                      "claude": "DEGRADED_EXTERNAL",
                      "codex": "DEGRADED_EXTERNAL",
                      "openclaw": "UNVERIFIED"}
        dim = probe_capability("reviewer_capability", capability,
                                mandatory=True)
        self.assertEqual(dim.status, STATUS_DEGRADED)
        self.assertEqual(dim.reason_code, "OPTIONAL_REVIEWER_DEGRADED")

    def test_stale_acceptance_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "schema_version": "aios-acceptance/2.0",
                "core_result": "PASS",
                "overall_status": "HEALTHY",
            }
            report_path = tmp_path / "canary_20200101_000000.json"
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            old_time = report_path.stat().st_mtime - 10 * 3600
            os.utime(report_path, (old_time, old_time))
            dim = probe_latest_e2e_acceptance(
                tmp_path, max_age_seconds=8 * 3600, kind="canary",
            )
            self.assertEqual(dim.status, STATUS_UNKNOWN)
            self.assertEqual(dim.reason_code,
                             "ACCEPTANCE_EVIDENCE_STALE")

    def test_missing_acceptance_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            dim = probe_latest_e2e_acceptance(
                Path(tmp), max_age_seconds=8 * 3600, kind="canary",
            )
            self.assertEqual(dim.status, STATUS_UNKNOWN)
            self.assertEqual(dim.reason_code,
                             "ACCEPTANCE_EVIDENCE_MISSING")

    def test_acceptable_acceptance_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = {
                "schema_version": "aios-acceptance/2.0",
                "core_result": "PASS",
                "overall_status": "HEALTHY",
            }
            report_path = tmp_path / "canary_20260724_000000.json"
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            dim = probe_latest_e2e_acceptance(
                tmp_path, max_age_seconds=8 * 3600, kind="canary",
            )
            self.assertEqual(dim.status, STATUS_HEALTHY)
            self.assertEqual(dim.reason_code,
                             "LATEST_ACCEPTANCE_PASSED")


class TestRuntimeDependency(unittest.TestCase):
    def test_current_when_dependency_older_than_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dep.py"
            path.write_text("print('hello')\n", encoding="utf-8")
            future = path.stat().st_mtime + 5
            status, reason, _ = evaluate_runtime_dependency(
                pid=12345, process_started_at=future,
                dependency_paths=[path],
            )
            self.assertEqual(status, STATUS_HEALTHY)
            self.assertEqual(reason, "RUNTIME_DEPENDENCY_CURRENT")

    def test_stale_when_dependency_newer_than_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dep.py"
            path.write_text("print('hello')\n", encoding="utf-8")
            past = path.stat().st_mtime - 5
            status, reason, _ = evaluate_runtime_dependency(
                pid=12345, process_started_at=past,
                dependency_paths=[path],
            )
            self.assertEqual(status, STATUS_FAILED)
            self.assertEqual(reason, "RUNTIME_DEPENDENCY_STALE")

    def test_missing_dependency_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.py"
            status, reason, _ = evaluate_runtime_dependency(
                pid=12345, process_started_at=1.0,
                dependency_paths=[missing],
            )
            self.assertEqual(status, STATUS_UNKNOWN)
            self.assertEqual(reason, "DEPENDENCY_MISSING")

    def test_no_pid_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dep.py"
            path.write_text("ok\n", encoding="utf-8")
            status, reason, _ = evaluate_runtime_dependency(
                pid=None, process_started_at=None,
                dependency_paths=[path],
            )
            self.assertEqual(status, STATUS_UNKNOWN)
            self.assertEqual(reason, "PROCESS_MISSING")


class TestAcceptanceClassification(unittest.TestCase):
    def setUp(self):
        acceptance.checks.clear()
        # Reset the real-E2E slots so each test starts from a clean
        # baseline. Tests that want to assert "all required fields are
        # present" populate them via ``_populate_mandatory_success_checks``.
        acceptance._reset_real_e2e_state()
        # P5F: isolate ``write_report`` / ``canary_main`` writes to a
        # temporary directory so offline tests cannot pollute the
        # production ``logs/acceptance`` directory.
        self._p5f_reports_dir = tempfile.mkdtemp(prefix="aios-p5f-")
        self._p5f_original_reports = acceptance.REPORTS
        acceptance.REPORTS = Path(self._p5f_reports_dir)

    def tearDown(self):
        # Restore the production REPORTS path and clean up the temp
        # directory used by the test.
        acceptance.REPORTS = self._p5f_original_reports
        import shutil
        shutil.rmtree(self._p5f_reports_dir, ignore_errors=True)

    def _populate_mandatory_success_checks(self):
        acceptance.check("e2e:gateway-ack-under-2s", True, {"ack_ms": 50})
        acceptance.check("e2e:unique-parent-id", True, {"count": 1})
        acceptance.check("e2e:real-parent-completed", True,
                          {"status": "completed"})
        acceptance.check("e2e:independent-semantic-gate", True,
                          [{"actual_executor": "opencode",
                            "verification": {"passed": True,
                                             "reviewer": "hermes",
                                             "reviewer_backend":
                                                 "hermes-cli"}}])
        acceptance.check("e2e:actual-executor-recorded", True,
                          [{"actual_executor": "opencode"}])
        acceptance.check("e2e:test-evidence-not-admitted-to-learning", True,
                          {"learning_events": 0})
        acceptance.check("e2e:parent-trace-recorded", True, {"events": 5})
        # Populate the real-E2E slots so the canary truthfulness guard
        # does not reject this success path. The values mirror what a
        # genuine ``run_real_e2e`` would capture.
        acceptance._REAL_E2E_PARENT_ID = "12345678-1234-5678-1234-567812345678"
        acceptance._REAL_E2E_NODES = [{
            "actual_executor": "opencode",
            "verification": {
                "passed": True,
                "reviewer": "hermes",
                "reviewer_backend": "hermes-cli",
            },
        }]
        # P8D: short-circuit the new routing-snapshot gates so legacy
        # tests do not require a live monitor (8086).
        for name in (
            "registered_tools_have_effective_route",
            "canary_sender_isolated",
            "actual_binding_recorded",
            "tool_switch_recorded",
            "model_identity_preserved",
            "no_hidden_fallback",
            "claude_model_failover_success",
            "codex_effective_route_truthful",
            "tool_failover_success",
            "all_enabled_tools_operational_signal",
        ):
            acceptance._p8d_set_override(name, True)
        acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
        acceptance._REAL_E2E_REVIEWER = "hermes"
        acceptance._REAL_E2E_RESULT_PRESENT = True
        acceptance._REAL_E2E_VERIFICATION_PRESENT = True

    def test_opencode_hermes_success_claude_down_is_pass(self):
        self._populate_mandatory_success_checks()
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
                                  return_value={"opencode": 1, "hermes": 1,
                                                "claude": 0, "codex": 0,
                                                "openclaw": 0,
                                                "telegram": 0,
                                                "feishu": 0}):
                    payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "PASS")
        self.assertEqual(payload["overall_status"], "DEGRADED")
        self.assertEqual(payload["capability_status"], "DEGRADED")
        self.assertIn("claude", payload["optional_capabilities"])
        self.assertIn("codex", payload["optional_capabilities"])
        self.assertEqual(payload["mandatory_failures"], [])

    def test_authoritative_parent_missing_is_state_failure(self):
        acceptance.check("e2e:gateway-ack-under-2s", True, {"ack_ms": 50})
        acceptance.check("e2e:unique-parent-id", True, {"count": 1})
        acceptance.check("e2e:real-parent-completed", False, "timeout")
        acceptance.check("e2e:independent-semantic-gate", False, [])
        acceptance.check("e2e:actual-executor-recorded", False, [])
        acceptance.check("e2e:test-evidence-not-admitted-to-learning", True,
                          {"learning_events": 0})
        acceptance.check("e2e:parent-trace-recorded", False, "no trace")
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={}):
                    payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "FAIL")
        self.assertEqual(payload["overall_status"], "FAILED")
        self.assertIn("AUTHORITATIVE_STATE_FAILURE",
                      payload["failure_classes"])

    def test_stale_runtime_dependency_classification(self):
        self._populate_mandatory_success_checks()
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": False,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": [{"unit":
                                                       "aios-orchestrator",
                                                   "status": "STALE"}]}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={}):
                    payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "FAIL")
        self.assertIn("RUNTIME_DEPENDENCY_STALE",
                      payload["failure_classes"])
        self.assertIn("runtime_revision_current",
                      payload["mandatory_failures"])

    def test_optional_only_failure_is_degraded(self):
        self._populate_mandatory_success_checks()
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE",
                                            "hermes": "AVAILABLE",
                                            "claude": "DEGRADED_EXTERNAL"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={"claude": 0}):
                    payload, _ = acceptance.write_report("canary")
        self.assertEqual(payload["core_result"], "PASS")
        self.assertEqual(payload["overall_status"], "DEGRADED")
        self.assertEqual(payload["mandatory_failures"], [])
        self.assertEqual(payload["capability_matrix"]["claude"],
                         "DEGRADED_EXTERNAL")
        self.assertIn("claude", payload["optional_capabilities"])


class TestRepairMetadata(unittest.TestCase):
    def test_legal_verdict_no_repair_attributes(self):
        with patch.object(verification_gate, "_review_policy",
                          return_value={"exclude_executor": True,
                                        "reviewers":
                                            [{"id": "hermes",
                                              "backend": "hermes-cli"}]}):
            with patch.object(verification_gate, "_call_reviewer_once",
                              return_value={
                                  "reviewer": "hermes",
                                  "returncode": 0,
                                  "stdout_bytes": 0,
                                  "stderr_bytes": 0,
                                  "stderr_text": "",
                                  "stdout_text":
                                      '{"passed":true,"reason":"ok"}',
                                  "latency_ms": 10,
                                  "live_recovery": False,
                                  "extract_category":
                                      verification_gate.VERDICT_EXTRACT_OK,
                                  "parsed_value": {"passed": True,
                                                   "reason": "ok"},
                                  "provider_kind": "AVAILABLE",
                                  "provider_description": "ok",
                                  "provider_fatal": False,
                                  "adapter": None,
                              }):
                outcome = verification_gate._semantic_review(
                    "test prompt", executor="opencode",
                )
        accepted = [a for a in outcome["attempts"]
                    if a.get("reason") == "verdict_accepted"]
        self.assertEqual(len(accepted), 1)
        accepted_attempt = accepted[0]
        self.assertFalse(accepted_attempt["repair_attempted"])
        self.assertFalse(accepted_attempt["repair_success"])
        self.assertEqual(outcome["repair_attempted"], False)
        self.assertEqual(outcome["repair_success"], False)

    def test_repair_succeeds_records_success(self):
        with patch.object(verification_gate, "_review_policy",
                          return_value={"exclude_executor": True,
                                        "reviewers":
                                            [{"id": "hermes",
                                              "backend": "hermes-cli"}]}):
            with patch.object(verification_gate, "_call_reviewer_once",
                              side_effect=[
                                  {
                                      "reviewer": "hermes",
                                      "returncode": 0,
                                      "stdout_bytes": 0,
                                      "stderr_bytes": 0,
                                      "stderr_text": "",
                                      "stdout_text": "not json",
                                      "latency_ms": 10,
                                      "live_recovery": False,
                                      "extract_category":
                                          verification_gate.VERDICT_EXTRACT_NO_JSON_OBJECT,
                                      "parsed_value": None,
                                      "provider_kind":
                                          "MALFORMED_RESPONSE",
                                      "provider_description": "no JSON",
                                      "provider_fatal": False,
                                      "adapter": None,
                                  },
                                  {
                                      "reviewer": "hermes",
                                      "returncode": 0,
                                      "stdout_bytes": 0,
                                      "stderr_bytes": 0,
                                      "stderr_text": "",
                                      "stdout_text":
                                          '{"passed":true,"reason":"repaired"}',
                                      "latency_ms": 12,
                                      "live_recovery": False,
                                      "extract_category":
                                          verification_gate.VERDICT_EXTRACT_OK,
                                      "parsed_value": {"passed": True,
                                                       "reason": "repaired"},
                                      "provider_kind": "AVAILABLE",
                                      "provider_description": "ok",
                                      "provider_fatal": False,
                                      "adapter": None,
                                  },
                              ]):
                outcome = verification_gate._semantic_review(
                    "test prompt", executor="opencode",
                )
        accepted = [a for a in outcome["attempts"]
                    if a.get("reason") == "verdict_accepted"]
        self.assertEqual(len(accepted), 1)
        accepted_attempt = accepted[0]
        self.assertTrue(accepted_attempt["repair_attempted"])
        self.assertTrue(accepted_attempt["repair_success"])
        self.assertEqual(outcome["repair_attempted"], True)
        self.assertEqual(outcome["repair_success"], True)


class TestExitCodeSemantics(unittest.TestCase):
    def setUp(self):
        acceptance.checks.clear()
        acceptance._reset_real_e2e_state()
        # P5F: isolate ``canary_main`` writes to a temporary directory.
        self._p5f_reports_dir = tempfile.mkdtemp(prefix="aios-p5f-")
        self._p5f_original_reports = acceptance.REPORTS
        acceptance.REPORTS = Path(self._p5f_reports_dir)

    def tearDown(self):
        acceptance.REPORTS = self._p5f_original_reports
        import shutil
        shutil.rmtree(self._p5f_reports_dir, ignore_errors=True)

    def test_core_pass_with_optional_degradation_returns_zero(self):
        # canary_main() clears acceptance.checks before running the
        # real E2E, so we patch run_real_e2e with a function that
        # populates the mandatory checks inside the caller's lifetime.
        def fake_real_e2e():
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
            # Mirror the real run_real_e2e side-effects so the
            # truthfulness guard accepts this path.
            acceptance._REAL_E2E_PARENT_ID = (
                "abcdef01-2345-6789-abcd-ef0123456789"
            )
            acceptance._REAL_E2E_NODES = [{
                "actual_executor": "opencode",
                "verification": {"passed": True, "reviewer": "hermes",
                                 "reviewer_backend": "hermes-cli"},
            }]
            acceptance._REAL_E2E_ACTUAL_EXECUTOR = "opencode"
            acceptance._REAL_E2E_REVIEWER = "hermes"
            acceptance._REAL_E2E_RESULT_PRESENT = True
            acceptance._REAL_E2E_VERIFICATION_PRESENT = True

        acceptance.checks.clear()
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE",
                                            "hermes": "AVAILABLE",
                                            "claude": "DEGRADED_EXTERNAL"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={"claude": 0}):
                    with patch.object(acceptance, "run_real_e2e",
                                       fake_real_e2e):
                        exit_code = acceptance.canary_main()
        self.assertEqual(exit_code, 0)

    def test_core_fail_returns_non_zero(self):
        acceptance.checks.clear()
        for name in ("e2e:gateway-ack-under-2s",
                     "e2e:unique-parent-id",
                     "e2e:real-parent-completed",
                     "e2e:independent-semantic-gate",
                     "e2e:actual-executor-recorded",
                     "e2e:test-evidence-not-admitted-to-learning",
                     "e2e:parent-trace-recorded"):
            acceptance.check(name, False, "timeout")
        with patch.object(acceptance, "_runtime_revision_status",
                          return_value={"all_current": True,
                                        "loaded_revisions": {},
                                        "expected_revisions": {},
                                        "drift": []}):
            with patch.object(acceptance, "_build_capability_matrix",
                              return_value={"opencode": "AVAILABLE"}):
                with patch.object(acceptance, "_provider_call_counts",
                                  return_value={}):
                    with patch.object(acceptance, "run_real_e2e",
                                       lambda: None):
                        exit_code = acceptance.canary_main()
        self.assertEqual(exit_code, 1)


class TestAuthoritativeStateProbe(unittest.TestCase):
    def test_redis_unreachable_is_failed(self):
        dim = probe_authoritative_state(parent_present=False,
                                          result_present=False,
                                          verification_present=False,
                                          redis_reachable=False)
        self.assertEqual(dim.status, STATUS_FAILED)
        self.assertEqual(dim.reason_code, "REDIS_UNREACHABLE")

    def test_no_recent_evidence_is_unknown(self):
        # P5F: no recent acceptance evidence at all is UNKNOWN, not
        # FAILED. AIOS is idle most of the time; the absence of an
        # active workflow is the normal state and MUST NOT be
        # misreported as a failure.
        dim = probe_authoritative_state(parent_present=False,
                                          result_present=False,
                                          verification_present=False,
                                          redis_reachable=True)
        self.assertEqual(dim.status, STATUS_UNKNOWN)
        self.assertEqual(dim.reason_code,
                         "NO_RECENT_AUTHORITATIVE_EVIDENCE")

    def test_all_present_with_recent_pass_is_healthy(self):
        # P5F: when the latest acceptance is a real PASS (mocked
        # here as a fresh canary) and Redis still agrees that the
        # parent/result/verification are present, the dimension is
        # HEALTHY with the idempotent reason code.
        latest = {
            "task_id": "01234567-89ab-cdef-0123-456789abcdef",
            "core_result": "PASS",
            "age_seconds": 5,
        }
        dim = probe_authoritative_state(parent_present=True,
                                          result_present=True,
                                          verification_present=True,
                                          redis_reachable=True,
                                          latest_acceptance=latest)
        self.assertEqual(dim.status, STATUS_HEALTHY)
        self.assertEqual(dim.reason_code,
                         "LATEST_ACCEPTANCE_AUTHORITATIVE_STATE_COMPLETE")

    def test_idle_with_recent_pass_is_healthy(self):
        # P5F: when the latest acceptance was a real PASS but we are
        # now idle (Redis has expired the workflow state), the probe
        # MUST report HEALTHY with the IDLE_WITH_RECENT_AUTHORITATIVE_SUCCESS
        # reason code instead of declaring FAILED merely because the
        # in-flight workflow has been cleaned up.
        latest = {
            "task_id": "01234567-89ab-cdef-0123-456789abcdef",
            "core_result": "PASS",
            "age_seconds": 60,
        }
        dim = probe_authoritative_state(parent_present=False,
                                          result_present=False,
                                          verification_present=False,
                                          redis_reachable=True,
                                          latest_acceptance=latest)
        self.assertEqual(dim.status, STATUS_HEALTHY)
        self.assertEqual(dim.reason_code,
                         "IDLE_WITH_RECENT_AUTHORITATIVE_SUCCESS")

    def test_missing_parent_with_pass_is_contradiction(self):
        # P5F: when the latest acceptance is PASS but Redis carries
        # only a partial snapshot (here result+verification present
        # but parent missing), the probe MUST report
        # AUTHORITATIVE_STATE_CONTRADICTION rather than silently
        # fudging to HEALTHY.
        latest = {
            "task_id": "01234567-89ab-cdef-0123-456789abcdef",
            "core_result": "PASS",
            "age_seconds": 90,
        }
        dim = probe_authoritative_state(parent_present=False,
                                          result_present=True,
                                          verification_present=True,
                                          redis_reachable=True,
                                          latest_acceptance=latest)
        self.assertEqual(dim.status, STATUS_FAILED)
        self.assertEqual(dim.reason_code,
                         "AUTHORITATIVE_STATE_CONTRADICTION")

    def test_post_canary_partial_state_is_healthy(self):
        # P5F: after a successful canary, the short-lived result /
        # verification string keys naturally expire while the trace
        # zset is preserved. The probe MUST treat this as
        # IDLE_WITH_RECENT_AUTHORITATIVE_SUCCESS (the canary is the
        # source of truth, Redis is supplementary evidence).
        latest = {
            "task_id": "01234567-89ab-cdef-0123-456789abcdef",
            "core_result": "PASS",
            "age_seconds": 90,
        }
        dim = probe_authoritative_state(parent_present=True,
                                          result_present=False,
                                          verification_present=False,
                                          redis_reachable=True,
                                          latest_acceptance=latest)
        self.assertEqual(dim.status, STATUS_HEALTHY)
        self.assertEqual(dim.reason_code,
                         "IDLE_WITH_RECENT_AUTHORITATIVE_SUCCESS")

    def test_latest_pass_failure_is_failed(self):
        latest = {
            "task_id": "01234567-89ab-cdef-0123-456789abcdef",
            "core_result": "FAIL",
            "age_seconds": 10,
        }
        dim = probe_authoritative_state(parent_present=True,
                                          result_present=True,
                                          verification_present=True,
                                          redis_reachable=True,
                                          latest_acceptance=latest)
        self.assertEqual(dim.status, STATUS_FAILED)
        self.assertEqual(dim.reason_code, "LATEST_ACCEPTANCE_FAILED")

    def test_stale_acceptance_is_unknown(self):
        # P5F: even when Redis is fully present, an old canary (>8h)
        # MUST downgrade to UNKNOWN rather than claiming HEALTHY.
        latest = {
            "task_id": "01234567-89ab-cdef-0123-456789abcdef",
            "core_result": "PASS",
            "age_seconds": 9 * 3600,
        }
        dim = probe_authoritative_state(parent_present=True,
                                          result_present=True,
                                          verification_present=True,
                                          redis_reachable=True,
                                          latest_acceptance=latest)
        self.assertEqual(dim.status, STATUS_UNKNOWN)
        self.assertEqual(dim.reason_code,
                         "NO_RECENT_AUTHORITATIVE_EVIDENCE")

    def test_acceptance_without_task_id_is_unknown(self):
        # P5F: a report that lacks a real task id can never be
        # authoritative, regardless of what core_result claims.
        latest = {
            "task_id": "",
            "core_result": "PASS",
            "age_seconds": 5,
        }
        dim = probe_authoritative_state(parent_present=True,
                                          result_present=True,
                                          verification_present=True,
                                          redis_reachable=True,
                                          latest_acceptance=latest)
        self.assertEqual(dim.status, STATUS_UNKNOWN)
        self.assertEqual(dim.reason_code,
                         "NO_RECENT_AUTHORITATIVE_EVIDENCE")


class TestServiceStateProbe(unittest.TestCase):
    def test_all_active_is_healthy(self):
        dim = probe_service_state(["aios-monitor.service"],
                                    lambda unit: True)
        self.assertEqual(dim.status, STATUS_HEALTHY)

    def test_critical_unit_down_is_failed(self):
        def active(unit):
            return "aios-entry-gateway.service" not in unit
        dim = probe_service_state(
            ["aios-entry-gateway.service", "aios-orchestrator.service"],
            active,
        )
        self.assertEqual(dim.status, STATUS_FAILED)

    def test_non_critical_unit_down_is_degraded(self):
        def active(unit):
            return unit != "aios-orchestrator.service"
        # critical missing → FAILED; but only optional missing → DEGRADED
        dim = probe_service_state(
            ["aios-monitor.service", "aios-orchestrator.service"],
            active,
        )
        self.assertEqual(dim.status, STATUS_FAILED)
        def optional_active(unit):
            return unit != "aios-executor-opencode.service"
        dim = probe_service_state(
            ["aios-monitor.service", "aios-executor-opencode.service"],
            optional_active,
        )
        self.assertEqual(dim.status, STATUS_DEGRADED)


class TestRuntimeRevisionProbe(unittest.TestCase):
    def test_stale_status_recorded_when_dependency_newer(self):
        from aios_health_model import probe_runtime_revision
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dep.py"
            path.write_text("print('hello')\n", encoding="utf-8")
            past = path.stat().st_mtime - 5
            dim = probe_runtime_revision(pid=999, process_started_at=past,
                                          dependency_paths=[path])
            self.assertEqual(dim.status, STATUS_FAILED)
            self.assertEqual(dim.reason_code,
                             "RUNTIME_DEPENDENCY_STALE")


if __name__ == "__main__":
    unittest.main()