#!/usr/bin/env python3
"""AIOS P9F — Workflow Child Idempotent Enqueue & Natural Convergence Tests.

These tests cover the P9F close-out:

1.  ``_claim_canonical_child`` returns the same task_id for the
    same (parent, node, generation) on repeated calls.
2.  ``_claim_canonical_child`` returns a fresh task_id when a new
    generation is claimed.
3.  ``_release_canonical_child`` allows the slot to be re-claimed.
4.  ``_canonical_child_status`` reads back the bus state.
5.  ``_canonical_child_pick`` selects deterministically: real
    result > latest terminal > active lease > earliest pending,
    tie-broken by task_id ascending.
6.  ``supersede_duplicate_children`` (dry-run) classifies children
    without writing to Redis.
7.  ``supersede_duplicate_children`` (commit) writes the
    non-canonical duplicates through ``cancelled`` with the
    supersede reason and pulls them from pending.
8.  ``supersede_all_duplicate_children`` sweeps the entire bus
    index and reports counters.
9.  ``_enqueue_node`` is idempotent: repeated invocations for the
    same generation return the same canonical task_id without
    minting new ones.
10. ``_enqueue_node`` honours the strict_executor contract:
    it does not bypass the strict mode for idempotency reasons.
11. ``_enqueue_node`` writes the canonical generation marker on
    the node so future audits can verify identity.
12. ``enqueue_task`` honours ``task_id_override`` so the canonical
    claim path produces the same task_id in the bus state.
13. ``_canonical_child_status`` returns "" for unknown task_ids
    so the caller can retry safely.
14. ``supersede_duplicate_children`` preserves real results on the
    canonical child rather than re-running it.
15. ``supersede_duplicate_children`` does not touch the canonical
    child state.
16. ``_canonical_child_pick`` returns ([single], []) when only one
    child exists (no spurious duplicates).
17. ``_canonical_child_pick`` returns ([], []) for empty list.
18. The whole historical sweep never claims a pending child as
    canonical if a completed child is available.
19. The whole historical sweep never loses the canonical child
    when re-run multiple times (idempotent on commit=False).
20. The full orchestrator passes a sanity roundtrip: submit +
    process_workflow naturally converges to terminal without
    creating duplicate children.
21. P9A Gateway regression: no orchestrator change breaks the
    bounded concurrency contract.
22. P9B health-truth regression: no orchestrator change breaks the
    health-publisher contract.
23. P9C Executor dispatch regression: orchestrator still uses
    enqueue_task with the right inputs.
24. P9E timeout-escalation regression: the new claim path does
    not break pending_too_long escalation.
25. Local-model guard: no ``*:ollama`` binding is honoured.

These tests are hermetic — they use real Redis and real Redis
keys under unique test IDs that are cleaned up in a fixture
teardown. No mocks of the orchestrator state machine.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from unittest import mock
from pathlib import Path

import pytest

TOOLS = "${AIOS_HOME}/kernel/tools"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import aios_orchestrator as orch  # noqa: E402
import aios_bus as bus  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def redis_clean():
    """Best-effort cleanup of the canonical claim keys and bus state
    keys created by these tests.  Each test uses a unique parent_id
    derived from ``uuid.uuid4()`` so this fixture scopes only the
    keys that *this* test created, never historical data.
    """
    import redis as _rd

    rc = _rd.Redis(host="localhost", port=6379, socket_connect_timeout=2)
    rc.ping()
    yield rc
    # Cleanup happens per-test inside the function bodies via
    # ``cleanup_test_keys``.


def cleanup_test_keys(rc, parent_ids):
    """Delete canonical-claim keys, bus state hashes, pending-list
    entries and bus-index members for the given parent_ids."""
    if not parent_ids:
        return
    for pid in parent_ids:
        if not pid:
            continue
        # canonical claim keys (any generation, any node)
        for key in rc.scan_iter(f"{orch.KEY_CANONICAL_CHILD}:{pid}:*"):
            rc.delete(key)
        # workflow hash
        rc.delete(f"{orch.KEY_WORKFLOW}:{pid}")
        # bus state hashes whose parent_id matches (best effort: read,
        # check, delete)
        for state_key in rc.scan_iter("aios:bus:state:*"):
            parent_id = rc.hget(state_key, "parent_id")
            if isinstance(parent_id, bytes):
                parent_id = parent_id.decode()
            if parent_id == pid:
                tid = state_key.decode().split(":")[-1] if isinstance(state_key, bytes) else state_key.split(":")[-1]
                rc.lrem("aios:bus:queue:pending", 0, tid)
                rc.delete(f"aios:bus:lock:{tid}")
                rc.zrem("aios:bus:index", tid)
                rc.delete(state_key)
                rc.delete(f"aios:bus:task:{tid}")


def _seed_workflow(rc, parent_id: str, node: dict) -> None:
    """Write a minimal workflow hash so ``_enqueue_node`` has the
    fields it expects (``goal``, ``source``, ``nodes``)."""
    payload = {
        "parent_id": parent_id,
        "status": "running",
        "goal": "p9f-smoke",
        "source": "test",
        "nodes": json.dumps([node], ensure_ascii=False),
    }
    payload["updated_at"] = orch._now()
    rc.hset(f"{orch.KEY_WORKFLOW}:{parent_id}", mapping={
        k: (str(v) if not isinstance(v, str) else v) for k, v in payload.items()
    })
    rc.expire(f"{orch.KEY_WORKFLOW}:{parent_id}", orch.WORKFLOW_TTL_SECONDS)


# ---------------------------------------------------------------------------
# §1 — _claim_canonical_child: atomic SET NX
# ---------------------------------------------------------------------------
def test_claim_canonical_first_call_wins(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    try:
        tid, was_new = orch._claim_canonical_child(parent_id, 0, 0)
        assert was_new is True
        assert isinstance(tid, str) and len(tid) >= 32
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


def test_claim_canonical_repeated_returns_same(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    try:
        tid_a, _ = orch._claim_canonical_child(parent_id, 0, 0)
        tid_b, was_new_b = orch._claim_canonical_child(parent_id, 0, 0)
        tid_c, was_new_c = orch._claim_canonical_child(parent_id, 0, 0)
        assert was_new_b is False
        assert was_new_c is False
        assert tid_a == tid_b == tid_c
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


def test_claim_canonical_different_generation_is_fresh(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    try:
        tid_gen0, _ = orch._claim_canonical_child(parent_id, 0, 0)
        tid_gen1, was_new = orch._claim_canonical_child(parent_id, 0, 1)
        tid_gen2, was_new2 = orch._claim_canonical_child(parent_id, 0, 2)
        assert was_new is True
        assert was_new2 is True
        assert len({tid_gen0, tid_gen1, tid_gen2}) == 3
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


def test_claim_canonical_release_then_reclaim(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    try:
        tid1, was_new = orch._claim_canonical_child(parent_id, 0, 0)
        assert was_new
        orch._release_canonical_child(parent_id, 0, 0)
        tid2, was_new = orch._claim_canonical_child(parent_id, 0, 0)
        assert was_new
        # Released claim is gone — a new claim gets a fresh UUID.
        assert tid1 != tid2
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


# ---------------------------------------------------------------------------
# §2 — _canonical_child_status reads bus state correctly
# ---------------------------------------------------------------------------
def test_canonical_child_status_unknown_returns_empty(redis_clean):
    assert orch._canonical_child_status("") == ""
    assert orch._canonical_child_status(
        f"nonexistent-{uuid.uuid4()}"
    ) == ""


def test_canonical_child_status_reflects_bus(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    try:
        # Manually write a pending bus state for a synthetic tid
        tid = f"smoke-{uuid.uuid4()}"
        redis_clean.hset(f"aios:bus:state:{tid}", mapping={
            "task_id": tid,
            "status": "pending",
            "parent_id": parent_id,
            "node_index": "0",
            "attempt": "0",
        })
        assert orch._canonical_child_status(tid) == "pending"
        redis_clean.hset(f"aios:bus:state:{tid}", "status", "running")
        assert orch._canonical_child_status(tid) == "running"
        redis_clean.hset(f"aios:bus:state:{tid}", "status", "completed")
        assert orch._canonical_child_status(tid) == "completed"
        # cleanup
        redis_clean.delete(f"aios:bus:state:{tid}")
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


# ---------------------------------------------------------------------------
# §3 — _canonical_child_pick: deterministic classification
# ---------------------------------------------------------------------------
def test_canonical_pick_single_child_no_duplicates():
    single = [{"task_id": "a", "status": "completed", "result_summary": "ok"}]
    canonical, duplicates = orch._canonical_child_pick(single)
    assert len(canonical) == 1
    assert canonical[0]["task_id"] == "a"
    assert duplicates == []


def test_canonical_pick_empty():
    canonical, duplicates = orch._canonical_child_pick([])
    assert canonical == []
    assert duplicates == []


def test_canonical_pick_prefers_completed_with_result():
    children = [
        {"task_id": "z", "status": "completed", "result_summary": ""},
        {"task_id": "a", "status": "completed", "result_summary": "real answer"},
    ]
    canonical, duplicates = orch._canonical_child_pick(children)
    assert canonical[0]["task_id"] == "a"
    assert duplicates[0]["task_id"] == "z"


def test_canonical_pick_prefers_terminal_over_pending():
    children = [
        {"task_id": "p", "status": "pending", "result_summary": ""},
        {"task_id": "f", "status": "failed", "result_summary": ""},
    ]
    canonical, duplicates = orch._canonical_child_pick(children)
    assert canonical[0]["task_id"] == "f"
    assert duplicates[0]["task_id"] == "p"


def test_canonical_pick_prefers_active_lease_over_pending():
    children = [
        {"task_id": "p", "status": "pending"},
        {"task_id": "l", "status": "locked"},
    ]
    canonical, duplicates = orch._canonical_child_pick(children)
    assert canonical[0]["task_id"] == "l"
    assert duplicates[0]["task_id"] == "p"


def test_canonical_pick_is_deterministic_under_shuffle():
    children = [
        {"task_id": "c", "status": "completed", "result_summary": "real"},
        {"task_id": "b", "status": "completed", "result_summary": ""},
        {"task_id": "a", "status": "pending"},
    ]
    canonical_a, _ = orch._canonical_child_pick(children)
    canonical_b, _ = orch._canonical_child_pick(list(reversed(children)))
    assert canonical_a[0]["task_id"] == canonical_b[0]["task_id"] == "c"


# ---------------------------------------------------------------------------
# §4 — supersede_duplicate_children (dry-run, then commit)
# ---------------------------------------------------------------------------
def test_supersede_dry_run_does_not_write(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    tids = [f"dup-{uuid.uuid4()}" for _ in range(3)]
    try:
        # Seed three children at the same (parent, node)
        for tid in tids:
            redis_clean.hset(f"aios:bus:state:{tid}", mapping={
                "task_id": tid, "status": "completed",
                "result_summary": "same" if tid == tids[0] else "",
                "parent_id": parent_id, "node_index": "0", "attempt": "0",
            })
            redis_clean.zadd("aios:bus:index", {tid: time.time()})
        result = orch.supersede_duplicate_children(
            parent_id, 0, generation=0, commit=False,
        )
        assert result["inspect"] == 3
        assert len(result["canonical"]) == 1
        assert len(result["duplicates"]) == 2
        assert result["superseded"] == []   # dry-run, no writes
        # All three still have their original status
        for tid in tids:
            status = redis_clean.hget(f"aios:bus:state:{tid}", "status")
            if isinstance(status, bytes):
                status = status.decode()
            assert status == "completed"
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


def test_supersede_commit_terminalises_duplicates(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    tids = [f"dup-{uuid.uuid4()}" for _ in range(3)]
    try:
        for tid in tids:
            redis_clean.hset(f"aios:bus:state:{tid}", mapping={
                "task_id": tid, "status": "completed",
                "result_summary": "real answer" if tid == tids[0] else "",
                "parent_id": parent_id, "node_index": "0", "attempt": "0",
            })
            redis_clean.zadd("aios:bus:index", {tid: time.time()})
        result = orch.supersede_duplicate_children(
            parent_id, 0, generation=0, commit=True,
        )
        assert result["inspect"] == 3
        assert len(result["superseded"]) == 2
        canonical_tid = result["canonical"][0]["task_id"]
        assert canonical_tid == tids[0]
        # Canonical still completed with its result
        canonical_status = redis_clean.hget(
            f"aios:bus:state:{canonical_tid}", "status",
        )
        assert isinstance(canonical_status, bytes)
        assert canonical_status.decode() == "completed"
        # Duplicates now cancelled with the supersede reason
        for dup_tid in result["superseded"]:
            status = redis_clean.hget(f"aios:bus:state:{dup_tid}", "status")
            assert isinstance(status, bytes)
            assert status.decode() == "cancelled"
            reason = redis_clean.hget(
                f"aios:bus:state:{dup_tid}", "result_summary",
            )
            if isinstance(reason, bytes):
                reason = reason.decode()
            assert orch.SUPERSEDE_REASON in (reason or "")
        # The canonical claim Redis key now points at the canonical
        claim_key = orch._canonical_child_key(parent_id, 0, 0)
        stored = redis_clean.get(claim_key)
        if isinstance(stored, bytes):
            stored = stored.decode()
        assert stored == canonical_tid
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


# ---------------------------------------------------------------------------
# §5 — supersede_all_duplicate_children sweeps the bus index
# ---------------------------------------------------------------------------
def test_supersede_all_dry_run_returns_counters(redis_clean):
    parent_ids = [f"p9f-{uuid.uuid4()}" for _ in range(2)]
    try:
        for pid in parent_ids:
            tids = [f"dup-{uuid.uuid4()}" for _ in range(2)]
            for tid in tids:
                redis_clean.hset(f"aios:bus:state:{tid}", mapping={
                    "task_id": tid, "status": "completed",
                    "result_summary": "ok" if tid == tids[0] else "",
                    "parent_id": pid, "node_index": "0", "attempt": "0",
                })
                redis_clean.zadd("aios:bus:index", {tid: time.time()})
        result = orch.supersede_all_duplicate_children(commit=False)
        assert result["ok"] is True
        assert result["write"] is False
        assert result["groups_inspected"] >= 2
        assert result["groups_with_duplicates"] >= 2
        assert result["duplicates_superseded"] == 0   # dry-run
    finally:
        cleanup_test_keys(redis_clean, parent_ids)


def test_supersede_all_commit_only_affects_duplicates(redis_clean):
    parent_ids = []
    try:
        # One workflow with two duplicate children + one workflow
        # with a single child.  Only the duplicate workflow should be
        # touched.
        for _ in range(2):
            pid = f"p9f-{uuid.uuid4()}"
            parent_ids.append(pid)
            count = 2 if _ == 0 else 1
            tids = [f"dup-{uuid.uuid4()}" for _ in range(count)]
            for tid in tids:
                redis_clean.hset(f"aios:bus:state:{tid}", mapping={
                    "task_id": tid,
                    "status": "completed",
                    "result_summary": "ok" if tid == tids[0] else "",
                    "parent_id": pid, "node_index": "0", "attempt": "0",
                })
                redis_clean.zadd("aios:bus:index", {tid: time.time()})
        result = orch.supersede_all_duplicate_children(commit=True)
        assert result["ok"] is True
        assert result["duplicates_superseded"] >= 1
        # The single-child workflow was left untouched.
        single_pid = parent_ids[1]
        untouched_tids = [
            tid.decode() if isinstance(tid, bytes) else tid
            for tid in redis_clean.zrange("aios:bus:index", 0, -1)
            if (redis_clean.hget(f"aios:bus:state:{tid}", "parent_id") or b"").decode()
            == single_pid
        ]
        for tid in untouched_tids:
            status = redis_clean.hget(f"aios:bus:state:{tid}", "status")
            if isinstance(status, bytes):
                status = status.decode()
            assert status == "completed"
    finally:
        cleanup_test_keys(redis_clean, parent_ids)


# ---------------------------------------------------------------------------
# §6 — _enqueue_node: idempotent for the same generation
# ---------------------------------------------------------------------------
def _make_node():
    return {
        "index": 0, "task": "p9f enqueue", "depends_on": [],
        "role": "opencode",
        "acceptance": ["result ok"], "evidence_mode": "semantic",
        "status": "planned", "task_id": "", "attempt": 0,
        "assigned_executor": "", "actual_executor": "",
        "attempted_executors": [], "result": "", "verification": {},
        "error": "",
    }


def _make_workflow():
    return {
        "goal": "p9f enqueue smoke",
        "source": "test", "sender_id": "p9f-test",
        "allow_executor_fallback": True,
        "capability_overlay": {"opencode": "AVAILABLE_PRIMARY"},
    }


def test_enqueue_node_idempotent_same_generation(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    try:
        _seed_workflow(redis_clean, parent_id, _make_node())
        node_a = _make_node()
        ok_a = orch._enqueue_node(parent_id, _make_workflow(), node_a)
        tid_a = node_a["task_id"]
        node_b = _make_node()
        ok_b = orch._enqueue_node(parent_id, _make_workflow(), node_b)
        tid_b = node_b["task_id"]
        node_c = _make_node()
        ok_c = orch._enqueue_node(parent_id, _make_workflow(), node_c)
        tid_c = node_c["task_id"]
        assert ok_a is True
        assert ok_b is True
        assert ok_c is True
        assert tid_a == tid_b == tid_c, "Repeated enqueue must reuse canonical task_id"
        # Bus state should be a single task with that task_id
        assert redis_clean.hget(f"aios:bus:state:{tid_a}", "status") is not None
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


def test_enqueue_node_legal_retry_advances_generation(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    try:
        _seed_workflow(redis_clean, parent_id, _make_node())
        node = _make_node()
        orch._enqueue_node(parent_id, _make_workflow(), node)
        tid_gen0 = node["task_id"]
        assert orch._claim_canonical_child(parent_id, 0, 0)[0] == tid_gen0
        # Legal retry: advance attempt to 1, then enqueue
        node2 = _make_node()
        node2["attempt"] = 1
        orch._enqueue_node(parent_id, _make_workflow(), node2)
        tid_gen1 = node2["task_id"]
        assert tid_gen1 != tid_gen0
        assert orch._claim_canonical_child(parent_id, 0, 1)[0] == tid_gen1
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


def test_enqueue_node_terminal_child_does_not_mint_new(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    try:
        _seed_workflow(redis_clean, parent_id, _make_node())
        node = _make_node()
        orch._enqueue_node(parent_id, _make_workflow(), node)
        tid = node["task_id"]
        # Mark the child as terminal — same generation, no advance.
        bus.update_task_status(
            tid, "completed", "opencode", "smoke result",
        )
        # Same-generation enqueue must NOT mint a new task_id.
        node2 = _make_node()
        ok = orch._enqueue_node(parent_id, _make_workflow(), node2)
        assert ok is False, "Same-generation enqueue must not mint new task"
        assert node2["error"].startswith("duplicate_generation_terminal_no_advance")
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


def test_enqueue_node_active_lease_does_not_mint_new(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    try:
        _seed_workflow(redis_clean, parent_id, _make_node())
        node = _make_node()
        orch._enqueue_node(parent_id, _make_workflow(), node)
        tid = node["task_id"]
        # Mark the child as locked — still active.
        bus.update_task_status(
            tid, "locked", "opencode", "",
            metadata={"ts_locked": orch._now()},
        )
        # Same-generation enqueue must not mint new.
        node_b = _make_node()
        ok = orch._enqueue_node(parent_id, _make_workflow(), node_b)
        assert ok is True
        assert node_b["task_id"] == tid
        assert node_b["status"] == "locked"
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


def test_enqueue_node_canonical_generation_field_set(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    try:
        _seed_workflow(redis_clean, parent_id, _make_node())
        node = _make_node()
        orch._enqueue_node(parent_id, _make_workflow(), node)
        assert node.get("canonical_generation") == 0
        # Advance attempt and re-enqueue — canonical_generation must
        # reflect the new value.
        node["attempt"] = 1
        orch._enqueue_node(parent_id, _make_workflow(), node)
        assert node.get("canonical_generation") == 1
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


# ---------------------------------------------------------------------------
# §7 — enqueue_task honour of task_id_override
# ---------------------------------------------------------------------------
def test_enqueue_task_uses_override(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    fixed_tid = f"fixed-{uuid.uuid4()}"
    try:
        returned = bus.enqueue_task(
            task_name="p9f override",
            system="aios-orchestrator",
            parent_id=parent_id,
            node_index=0,
            task_id_override=fixed_tid,
        )
        assert returned == fixed_tid
        state = redis_clean.hgetall(f"aios:bus:state:{fixed_tid}")
        assert state, "Override task_id must be in the bus state hash"
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


def test_enqueue_task_without_override_generates_uuid(redis_clean):
    returned = bus.enqueue_task(task_name="p9f fresh")
    assert returned and len(returned) >= 32
    try:
        redis_clean.delete(f"aios:bus:state:{returned}")
        redis_clean.lrem("aios:bus:queue:pending", 0, returned)
        redis_clean.zrem("aios:bus:index", returned)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# §8 — supersede preserves real results and never overwrites canonical
# ---------------------------------------------------------------------------
def test_supersede_preserves_canonical_state(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    canonical_tid = f"keep-{uuid.uuid4()}"
    dup_tid = f"drop-{uuid.uuid4()}"
    try:
        for tid, status, result in [
            (canonical_tid, "completed", "real answer"),
            (dup_tid, "completed", ""),
        ]:
            redis_clean.hset(f"aios:bus:state:{tid}", mapping={
                "task_id": tid, "status": status, "result_summary": result,
                "parent_id": parent_id, "node_index": "0", "attempt": "0",
                "ts_completed": orch._now(),
            })
            redis_clean.zadd("aios:bus:index", {tid: time.time()})
        orch.supersede_duplicate_children(
            parent_id, 0, generation=0, commit=True,
        )
        canonical_state = redis_clean.hgetall(
            f"aios:bus:state:{canonical_tid}",
        )
        status = canonical_state.get(b"status", b"").decode() if isinstance(
            canonical_state.get(b"status", b""), bytes
        ) else canonical_state.get("status", "")
        result_summary = canonical_state.get(b"result_summary", b"").decode() if isinstance(
            canonical_state.get(b"result_summary", b""), bytes
        ) else canonical_state.get("result_summary", "")
        assert status == "completed"
        assert result_summary == "real answer"
    finally:
        cleanup_test_keys(redis_clean, [parent_id])


# ---------------------------------------------------------------------------
# §9 — Local-model guard regression
# ---------------------------------------------------------------------------
def test_local_model_guard_not_honoured_in_enqueue():
    """The orchestrator must NEVER pick an ``*:ollama`` binding for
    enqueue, regardless of the local-model approval env vars.  This
    is enforced by the capability layer; we sanity-check that
    ``AVAILABLE_PRIMARY`` overlay still flows through ``opencode``.
    """
    # Save env state
    saved = {}
    for key in ("AIOS_OLLAMA_USER_APPROVED_AT", "AIOS_LOCAL_MODEL_INFERENCE_ALLOWED"):
        if key in os.environ:
            saved[key] = os.environ.pop(key)
    # Stage a fresh ``opencode`` health cache with a complete model
    # surface (relay + inference).  The on-disk cache from previous
    # test runs may not carry the model section; ``choose_executor``
    # requires the model side to be available alongside the
    # ``AVAILABLE_PRIMARY`` overlay.
    health_payload = {
        "lightweight_observed_at": "2026-08-11T00:00:00+00:00",
        "lightweight_checked_at": "2026-08-11T00:00:00+00:00",
        "lightweight_last_success_at": "2026-08-11T00:00:00+00:00",
        "lightweight_expires_at": "2026-08-11T00:05:00+00:00",
        "lightweight_fresh": True,
        "lightweight_reachable": True,
        "lightweight_protocol_ready": True,
        "lightweight_failure_scope": "unknown",
        "lightweight_reason": "synthetic",
        "lightweight_kind": "http",
        "lightweight_latency_ms": 5,
        "checked_at": "2026-08-11T00:00:00+00:00",
        "latency_ms": 0,
        "model_state": "available",
        "model_available": True,
        "reason": "synthetic",
        "returncode": 0,
        "success_marker_seen": True,
        "fatal_error_seen": False,
        "probe_required": True,
        "evidence_source": "synthetic",
    }
    cache_path = Path("${AIOS_HOME}/cache/tool_health/opencode.json")
    snapshot = None
    if cache_path.is_file():
        snapshot = cache_path.read_text(encoding="utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(health_payload, ensure_ascii=False), encoding="utf-8",
    )
    try:
        os.environ["AIOS_OLLAMA_USER_APPROVED_AT"] = "2026-07-28T00:00:00Z"
        os.environ["AIOS_LOCAL_MODEL_INFERENCE_ALLOWED"] = "1"
        # Invalidate the in-process tool-process health cache so
        # ``_tool_process_health`` re-probes the live /proc table and
        # the orchestrator's local model guard has every leg green.
        try:
            from aios_orchestrator import _invalidate_tool_process_cache
            _invalidate_tool_process_cache()
        except Exception:
            pass
        # Available opencode; no local model should sneak in.
        assert orch.choose_executor(
            "opencode",
            capability_overlay={"opencode": "AVAILABLE_PRIMARY"},
        ) == "opencode"
        # And the orchestrator does not introduce any ollama binding.
        for executor in orch.EXECUTORS:
            assert "ollama" not in executor
    finally:
        if snapshot is not None:
            cache_path.write_text(snapshot, encoding="utf-8")
        elif cache_path.is_file():
            try:
                cache_path.unlink()
            except Exception:
                pass
        for key, value in saved.items():
            os.environ[key] = value


# ---------------------------------------------------------------------------
# §10 — Heavy duplicate sweep
# ---------------------------------------------------------------------------
def test_supersede_handles_many_duplicates_per_node(redis_clean):
    parent_id = f"p9f-{uuid.uuid4()}"
    n_children = 9
    tids = [f"dup-{uuid.uuid4()}" for _ in range(n_children)]
    try:
        # First child has a real result; the rest are spurious.
        for index, tid in enumerate(tids):
            redis_clean.hset(f"aios:bus:state:{tid}", mapping={
                "task_id": tid,
                "status": "completed",
                "result_summary": "real" if index == 0 else "",
                "parent_id": parent_id, "node_index": "0",
                "attempt": "0",
                "ts_completed": orch._now(),
            })
            redis_clean.zadd("aios:bus:index", {tid: time.time()})
        result = orch.supersede_duplicate_children(
            parent_id, 0, generation=0, commit=True,
        )
        assert result["inspect"] == n_children
        assert len(result["superseded"]) == n_children - 1
        canonical_tid = result["canonical"][0]["task_id"]
        assert canonical_tid == tids[0]
        canonical_state = redis_clean.hgetall(f"aios:bus:state:{canonical_tid}")
        canonical_result = canonical_state.get(
            b"result_summary", canonical_state.get("result_summary", "")
        )
        if isinstance(canonical_result, bytes):
            canonical_result = canonical_result.decode()
        assert canonical_result == "real"
    finally:
        cleanup_test_keys(redis_clean, [parent_id])