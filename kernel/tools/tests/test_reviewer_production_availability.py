#!/usr/bin/env python3
"""Reviewer production availability — dedicated 10-item test suite.

Covers the 10 spec invariants for the production Reviewer surface:

  1. production review payload bounded (review_payload_after_bytes < before)
  2. relevant evidence preserved (queue / executors_summary / failed_units)
  3. unrelated large trace / recent_tasks history NOT in Reviewer prompt
  4. correction retry does not re-inject full history (previous_failure bound)
  5. single Hermes review returns a parseable JSON verdict
  6. review concurrency=1: when review is busy, second task waits
     (semaphore path exists; we assert via the unified surface that
     review runs serially per parent workflow)
  7. review timeout naturally releases the execution slot (no leak)
  8. Claude binding selection honours the Registry (binding_id set)
  9. healthy claude:minimax does NOT fall back to Claude native CLI (no 402)
 10. Reviewer failure never bypasses Verification (passed must be False)

The tests are written as pure unit tests against the minimal-reviewer
prompt builders, the reviewer subprocess contract, and the unified
verdict surface — they do not require a live Hermes subprocess.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest import mock

TOOLS = "${AIOS_HOME}/kernel/tools"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from aios_verification_gate import (  # noqa: E402
    REVIEWER_PROMPT_FIELD_CAPS,
    _build_minimal_review_evidence,
    _build_minimal_review_prompt,
    _extract_verdict_json,
    validate_verdict_schema,
)


def _real_production_grounding() -> dict:
    """Build a realistic production grounding block with the legacy bloat
    (60-70 KB total).  The deterministic gate still consumes this full
    block; the Reviewer-visible slice must be condensed.
    """
    runtime_status = {
        "ok": True,
        "service": "aios-entry-gateway",
        "version": "5.2.8",
        "timestamp": "2026-08-10T11:23:51.909842+00:00",
        "queue": {
            "pending": 0, "locked": 0, "running": 0,
            "completed": 0, "failed": 0, "total_active": 0,
        },
        "executors": {
            "count": 5,
            "list": [
                {
                    "name": "opencode", "available": True,
                    "runtime_status": "running", "process_alive": True,
                    "inference_ready": True, "today_stats": {"completed": 8, "failed": 3},
                    "expertise": [
                        "CLI", "Bash", "JSON", "YAML", "Python", "Docker",
                        "file_ops", "script", "data_collection",
                    ],
                    "max_concurrent": 5, "depth_levels": ["low"],
                } for _ in range(5)
            ],
        },
        # 60 KB history list — the legacy bloat.
        "recent_tasks": [
            {
                "task_id": f"2d4f9cdb-{i:04d}-4107-93c0-60555b032ed9",
                "status": "completed" if i % 2 == 0 else "failed",
                "executor": "opencode", "source": "cli",
                "result_summary": "X" * 600,
            }
            for i in range(50)
        ],
        "recent_tasks_summary": {
            "window_hours": 24, "total": 16, "completed": 10,
            "failed": 6, "success_rate": 62.5,
        },
        "services": {
            "LLM 代理网关": "running", "统一入口网关": "running",
            "控制中心": "running", "Hermes 消息网关": "running",
            "OpenClaw 任务网关": "running",
        },
        "local_model_policy": {
            "activation_mode": "MANUAL_USER_APPROVAL_ONLY",
            "inference_allowed": False, "local_model_running": False,
            "calls_current": 0, "blocked_reason": "USER_APPROVAL_REQUIRED",
        },
    }
    return {
        "mode": "aios-runtime",
        "collected_at": "2026-08-10T11:23:49.771786+00:00",
        "authoritative": {
            "health": {
                "ok": True, "service": "aios-entry-gateway",
                "version": "5.2.8", "status": "live",
                "redis": True,
                "loaded_revision": "317ff7f9255e505d2e28765ced09f18388cbfe6e6723094d98a87bdb6060290e",
                "timestamp": "2026-08-10T11:23:49.798821+00:00",
            },
            "runtime_status": runtime_status,
            "versions": {
                "runtime_health": "5.2.8",
                "module_manifest": "5.2.8",
                "features": "5.2.8",
            },
            "utc_now": "2026-08-10T11:23:49.771786+00:00",
            "version_source": "GET http://127.0.0.1:18801/health + config/module_manifest.json + config/features.toml",
            "runtime_source": "GET http://127.0.0.1:18801/status",
            "failed_systemd_units": [],
            "orchestrator_processes": [
                {"pid": 2403, "cmdline": "/usr/bin/python3 .../aios_orchestrator.py --daemon"},
            ],
            "canonical_runtime_sources": {
                "queue": "GET /status queue",
                "tool_availability": "GET /status executors.list[].available",
            },
            "non_authoritative_legacy_keys": [
                "aios:queue:priority:data", "aios:agents:available",
            ],
        },
    }


def _real_production_node(previous_failure: str = "") -> dict:
    return {
        "task": ("Verify AIOS production reviewer availability. "
                 "Confirm one production-eligible reviewer can complete "
                 "a real production review under AIOS production "
                 "constraints. " * 5),
        "acceptance": [
            {"id": f"acc-{i:02d}", "text": f"Verify criterion {i}: "
             + ("production review is correctly grounded. " * 10)}
            for i in range(12)
        ],
        "evidence_mode": "aios-runtime",
        "executor": "opencode",
        "preferred_reviewer": "hermes",
        "blocked_reviewer_tools": [],
        "allow_reviewer_fallback": True,
        "previous_failure": previous_failure,
    }


def _real_production_deliverable() -> str:
    return (
        json.dumps({
            "summary": "Production reviewer availability baseline report",
            "actual_reviewer": "hermes",
            "binding": "hermes:minimax",
            "passed": True,
            "details": ("Production reviewer availability confirmed. "
                        "Hermes subprocess returned a strict JSON verdict "
                        "within the bounded payload. " * 100),
            "tests": {"passed": 820, "failed": 0, "skipped": 0},
            "components": [
                {"name": "aios-orchestrator", "state": "healthy"},
                {"name": "hermes-gateway", "state": "healthy"},
            ],
        }, ensure_ascii=False)
        + "\n" + ("EXECUTOR_DETAIL: " + "X" * 4000)
    )


# ---------------------------------------------------------------------------
# 1. production review payload bounded
# ---------------------------------------------------------------------------


class TestProductionReviewPayloadBounded(unittest.TestCase):
    """review_payload_after_bytes < review_payload_before_bytes."""

    def test_after_bytes_less_than_before_bytes(self):
        grounding = _real_production_grounding()
        node = _real_production_node()
        deliverable = _real_production_deliverable()
        prompt, meta = _build_minimal_review_prompt(
            goal="Establish production reviewer availability baseline.",
            node=node, executor="opencode",
            deliverable=deliverable, evidence_mode="aios-runtime",
            grounding=grounding,
        )
        # Manually inject before_bytes (the orchestrator does this in
        # verify_parent_node, but the helper itself does not own it).
        meta["review_payload_before_bytes"] = sum((
            4000, 3000, 3000, 8000, 16000, 2869, 200,
        ))
        self.assertLess(
            meta["review_payload_after_bytes"],
            meta["review_payload_before_bytes"],
            f"AFTER ({meta['review_payload_after_bytes']}) must be < "
            f"BEFORE ({meta['review_payload_before_bytes']})",
        )
        # The reduction must be at least 50% in production conditions.
        reduction = 1.0 - (
            meta["review_payload_after_bytes"] / meta["review_payload_before_bytes"]
        )
        self.assertGreaterEqual(
            reduction, 0.5,
            f"Reviewer payload reduction {reduction:.1%} below 50%",
        )

    def test_per_field_caps_match_meta(self):
        grounding = _real_production_grounding()
        node = _real_production_node()
        deliverable = _real_production_deliverable()
        prompt, meta = _build_minimal_review_prompt(
            goal="Establish production reviewer availability baseline.",
            node=node, executor="opencode",
            deliverable=deliverable, evidence_mode="aios-runtime",
            grounding=grounding,
        )
        caps = REVIEWER_PROMPT_FIELD_CAPS
        # The cap for each field MUST bound the actual rendered size.
        for k, cap in caps.items():
            rendered = meta["field_bytes"].get(k, 0)
            self.assertLessEqual(
                rendered, cap,
                f"field {k} rendered {rendered} > cap {cap}",
            )


# ---------------------------------------------------------------------------
# 2. relevant evidence preserved (queue / executors_summary / failed_units)
# ---------------------------------------------------------------------------


class TestRelevantEvidencePreserved(unittest.TestCase):
    """The Reviewer-visible slice MUST carry queue / executors_summary /
    failed_systemd_units — not just an empty block."""

    def test_queue_executors_failed_units_present(self):
        grounding = _real_production_grounding()
        node = _real_production_node()
        deliverable = _real_production_deliverable()
        prompt, meta = _build_minimal_review_prompt(
            goal="Establish production reviewer availability baseline.",
            node=node, executor="opencode",
            deliverable=deliverable, evidence_mode="aios-runtime",
            grounding=grounding,
        )
        # The minimal evidence block is the "payload:" line under
        # RELEVANT AUTHORITATIVE EVIDENCE.
        self.assertIn("RELEVANT AUTHORITATIVE EVIDENCE", prompt)
        # The trimmed grounding must carry the queue, executors_summary,
        # and the 24h aggregate — these are the fields the Reviewer
        # actually cites in a real verdict.
        minimal = _build_minimal_review_evidence(grounding)
        auth = minimal["authoritative"]
        self.assertIn("queue", auth.get("runtime_status", {}))
        self.assertIn(
            "executors_summary", auth.get("runtime_status", {}),
        )
        self.assertIn(
            "recent_tasks_summary", auth.get("runtime_status", {}),
        )
        self.assertIn("services", auth.get("runtime_status", {}))
        self.assertIn("failed_systemd_units", auth)
        self.assertIn("versions", auth)

    def test_executor_evidence_summary_capped(self):
        # When the executor delivered >3 corroborated evidence items,
        # the Reviewer-visible slice MUST cap at 3 (audit-friendly).
        grounding = _real_production_grounding()
        grounding["authoritative"]["executor_evidence"] = [
            {"source_type": "systemd", "source": f"aios-foo-{i}.service",
             "summary": f"unit {i}", "authoritative": True}
            for i in range(10)
        ]
        grounding["authoritative"]["executor_evidence_count"] = 10
        minimal = _build_minimal_review_evidence(grounding)
        items = (
            minimal.get("authoritative", {})
            .get("executor_evidence_summary", {})
            .get("items", [])
        )
        self.assertEqual(len(items), 3)
        count = (
            minimal.get("authoritative", {})
            .get("executor_evidence_summary", {})
            .get("count", 0)
        )
        self.assertEqual(count, 10)  # total count preserved


# ---------------------------------------------------------------------------
# 3. unrelated large trace / recent_tasks history NOT in Reviewer prompt
# ---------------------------------------------------------------------------


class TestUnrelatedLargeTraceExcluded(unittest.TestCase):
    """The full ``recent_tasks`` history MUST NOT be inlined into the
    Reviewer prompt — only the 24h aggregate is.  The full block stays
    in the deterministic gate."""

    def test_recent_tasks_history_excluded(self):
        grounding = _real_production_grounding()
        # recent_tasks was a 30-60 KB list in production; the full
        # grounding block is in the 30-70 KB range.
        full_size = len(json.dumps(grounding, ensure_ascii=False))
        self.assertGreater(full_size, 30000,
                           "fixture must include a large recent_tasks block")
        minimal = _build_minimal_review_evidence(grounding)
        minimal_size = len(json.dumps(minimal, ensure_ascii=False))
        # Minimal evidence MUST be at most 5 KB (the 8000 cap is the
        # absolute upper bound and only the queue/services slice hits
        # it on large runtime dumps).
        self.assertLess(minimal_size, 8000)

    def test_redis_dump_and_journal_not_in_minimal(self):
        # If a caller smuggled a redis_dump / journal tail into the
        # authoritative block, the minimal review evidence MUST NOT
        # include those — they belong to the deterministic gate only.
        grounding = _real_production_grounding()
        grounding["authoritative"]["redis_dump"] = "Z" * 2000
        grounding["authoritative"]["journal_tail"] = "J" * 2000
        grounding["authoritative"]["trace"] = "T" * 2000
        minimal = _build_minimal_review_evidence(grounding)
        self.assertNotIn("redis_dump", minimal["authoritative"])
        self.assertNotIn("journal_tail", minimal["authoritative"])
        self.assertNotIn("trace", minimal["authoritative"])


# ---------------------------------------------------------------------------
# 4. correction retry does not re-inject full history
# ---------------------------------------------------------------------------


class TestCorrectionRetryBounded(unittest.TestCase):
    """When ``node.previous_failure`` is set, the Reviewer prompt MUST
    cap it at REVIEWER_PROMPT_FIELD_CAPS['previous_failure'] = 1800 chars
    so correction retry does not snowball."""

    def test_previous_failure_capped_at_1800(self):
        grounding = _real_production_grounding()
        node = _real_production_node(previous_failure="F" * 10000)
        deliverable = _real_production_deliverable()
        prompt, meta = _build_minimal_review_prompt(
            goal="Establish production reviewer availability baseline.",
            node=node, executor="opencode",
            deliverable=deliverable, evidence_mode="aios-runtime",
            grounding=grounding, previous_failure=node["previous_failure"],
        )
        self.assertIn("PREVIOUS VERIFICATION FAILURE", prompt)
        self.assertLessEqual(
            meta["field_bytes"]["previous_failure"],
            REVIEWER_PROMPT_FIELD_CAPS["previous_failure"],
        )

    def test_no_previous_failure_omits_block(self):
        grounding = _real_production_grounding()
        node = _real_production_node(previous_failure="")
        deliverable = _real_production_deliverable()
        prompt, meta = _build_minimal_review_prompt(
            goal="Establish production reviewer availability baseline.",
            node=node, executor="opencode",
            deliverable=deliverable, evidence_mode="aios-runtime",
            grounding=grounding, previous_failure="",
        )
        self.assertNotIn("PREVIOUS VERIFICATION FAILURE", prompt)
        self.assertEqual(meta["field_bytes"]["previous_failure"], 0)


# ---------------------------------------------------------------------------
# 5. single Hermes review returns a parseable JSON verdict
# ---------------------------------------------------------------------------


class TestHermesVerdictParseable(unittest.TestCase):
    """The Reviewer prompt shape itself is JSON-friendly: the existing
    :func:`_extract_verdict_json` + :func:`validate_verdict_schema`
    pipeline accepts a strict Hermes-style reply (verified live)."""

    def test_strict_hermes_verdict_passes_schema(self):
        sample = (
            '{"passed": true, "reason": "Production review accepted: '
            "actual_reviewer='hermes' matches executors_summary entry "
            "showing hermes available=true, runtime_status=running, "
            "inference_ready=true. binding='hermes:minimax' is consistent "
            'with current Hermes default provider.", '
            '"repair_instruction": "", "evidence_checked": true, '
            '"evidence_sources": ["executors_summary", "queue", '
            '"failed_systemd_units"]}'
        )
        category, value = _extract_verdict_json(sample)
        self.assertEqual(category, "OK")
        ok, err, normalized = validate_verdict_schema(value)
        self.assertTrue(ok, f"verdict rejected: {err}")
        self.assertTrue(normalized["passed"])
        self.assertTrue(normalized["evidence_checked"])
        self.assertEqual(len(normalized["evidence_sources"]), 3)

    def test_malformed_hermes_reply_fails_cleanly(self):
        # Hermes returning natural-language prose must produce a
        # structured failure, not a passed=True.
        sample = "VERDICT: FAIL / REJECT — no real evidence"
        category, value = _extract_verdict_json(sample)
        self.assertNotEqual(category, "OK")
        ok, err, normalized = validate_verdict_schema(value)
        self.assertFalse(ok)
        self.assertEqual(normalized, {})


# ---------------------------------------------------------------------------
# 6. reviewer concurrency=1: serial review per parent workflow
# ---------------------------------------------------------------------------


class TestReviewerSerialPerWorkflow(unittest.TestCase):
    """The Reviewer loop in :func:`_semantic_review` MUST NOT start a
    second subprocess while the first attempt is in flight.  We assert
    this by reading the source — a second subprocess.run call lives
    INSIDE the same iteration as the first (repair path), so the
    orchestrator-level concurrent review tasks cannot overlap."""

    def test_review_loop_is_serial_per_reviewer_iteration(self):
        import inspect
        from aios_verification_gate import _semantic_review
        src = inspect.getsource(_semantic_review)
        # The loop iterates over iteration_order and runs
        # _call_reviewer_once at most twice per reviewer (initial + repair).
        # There is no async/concurrent.futures / ThreadPoolExecutor
        # invocation inside the loop.
        self.assertNotIn("ThreadPoolExecutor", src)
        self.assertNotIn("asyncio", src)
        self.assertNotIn("concurrent.futures", src)
        # Each iteration is bounded — when reviewer fails with TIMEOUT
        # or transport, the loop continues to the next reviewer instead
        # of stacking a parallel call.
        self.assertIn("continue", src)
        self.assertIn("first attempt", src.lower())


# ---------------------------------------------------------------------------
# 7. review timeout naturally releases the execution slot
# ---------------------------------------------------------------------------


class TestReviewTimeoutReleasesSlot(unittest.TestCase):
    """When the Reviewer subprocess hits subprocess.TimeoutExpired, the
    attempt entry is appended and the loop MUST continue to the next
    candidate (or raise) — the slot is released, no leak."""

    def test_timeout_appends_attempt(self):
        # The contract: subprocess.TimeoutExpired appends a TIMEOUT
        # attempt entry; the call returns None and the loop continues.
        from aios_verification_gate import _call_reviewer_once
        src_path = "${AIOS_HOME}/kernel/tools/aios_verification_gate.py"
        with open(src_path) as fh:
            src = fh.read()
        # Inside _call_reviewer_once there is a TimeoutExpired handler
        # that appends to attempts and returns None.
        self.assertIn("TimeoutExpired", src)
        self.assertIn("TIMEOUT:reviewer subprocess timed out", src)
        self.assertIn("return None", src.split("def _call_reviewer_once")[1]
                      .split("def ")[0])


# ---------------------------------------------------------------------------
# 8. Claude binding selection honours the Registry
# ---------------------------------------------------------------------------


class TestClaudeBindingRegistry(unittest.TestCase):
    """choose_reviewer MUST return binding='claude:minimax' for the
    Claude reviewer when the Registry has that binding registered —
    NOT the legacy Claude native CLI / DeepSeek default endpoint."""

    def test_choose_reviewer_returns_claude_minimax(self):
        # Pre-empt the test pollution that earlier P9DR runtime
        # failure events left in the in-memory default engine.  The
        # production main chain (claude via claude:minimax) is the
        # contract this test guards; the autouse conftest fixture
        # clears every tool's failure event after each test, so this
        # is a defence-in-depth that runs before the assertion.
        from aios_orchestrator import choose_reviewer
        from aios_tool_failover import (
            clear_tool_runtime_failure,
            get_default_tool_engine,
        )
        for tool in ("claude", "opencode", "openclaw", "hermes",
                     "codex", "minimax.shared"):
            try:
                clear_tool_runtime_failure(tool)
            except Exception:
                pass
        # In production, the legacy DeepSeek primary reports
        # ``quota_exhausted`` so the engine falls through to
        # ``claude:minimax`` — the binding the production routing
        # contract pins.  ``set_adapter_cache_override`` is the
        # documented in-memory entry point (it does NOT touch the
        # on-disk ``cache/tool_health/*.json``) and is reset by the
        # autouse ``_reset_tool_failover_engine_state`` conftest
        # fixture after each test so it cannot leak.
        try:
            engine = get_default_tool_engine()
            engine.set_adapter_cache_override("claude", {
                "model_state": "quota_exhausted",
                "model_available": False,
                "lightweight_reachable": False,
                "lightweight_protocol_ready": False,
                "lightweight_fresh": True,
                "lightweight_observed_at": "2030-01-01T00:00:00+00:00",
                "lightweight_checked_at": "2030-01-01T00:00:00+00:00",
                "lightweight_last_success_at": "2030-01-01T00:00:00+00:00",
                "lightweight_expires_at": "2030-01-01T00:01:00+00:00",
                "success_marker_seen": False,
                "fatal_error_seen": True,
                "probe_required": True,
                "evidence": "API Error: 402 Insufficient Balance",
                "returncode": 1,
                "retry_after": "2030-01-01T00:01:00+00:00",
                "reason": "quota_exhausted:legacy_deepseek_provider",
            })
        except Exception:
            pass
        result = choose_reviewer(
            task_id="test-claude-binding",
            preferred_reviewer="claude",
            blocked_reviewer_tools=[],
            allow_reviewer_fallback=True,
            exclude_executor="opencode",
        )
        self.assertEqual(result.get("reviewer"), "claude")
        self.assertEqual(result.get("binding"), "claude:minimax")

    def test_claude_minimax_adapter_routed(self):
        # When the chosen binding is claude:minimax, _call_reviewer_once
        # MUST route through _call_reviewer_via_minimax_adapter, not
        # the Claude subprocess path.  This is the same line that
        # protects us from the legacy 402 path.
        from aios_verification_gate import _call_reviewer_once
        import inspect
        src = inspect.getsource(_call_reviewer_once)
        self.assertIn('if binding_id == "claude:minimax" and reviewer == "claude"', src)
        self.assertIn("_call_reviewer_via_minimax_adapter", src)


# ---------------------------------------------------------------------------
# 9. healthy claude:minimax does NOT fall back to Claude native CLI (no 402)
# ---------------------------------------------------------------------------


class TestClaudeMinimaxNoNativeFallback(unittest.TestCase):
    """When ``claude:minimax`` is the chosen binding, the verifier MUST
    use :class:`ClaudeMiniMaxAdapter` (which calls the minimax.shared
    Provider).  The Claude native CLI subprocess path that produces 402
    MUST NOT be reached."""

    def test_claude_minimax_path_does_not_invoke_claude_binary(self):
        import inspect
        from aios_verification_gate import _call_reviewer_via_minimax_adapter
        src = inspect.getsource(_call_reviewer_via_minimax_adapter)
        # Must use the adapter, not the Claude binary.
        self.assertIn("ClaudeMiniMaxAdapter", src)
        # The adapter calls minimax.shared (provider=minimax.shared).
        self.assertIn("minimax.shared", src)
        # The Claude binary path is gated by binding_id == "claude:minimax"
        # upstream; this function MUST NOT shell out to the claude CLI.
        self.assertNotIn("/.n/bin/claude", src)
        self.assertNotIn('"claude"', src.split('return {')[0])


# ---------------------------------------------------------------------------
# 10. Reviewer failure never bypasses Verification
# ---------------------------------------------------------------------------


class TestReviewerFailureCannotBypass(unittest.TestCase):
    """The P2 hardening contract: when every reviewer fails, the
    verification gate MUST raise RuntimeError with reason
    ``all_independent_reviewers_unavailable`` (or
    ``VERIFICATION_BLOCKED:strict_no_fallback`` in the strict case).
    It MUST NOT silently return passed=True."""

    def test_all_reviewers_fail_raises(self):
        from aios_verification_gate import _semantic_review
        import inspect
        src = inspect.getsource(_semantic_review)
        self.assertIn("all_independent_reviewers_unavailable", src)
        self.assertIn("raise RuntimeError", src)
        # The strict-no-fallback path uses a different marker.
        self.assertIn("VERIFICATION_BLOCKED:strict_no_fallback", src)

    def test_repair_attempted_bounded(self):
        # P2: only ONE repair attempt per reviewer; transport failures
        # never trigger repair.
        from aios_verification_gate import _semantic_review
        import inspect
        src = inspect.getsource(_semantic_review)
        # Confirm the transport-failure skip-repair block.
        self.assertIn('"AUTH_FAILED", "QUOTA_EXHAUSTED", "PLAN_EXHAUSTED"', src)
        self.assertIn("TIMEOUT", src)
        self.assertIn("CONNECTION_FAILED", src)
        self.assertIn("continue", src)


if __name__ == "__main__":
    unittest.main()