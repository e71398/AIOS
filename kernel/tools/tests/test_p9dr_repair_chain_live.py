#!/usr/bin/env python3
"""AIOS P9D-R-live — Repair-Chain Coverage Tests.

Validates the *runtime* repair chain that was previously broken at
the dispatch_claim_timeout → repair boundary.

The contract under test:

    primary tool daemon down
      → child queued (never claimed)
      → 60+ s dispatch_claim_timeout
      → _expire_child_for_repair marks node "repairing"
        (NOT "failed")
      → next process_workflow pass sees "repairing"
      → _repair_node is called → attempt advances
      → child re-enqueued with the next executor
      → node completed

Tests use the in-memory ``_redis_client`` so they never depend on
running executor daemons.  They assert:
  * ``_expire_child_for_repair`` sets ``status=repairing``, not
    ``"failed"``.
  * ``_repair_node`` advances ``attempt`` exactly once per call.
  * ``MAX_REPAIRS`` produces a terminal ``failed`` with
    ``repair_exhausted``.
  * Strict executor mode does not fall back.
  * P9F canonical claim is invariant under repeated enqueue.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any, Dict
from unittest.mock import patch

import pytest

TOOLS = "${AIOS_HOME}/kernel/tools"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


@pytest.fixture
def orch():
    mod = importlib.import_module("aios_orchestrator")
    return mod


@pytest.fixture
def clean_redis():
    """Clean all aios:orchestrator:* and aios:bus:* keys for
    pristine test isolation.  Tests that depend on a clean state
    SHOULD take this fixture."""
    import redis as _redis
    import os
    host = os.environ.get("AIOS_REDIS_HOST", "127.0.0.1")
    port = int(os.environ.get("AIOS_REDIS_PORT", "6379"))
    db = int(os.environ.get("AIOS_REDIS_DB", "0"))
    client = _redis.Redis(host=host, port=port, db=db)
    for prefix in ("aios:orchestrator:*", "aios:bus:*"):
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor=cursor, match=prefix, count=500)
            if keys:
                client.delete(*keys)
            if cursor == 0:
                break
    yield


def _save_workflow(wf_helper, parent_id, **fields):
    return wf_helper._save_workflow(parent_id, **fields)


def _make_minimal_workflow(
    parent_id: str,
    *,
    strict_executor: str = "",
    allow_fallback: bool = True,
    initial_role: str = "opencode",
    initial_assigned: str = "",
    initial_attempt: int = 0,
    initial_status: str = "planned",
    initial_actual_executor: str = "",
    initial_attempted_executors=None,
):
    """Synthesize a single-node workflow."""
    node = {
        "index": 0,
        "task": "return ok",
        "depends_on": [],
        "role": initial_role,
        "acceptance": ["Exact string match."],
        "evidence_mode": "semantic",
        "status": initial_status,
        "task_id": "",
        "attempt": initial_attempt,
        "assigned_executor": initial_assigned,
        "actual_executor": initial_actual_executor,
        "attempted_executors": list(initial_attempted_executors or []),
        "result": "",
        "verification": {},
        "error": "",
    }
    return {
        "parent_id": parent_id,
        "goal": "return ok",
        "source": "api",
        "sender_id": "p9dr-repair-live",
        "allow_executor_fallback": allow_fallback,
        "preferred_executor": "",
        "strict_executor": strict_executor,
        "repair_count": 0,
        "user_verification_criteria": [],
        "capability_overlay": {},
        "blocked_tools": "[]",
        "blocked_resources": "[]",
        "blocked_model_bindings": "[]",
        "blocked_planner_tools": "[]",
        "blocked_reviewer_tools": "[]",
        "nodes": [node],
    }


def _persist_workflow(orch, parent_id, workflow):
    _save_workflow(orch, parent_id,
        goal="return ok",
        source="api",
        status="running",
        nodes=workflow["nodes"],
        allow_executor_fallback=workflow["allow_executor_fallback"],
        preferred_executor=workflow["preferred_executor"],
        strict_executor=workflow["strict_executor"],
        repair_count=0,
        capability_overlay={},
        blocked_tools="[]",
        blocked_resources="[]",
        blocked_model_bindings="[]",
        blocked_planner_tools="[]",
        blocked_reviewer_tools="[]",
    )
    return workflow


# ---------------------------------------------------------------------------
# §7.1 — _expire_child_for_repair sets status=repairing (NOT failed)
# ---------------------------------------------------------------------------

class TestDispatchClaimTimeoutEntersRepair:
    def test_expire_marks_node_repairing_not_failed(self, orch, clean_redis):
        """``_expire_child_for_repair`` MUST set ``status=repairing``
        (NOT ``status=failed``) so the next ``process_workflow`` pass
        observes the node and calls ``_repair_node``."""
        wf = _make_minimal_workflow(
            "p9dr-test-repair-1",
            strict_executor="",
            allow_fallback=True,
        )
        _persist_workflow(orch, wf["parent_id"], wf)
        with patch.object(orch, "_is_executor_available", return_value=True):
            assert orch._enqueue_node(wf["parent_id"], wf, wf["nodes"][0])
        task_id = wf["nodes"][0]["task_id"]
        wf["nodes"][0]["queued_at"] = "2026-08-02T00:00:00+00:00"
        _save_workflow(orch, wf["parent_id"], nodes=wf["nodes"])
        child_state = {
            "task_id": task_id,
            "status": "pending",
            "executor": "",
            "ts_queued": "2026-08-02T00:00:00+00:00",
        }
        orch._expire_child_for_repair(
            wf["parent_id"], wf, wf["nodes"][0], child_state,
            "dispatch_claim_timeout:120s",
        )
        node = wf["nodes"][0]
        assert node["status"] == "repairing", (
            "node must be in non-terminal repairing state, not failed"
        )
        assert node["status"] != "failed"
        assert "opencode" in node["attempted_executors"]
        assert node["task_id"] == ""
        assert node["repair_reason"] == "dispatch_claim_timeout:120s"
        assert node["repair_started_at"]
        assert node["repair_failure_scope"] == "TOOL_PROCESS"


# ---------------------------------------------------------------------------
# §7.2 — attempt and generation advance deterministically
# ---------------------------------------------------------------------------

class TestAttemptGenerationAdvance:
    def test_repair_node_increments_attempt(self, orch, clean_redis):
        """When ``_repair_node`` is called, ``attempt`` MUST advance
        exactly once per call."""
        wf = _make_minimal_workflow(
            "p9dr-test-repair-2",
            allow_fallback=True,
            initial_status="repairing",
            initial_actual_executor="opencode",
            initial_attempted_executors=["opencode"],
        )
        wf["nodes"][0]["error"] = "dispatch_claim_timeout:60s"
        wf["nodes"][0]["repair_reason"] = "dispatch_claim_timeout:60s"
        _persist_workflow(orch, wf["parent_id"], wf)
        with patch.object(orch, "_enqueue_node", return_value=True) as eq:
            orch._repair_node(
                wf["parent_id"], wf, wf["nodes"][0],
                "dispatch_claim_timeout:60s", "", "opencode",
            )
        node = wf["nodes"][0]
        assert node["attempt"] == 1, (
            "attempt must advance from 0 to 1 exactly once"
        )
        assert wf["repair_count"] == 1
        assert "opencode" in node["attempted_executors"]
        assert eq.called
        eq_kwargs = eq.call_args.kwargs
        assert "opencode" in eq_kwargs.get("exclude_executors", [])


class TestMaxRepairs:
    def test_max_repairs_terminal_failure(self, orch, clean_redis):
        """When ``attempt >= MAX_REPAIRS`` and the node is still in
        ``repairing`` state, the next process pass MUST promote the
        node to terminal ``failed`` with ``repair_exhausted``."""
        wf = _make_minimal_workflow(
            "p9dr-test-repair-max",
            allow_fallback=True,
            initial_status="repairing",
            initial_actual_executor="opencode",
            initial_attempted_executors=["opencode", "codex"],
            initial_attempt=orch.MAX_REPAIRS,
        )
        wf["nodes"][0]["error"] = "dispatch_claim_timeout:60s"
        wf["nodes"][0]["repair_reason"] = "dispatch_claim_timeout:60s"
        _persist_workflow(orch, wf["parent_id"], wf)
        with patch.object(orch, "_repair_node", return_value=None):
            orch.process_workflow(wf["parent_id"])
        refreshed = orch.get_workflow(wf["parent_id"])
        node_after = refreshed["nodes"][0]
        assert node_after["status"] == "failed"
        assert "repair_exhausted" in node_after["error"]


# ---------------------------------------------------------------------------
# §7.3 — Second tool takes over
# ---------------------------------------------------------------------------

class TestSecondToolTakesOver:
    def test_exclude_opencode_picks_codex(self, orch, clean_redis):
        """With ``opencode`` in ``attempted_executors`` and ``codex``
        available, ``choose_executor`` MUST return codex."""
        with patch.object(
            orch, "_is_executor_available",
            side_effect=lambda n, capability_overlay=None:
            n == "codex",
        ):
            chosen = orch.choose_executor("opencode", exclude={"opencode", "claude"})
        assert chosen == "codex", (
            f"after opencode and claude are excluded, choose_executor "
            f"must return codex, got {chosen!r}"
        )

    def test_exclude_opencode_and_codex_picks_claude(self, orch, clean_redis):
        """With ``opencode`` and ``codex`` both excluded and ``claude``
        available, ``choose_executor`` MUST return claude."""
        with patch.object(
            orch, "_is_executor_available",
            side_effect=lambda n, capability_overlay=None:
            n == "claude",
        ):
            chosen = orch.choose_executor(
                "opencode", exclude={"opencode", "codex"},
            )
        assert chosen == "claude"


# ---------------------------------------------------------------------------
# §7.4 — No duplicate child
# ---------------------------------------------------------------------------

class TestNoDuplicateChild:
    def test_repair_node_does_not_mint_duplicate_child(self, orch, clean_redis):
        """Two claims for the same (parent_id, node_index, generation)
        MUST yield the same canonical task_id, never a new UUID."""
        canonical_tid_first, was_new = orch._claim_canonical_child(
            "p9dr-test-no-dup", 0, 0,
        )
        assert was_new, "first claim must be new"
        canonical_tid_second, was_new_second = orch._claim_canonical_child(
            "p9dr-test-no-dup", 0, 0,
        )
        assert not was_new_second, "second claim must NOT be new"
        assert canonical_tid_first == canonical_tid_second, (
            "second claim must return the original canonical task_id"
        )
        orch._release_canonical_child("p9dr-test-no-dup", 0, 0)


# ---------------------------------------------------------------------------
# §7.7 — Strict no-fallback still blocks
# ---------------------------------------------------------------------------

class TestStrictNoFallback:
    def test_strict_executor_does_not_fallback(self, orch, clean_redis):
        """``strict_executor + allow_executor_fallback=False`` MUST
        surface ``strict_executor_unavailable`` instead of silently
        picking a different executor."""
        wf = _make_minimal_workflow(
            "p9dr-test-strict",
            strict_executor="opencode",
            allow_fallback=False,
        )
        _persist_workflow(orch, wf["parent_id"], wf)
        with patch.object(
            orch, "_is_executor_available", return_value=False,
        ):
            ok = orch._enqueue_node(
                wf["parent_id"], wf, wf["nodes"][0],
            )
        assert not ok
        assert wf["nodes"][0]["status"] == "failed"
        assert "strict_executor_unavailable" in wf["nodes"][0]["error"]


# ---------------------------------------------------------------------------
# §7.8 — P9F idempotent enqueue still works
# ---------------------------------------------------------------------------

class TestP9FIdempotentEnqueue:
    def test_repeated_enqueue_reuses_canonical_child(self, orch, clean_redis):
        """Two enqueue_node calls with the same (parent, node, gen)
        MUST yield the same canonical task_id, and the second call
        MUST NOT re-enqueue."""
        wf = _make_minimal_workflow(
            "p9dr-test-p9f",
            allow_fallback=True,
        )
        _persist_workflow(orch, wf["parent_id"], wf)
        with patch.object(
            orch, "_is_executor_available", return_value=True,
        ):
            ok1 = orch._enqueue_node(
                wf["parent_id"], wf, wf["nodes"][0],
            )
            original_tid = wf["nodes"][0]["task_id"]
            ok2 = orch._enqueue_node(
                wf["parent_id"], wf, wf["nodes"][0],
            )
        assert ok1 and ok2
        assert wf["nodes"][0]["task_id"] == original_tid, (
            "P9F canonical-claim MUST yield the same task_id on "
            "a repeat enqueue of the same generation"
        )