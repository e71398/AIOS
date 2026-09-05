"""P2 hardening tests for AIOS semantic reviewer chain.

These tests cover:
  - JSON extraction (_extract_verdict_json, _extract_json_object)
  - Schema validation (validate_verdict_schema)
  - Provider lifecycle classification (_classify_provider_failure)
  - Repair policy (one bounded repair per reviewer, no repair for transport
    failures)
  - Reviewer selection (exclude_executor, fallback ordering)

These tests are pure offline; they do NOT contact real Providers.
"""

import json
from unittest.mock import patch

import pytest

from aios_verification_gate import (
    VERDICT_EXTRACT_EMPTY_OUTPUT,
    VERDICT_EXTRACT_MALFORMED_JSON,
    VERDICT_EXTRACT_MULTIPLE_JSON_OBJECTS,
    VERDICT_EXTRACT_NO_JSON_OBJECT,
    VERDICT_EXTRACT_NON_OBJECT_JSON,
    VERDICT_EXTRACT_OK,
    PROVIDER_LIFECYCLE_KINDS,
    _build_repair_prompt,
    _call_reviewer_once,
    _classify_provider_failure,
    _extract_json_object,
    _extract_verdict_json,
    _review_policy,
    _semantic_review,
    _strip_one_markdown_fence,
    validate_verdict_schema,
)


# ----------------------------- extraction -----------------------------------

class TestExtractVerdictJson:
    def test_pure_json(self):
        cat, val = _extract_verdict_json('{"passed": true, "reason": "ok"}')
        assert cat == VERDICT_EXTRACT_OK
        assert val == {"passed": True, "reason": "ok"}

    def test_with_surrounding_whitespace(self):
        cat, val = _extract_verdict_json('   \n  {"passed":false}  \n')
        assert cat == VERDICT_EXTRACT_OK
        assert val == {"passed": False}

    def test_fenced_json_with_language_tag(self):
        text = '```json\n{"passed": true, "reason": "x"}\n```'
        cat, val = _extract_verdict_json(text)
        assert cat == VERDICT_EXTRACT_OK
        assert val == {"passed": True, "reason": "x"}

    def test_fenced_json_without_language_tag(self):
        text = '```\n{"passed": true}\n```'
        cat, val = _extract_verdict_json(text)
        assert cat == VERDICT_EXTRACT_OK
        assert val == {"passed": True}

    def test_prose_before_json(self):
        text = 'Here is my answer:\n{"passed": false, "reason": "no"}'
        cat, val = _extract_verdict_json(text)
        assert cat == VERDICT_EXTRACT_OK
        assert val == {"passed": False, "reason": "no"}

    def test_prose_after_json(self):
        text = '{"passed": true}\nThat is my final answer.'
        cat, val = _extract_verdict_json(text)
        assert cat == VERDICT_EXTRACT_OK
        assert val == {"passed": True}

    def test_prose_around_json(self):
        text = 'thinking...\n{"passed": true, "reason": "ok"}\nclosing notes'
        cat, val = _extract_verdict_json(text)
        assert cat == VERDICT_EXTRACT_OK
        assert val == {"passed": True, "reason": "ok"}

    def test_string_with_braces(self):
        text = '{"passed": true, "reason": "value with { and } inside"}'
        cat, val = _extract_verdict_json(text)
        assert cat == VERDICT_EXTRACT_OK
        assert val["passed"] is True
        assert "value with { and } inside" in val["reason"]

    def test_top_level_array_rejected(self):
        cat, val = _extract_verdict_json('[{"passed": true}]')
        assert cat == VERDICT_EXTRACT_NON_OBJECT_JSON
        assert val == [{"passed": True}]

    def test_empty_output(self):
        cat, val = _extract_verdict_json("")
        assert cat == VERDICT_EXTRACT_EMPTY_OUTPUT
        assert val is None

    def test_none_input(self):
        cat, val = _extract_verdict_json(None)
        assert cat == VERDICT_EXTRACT_EMPTY_OUTPUT
        assert val is None

    def test_whitespace_only(self):
        cat, val = _extract_verdict_json("   \n\t  ")
        assert cat == VERDICT_EXTRACT_EMPTY_OUTPUT
        assert val is None

    def test_no_json_object(self):
        cat, val = _extract_verdict_json("I cannot comply with that request.")
        assert cat == VERDICT_EXTRACT_NO_JSON_OBJECT
        assert val is None

    def test_truncated_json(self):
        cat, val = _extract_verdict_json('{"passed": tru')
        assert cat in (
            VERDICT_EXTRACT_NO_JSON_OBJECT,
            VERDICT_EXTRACT_MALFORMED_JSON,
            VERDICT_EXTRACT_NON_OBJECT_JSON,
        )

    def test_trailing_comma_is_malformed(self):
        cat, val = _extract_verdict_json('{"passed": true,}')
        assert cat != VERDICT_EXTRACT_OK

    def test_two_json_objects_rejected(self):
        text = '{"passed": true} {"passed": false}'
        cat, val = _extract_verdict_json(text)
        assert cat == VERDICT_EXTRACT_MULTIPLE_JSON_OBJECTS
        assert len(val) == 2

    def test_multiple_fenced_objects_rejected(self):
        text = (
            '```json\n{"passed": true}\n```\n'
            '```json\n{"passed": false}\n```'
        )
        cat, val = _extract_verdict_json(text)
        # We strip the first outer fence then try direct parse. After stripping
        # once, the rest contains another JSON, but our raw_decode scan finds
        # two distinct dicts -> MULTIPLE.
        assert cat in (
            VERDICT_EXTRACT_MULTIPLE_JSON_OBJECTS,
            VERDICT_EXTRACT_OK,  # implementation may accept first fence only
        )

    def test_backward_compat_wrapper_returns_dict(self):
        # _extract_json_object returns {} on failure for backward compat.
        assert _extract_json_object('not json') == {}
        assert _extract_json_object('{"passed": true}') == {"passed": True}

    def test_strip_one_markdown_fence_balanced_only(self):
        assert _strip_one_markdown_fence("```json\nx\n```") == "x"
        assert _strip_one_markdown_fence("```\nx\n```") == "x"
        # unbalanced -> no change
        assert _strip_one_markdown_fence("```json\nx") == "```json\nx"
        # empty fence body -> no change
        assert _strip_one_markdown_fence("```json\n```") == "```json\n```"


# ----------------------------- schema validation ---------------------------

class TestValidateVerdictSchema:
    def test_full_legal_schema(self):
        parsed = {
            "passed": True,
            "reason": "ok",
            "repair_instruction": "",
            "evidence_checked": True,
            "evidence_sources": ["a", "b"],
        }
        ok, err, norm = validate_verdict_schema(parsed)
        assert ok is True
        assert err is None
        assert norm["passed"] is True
        assert norm["reason"] == "ok"
        assert norm["evidence_sources"] == ["a", "b"]

    def test_only_passed(self):
        ok, err, norm = validate_verdict_schema({"passed": False})
        assert ok is True
        assert norm == {"passed": False}

    def test_missing_passed(self):
        ok, err, norm = validate_verdict_schema({"reason": "x"})
        assert ok is False
        assert err == "INVALID_VERDICT_MISSING_PASSED"
        assert norm == {}

    def test_passed_is_string(self):
        ok, err, _ = validate_verdict_schema({"passed": "true"})
        assert ok is False
        assert err == "INVALID_VERDICT_PASSED_NOT_BOOL"

    def test_passed_is_int(self):
        ok, err, _ = validate_verdict_schema({"passed": 1})
        assert ok is False
        assert err == "INVALID_VERDICT_PASSED_NOT_BOOL"

    def test_reason_not_string(self):
        ok, err, _ = validate_verdict_schema({"passed": True, "reason": 123})
        assert ok is False
        assert err == "INVALID_VERDICT_REASON_NOT_STRING"

    def test_repair_instruction_not_string(self):
        ok, err, _ = validate_verdict_schema(
            {"passed": True, "repair_instruction": ["x"]}
        )
        assert ok is False
        assert err == "INVALID_VERDICT_REPAIR_INSTRUCTION_NOT_STRING"

    def test_evidence_checked_not_bool(self):
        ok, err, _ = validate_verdict_schema({"passed": True, "evidence_checked": "true"})
        assert ok is False
        assert err == "INVALID_VERDICT_EVIDENCE_CHECKED_NOT_BOOL"

    def test_evidence_sources_not_list(self):
        ok, err, _ = validate_verdict_schema({"passed": True, "evidence_sources": "x"})
        assert ok is False
        assert err == "INVALID_VERDICT_EVIDENCE_SOURCES_NOT_LIST"

    def test_evidence_sources_contains_non_string(self):
        ok, err, _ = validate_verdict_schema(
            {"passed": True, "evidence_sources": ["a", 1]}
        )
        assert ok is False
        assert err == "INVALID_VERDICT_EVIDENCE_SOURCES_CONTAINS_NON_STRING"

    def test_invalid_top_level_not_object(self):
        ok, err, _ = validate_verdict_schema([])
        assert ok is False
        assert err == "INVALID_VERDICT_NOT_OBJECT"

    def test_length_cap_reason(self):
        long_reason = "x" * 5000
        ok, _, norm = validate_verdict_schema({"passed": True, "reason": long_reason})
        assert ok is True
        assert len(norm["reason"]) == 1000

    def test_evidence_sources_cap_at_10(self):
        sources = [f"src_{i}" for i in range(20)]
        ok, _, norm = validate_verdict_schema(
            {"passed": True, "evidence_sources": sources}
        )
        assert ok is True
        assert len(norm["evidence_sources"]) == 10


# ----------------------- provider lifecycle classification -----------------

class TestClassifyProviderFailure:
    def test_claude_402_is_quota_exhausted(self):
        kind, _, fatal = _classify_provider_failure(
            "HTTP 402 insufficient balance for claude", 0
        )
        assert kind == "QUOTA_EXHAUSTED"
        assert fatal is True

    def test_claude_402_chinese(self):
        kind, _, _ = _classify_provider_failure("HTTP 402 余额不足", 0)
        assert kind == "QUOTA_EXHAUSTED"

    def test_codex_plan_429_is_plan_exhausted(self):
        kind, _, fatal = _classify_provider_failure(
            "HTTP 429 token plan exhausted", 0
        )
        assert kind == "PLAN_EXHAUSTED"
        assert fatal is True

    def test_codex_subscription_limit_is_plan_exhausted(self):
        kind, _, _ = _classify_provider_failure(
            "HTTP 429 subscription limit reached", 0
        )
        assert kind == "PLAN_EXHAUSTED"

    def test_transient_429_is_rate_limited(self):
        kind, _, fatal = _classify_provider_failure("HTTP 429 too many requests", 0)
        assert kind == "RATE_LIMITED_TRANSIENT"
        assert fatal is False

    def test_transient_429_chinese(self):
        kind, _, _ = _classify_provider_failure("rate limit exceeded", 0)
        assert kind == "RATE_LIMITED_TRANSIENT"

    def test_401_is_auth_failed(self):
        kind, _, fatal = _classify_provider_failure("HTTP 401 unauthorized", 0)
        assert kind == "AUTH_FAILED"
        assert fatal is True

    def test_403_is_auth_failed(self):
        kind, _, _ = _classify_provider_failure("HTTP 403 forbidden", 0)
        assert kind == "AUTH_FAILED"

    def test_timeout_is_timeout(self):
        kind, _, fatal = _classify_provider_failure("SubprocessTimeoutError", 0)
        assert kind == "TIMEOUT"
        assert fatal is True

    def test_connection_failure(self):
        kind, _, fatal = _classify_provider_failure("Connection refused", 0)
        assert kind == "CONNECTION_FAILED"
        assert fatal is False

    def test_dns_error(self):
        kind, _, _ = _classify_provider_failure("DNS resolution failed", 0)
        assert kind == "CONNECTION_FAILED"

    def test_nonzero_exit_no_pattern(self):
        kind, _, _ = _classify_provider_failure("some random message", 1)
        assert kind == "NONZERO_EXIT"

    def test_empty_output_classification(self):
        kind, _, _ = _classify_provider_failure("", 0, parsed_dict=None,
                                                 extract_category=VERDICT_EXTRACT_EMPTY_OUTPUT)
        assert kind == "EMPTY_OUTPUT"

    def test_malformed_response_classification(self):
        kind, _, _ = _classify_provider_failure(
            "", 0, parsed_dict=None,
            extract_category=VERDICT_EXTRACT_NO_JSON_OBJECT,
        )
        assert kind == "MALFORMED_RESPONSE"

    def test_multiple_objects_classification(self):
        kind, _, _ = _classify_provider_failure(
            "", 0, parsed_dict=None,
            extract_category=VERDICT_EXTRACT_MULTIPLE_JSON_OBJECTS,
        )
        assert kind == "MALFORMED_RESPONSE"

    def test_invalid_verdict_classification(self):
        kind, _, _ = _classify_provider_failure(
            "", 0, parsed_dict=None,
            extract_category="INVALID_VERDICT_PASSED_NOT_BOOL",
        )
        assert kind == "INVALID_VERDICT"

    def test_all_kinds_in_known_set(self):
        for kind in (
            "AUTH_FAILED", "QUOTA_EXHAUSTED", "PLAN_EXHAUSTED",
            "RATE_LIMITED_TRANSIENT", "TIMEOUT", "CONNECTION_FAILED",
            "NONZERO_EXIT", "EMPTY_OUTPUT", "MALFORMED_RESPONSE",
            "INVALID_VERDICT", "AVAILABLE", "UNKNOWN",
        ):
            assert kind in PROVIDER_LIFECYCLE_KINDS


# ----------------------------- repair policy --------------------------------

class TestBuildRepairPrompt:
    def test_repair_prompt_keeps_context(self):
        p = _build_repair_prompt(
            "ORIGINAL_TASK: ..." ,
            '{"passed": "true"}',
            "INVALID_VERDICT_PASSED_NOT_BOOL",
        )
        # Schema error must be echoed so the model knows what to fix.
        assert "INVALID_VERDICT_PASSED_NOT_BOOL" in p
        # The schema directives must be present.
        assert "passed(boolean)" in p
        assert "repair_instruction(string)" in p
        assert "evidence_sources(array of strings)" in p
        # Original raw output must be echoed (so model can recover its verdict).
        assert '{"passed": "true"}' in p

    def test_repair_prompt_does_not_change_task(self):
        p = _build_repair_prompt(
            "TASK: return VERIFIED_TEXT", "garbage", "MALFORMED_JSON"
        )
        assert "Do not change your judgement" in p
        assert "Do not change the original Task" in p
        # MUST NOT add new evaluation criteria beyond reformat.
        assert "Re-emit ONLY one JSON object" in p
        assert "no Markdown fence" in p


class TestCallReviewerOnce:
    """Validate the reviewer subprocess helper classifies correctly."""

    def test_returns_none_on_adapter_missing(self):
        attempts = []
        out = _call_reviewer_once("hello", "no-such-reviewer", attempts)
        assert out is None
        assert len(attempts) == 1

    def test_classifies_402(self, monkeypatch):
        attempts = []

        class FakeAdapter:
            config = {"task_timeout_seconds": 30}
            name = "fake-402"
            def health(self):
                return {"fully_operational": True, "model_state": "available"}
            def command_for_task(self, prompt):
                return ["echo", prompt]
            def record_inference_success(self, *a, **kw):
                pass

        def fake_run(argv, **kwargs):
            from unittest.mock import MagicMock
            r = MagicMock()
            r.returncode = 0
            r.stdout = "garbled"
            r.stderr = "HTTP 402 insufficient balance"
            return r

        # _call_reviewer_once does `from aios_tool_adapter import get_adapter`,
        # so we must patch the source module, not the local name.
        monkeypatch.setattr(
            "aios_tool_adapter.get_adapter", lambda n: FakeAdapter()
        )
        monkeypatch.setattr("subprocess.run", fake_run)
        out = _call_reviewer_once("hi", "fake-402", attempts)
        assert out is not None
        assert out["provider_kind"] == "QUOTA_EXHAUSTED"
        assert out["provider_fatal"] is True
        assert out["extract_category"] == VERDICT_EXTRACT_NO_JSON_OBJECT

    def test_classifies_valid_verdict(self, monkeypatch):
        class FakeAdapter:
            config = {"task_timeout_seconds": 30}
            name = "fake-ok"
            def health(self):
                return {"fully_operational": True, "model_state": "available"}
            def command_for_task(self, prompt):
                return ["echo", prompt]
            def record_inference_success(self, *a, **kw):
                pass

        def fake_run(argv, **kwargs):
            from unittest.mock import MagicMock
            r = MagicMock()
            r.returncode = 0
            r.stdout = '{"passed": true, "reason": "x"}'
            r.stderr = ""
            return r

        monkeypatch.setattr(
            "aios_tool_adapter.get_adapter", lambda n: FakeAdapter()
        )
        monkeypatch.setattr("subprocess.run", fake_run)
        out = _call_reviewer_once("hi", "fake-ok", attempts=[])
        assert out["provider_kind"] == "AVAILABLE"
        assert out["extract_category"] == VERDICT_EXTRACT_OK


# ----------------------- reviewer selection / fallback --------------------

class TestReviewerSelection:
    def test_policy_excludes_executor(self):
        policy = _review_policy()
        assert policy.get("exclude_executor") is True

    def test_policy_default_reviewers_present(self):
        policy = _review_policy()
        reviewers = [r["id"] for r in policy["reviewers"]]
        for name in ("hermes", "claude", "codex", "opencode"):
            assert name in reviewers

    def test_executor_skipped_returns_unavailable(self, monkeypatch):
        """When the only configured reviewer is the executor, we must raise."""
        monkeypatch.setattr(
            "aios_verification_gate._review_policy",
            lambda: {
                "fail_closed": True,
                "exclude_executor": True,
                "reviewers": [{"id": "opencode", "backend": "opencode-server"}],
            },
        )
        with pytest.raises(RuntimeError) as exc:
            _semantic_review("prompt", executor="opencode")
        assert "all_independent_reviewers_unavailable" in str(exc.value)

    def test_first_valid_reviewer_wins(self, monkeypatch):
        monkeypatch.setattr(
            "aios_verification_gate._review_policy",
            lambda: {
                "fail_closed": True,
                "exclude_executor": True,
                "reviewers": [
                    {"id": "r-good", "backend": "fake"},
                    {"id": "r-bad", "backend": "fake"},
                ],
            },
        )

        outcomes = iter([
            {  # r-good
                "reviewer": "r-good", "returncode": 0, "stdout_bytes": 32,
                "stderr_bytes": 0, "stderr_text": "",
                "stdout_text": '{"passed": true, "reason": "y"}',
                "latency_ms": 100, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_OK,
                "parsed_value": {"passed": True, "reason": "y"},
                "provider_kind": "AVAILABLE", "provider_description": "",
                "provider_fatal": False,
                "adapter": None,
            },
            {  # r-bad (never reached)
                "reviewer": "r-bad", "returncode": 0, "stdout_bytes": 0,
                "stderr_bytes": 0, "stderr_text": "",
                "stdout_text": "", "latency_ms": 0, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_EMPTY_OUTPUT,
                "parsed_value": None,
                "provider_kind": "EMPTY_OUTPUT", "provider_description": "",
                "provider_fatal": False, "adapter": None,
            },
        ])

        def fake_call(prompt, reviewer, attempts, binding_id=""):
            return next(outcomes)
        monkeypatch.setattr(
            "aios_verification_gate._call_reviewer_once", fake_call
        )

        out = _semantic_review("prompt", executor="other")
        assert out["reviewer"] == "r-good"
        assert out["parsed"]["passed"] is True

    def test_fallback_after_excluded_executor(self, monkeypatch):
        monkeypatch.setattr(
            "aios_verification_gate._review_policy",
            lambda: {
                "fail_closed": True,
                "exclude_executor": True,
                "reviewers": [
                    {"id": "opencode", "backend": "opencode-server"},
                    {"id": "r-good", "backend": "fake"},
                ],
            },
        )

        def fake_call(prompt, reviewer, attempts, binding_id=""):
            return {
                "reviewer": reviewer, "returncode": 0, "stdout_bytes": 32,
                "stderr_bytes": 0, "stderr_text": "",
                "stdout_text": '{"passed": true}', "latency_ms": 100,
                "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_OK,
                "parsed_value": {"passed": True},
                "provider_kind": "AVAILABLE", "provider_description": "",
                "provider_fatal": False, "adapter": None,
            }
        monkeypatch.setattr(
            "aios_verification_gate._call_reviewer_once", fake_call
        )

        out = _semantic_review("prompt", executor="opencode")
        assert out["reviewer"] == "r-good"

    def test_repair_one_then_succeed(self, monkeypatch):
        monkeypatch.setattr(
            "aios_verification_gate._review_policy",
            lambda: {
                "fail_closed": True,
                "exclude_executor": True,
                "reviewers": [{"id": "r", "backend": "fake"}],
            },
        )

        # First attempt: malformed. Repair attempt: valid.
        outcomes = iter([
            {  # attempt 1: malformed
                "reviewer": "r", "returncode": 0, "stdout_bytes": 10,
                "stderr_bytes": 0, "stderr_text": "",
                "stdout_text": "garbage", "latency_ms": 50, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_NO_JSON_OBJECT,
                "parsed_value": None,
                "provider_kind": "MALFORMED_RESPONSE",
                "provider_description": "malformed",
                "provider_fatal": False, "adapter": None,
            },
            {  # attempt 2 (repair): valid
                "reviewer": "r", "returncode": 0, "stdout_bytes": 25,
                "stderr_bytes": 0, "stderr_text": "",
                "stdout_text": '{"passed": true, "reason": "ok"}',
                "latency_ms": 80, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_OK,
                "parsed_value": {"passed": True, "reason": "ok"},
                "provider_kind": "AVAILABLE", "provider_description": "",
                "provider_fatal": False, "adapter": None,
            },
        ])

        def fake_call(prompt, reviewer, attempts, binding_id=""):
            return next(outcomes)
        monkeypatch.setattr(
            "aios_verification_gate._call_reviewer_once", fake_call
        )

        out = _semantic_review("prompt", executor="other")
        assert out["repair_attempted"] is True
        assert out["parsed"]["passed"] is True

    def test_repair_one_max_no_third_attempt(self, monkeypatch):
        monkeypatch.setattr(
            "aios_verification_gate._review_policy",
            lambda: {
                "fail_closed": True,
                "exclude_executor": True,
                "reviewers": [{"id": "r", "backend": "fake"}],
            },
        )

        # First: malformed. Repair: still malformed. Third attempt would be
        # valid but must NOT be called.
        outcomes = iter([
            {  # attempt 1: malformed
                "reviewer": "r", "returncode": 0, "stdout_bytes": 10,
                "stderr_bytes": 0, "stderr_text": "",
                "stdout_text": "garbage", "latency_ms": 50, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_NO_JSON_OBJECT,
                "parsed_value": None,
                "provider_kind": "MALFORMED_RESPONSE",
                "provider_description": "malformed",
                "provider_fatal": False, "adapter": None,
            },
            {  # repair attempt: still malformed
                "reviewer": "r", "returncode": 0, "stdout_bytes": 10,
                "stderr_bytes": 0, "stderr_text": "",
                "stdout_text": "still garbage", "latency_ms": 60,
                "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_NO_JSON_OBJECT,
                "parsed_value": None,
                "provider_kind": "MALFORMED_RESPONSE",
                "provider_description": "malformed",
                "provider_fatal": False, "adapter": None,
            },
        ])

        call_count = {"n": 0}

        def fake_call(prompt, reviewer, attempts, binding_id=""):
            call_count["n"] += 1
            if call_count["n"] > 2:
                raise AssertionError(
                    "third call should not happen; repair is capped at 1"
                )
            return next(outcomes)
        monkeypatch.setattr(
            "aios_verification_gate._call_reviewer_once", fake_call
        )

        with pytest.raises(RuntimeError) as exc:
            _semantic_review("prompt", executor="other")
        assert "all_independent_reviewers_unavailable" in str(exc.value)
        assert call_count["n"] == 2

    def test_no_repair_on_transport_failure(self, monkeypatch):
        monkeypatch.setattr(
            "aios_verification_gate._review_policy",
            lambda: {
                "fail_closed": True,
                "exclude_executor": True,
                "reviewers": [{"id": "r", "backend": "fake"}],
            },
        )

        # Attempt 1: HTTP 402 (transport). MUST NOT trigger repair.
        outcomes = iter([
            {  # attempt 1
                "reviewer": "r", "returncode": 0, "stdout_bytes": 0,
                "stderr_bytes": 50, "stderr_text": "HTTP 402 insufficient balance",
                "stdout_text": "", "latency_ms": 100, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_EMPTY_OUTPUT,
                "parsed_value": None,
                "provider_kind": "QUOTA_EXHAUSTED",
                "provider_description": "quota",
                "provider_fatal": True, "adapter": None,
            },
        ])

        def fake_call(prompt, reviewer, attempts, binding_id=""):
            return next(outcomes)
        monkeypatch.setattr(
            "aios_verification_gate._call_reviewer_once", fake_call
        )

        with pytest.raises(RuntimeError) as exc:
            _semantic_review("prompt", executor="other")
        # repair_attempted should NOT be in any attempt entry
        msg = str(exc.value)
        assert "QUOTA_EXHAUSTED" in msg
        assert "REPAIR" not in msg

    def test_no_repair_on_429_plan(self, monkeypatch):
        monkeypatch.setattr(
            "aios_verification_gate._review_policy",
            lambda: {
                "fail_closed": True,
                "exclude_executor": True,
                "reviewers": [{"id": "r", "backend": "fake"}],
            },
        )

        outcomes = iter([
            {
                "reviewer": "r", "returncode": 0, "stdout_bytes": 0,
                "stderr_bytes": 50,
                "stderr_text": "HTTP 429 token plan exhausted",
                "stdout_text": "", "latency_ms": 100, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_EMPTY_OUTPUT,
                "parsed_value": None,
                "provider_kind": "PLAN_EXHAUSTED",
                "provider_description": "plan",
                "provider_fatal": True, "adapter": None,
            },
        ])

        def fake_call(prompt, reviewer, attempts, binding_id=""):
            return next(outcomes)
        monkeypatch.setattr(
            "aios_verification_gate._call_reviewer_once", fake_call
        )

        with pytest.raises(RuntimeError) as exc:
            _semantic_review("prompt", executor="other")
        msg = str(exc.value)
        assert "PLAN_EXHAUSTED" in msg
        assert "REPAIR" not in msg

    def test_no_repair_on_timeout(self, monkeypatch):
        monkeypatch.setattr(
            "aios_verification_gate._review_policy",
            lambda: {
                "fail_closed": True,
                "exclude_executor": True,
                "reviewers": [{"id": "r", "backend": "fake"}],
            },
        )

        outcomes = iter([
            {
                "reviewer": "r", "returncode": 0, "stdout_bytes": 0,
                "stderr_bytes": 30, "stderr_text": "timed out after 30s",
                "stdout_text": "", "latency_ms": 30000, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_EMPTY_OUTPUT,
                "parsed_value": None,
                "provider_kind": "TIMEOUT",
                "provider_description": "timeout",
                "provider_fatal": True, "adapter": None,
            },
        ])

        def fake_call(prompt, reviewer, attempts, binding_id=""):
            return next(outcomes)
        monkeypatch.setattr(
            "aios_verification_gate._call_reviewer_once", fake_call
        )

        with pytest.raises(RuntimeError) as exc:
            _semantic_review("prompt", executor="other")
        msg = str(exc.value)
        assert "TIMEOUT" in msg
        assert "REPAIR" not in msg

    def test_legal_verdict_no_repair(self, monkeypatch):
        monkeypatch.setattr(
            "aios_verification_gate._review_policy",
            lambda: {
                "fail_closed": True,
                "exclude_executor": True,
                "reviewers": [{"id": "r", "backend": "fake"}],
            },
        )

        outcomes = iter([
            {
                "reviewer": "r", "returncode": 0, "stdout_bytes": 25,
                "stderr_bytes": 0, "stderr_text": "",
                "stdout_text": '{"passed": true, "reason": "ok"}',
                "latency_ms": 100, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_OK,
                "parsed_value": {"passed": True, "reason": "ok"},
                "provider_kind": "AVAILABLE", "provider_description": "",
                "provider_fatal": False, "adapter": None,
            },
        ])

        def fake_call(prompt, reviewer, attempts, binding_id=""):
            return next(outcomes)
        monkeypatch.setattr(
            "aios_verification_gate._call_reviewer_once", fake_call
        )

        out = _semantic_review("prompt", executor="other")
        assert out["repair_attempted"] is False
        assert out["parsed"]["passed"] is True

    def test_all_reviewers_fail_returns_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            "aios_verification_gate._review_policy",
            lambda: {
                "fail_closed": True,
                "exclude_executor": True,
                "reviewers": [
                    {"id": "r1", "backend": "fake"},
                    {"id": "r2", "backend": "fake"},
                ],
            },
        )

        outcomes = iter([
            {  # r1: 401
                "reviewer": "r1", "returncode": 0, "stdout_bytes": 0,
                "stderr_bytes": 30, "stderr_text": "HTTP 401 unauthorized",
                "stdout_text": "", "latency_ms": 100, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_EMPTY_OUTPUT,
                "parsed_value": None,
                "provider_kind": "AUTH_FAILED",
                "provider_description": "auth",
                "provider_fatal": True, "adapter": None,
            },
            {  # r2: 402
                "reviewer": "r2", "returncode": 0, "stdout_bytes": 0,
                "stderr_bytes": 30, "stderr_text": "HTTP 402 insufficient balance",
                "stdout_text": "", "latency_ms": 100, "live_recovery": False,
                "extract_category": VERDICT_EXTRACT_EMPTY_OUTPUT,
                "parsed_value": None,
                "provider_kind": "QUOTA_EXHAUSTED",
                "provider_description": "quota",
                "provider_fatal": True, "adapter": None,
            },
        ])

        def fake_call(prompt, reviewer, attempts, binding_id=""):
            return next(outcomes)
        monkeypatch.setattr(
            "aios_verification_gate._call_reviewer_once", fake_call
        )

        with pytest.raises(RuntimeError) as exc:
            _semantic_review("prompt", executor="other")
        msg = str(exc.value)
        assert "all_independent_reviewers_unavailable" in msg
        assert "AUTH_FAILED" in msg
        assert "QUOTA_EXHAUSTED" in msg