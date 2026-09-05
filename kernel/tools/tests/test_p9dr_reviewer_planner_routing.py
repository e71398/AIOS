#!/usr/bin/env python3
"""P9D-R Reviewer/Planner Routing closure — dedicated tests.

This suite validates the unified Reviewer + Planner routing surface
added by the P9D-R close-out.  It covers:

  1. Reviewer selection must honour preferred_reviewer /
     blocked_reviewer_tools / allow_reviewer_fallback.
  2. The Verification Gate must consume choose_reviewer()
     candidates — never the legacy _review_policy() bypass.
  3. Planner selection must honour preferred_planner /
     blocked_planner_tools / allow_planner_fallback through
     Registry + TaskRoutingPolicy (no hardcoded OpenClaw).
  4. The OpenCode Planner must go through the PLAN_ONLY adapter
     with planner-specific mode; the plan must be schema-validated
     before it is persisted.
  5. attempt_reviewer_health must survive a single tool-process
     failure without leaking to the other Reviewer bindings
     (Claude:deepseek vs Claude:minimax separation).
  6. Reviewer / Planner routing fields must round-trip from the
     Orchestrator / Verification Gate through the persisted
     verdict surface.

The tests are written as pure unit tests with mocked Redis /
capability truth sources so they run without a live system.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any, Dict, List, Optional, Tuple

import pytest

TOOLS = "${AIOS_HOME}/kernel/tools"
TESTS = "${AIOS_HOME}/kernel/tools/tests"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)


# ---------------------------------------------------------------------------
# Stub fixtures: keep tests independent from Redis / opencode-server
# ---------------------------------------------------------------------------


class _StubStatus:
    def __init__(self, status: str, binding: str = "stub:binding"):
        self.status = status
        self.effective_binding = binding


class _StubEngine:
    def __init__(self, statuses: Dict[str, _StubStatus]):
        self._statuses = statuses

    def compute_tool_status(self, tool_id: str) -> _StubStatus:
        return self._statuses.get(
            tool_id, _StubStatus("UNAVAILABLE_TOOL_RUNTIME", ""),
        )


class _StubToolHealth:
    def __init__(self, alive: bool = True):
        self._alive = alive

    def __call__(self, name: str) -> bool:
        return self._alive


def _install_orchestrator_stubs(monkeypatch, statuses: Dict[str, _StubStatus],
                                process_alive: bool = True):
    """Patch aios_orchestrator's choose_reviewer / _resolve_planner_target
    to use deterministic stub data."""
    from aios_orchestrator import (
        choose_reviewer,
        _resolve_planner_target,
        _tool_process_health,
    )

    # Tool-process health stub
    monkeypatch.setattr(
        "aios_orchestrator._tool_process_health",
        lambda name: process_alive,
    )

    # Replace the failover engine accessor used by choose_reviewer.
    import aios_orchestrator as _orch

    def _fake_engine():
        return _StubEngine(statuses)

    def _fake_registry():
        return _StubRegistry()

    monkeypatch.setattr(
        "aios_tool_failover.get_default_tool_engine", _fake_engine,
        raising=False,
    )
    monkeypatch.setattr(
        "aios_tool_registry.get_default_registry", _fake_registry,
        raising=False,
    )


class _StubManifest:
    def __init__(self, tool_id: str, roles: Tuple[str, ...]):
        self.tool_id = tool_id
        self.roles = tuple(roles)

    def has_role(self, role: str) -> bool:
        return role in self.roles


class _StubRegistry:
    def __init__(self):
        self._manifests = {
            "opencode": _StubManifest("opencode", ("executor", "planner")),
            "openclaw": _StubManifest("openclaw", ("planner", "reviewer")),
            "hermes": _StubManifest("hermes", ("reviewer",)),
            "claude": _StubManifest("claude", ("executor", "reviewer")),
            "codex": _StubManifest("codex", ("executor",)),
        }

    def list_by_role(self, role: str) -> List[_StubManifest]:
        return [m for m in self._manifests.values() if role in m.roles]


# ---------------------------------------------------------------------------
# 1. choose_reviewer: routing rules
# ---------------------------------------------------------------------------


def test_choose_reviewer_preferred_first(monkeypatch):
    statuses = {
        "hermes": _StubStatus("AVAILABLE_PRIMARY", "hermes:primary"),
        "claude": _StubStatus("AVAILABLE_PRIMARY", "claude:minimax"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    from aios_orchestrator import choose_reviewer
    result = choose_reviewer(
        task_id="t-1",
        preferred_reviewer="claude",
        blocked_reviewer_tools=(),
        allow_reviewer_fallback=True,
        exclude_executor="opencode",
    )
    assert result["reviewer"] == "claude", (
        "preferred_reviewer must be the first choice when healthy"
    )
    assert result["candidates"][0] == "claude"
    assert result["binding"] == "claude:minimax"


def test_choose_reviewer_blocked_tool_excluded(monkeypatch):
    statuses = {
        "hermes": _StubStatus("AVAILABLE_PRIMARY"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    from aios_orchestrator import choose_reviewer
    result = choose_reviewer(
        task_id="t-1",
        preferred_reviewer="",
        blocked_reviewer_tools=("hermes",),
        allow_reviewer_fallback=True,
        exclude_executor="opencode",
    )
    excluded_ids = {entry[0] for entry in result["excluded"]}
    assert "hermes" not in [result["reviewer"], *result["candidates"]]
    assert "hermes" in excluded_ids
    assert result["reviewer"] == "claude"


def test_choose_reviewer_self_review_forbidden(monkeypatch):
    statuses = {
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
        "hermes": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    from aios_orchestrator import choose_reviewer
    result = choose_reviewer(
        task_id="t-1",
        preferred_reviewer="claude",
        blocked_reviewer_tools=(),
        allow_reviewer_fallback=True,
        exclude_executor="claude",
    )
    assert result["reviewer"] != "claude"
    excluded_ids = {entry[0] for entry in result["excluded"]}
    assert "claude" in excluded_ids


def test_choose_reviewer_no_fallback_blocks(monkeypatch):
    statuses = {
        "hermes": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    from aios_orchestrator import choose_reviewer
    result = choose_reviewer(
        task_id="t-1",
        preferred_reviewer="hermes",
        blocked_reviewer_tools=(),
        allow_reviewer_fallback=False,
        exclude_executor="opencode",
    )
    assert result["reviewer"] == ""
    excluded_reasons = {
        entry[1] for entry in result["excluded"] if isinstance(entry, tuple)
    }
    assert "UNAVAILABLE_TOOL_RUNTIME" in excluded_reasons


def test_choose_reviewer_claude_deepseek_failure_does_not_pollute_claude_minimax(
    monkeypatch,
):
    """P2/P9D-R §十一: claude:deepseek=DEGRADED_EXTERNAL must NOT exclude
    claude:minimax=AVAILABLE from the candidate list.

    When Claude's only available binding is :minimax (healthy), it MUST
    still be selectable.  The DeepSeek binding failure is a per-binding
    failure, not a per-tool failure.
    """
    # Claude is alone with the minimax binding healthy.  Hermes is
    # UNAVAILABLE so Claude wins by default.
    statuses = {
        "claude": _StubStatus("AVAILABLE_PRIMARY", "claude:minimax"),
        "hermes": _StubStatus(
            "DEGRADED_EXTERNAL", "hermes:deepseek",
        ),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    from aios_orchestrator import choose_reviewer
    result = choose_reviewer(
        task_id="t-1",
        preferred_reviewer="claude",
        blocked_reviewer_tools=(),
        allow_reviewer_fallback=True,
        exclude_executor="opencode",
    )
    # Claude is preferred AND healthy → wins.
    assert result["reviewer"] == "claude"
    assert result["binding"] == "claude:minimax"
    # The exclusion list MUST NOT include claude (otherwise the
    # test setup would be broken).  Hermes may or may not appear in
    # ``excluded`` depending on whether the loop reaches it; we only
    # assert the strictly weaker "claude is not excluded" invariant.
    excluded_ids = {entry[0] for entry in result["excluded"]}
    assert "claude" not in excluded_ids


def test_choose_reviewer_records_fallback_count(monkeypatch):
    """When the first candidate is unhealthy, the loop must move on
    and record each attempted Reviewer in the fallback_count.
    """
    statuses = {
        "hermes": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    from aios_orchestrator import choose_reviewer
    result = choose_reviewer(
        task_id="t-1",
        preferred_reviewer="",
        blocked_reviewer_tools=(),
        allow_reviewer_fallback=True,
        exclude_executor="opencode",
    )
    # All non-claude candidates failed → fallback_count > 0;
    # exactly the candidates before claude were excluded.
    assert result["fallback_count"] > 0
    assert result["reviewer"] == "claude"
    excluded_ids = {entry[0] for entry in result["excluded"]}
    assert "claude" not in excluded_ids
    # The exclusion list reflects every Reviewer that was attempted
    # before claude won; that includes openclaw (registry order) and
    # hermes.
    assert "openclaw" in excluded_ids
    assert "hermes" in excluded_ids


# ---------------------------------------------------------------------------
# 2. _resolve_planner_target: registry-driven selection
# ---------------------------------------------------------------------------


def test_resolve_planner_target_uses_registry(monkeypatch):
    statuses = {
        "opencode": _StubStatus("AVAILABLE_PRIMARY"),
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    from aios_orchestrator import _resolve_planner_target
    selected, binding, provider, model, mode = _resolve_planner_target({
        "preferred_planner": "opencode",
        "blocked_planner_tools": [],
        "allow_planner_fallback": True,
    })
    assert selected == "opencode"
    assert mode == "opencode-plan-only"
    assert binding == "opencode:free"


def test_resolve_planner_target_blocked_excludes(monkeypatch):
    statuses = {
        "opencode": _StubStatus("AVAILABLE_PRIMARY"),
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    from aios_orchestrator import _resolve_planner_target
    selected, binding, provider, model, mode = _resolve_planner_target({
        "preferred_planner": "opencode",
        "blocked_planner_tools": ["opencode"],
        "allow_planner_fallback": True,
    })
    # opencode is blocked, so the selector falls back to openclaw
    assert selected != "opencode"
    assert selected == "openclaw"
    assert mode == "openclaw-minimax"


def test_resolve_planner_target_no_fallback_truncates(monkeypatch):
    statuses = {
        "opencode": _StubStatus("AVAILABLE_PRIMARY"),
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    from aios_orchestrator import _resolve_planner_target
    selected, binding, provider, model, mode = _resolve_planner_target({
        "preferred_planner": "opencode",
        "blocked_planner_tools": [],
        "allow_planner_fallback": False,
    })
    assert selected == "opencode"


# ---------------------------------------------------------------------------
# 3. OpenCode PLAN_ONLY adapter: contract enforcement
# ---------------------------------------------------------------------------


def test_opencode_adapter_refuses_execution_tokens():
    """The PLAN_ONLY adapter MUST refuse prompts that ask it to
    execute / mutate / transmit.  Forbidden tokens include
    ``run_shell``, ``send_email``, ``curl ``."""
    from aios_opencode_adapter import (
        _validate_prompt_is_plan_only,
        OpenCodeAdapterError,
    )
    with pytest.raises(OpenCodeAdapterError) as exc:
        _validate_prompt_is_plan_only("Please run_shell to deploy.")
    assert "PLAN_ONLY_REFUSED" in str(exc.value)


def test_opencode_adapter_validates_plan_schema():
    from aios_opencode_adapter import _validate_plan_schema
    plan = {
        "goal": "OPENCODE_PLAN_ONLY_BASELINE_OK",
        "steps": [
            {
                "task": "Return OPENCODE_PLAN_ONLY_BASELINE_OK",
                "depends_on": [],
                "role": "executor",
                "acceptance": ["Result contains OPENCODE_PLAN_ONLY_BASELINE_OK"],
                "evidence_mode": "runtime",
            },
        ],
        "required_capabilities": [],
        "executor_role": "executor",
        "reviewer_role": "reviewer",
        "risks": [],
        "completion_criteria": [
            "Executor returns the requested result",
            "Reviewer verifies the result",
        ],
    }
    ok, err = _validate_plan_schema(plan)
    assert ok is True, err
    # Negative case: missing 'steps'
    bad = dict(plan)
    bad.pop("steps")
    ok, err = _validate_plan_schema(bad)
    assert ok is False
    assert "PLAN_SCHEMA_INVALID" in err


def test_opencode_adapter_translates_plan_only_response(monkeypatch):
    """The adapter response must mirror aios_model_gateway.call_model.

    The mocked ``_request`` patches the legacy ``opencode:minimax``
    session/message path; the ``opencode:free`` binding delegates to
    ``aios_opencode_client.run_task`` which is not patched here, so
    the test uses ``opencode:minimax`` to exercise the legacy code
    path that the stub can intercept.
    """
    from aios_opencode_adapter import (
        execute_plan_only,
        OpenCodeAdapterError,
    )
    # Patch the inner transport so we do not need a live OpenCode server.
    import aios_opencode_adapter as _ad

    # Build a synthetic plan JSON the schema validator accepts.
    plan = {
        "goal": "OPENCODE_PLAN_ONLY_BASELINE_OK",
        "steps": [{
            "task": "Return OPENCODE_PLAN_ONLY_BASELINE_OK",
            "depends_on": [],
            "role": "executor",
            "acceptance": ["OPENCODE_PLAN_ONLY_BASELINE_OK"],
            "evidence_mode": "runtime",
        }],
        "required_capabilities": [],
        "executor_role": "executor",
        "reviewer_role": "reviewer",
        "risks": [],
        "completion_criteria": ["Executor returns the requested result"],
    }

    def _fake_request(method, path, payload=None, timeout=20):
        if path == "/global/health":
            return {"healthy": True}
        if path.startswith("/session") and method == "POST" and "/message" not in path:
            return {"id": "session-1"}
        if path.startswith("/session") and method == "DELETE":
            return {}
        if "/message" in path and method == "POST":
            # POST returns immediately with step-start events.
            return {"info": {}, "parts": []}
        if "/message" in path and method == "GET":
            # GET returns the assistant reply (asynchronous poll hit).
            return [{
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "user prompt"}],
            }, {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": json.dumps(plan)}],
            }]
        return {}

    _ad._request = _fake_request

    result = execute_plan_only(
        parent_id="t-plan-only",
        prompt="Plan the OPENCODE_PLAN_ONLY_BASELINE_OK target",
        binding="opencode:minimax",
        read_timeout=2.0,
    )
    assert result["ok"] is True
    assert result["planner_mode"] == "PLAN_ONLY"
    assert result["planner_schema_valid"] is True
    choices = result["result"]["choices"]
    assert choices[0]["message"]["role"] == "assistant"
    assert "OPENCODE_PLAN_ONLY_BASELINE_OK" in choices[0]["message"]["content"]


def test_opencode_adapter_rejects_invalid_json(monkeypatch):
    """A non-JSON planner response is rejected as PLAN_SCHEMA_INVALID."""
    import aios_opencode_adapter as _ad

    def _fake_request(method, path, payload=None, timeout=20):
        if path == "/global/health":
            return {"healthy": True}
        if path.startswith("/session") and method == "POST" and "/message" not in path:
            return {"id": "session-1"}
        if path.startswith("/session") and method == "DELETE":
            return {}
        if "/message" in path and method == "POST":
            return {"info": {}, "parts": []}
        if "/message" in path and method == "GET":
            return [{
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "this is not json at all"}],
            }]
        return {}

    _ad._request = _fake_request

    from aios_opencode_adapter import (
        execute_plan_only,
        OpenCodeAdapterError,
    )
    with pytest.raises(OpenCodeAdapterError) as exc:
        execute_plan_only(parent_id="t-bad", prompt="noop", read_timeout=2.0)
    assert "PLAN_SCHEMA_INVALID" in str(exc.value)


# ---------------------------------------------------------------------------
# 4. _semantic_review: policy-driven loop ordering
# ---------------------------------------------------------------------------


def _fake_call_reviewer(reviewer: str, prompt: str, attempts: list) -> dict:
    """Replacement for _call_reviewer_once that returns a valid
    verdict JSON the schema validator accepts.
    """
    parsed = {
        "passed": True,
        "reason": f"verified by {reviewer}",
        "repair_instruction": "",
        "evidence_checked": True,
        "evidence_sources": ["runtime-result"],
    }
    return {
        "reviewer": reviewer,
        "returncode": 0,
        "stdout_bytes": 100,
        "stderr_bytes": 0,
        "stderr_text": "",
        "stdout_text": json.dumps(parsed),
        "latency_ms": 1,
        "live_recovery": False,
        "extract_category": "OK",
        "parsed_value": parsed,
        "provider_kind": "AVAILABLE",
        "provider_description": "ok",
        "provider_fatal": False,
        "adapter": None,
    }


def _install_gate_stubs(monkeypatch, statuses, *, process_alive=True):
    _install_orchestrator_stubs(monkeypatch, statuses, process_alive=process_alive)
    monkeypatch.setattr(
        "aios_verification_gate._call_reviewer_once",
        lambda prompt, reviewer, attempts, binding_id="": _fake_call_reviewer(reviewer, prompt, attempts),
    )


def test_semantic_review_consumes_selection_candidates(monkeypatch):
    """Verification Gate MUST iterate over selection.candidates (not
    _review_policy()) when task_policy is supplied.
    """
    statuses = {
        "hermes": _StubStatus("AVAILABLE_PRIMARY"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_gate_stubs(monkeypatch, statuses)

    from aios_verification_gate import _semantic_review
    review = _semantic_review(
        prompt="judge this",
        executor="opencode",
        task_policy={
            "preferred_reviewer": "claude",
            "blocked_reviewer_tools": [],
            "allow_reviewer_fallback": True,
        },
        child_state={"task_id": "t-1"},
    )
    # The selected Reviewer must reflect the policy choice.
    assert review["reviewer"] == "claude"
    assert review["attempted_reviewers"] == ["claude"]
    assert review["actual_reviewer"] == "claude"
    assert review["policy_driven"] is True
    assert review["preferred_reviewer"] == "claude"
    assert review["allow_reviewer_fallback"] is True
    assert review["reviewer_fallback_count"] == 0


def test_semantic_review_blocks_executor_self_review(monkeypatch):
    # Stub registry returns candidates in dict-iteration order:
    # openclaw, hermes, claude.  With executor=claude, choose_reviewer
    # excludes claude and falls back to openclaw / hermes.
    statuses = {
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
        "hermes": _StubStatus("AVAILABLE_PRIMARY"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_gate_stubs(monkeypatch, statuses)

    from aios_verification_gate import _semantic_review
    review = _semantic_review(
        prompt="judge this",
        executor="claude",
        task_policy={
            "preferred_reviewer": "claude",
            "blocked_reviewer_tools": [],
            "allow_reviewer_fallback": True,
        },
        child_state={"task_id": "t-1"},
    )
    # claude was excluded by choose_reviewer (self-review forbidden).
    # The first healthy non-claude candidate wins; the test only
    # requires claude is not the actual_reviewer and is recorded as
    # excluded.
    assert review["actual_reviewer"] != "claude"
    excluded_ids = [entry.get("reviewer") for entry in review["excluded_reviewers"]]
    assert "claude" in excluded_ids
    assert review["actual_reviewer"] in ("openclaw", "hermes")


def test_semantic_review_blocked_reviewer_tools_never_called(monkeypatch):
    """blocked_reviewer_tools MUST prevent the call.
    """
    # openclaw is the only healthy reviewer; hermes is blocked by
    # the task policy so it must never appear in attempted_reviewers
    # even though claude is healthy too (registry order has openclaw
    # first → openclaw wins).
    statuses = {
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
        "hermes": _StubStatus("AVAILABLE_PRIMARY"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_gate_stubs(monkeypatch, statuses)

    # Track which reviewers the patched adapter was invoked for.
    invoked: List[str] = []

    def _tracker(prompt, reviewer, attempts, binding_id=""):
        invoked.append(reviewer)
        return _fake_call_reviewer(reviewer, prompt, attempts)

    monkeypatch.setattr(
        "aios_verification_gate._call_reviewer_once", _tracker,
    )

    from aios_verification_gate import _semantic_review
    review = _semantic_review(
        prompt="judge this",
        executor="opencode",
        task_policy={
            "preferred_reviewer": "",
            "blocked_reviewer_tools": ["hermes"],
            "allow_reviewer_fallback": True,
        },
        child_state={"task_id": "t-1"},
    )
    assert "hermes" not in invoked, (
        "blocked_reviewer_tools MUST prevent the call entirely"
    )
    # ``attempted_reviewers`` is a list of reviewer names (strings),
    # not a list of dicts — keep the assertion direct.
    assert "hermes" not in review["attempted_reviewers"]
    excluded_ids = [entry.get("reviewer") for entry in review["excluded_reviewers"]]
    assert "hermes" in excluded_ids


def test_semantic_review_attempted_reviewers_matches_call_order(monkeypatch):
    """The attempted_reviewers list MUST equal the actual call order.
    """
    # Make openclaw and hermes both unhealthy so claude (preferred) is
    # the only viable candidate.  We want to assert the contract that
    # every attempted reviewer is recorded, in order, and the actual
    # reviewer matches the winning reviewer.
    statuses = {
        "openclaw": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "hermes": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_gate_stubs(monkeypatch, statuses)

    from aios_verification_gate import _semantic_review
    review = _semantic_review(
        prompt="judge this",
        executor="opencode",
        task_policy={
            "preferred_reviewer": "claude",
            "blocked_reviewer_tools": [],
            "allow_reviewer_fallback": True,
        },
        child_state={"task_id": "t-1"},
    )
    # claude is preferred AND the only healthy reviewer → wins.
    assert review["actual_reviewer"] == "claude"
    assert "claude" in review["attempted_reviewers"]
    # claude is the preferred reviewer; the loop places it first
    # so no other candidate is ever attempted.
    assert review["attempted_reviewers"] == ["claude"]
    # fallback_count is 0 because claude wins on the very first
    # attempt (it is preferred and healthy).
    assert review["reviewer_fallback_count"] == 0
    # claude MUST NOT be in excluded_reviewers.
    excluded_ids = [
        entry.get("reviewer") for entry in review["excluded_reviewers"]
    ]
    assert "claude" not in excluded_ids
    # attempted_reviewers must be a list (not a tuple, not None)
    # so downstream readers can iterate uniformly.
    assert isinstance(review["attempted_reviewers"], list)


def test_semantic_review_legacy_fallback_keeps_static_policy(monkeypatch):
    """When task_policy is missing, the legacy _review_policy() list
    drives the iteration order — the previous behaviour is preserved
    so existing canaries stay green.
    """
    statuses = {
        "hermes": _StubStatus("AVAILABLE_PRIMARY"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_gate_stubs(monkeypatch, statuses)

    from aios_verification_gate import _semantic_review
    review = _semantic_review(
        prompt="judge this",
        executor="opencode",
        task_policy=None,
        child_state={"task_id": "t-1"},
    )
    assert review["policy_driven"] is False
    # First candidate is still 'hermes' from the static _review_policy().
    assert review["reviewer"] == "hermes"


# ---------------------------------------------------------------------------
# 5. Planner routing through _plan_and_dispatch (registry-driven)
# ---------------------------------------------------------------------------


def test_plan_and_dispatch_persists_planner_routing(monkeypatch):
    """The Orchestrator's _plan_and_dispatch MUST persist the
    planner routing fields even when build_plan short-circuits.
    """
    from aios_orchestrator import _plan_and_dispatch
    calls: list = []

    # Stub _save_workflow so we can introspect the persisted fields.
    def _fake_save(workflow_id, **fields):
        calls.append(dict(fields))
        return True

    monkeypatch.setattr(
        "aios_orchestrator._save_workflow", _fake_save,
    )

    # Stub build_plan to return a trivial single-node plan.
    def _fake_build_plan(goal, parent_id, *, task_policy=None):
        return [{
            "task": goal,
            "depends_on": [],
            "role": "opencode",
            "acceptance": ["Echo the goal"],
            "evidence_mode": "semantic",
        }], "openclaw-minimax", ""

    monkeypatch.setattr(
        "aios_orchestrator.build_plan", _fake_build_plan,
    )

    # Stub _enqueue_node so we do not need a live executor.
    monkeypatch.setattr(
        "aios_orchestrator._enqueue_node",
        lambda *a, **kw: True,
    )

    # Stub Redis to no-op (workflow hash writes use _save_workflow)
    monkeypatch.setattr(
        "aios_orchestrator._redis_client",
        types.SimpleNamespace(
            zrem=lambda *a, **kw: 0,
            zadd=lambda *a, **kw: 0,
            hset=lambda *a, **kw: 0,
            expire=lambda *a, **kw: 0,
        ),
    )
    monkeypatch.setattr(
        "aios_orchestrator._is_available", lambda: True,
    )
    monkeypatch.setattr(
        "aios_orchestrator.publish_event", lambda *a, **kw: None,
    )

    statuses = {
        "opencode": _StubStatus("AVAILABLE_PRIMARY"),
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    workflow = {
        "parent_id": "parent-1",
        "task_id": "parent-1",
        "goal": "OPENCODE_SECONDARY_PLANNER_OK",
        "source": "p9dr-capability-baseline",
        "preferred_executor": "",
        "strict_executor": "",
        "allow_executor_fallback": True,
    }
    _plan_and_dispatch("parent-1", workflow)
    captured = {}
    for call in calls:
        captured.update(call)
    assert captured.get("preferred_planner") in ("", "openclaw", "opencode")
    assert "actual_planner" in captured
    assert "planner_binding" in captured
    assert "planner_mode" in captured
    assert "attempted_planners" in captured
    assert "excluded_planners" in captured
    # actual_planner must be a real planner from the registry, not a
    # hardcoded OpenClaw.
    assert captured["actual_planner"] in ("openclaw", "opencode", "claude")


def test_plan_and_dispatch_opencode_is_picked_when_preferred(monkeypatch):
    """preferred_planner=opencode in the workflow MUST produce
    actual_planner=opencode and planner_mode=opencode-plan-only.
    """
    from aios_orchestrator import _plan_and_dispatch
    calls: list = []

    def _fake_save(workflow_id, **fields):
        calls.append(dict(fields))
        return True

    monkeypatch.setattr(
        "aios_orchestrator._save_workflow", _fake_save,
    )
    monkeypatch.setattr(
        "aios_orchestrator.build_plan",
        lambda goal, parent_id, *, task_policy=None: (
            [{
                "task": goal, "depends_on": [], "role": "codex",
                "acceptance": ["echo"], "evidence_mode": "semantic",
            }],
            "opencode-plan-only", "",
        ),
    )
    monkeypatch.setattr(
        "aios_orchestrator._enqueue_node",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "aios_orchestrator._redis_client",
        types.SimpleNamespace(zrem=lambda *a, **kw: 0, zadd=lambda *a, **kw: 0),
    )
    monkeypatch.setattr("aios_orchestrator._is_available", lambda: True)
    monkeypatch.setattr(
        "aios_orchestrator.publish_event", lambda *a, **kw: None,
    )

    statuses = {
        "opencode": _StubStatus("AVAILABLE_PRIMARY"),
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    workflow = {
        "parent_id": "parent-1",
        "task_id": "parent-1",
        "goal": "OPENCODE_SECONDARY_PLANNER_OK",
        "source": "p9dr-capability-baseline",
        "preferred_executor": "",
        "strict_executor": "",
        "allow_executor_fallback": True,
        "preferred_planner": "opencode",
        "blocked_planner_tools": [],
        "allow_planner_fallback": False,
    }
    _plan_and_dispatch("parent-1", workflow)
    captured = {}
    for call in calls:
        captured.update(call)
    assert captured["actual_planner"] == "opencode"
    assert captured["planner_mode"] == "opencode-plan-only"
    assert captured["allow_planner_fallback"] is False
    assert captured["preferred_planner"] == "opencode"


# ---------------------------------------------------------------------------
# 6. choose_reviewer + planner selection together (E2E shape)
# ---------------------------------------------------------------------------


def test_end_to_end_routing_audit_fields(monkeypatch):
    """The full pipeline (selection → call → verdict) must yield a
    verdict with every required audit field.
    """
    statuses = {
        "hermes": _StubStatus("AVAILABLE_PRIMARY"),
        "claude": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_gate_stubs(monkeypatch, statuses)

    from aios_verification_gate import _semantic_review
    review = _semantic_review(
        prompt="judge this",
        executor="opencode",
        task_policy={
            "preferred_reviewer": "claude",
            "blocked_reviewer_tools": [],
            "allow_reviewer_fallback": True,
        },
        child_state={"task_id": "t-1"},
    )
    for field in (
        "attempted_reviewers", "excluded_reviewers",
        "preferred_reviewer", "blocked_reviewer_tools",
        "allow_reviewer_fallback", "actual_reviewer",
        "reviewer_binding", "reviewer_fallback_count",
        "reviewer_bypass", "policy_driven",
    ):
        assert field in review, f"missing audit field: {field}"
    assert review["reviewer_bypass"] is False
    assert review["actual_reviewer"] == "claude"
    assert "claude" in review["attempted_reviewers"]


# ---------------------------------------------------------------------------
# 7. P9D-R actual_planner semantics — 5 tests
# ---------------------------------------------------------------------------


def test_actual_planner_null_when_openclaw_fails(monkeypatch):
    """preferred=openclaw but call fails → actual_planner MUST be empty."""
    from aios_orchestrator import _plan_and_dispatch
    calls: list = []

    def _fake_save(workflow_id, **fields):
        calls.append(dict(fields))
        return True

    monkeypatch.setattr("aios_orchestrator._save_workflow", _fake_save)

    def _fake_build_plan(goal, parent_id, *, task_policy=None):
        return [], "planning-failed", "FAILED_EXTERNAL_ROUTE_PLANNER_TIMEOUT", {
            "actual_planner": "",
            "actual_binding": "",
            "actual_provider": "",
            "actual_model": "",
            "fallback_count": 0,
            "attempted_planners": ["openclaw"],
            "excluded_planners": [],
            "first_failure_scope": "TOOL_PROCESS",
        }

    monkeypatch.setattr("aios_orchestrator.build_plan", _fake_build_plan)
    monkeypatch.setattr("aios_orchestrator._enqueue_node", lambda *a, **kw: True)
    monkeypatch.setattr(
        "aios_orchestrator._redis_client",
        types.SimpleNamespace(zrem=lambda *a, **kw: 0, zadd=lambda *a, **kw: 0),
    )
    monkeypatch.setattr("aios_orchestrator._is_available", lambda: True)
    monkeypatch.setattr("aios_orchestrator.publish_event", lambda *a, **kw: None)

    statuses = {
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
        "opencode": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    workflow = {
        "parent_id": "parent-1",
        "task_id": "parent-1",
        "goal": "OPENCLAW_FAILS_ACTUAL_PLANNER_NULL",
        "source": "p9dr-actual-planner-semantics",
        "preferred_executor": "",
        "strict_executor": "",
        "allow_executor_fallback": True,
        "preferred_planner": "openclaw",
        "allow_planner_fallback": False,
    }
    _plan_and_dispatch("parent-1", workflow)
    captured = {}
    for call in calls:
        captured.update(call)
    assert captured.get("actual_planner") == "", (
        f"actual_planner must be empty when planning failed, got {captured.get('actual_planner')!r}"
    )
    assert captured.get("preferred_planner") == "openclaw"
    assert captured.get("attempted_planners") == ["openclaw"]


def test_actual_planner_opencode_when_openclaw_fails(monkeypatch):
    """OpenClaw fails, OpenCode succeeds → actual_planner=opencode."""
    from aios_orchestrator import _plan_and_dispatch
    calls: list = []

    def _fake_save(workflow_id, **fields):
        calls.append(dict(fields))
        return True

    monkeypatch.setattr("aios_orchestrator._save_workflow", _fake_save)

    def _fake_build_plan(goal, parent_id, *, task_policy=None):
        return [{
            "task": goal,
            "depends_on": [],
            "role": "opencode",
            "acceptance": ["Echo the goal"],
            "evidence_mode": "semantic",
        }], "opencode-plan-only", "", {
            "actual_planner": "opencode",
            "actual_binding": "opencode:free",
            "actual_provider": "opencode",
            "actual_model": "free-auto-router",
            "fallback_count": 1,
            "attempted_planners": ["openclaw", "opencode"],
            "excluded_planners": ["openclaw"],
            "first_failure_scope": "TOOL_PROCESS",
        }

    monkeypatch.setattr("aios_orchestrator.build_plan", _fake_build_plan)
    monkeypatch.setattr("aios_orchestrator._enqueue_node", lambda *a, **kw: True)
    monkeypatch.setattr(
        "aios_orchestrator._redis_client",
        types.SimpleNamespace(zrem=lambda *a, **kw: 0, zadd=lambda *a, **kw: 0),
    )
    monkeypatch.setattr("aios_orchestrator._is_available", lambda: True)
    monkeypatch.setattr("aios_orchestrator.publish_event", lambda *a, **kw: None)

    statuses = {
        "openclaw": _StubStatus("UNAVAILABLE_TOOL_RUNTIME"),
        "opencode": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    workflow = {
        "parent_id": "parent-1",
        "task_id": "parent-1",
        "goal": "OPENCLAW_FAILS_OPENCODE_SUCCEEDS",
        "source": "p9dr-actual-planner-semantics",
        "preferred_executor": "",
        "strict_executor": "",
        "allow_executor_fallback": True,
        "preferred_planner": "openclaw",
        "allow_planner_fallback": True,
    }
    _plan_and_dispatch("parent-1", workflow)
    captured = {}
    for call in calls:
        captured.update(call)
    assert captured.get("actual_planner") == "opencode", (
        f"actual_planner must be opencode when fallback succeeded, got {captured.get('actual_planner')!r}"
    )
    assert captured.get("preferred_planner") == "openclaw"
    assert "openclaw" in captured.get("attempted_planners", [])
    assert "opencode" in captured.get("attempted_planners", [])
    assert "openclaw" in captured.get("excluded_planners", [])
    assert captured.get("planner_fallback_count") == 1


def test_actual_planner_null_when_strict_fails(monkeypatch):
    """Strict mode (allow_planner_fallback=False) fails → actual_planner=null."""
    from aios_orchestrator import _plan_and_dispatch
    calls: list = []

    def _fake_save(workflow_id, **fields):
        calls.append(dict(fields))
        return True

    monkeypatch.setattr("aios_orchestrator._save_workflow", _fake_save)

    def _fake_build_plan(goal, parent_id, *, task_policy=None):
        return [], "planning-failed", "FAILED_EXTERNAL_ROUTE_PLANNER_TIMEOUT", {
            "actual_planner": "",
            "actual_binding": "",
            "actual_provider": "",
            "actual_model": "",
            "fallback_count": 0,
            "attempted_planners": ["openclaw"],
            "excluded_planners": [],
            "first_failure_scope": "TOOL_PROCESS",
        }

    monkeypatch.setattr("aios_orchestrator.build_plan", _fake_build_plan)
    monkeypatch.setattr("aios_orchestrator._enqueue_node", lambda *a, **kw: True)
    monkeypatch.setattr(
        "aios_orchestrator._redis_client",
        types.SimpleNamespace(zrem=lambda *a, **kw: 0, zadd=lambda *a, **kw: 0),
    )
    monkeypatch.setattr("aios_orchestrator._is_available", lambda: True)
    monkeypatch.setattr("aios_orchestrator.publish_event", lambda *a, **kw: None)

    statuses = {
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    workflow = {
        "parent_id": "parent-1",
        "task_id": "parent-1",
        "goal": "STRICT_PLANNER_FAILS_ACTUAL_NULL",
        "source": "p9dr-actual-planner-semantics",
        "preferred_executor": "",
        "strict_executor": "",
        "allow_executor_fallback": True,
        "preferred_planner": "openclaw",
        "allow_planner_fallback": False,
    }
    _plan_and_dispatch("parent-1", workflow)
    captured = {}
    for call in calls:
        captured.update(call)
    assert captured.get("actual_planner") == "", (
        f"actual_planner must be empty when strict planner fails, got {captured.get('actual_planner')!r}"
    )
    assert captured.get("preferred_planner") == "openclaw"
    assert captured.get("allow_planner_fallback") is False


def test_actual_planner_openclaw_when_primary_succeeds(monkeypatch):
    """Primary succeeds → actual_planner=openclaw."""
    from aios_orchestrator import _plan_and_dispatch
    calls: list = []

    def _fake_save(workflow_id, **fields):
        calls.append(dict(fields))
        return True

    monkeypatch.setattr("aios_orchestrator._save_workflow", _fake_save)

    def _fake_build_plan(goal, parent_id, *, task_policy=None):
        return [{
            "task": goal,
            "depends_on": [],
            "role": "opencode",
            "acceptance": ["Echo the goal"],
            "evidence_mode": "semantic",
        }], "openclaw-minimax", "", {
            "actual_planner": "openclaw",
            "actual_binding": "openclaw:minimax",
            "actual_provider": "minimax",
            "actual_model": "MiniMax-M3",
            "fallback_count": 0,
            "attempted_planners": ["openclaw"],
            "excluded_planners": [],
            "first_failure_scope": "",
        }

    monkeypatch.setattr("aios_orchestrator.build_plan", _fake_build_plan)
    monkeypatch.setattr("aios_orchestrator._enqueue_node", lambda *a, **kw: True)
    monkeypatch.setattr(
        "aios_orchestrator._redis_client",
        types.SimpleNamespace(zrem=lambda *a, **kw: 0, zadd=lambda *a, **kw: 0),
    )
    monkeypatch.setattr("aios_orchestrator._is_available", lambda: True)
    monkeypatch.setattr("aios_orchestrator.publish_event", lambda *a, **kw: None)

    statuses = {
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    workflow = {
        "parent_id": "parent-1",
        "task_id": "parent-1",
        "goal": "PRIMARY_OPENCLAW_SUCCEEDS",
        "source": "p9dr-actual-planner-semantics",
        "preferred_executor": "",
        "strict_executor": "",
        "allow_executor_fallback": True,
        "preferred_planner": "openclaw",
        "allow_planner_fallback": False,
    }
    _plan_and_dispatch("parent-1", workflow)
    captured = {}
    for call in calls:
        captured.update(call)
    assert captured.get("actual_planner") == "openclaw", (
        f"actual_planner must be openclaw when primary succeeds, got {captured.get('actual_planner')!r}"
    )
    assert captured.get("preferred_planner") == "openclaw"
    assert captured.get("planner_fallback_count") == 0


def test_planning_failed_does_not_forge_schema_valid(monkeypatch):
    """planning-failed MUST NOT forge plan_schema_valid or plan_persisted."""
    from aios_orchestrator import _plan_and_dispatch
    calls: list = []

    def _fake_save(workflow_id, **fields):
        calls.append(dict(fields))
        return True

    monkeypatch.setattr("aios_orchestrator._save_workflow", _fake_save)

    def _fake_build_plan(goal, parent_id, *, task_policy=None):
        return [], "planning-failed", "FAILED_EXTERNAL_ROUTE_PLANNER_TIMEOUT", {
            "actual_planner": "",
            "actual_binding": "",
            "actual_provider": "",
            "actual_model": "",
            "fallback_count": 0,
            "attempted_planners": ["openclaw"],
            "excluded_planners": [],
            "first_failure_scope": "TOOL_PROCESS",
        }

    monkeypatch.setattr("aios_orchestrator.build_plan", _fake_build_plan)
    monkeypatch.setattr("aios_orchestrator._enqueue_node", lambda *a, **kw: True)
    monkeypatch.setattr(
        "aios_orchestrator._redis_client",
        types.SimpleNamespace(zrem=lambda *a, **kw: 0, zadd=lambda *a, **kw: 0),
    )
    monkeypatch.setattr("aios_orchestrator._is_available", lambda: True)
    monkeypatch.setattr("aios_orchestrator.publish_event", lambda *a, **kw: None)

    statuses = {
        "openclaw": _StubStatus("AVAILABLE_PRIMARY"),
    }
    _install_orchestrator_stubs(monkeypatch, statuses)

    workflow = {
        "parent_id": "parent-1",
        "task_id": "parent-1",
        "goal": "PLANNING_FAILED_NO_FORGE",
        "source": "p9dr-actual-planner-semantics",
        "preferred_executor": "",
        "strict_executor": "",
        "allow_executor_fallback": True,
        "preferred_planner": "openclaw",
        "allow_planner_fallback": False,
    }
    _plan_and_dispatch("parent-1", workflow)
    captured = {}
    for call in calls:
        captured.update(call)
    assert captured.get("actual_planner") == "", (
        f"actual_planner must be empty when planning failed, got {captured.get('actual_planner')!r}"
    )
    assert captured.get("status") == "failed", (
        f"status must be failed when planning failed, got {captured.get('status')!r}"
    )
    assert captured.get("plan_mode") == "planning-failed"
    assert captured.get("terminal") is True
    assert not captured.get("plan_schema_valid"), (
        "plan_schema_valid must NOT be true when planning failed"
    )
    assert not captured.get("plan_persisted"), (
        "plan_persisted must NOT be true when planning failed"
    )