#!/usr/bin/env python3
"""Production reviewer runtime + evidence correction retry tests (2026-08-10).

These tests lock down the two behaviours the final production closure
hinges on:

  1. Reviewer Runtime — Hermes subprocess health is observable,
     and the existing reviewer fallback surface can recover when
     the primary reviewer is unhealthy.  We do NOT assert reviewer
     subprocess wall-clock or shelling into Hermes here; we verify
     that the repair / verification helpers classify the failure
     correctly and the bounded retry budget is honoured.

  2. Evidence Correction — when a Reviewer / Verifier rejects a
     concrete numeric / factual claim that contradicts
     authoritative live evidence, the bounded correction retry
     passes the prior deliverable and the conflicting claim to a
     healthy Executor.  The retry is bounded (MAX_REPAIRS=2) and
     refuses to feed unbounded context (full journal / Redis)
     into the next attempt.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest import mock

TOOLS = os.path.join(os.path.dirname(__file__), "..")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


def _import_orchestrator():
    """Import the orchestrator module fresh for each test class so
    module-level caches (executor lists, capability overlays) start
    from a known state.
    """
    if "aios_orchestrator" in sys.modules:
        del sys.modules["aios_orchestrator"]
    if "aios_verification_gate" in sys.modules:
        del sys.modules["aios_verification_gate"]
    return importlib.import_module("aios_orchestrator")


def _import_verification_gate():
    if "aios_verification_gate" in sys.modules:
        del sys.modules["aios_verification_gate"]
    if "aios_orchestrator" in sys.modules:
        del sys.modules["aios_orchestrator"]
    return importlib.import_module("aios_verification_gate")


# ---------------------------------------------------------------------------
# Reviewer Runtime — failure taxonomy + choose_reviewer contract
# ---------------------------------------------------------------------------


class ReviewerRuntimeTests(unittest.TestCase):
    """Lock the Reviewer runtime health contract."""

    def setUp(self):
        self.gate = _import_verification_gate()
        self.orch = _import_orchestrator()
        # Pre-load the modules whose functions are imported inside
        # ``_call_reviewer_once`` and ``choose_reviewer`` so the
        # in-function imports resolve to a module we can mock.
        self.failover_mod = importlib.import_module("aios_tool_failover")
        self.registry_mod = importlib.import_module("aios_tool_registry")
        self.adapter_mod = importlib.import_module("aios_tool_adapter")

    def test_reviewer_subprocess_timeout_is_classified(self):
        """When the Hermes subprocess TIMEOUTs, ``_call_reviewer_once``
        MUST append a TIMEOUT-class attempt entry instead of letting
        the exception bubble up.  The contract is the building block
        for ``all_independent_reviewers_unavailable`` aggregation.
        """
        import subprocess as _real_subprocess
        attempts = []
        adapter = mock.Mock()
        adapter.health.return_value = {
            "fully_operational": True,
            "model_state": "ready",
        }
        adapter.config = {"task_timeout_seconds": 30}
        adapter.command_for_task.return_value = [
            "echo", "-z", "fake",
        ]
        with mock.patch.object(
            self.adapter_mod, "get_adapter", return_value=adapter
        ), mock.patch.object(
            self.gate, "subprocess"
        ) as _sp, mock.patch.object(
            self.gate, "_allow_live_semantic_recovery", return_value=False
        ):
            _sp.TimeoutExpired = _real_subprocess.TimeoutExpired
            _sp.run.side_effect = _real_subprocess.TimeoutExpired(
                cmd=["echo", "-z", "fake"], timeout=30
            )
            result = self.gate._call_reviewer_once(
                "prompt", "hermes", attempts, binding_id="hermes:minimax"
            )
        self.assertIsNone(result)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["reviewer"], "hermes")
        self.assertIn("TIMEOUT", attempts[0]["reason"])
        self.assertEqual(attempts[0]["provider_kind"], "TIMEOUT")
        self.assertTrue(attempts[0]["provider_fatal"])

    def test_reviewer_failure_scope_taxonomy_distinguishes_timeouts(self):
        """A Hermes subprocess timeout MUST NOT collapse into
        ``provider unavailable``.  The taxonomy in
        ``_classify_provider_failure`` separates TIMEOUT from
        NETWORK_ERROR so the verification gate can surface the
        ``REVIEW_TIMEOUT`` bucket to the orchestrator.
        """
        kind, desc, fatal = self.gate._classify_provider_failure(
            "TimeoutExpired: hermes subprocess timed out",
            returncode=-1,
            parsed_dict=None,
            extract_category="EMPTY_OUTPUT",
        )
        self.assertEqual(kind, "TIMEOUT")
        self.assertTrue(fatal)
        self.assertNotEqual(kind, "AUTH_FAILED")
        self.assertNotEqual(kind, "CONNECTION_FAILED")

    def test_hermes_binding_recorded_for_failure_scope(self):
        """When the primary reviewer is Hermes via ``hermes:minimax``
        binding, the call MUST remember the binding so the audit
        ledger distinguishes ``REVIEWER_PROCESS`` /
        ``REVIEWER_ADAPTER`` / ``REVIEW_TIMEOUT`` etc.
        """
        attempts: list = []
        adapter = mock.Mock()
        adapter.health.return_value = {
            "fully_operational": True,
            "model_state": "ready",
        }
        adapter.config = {"task_timeout_seconds": 30}
        adapter.command_for_task.return_value = ["hermes", "-z", "ok"]
        with mock.patch.object(
            self.adapter_mod, "get_adapter", return_value=adapter
        ), mock.patch.object(
            self.gate, "subprocess"
        ) as _sp, mock.patch.object(
            self.gate, "_allow_live_semantic_recovery", return_value=False
        ):
            fake_result = mock.Mock()
            fake_result.returncode = 0
            fake_result.stdout = '{"passed": true, "reason": "ok"}'
            fake_result.stderr = ""
            _sp.run.return_value = fake_result
            with mock.patch.object(
                self.gate,
                "_extract_verdict_json",
                return_value=("VALID", {"passed": True, "reason": "ok"}),
            ):
                result = self.gate._call_reviewer_once(
                    "p", "hermes", attempts, binding_id="hermes:minimax"
                )
        self.assertIsNotNone(result)
        self.assertEqual(result["binding_id"], "hermes:minimax")
        self.assertEqual(attempts, [])

    def _patch_choose_reviewer_dependencies(self, statuses, registry_callable):
        """Patch the in-function imports used by ``choose_reviewer``.

        ``registry_callable`` MUST be a callable that accepts a role
        string and returns the list of reviewer manifests.  We use
        ``side_effect`` so calling ``registry.list_by_role(role)``
        dispatches to the helper.
        """
        registry = mock.Mock()
        registry.list_by_role.side_effect = registry_callable
        engine = mock.Mock()
        engine.compute_tool_status.side_effect = (
            lambda n: statuses.get(n)
        )
        return (
            mock.patch.object(
                self.failover_mod,
                "get_default_tool_engine",
                create=True,
                return_value=engine,
            ),
            mock.patch.object(
                self.registry_mod,
                "get_default_registry",
                create=True,
                return_value=registry,
            ),
        )

    def _list_with(self, *tools):
        def _list(role):
            if role == "reviewer":
                return [mock.Mock(tool_id=t) for t in tools]
            return []
        return _list

    def test_strict_reviewer_must_still_call_healthy_hermes(self):
        """``allow_reviewer_fallback=False`` MUST NOT short-circuit
        a healthy primary reviewer call.
        """
        statuses = {"hermes": mock.Mock(status="AVAILABLE_PRIMARY")}
        engine_patch, registry_patch = self._patch_choose_reviewer_dependencies(
            statuses, self._list_with("hermes", "openclaw")
        )
        with engine_patch, registry_patch, mock.patch.object(
            self.orch, "_tool_process_health", return_value=True
        ):
            result = self.orch.choose_reviewer(
                task_id="t",
                preferred_reviewer="hermes",
                allow_reviewer_fallback=False,
            )
        self.assertEqual(result["reviewer"], "hermes")
        self.assertEqual(result["fallback_count"], 0)

    def test_reviewer_fallback_count_is_bounded(self):
        """When multiple reviewer candidates are unhealthy, the
        fallback_count MUST equal the number of candidates skipped
        before landing on a healthy one (or 0 when primary succeeds).
        """
        statuses = {
            "hermes": mock.Mock(
                status="UNAVAILABLE_TOOL_RUNTIME",
                effective_binding="",
            ),
            "openclaw": mock.Mock(
                status="AVAILABLE_WITH_MODEL_FALLBACK",
                effective_binding="openclaw:minimax",
            ),
        }
        engine_patch, registry_patch = self._patch_choose_reviewer_dependencies(
            statuses, self._list_with("hermes", "openclaw")
        )
        with engine_patch, registry_patch, mock.patch.object(
            self.orch, "_tool_process_health", return_value=True
        ):
            result = self.orch.choose_reviewer(
                task_id="t",
                preferred_reviewer="hermes",
                allow_reviewer_fallback=True,
            )
        self.assertEqual(result["reviewer"], "openclaw")
        self.assertEqual(result["fallback_count"], 1)
        excluded = {entry[0] for entry in result["excluded"]}
        self.assertIn("hermes", excluded)

    def test_reviewer_bypass_refused_when_primary_healthy(self):
        """A healthy primary reviewer MUST NOT be silently bypassed."""
        statuses = {"hermes": mock.Mock(status="AVAILABLE_PRIMARY")}
        engine_patch, registry_patch = self._patch_choose_reviewer_dependencies(
            statuses, self._list_with("hermes")
        )
        with engine_patch, registry_patch, mock.patch.object(
            self.orch, "_tool_process_health", return_value=True
        ):
            result = self.orch.choose_reviewer(
                task_id="t",
                preferred_reviewer="hermes",
            )
        self.assertEqual(result["reviewer"], "hermes")
        self.assertEqual(result["fallback_count"], 0)


# ---------------------------------------------------------------------------
# Evidence Correction — bounded correction retry contract
# ---------------------------------------------------------------------------


class EvidenceCorrectionTests(unittest.TestCase):
    """Lock the bounded correction retry contract."""

    def setUp(self):
        self.orch = _import_orchestrator()

    def test_numeric_claim_consistent_with_evidence_passes(self):
        """When the Executor's numeric claim matches authoritative
        evidence, no correction marker is appended and the prompt
        stays clean.
        """
        goal = "Top-level entries: 49"
        node = {
            "task": goal,
            "acceptance": ["report the exact directory entry count"],
            "evidence_mode": "semantic",
            "role": "opencode",
        }
        text = self.orch._execution_text(
            goal=goal, node=node, repair_reason="",
            previous_result="", single_node=True,
        )
        self.assertNotIn("material_false_numeric_claim", text)
        self.assertNotIn("Trusted correction requirements", text)

    def test_numeric_claim_inconsistent_with_evidence_emits_correction(self):
        """When ``repair_reason`` carries a numeric-claim conflict
        marker, the prompt MUST carry (a) explicit trusted guidance,
        (b) the prior deliverable clipped to a bounded length, and
        (c) the same authoritative evidence.
        """
        goal = "Top-level entries: 49"
        node = {
            "task": goal,
            "acceptance": ["report the exact directory entry count"],
            "evidence_mode": "semantic",
            "role": "opencode",
        }
        prev = "executor reported 'Top-level entries: 69' but live 'ls -A | wc -l' returns 76"
        reason = (
            "Material false numeric claim: executor reported '69' "
            "but live 'ls -A | wc -l' returns 76 — "
            "material_false_numeric_claim"
        )
        text = self.orch._execution_text(
            goal=goal, node=node,
            repair_reason=reason,
            previous_result=prev,
            single_node=True,
        )
        # Marker guidance MUST appear in the prompt.
        self.assertIn("Trusted correction requirements", text)
        self.assertIn(
            "Replace that number with the exact authoritative value",
            text,
        )
        # Prior deliverable MUST appear (clipped to bounded length).
        self.assertIn("Previous reviewer-rejected deliverable", text)
        self.assertIn(prev[:500], text)
        # Bounded: prior result MUST NOT exceed 1800 chars.
        self.assertLess(len(text), 4096 + 1800)

    def test_correction_retry_does_not_inject_unbounded_evidence(self):
        """The previous_result segment is bounded by
        ``PREVIOUS_RESULT_LIMIT`` (1800 chars).  Larger prior
        results MUST be clipped, not copied verbatim.
        """
        goal = "audit"
        node = {
            "task": goal,
            "acceptance": ["audit"],
            "evidence_mode": "semantic",
            "role": "opencode",
        }
        blob = ("X" * 10240)
        reason = "material_false_numeric_claim"
        text = self.orch._execution_text(
            goal=goal, node=node,
            repair_reason=reason,
            previous_result=blob,
            single_node=True,
        )
        self.assertNotIn(blob, text)
        self.assertIn(blob[:1800], text)

    def test_correction_retry_target_includes_numeric_markers(self):
        """``_verification_retry_target`` MUST allow a same-executor
        retry when the reason is a numeric / evidence-contradiction
        marker.
        """
        node = {
            "actual_executor": "codex",
            "attempted_executors": [],
        }
        reason = "Material false numeric claim — material_false_numeric_claim"
        with mock.patch.object(
            self.orch, "_is_executor_available", return_value=True
        ), mock.patch.object(
            self.orch, "_executor_model_available", return_value=True
        ):
            target = self.orch._verification_retry_target(
                node, reason, actual_executor="codex"
            )
        self.assertEqual(target, "codex")

    def test_correction_retry_skips_when_executor_already_attempted(self):
        """Task-local exclusion MUST still hold."""
        node = {
            "actual_executor": "codex",
            "attempted_executors": ["codex", "opencode"],
        }
        reason = "claim_contradicts_evidence"
        with mock.patch.object(
            self.orch, "_is_executor_available", return_value=True
        ), mock.patch.object(
            self.orch, "_executor_model_available", return_value=True
        ):
            target = self.orch._verification_retry_target(
                node, reason, actual_executor="codex"
            )
        self.assertEqual(target, "")

    def test_retry_is_bounded_by_max_repairs(self):
        """MAX_REPAIRS == 2 — bounded retry contract."""
        self.assertEqual(self.orch.MAX_REPAIRS, 2)
        node = {
            "actual_executor": "codex",
            "attempted_executors": [],
            "verification_same_executor_repairs": 1,
        }
        with mock.patch.object(
            self.orch, "_is_executor_available", return_value=True
        ), mock.patch.object(
            self.orch, "_executor_model_available", return_value=True
        ):
            target = self.orch._verification_retry_target(
                node, "material_false_numeric_claim",
                actual_executor="codex",
            )
        self.assertEqual(target, "")

    def test_correction_retry_carries_authoritative_evidence(self):
        """The retry prompt MUST carry the AIOS-owned live evidence
        baseline so the Executor can correct against the same source
        the Reviewer used to reject.
        """
        node = {
            "task": "report the current ${AIOS_HOME} top-level entry count",
            "acceptance": ["report the exact directory entry count"],
            "evidence_mode": "independent-live",
            "role": "opencode",
        }
        reason = "evidence_contradiction"
        prev = "executor reported an outdated count"
        with mock.patch.object(
            self.orch, "_collect_independent_evidence",
            return_value=(
                {
                    "collected_at": "2026-08-10T00:00:00Z",
                    "authoritative": {
                        "files": [
                            {"path": "${AIOS_HOME}", "exists": True},
                        ],
                    },
                },
                None,
            ),
        ):
            text = self.orch._execution_text(
                goal=node["task"], node=node,
                repair_reason=reason,
                previous_result=prev,
                single_node=True,
            )
        self.assertIn("AIOS-owned live evidence", text)
        self.assertIn("${AIOS_HOME}", text)

    def test_repair_focus_lines_emit_numeric_claim_guidance(self):
        """``_repair_focus_lines`` MUST recognise the new markers."""
        guidance_numeric = self.orch._repair_focus_lines(
            "Material false numeric claim: 69 vs 76 — material_false_numeric_claim"
        )
        self.assertTrue(any(
            "Replace that number with the exact authoritative value"
            in line for line in guidance_numeric
        ))
        guidance_evidence = self.orch._repair_focus_lines(
            "claim_contradicts_evidence"
        )
        self.assertTrue(any(
            "Replace the claim with the trusted baseline value"
            in line for line in guidance_evidence
        ))
        guidance_other = self.orch._repair_focus_lines(
            "truncated"
        )
        self.assertTrue(any(
            "truncated" in line.lower() for line in guidance_other
        ))
        self.assertEqual(
            self.orch._repair_focus_lines("all good"),
            [],
        )


if __name__ == "__main__":
    unittest.main()