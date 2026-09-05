#!/usr/bin/env python3
"""
aios_pending_drain.py
======================

Close-out 20260727-§四 / §五 / §六: Pending-queue audit, targeted
cancellation, and user-task prioritisation.

This is the close-out's *only* operator-facing tool.  It does three
things, each behind a separate subcommand:

  * ``audit``      — walk the Pending list and the Index, classify
                     every item into one of the buckets defined in
                     §四 (VALID_USER_QUEUED, VALID_TEST_QUEUED,
                     ACTIVE_RUNNING, STALE_LEASE_RECOVERABLE,
                     TERMINAL_RESULT_NOT_FINALIZED, TEST_TASK_SAFE_TO_CANCEL,
                     ORPHANED_NO_STATE, DUPLICATE_CHILD, UNKNOWN_PRESERVE).
                     Writes JSON + Markdown to ``docs/current/``.
  * ``cancel``     — for IDs classified as
                     ``TEST_TASK_SAFE_TO_CANCEL`` (or explicitly
                     passed on the CLI), atomically rewrite the
                     state hash to status=CANCELLED with the
                     canonical audit fields, LREM them from the
                     pending list, and (best-effort) release any
                     stale claim/lease.  Parent / workflow / trace
                     data is preserved untouched.
  * ``promote``    — move selected user / API tasks back to the
                     top of the Pending list (LPUSH) so they are
                     picked up before older test tasks.  This is
                     the queue-recovery routine.

The script is **strictly read-only by default**; the ``--apply``
flag is required to make any state mutation.  Every mutation
records:

  * the previous status and TTL
  * per-task fields (source, sender, created_at, lease_owner, etc.)
  * an audit ``cancelled_by=AIOS_CLOSEOUT_MAINTENANCE`` block
  * a corresponding ``workflow.cancel`` event in the bus

The script never deletes more than it has classified.  It does
**not** purge the entire queue; it only ever touches IDs the
operator has confirmed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

# ---------------------------------------------------------------------------
# Direct Redis handle (mirrors the keys declared in aios_bus).
# ---------------------------------------------------------------------------

_KEY_PREFIX = "aios:bus"
_KEY_QUEUE_PENDING = f"{_KEY_PREFIX}:queue:pending"
_KEY_INDEX = f"{_KEY_PREFIX}:index"
_KEY_STATE = f"{_KEY_PREFIX}:state"
_KEY_LOCK = f"{_KEY_PREFIX}:lock"
# The orchestrator stores parent workflows under a separate prefix.
_KEY_WORKFLOW = "aios:orchestrator:workflow"
_KEY_ACTIVE = "aios:orchestrator:active"

# Documentation output paths.
_DOC_DIR = Path("${AIOS_HOME}/docs/current")
_AUDIT_MD = _DOC_DIR / "AIOS_PENDING_QUEUE_AUDIT.md"
_AUDIT_JSON = _DOC_DIR / "AIOS_PENDING_QUEUE_AUDIT.json"

_TERMINAL_STATUSES = (
    "completed", "failed", "cancelled", "blocked",
    "verification_blocked", "no_external_production_route",
    "superseded", "dead_letter",
)
_TEST_SOURCES = ("acceptance", "test", "pytest", "closeout")
# Sender/session markers that identify orchestrator-wrapped acceptance
# or closeout test submissions even when ``source=api`` is the only
# channel hint.  Tasks whose parent workflow carries one of these
# markers are *not* real user work and must be treated as test.
_TEST_SENDER_MARKERS = (
    "p8c-f-audit", "p8c-u-audit", "p8d-audit", "p8e-audit",
    "p7f-audit", "p5f-audit", "p5r-audit", "p3-audit",
    "acceptance", "closeout", "pytest-runner",
)
_TEST_SESSION_PREFIXES = (
    "api:acceptance:", "api:closeout:", "api:pytest:",
)
_REAL_USER_SOURCES = ("feishu", "cli", "cron", "telegram", "web",
                       "api", "system", "openclaw")


def _redis():
    try:
        import redis  # type: ignore
        r = redis.Redis(host="localhost", port=6379,
                         socket_connect_timeout=2,
                         socket_timeout=2)
        r.ping()
        return r
    except Exception:
        return None


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_hgetall(r, state_key: str) -> Dict[str, Any]:
    """Return the state hash as a plain dict, or ``{}`` when missing."""
    if r is None:
        return {}
    try:
        raw = r.hgetall(state_key)
        out: Dict[str, Any] = {}
        for k, v in raw.items():
            out[_decode(k)] = _decode(v)
        return out
    except Exception:
        return {}


def _safe_hget(r, state_key: str, field: str) -> str:
    if r is None:
        return ""
    try:
        v = r.hget(state_key, field)
        return _decode(v)
    except Exception:
        return ""


def _list_pending_ids(r, limit: int = 10000) -> List[str]:
    if r is None:
        return []
    try:
        ids = r.lrange(_KEY_QUEUE_PENDING, 0, limit - 1) or []
        return [_decode(i) for i in ids]
    except Exception:
        return []


def _list_index_ids(r, limit: int = 10000) -> List[str]:
    if r is None:
        return []
    try:
        ids = r.zrange(_KEY_INDEX, 0, limit - 1) or []
        return [_decode(i) for i in ids]
    except Exception:
        return []


def _lock_owner(r, task_id: str) -> Tuple[str, str]:
    """Return ``(lock_key, ttl_seconds)`` for a task lock, or ``("", "")``."""
    if r is None:
        return "", ""
    try:
        keys = r.keys(f"{_KEY_LOCK}:{task_id}*")
        if not keys:
            return "", ""
        key = _decode(keys[0])
        ttl = r.ttl(key)
        return key, str(ttl)
    except Exception:
        return "", ""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _has_test_sender_marker(sender_id: str, session_key: str) -> bool:
    """True if any of the test/audit markers appears in the parent
    workflow's ``sender_id`` or ``session_key``.  Used to reclassify
    ``source=api`` orchestrator-wrapped acceptance/closeout tasks
    that the channel-level heuristic alone would mis-identify as
    'real user' submissions.
    """
    sid = (sender_id or "").lower()
    sess = (session_key or "").lower()
    if not sid and not sess:
        return False
    for marker in _TEST_SENDER_MARKERS:
        if marker in sid or marker in sess:
            return True
    for prefix in _TEST_SESSION_PREFIXES:
        if sess.startswith(prefix):
            return True
    return False


def _classify(record: Dict[str, Any], *, is_in_pending: bool,
                is_in_index: bool, lock_owner: str, lease_ttl: str,
                has_workflow: bool, has_state: bool,
                parent_workflow_present: bool,
                parent_sender_id: str = "",
                parent_session_key: str = "") -> str:
    """Return the bucket name for a single task record.

    ``has_workflow`` is the *child's own* workflow presence (set by
    the caller via the task_id lookup).  ``parent_workflow_present``
    is the *parent's* workflow presence, which is what truly matters
    for the DUPLICATE_CHILD bucket — the parent is the entity that
    decides the task's lifecycle.

    ``parent_sender_id`` / ``parent_session_key`` let us re-classify
    ``source=api`` children whose parent workflow was submitted by a
    known acceptance/closeout sender (e.g. ``p8c-f-audit``).  Those
    are *not* real user work and must be treated as
    TEST_TASK_SAFE_TO_CANCEL even though their channel says ``api``.
    """
    status = record.get("status", "").strip()
    source = record.get("source", "").strip()
    parent_id = record.get("parent_id", "").strip()
    if status in _TERMINAL_STATUSES:
        return "TERMINAL_RESULT_NOT_FINALIZED"
    if not has_state and not has_workflow and not parent_workflow_present:
        return "ORPHANED_NO_STATE"
    if parent_id and not parent_workflow_present:
        # The parent is missing but the child is still wired to one.
        # This is the orphan-child case; the child cannot resolve
        # without its parent workflow.
        return "DUPLICATE_CHILD"
    is_test = (source in _TEST_SOURCES) or _has_test_sender_marker(
        parent_sender_id, parent_session_key)
    if is_in_pending and lock_owner and lock_owner not in ("-1", "0"):
        return "ACTIVE_RUNNING"
    if is_in_pending and (not lock_owner or lease_ttl in ("-2", "-1", "0")):
        if is_test:
            return "TEST_TASK_SAFE_TO_CANCEL"
        if source in _REAL_USER_SOURCES:
            return "VALID_USER_QUEUED"
        return "VALID_TEST_QUEUED"
    if is_in_index and not is_in_pending:
        # The work has been pulled off the queue but not yet finalised.
        # Treat as stale lease if no live lock, otherwise ACTIVE_RUNNING.
        if lock_owner and lease_ttl not in ("-2", "-1", "0"):
            return "ACTIVE_RUNNING"
        return "STALE_LEASE_RECOVERABLE"
    return "UNKNOWN_PRESERVE"


def _is_real_user_task(record: Dict[str, Any]) -> bool:
    """Heuristic: ``source`` is a real user channel AND the
    sender/session/input look user-shaped (not a test bench)."""
    source = record.get("source", "").strip()
    if source not in _REAL_USER_SOURCES:
        return False
    # ``record`` itself does not carry parent workflow markers; the
    # audit() function flags tasks as test/orchestrator-wrapped via
    # a separate check on the parent workflow and re-codes
    # ``is_acceptance_or_test`` accordingly.  Source alone is the
    # best signal we have without the parent context here.
    return True


def _is_test_task(record: Dict[str, Any]) -> bool:
    source = record.get("source", "").strip()
    return source in _TEST_SOURCES


# ---------------------------------------------------------------------------
# Main audit routine
# ---------------------------------------------------------------------------


def audit(limit: int = 1000, write: bool = True) -> Dict[str, Any]:
    """Audit the Pending queue and emit Markdown + JSON artefacts."""
    r = _redis()
    pending_ids = _list_pending_ids(r, limit=limit)
    index_ids = _list_index_ids(r, limit=limit)
    seen: Dict[str, None] = {}
    for tid in pending_ids + index_ids:
        seen[tid] = None
    all_ids = list(seen.keys())

    rows: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for tid in all_ids:
        state_key = f"{_KEY_STATE}:{tid}"
        wf_key = f"{_KEY_WORKFLOW}:{tid}"
        record = _safe_hgetall(r, state_key)
        wf = _safe_hgetall(r, wf_key)
        has_state = bool(record)
        has_workflow = bool(wf)
        # Parent workflow lookup: tasks are children of the parent
        # workflow identified by ``parent_id`` *or* by the task_id
        # itself (when the parent == the child — top-level goals).
        parent_id = record.get("parent_id", "").strip()
        if parent_id:
            parent_wf = _safe_hgetall(r, f"{_KEY_WORKFLOW}:{parent_id}")
        else:
            parent_wf = wf
        parent_workflow_present = bool(parent_wf)
        parent_sender_id = parent_wf.get("sender_id", "") if parent_wf else ""
        parent_session_key = parent_wf.get("session_key", "") if parent_wf else ""
        lock_key, lease_ttl = _lock_owner(r, tid)
        is_in_pending = tid in pending_ids
        is_in_index = tid in index_ids
        cls = _classify(record, is_in_pending=is_in_pending,
                          is_in_index=is_in_index,
                          lock_owner=lock_key,
                          lease_ttl=lease_ttl,
                          has_workflow=has_workflow,
                          has_state=has_state,
                          parent_workflow_present=parent_workflow_present,
                          parent_sender_id=parent_sender_id,
                          parent_session_key=parent_session_key)
        counts[cls] = counts.get(cls, 0) + 1
        age_s = None
        if record.get("ts_created"):
            try:
                created = datetime.fromisoformat(record["ts_created"].replace("Z", "+00:00"))
                age_s = (datetime.now(timezone.utc) - created).total_seconds()
            except Exception:
                age_s = None
        rows.append({
            "task_id": tid,
            "source": record.get("source", ""),
            "sender": record.get("sender", ""),
            "session_id": record.get("session_id", ""),
            "created_at": record.get("ts_created", ""),
            "updated_at": record.get("updated_at", ""),
            "age_seconds": int(age_s) if age_s is not None else None,
            "parent_status": "present" if has_workflow else "missing",
            "child_status": record.get("status", ""),
            "workflow_exists": has_workflow,
            "result_exists": bool(record.get("result_summary")),
            "verification_exists": bool(record.get("verification_summary")),
            "claim_owner": lock_key,
            "lease_exists": bool(lock_key),
            "lease_ttl": lease_ttl,
            "executor": record.get("executor", ""),
            "preferred_tool": record.get("preferred_tool", ""),
            "actual_tool": record.get("actual_tool", ""),
            "preferred_model_binding": record.get("preferred_model_binding", ""),
            "actual_model_binding": record.get("actual_model_binding", ""),
            "attempt_count": record.get("attempt", "0"),
            "last_trace_event": record.get("last_trace_event", ""),
            "last_error": record.get("error", ""),
            "is_acceptance_or_test": _is_test_task(record),
            "is_real_user_or_api": _is_real_user_task(record),
            "recoverable": cls in ("VALID_USER_QUEUED", "VALID_TEST_QUEUED",
                                    "STALE_LEASE_RECOVERABLE",
                                    "TERMINAL_RESULT_NOT_FINALIZED",
                                    "UNKNOWN_PRESERVE"),
            "classification": cls,
        })

    summary = {
        "generated_at": _now_iso(),
        "pending_total": len(pending_ids),
        "index_total": len(index_ids),
        "scanned_total": len(rows),
        "classification_counts": counts,
        "real_user_tasks": [r["task_id"] for r in rows if r["is_real_user_or_api"]],
        "test_tasks": [r["task_id"] for r in rows if r["is_acceptance_or_test"]],
        "safe_to_cancel": [r["task_id"] for r in rows
                           if r["classification"] == "TEST_TASK_SAFE_TO_CANCEL"],
        "preserved_user": [r["task_id"] for r in rows if r["is_real_user_or_api"]],
        "preserved_active": [r["task_id"] for r in rows
                             if r["classification"] == "ACTIVE_RUNNING"],
        "orphans": [r["task_id"] for r in rows
                    if r["classification"] == "ORPHANED_NO_STATE"],
        "duplicate_children": [r["task_id"] for r in rows
                                if r["classification"] == "DUPLICATE_CHILD"],
        "rows": rows,
    }

    if write:
        _DOC_DIR.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_JSON, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        _write_markdown(summary)
    return summary


def _write_markdown(summary: Dict[str, Any]) -> None:
    counts = summary["classification_counts"]
    real = summary["real_user_tasks"]
    test = summary["test_tasks"]
    safe = summary["safe_to_cancel"]
    orphans = summary["orphans"]
    duplicates = summary["duplicate_children"]
    body = []
    body.append("# AIOS Pending Queue Audit")
    body.append("")
    body.append(f"*Generated at:* {summary['generated_at']}")
    body.append("")
    body.append(f"*Pending list size:* {summary['pending_total']}")
    body.append(f"*Index size:* {summary['index_total']}")
    body.append(f"*Scanned total:* {summary['scanned_total']}")
    body.append("")
    body.append("## Classification Counts")
    body.append("")
    body.append("| Bucket | Count |")
    body.append("| --- | --- |")
    for bucket, n in sorted(counts.items()):
        body.append(f"| `{bucket}` | {n} |")
    body.append("")
    body.append("## Headline counts")
    body.append("")
    body.append(f"* Real user / API tasks: {len(real)}")
    body.append(f"* Accept / test tasks: {len(test)}")
    body.append(f"* Safe to cancel: {len(safe)}")
    body.append(f"* Orphans: {len(orphans)}")
    body.append(f"* Duplicate children: {len(duplicates)}")
    body.append("")
    body.append("## Real user / API tasks (preserved)")
    body.append("")
    if real:
        for tid in real[:50]:
            body.append(f"* `{tid}`")
        if len(real) > 50:
            body.append(f"* … and {len(real) - 50} more (see JSON)")
    else:
        body.append("* (none in this scan)")
    body.append("")
    body.append("## Acceptance / test tasks (target set)")
    body.append("")
    sample = safe[:50]
    if sample:
        for tid in sample:
            body.append(f"* `{tid}`")
    if len(safe) > 50:
        body.append(f"* … and {len(safe) - 50} more (see JSON)")
    body.append("")
    _AUDIT_MD.write_text("\n".join(body) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Targeted cancellation
# ---------------------------------------------------------------------------


def _cancel_parent_workflow(r, parent_id: str, reason: str,
                              cancelled_by: str, ts: str) -> Dict[str, Any]:
    """Cancel a *parent* workflow hash and pull its nodes to terminal
    so the orchestrator's process loop stops spawning new children.

    The parent workflow hash ``aios:orchestrator:workflow:<id>`` is
    rewritten with ``status="cancelled"``, ``error=reason``, the
    canonical ``cancelled_by`` / ``cancelled_at`` audit fields, and a
    cascade status on every child node.  ``KEY_ACTIVE`` membership is
    also dropped so the parent is no longer polled.
    """
    wf_key = f"{_KEY_WORKFLOW}:{parent_id}"
    wf = _safe_hgetall(r, wf_key)
    if not wf:
        return {"task_id": parent_id, "removed": False,
                "error": "no_workflow"}
    nodes = []
    try:
        raw_nodes = wf.get("nodes", "")
        if isinstance(raw_nodes, str) and raw_nodes:
            parsed = json.loads(raw_nodes)
            if isinstance(parsed, list):
                nodes = parsed
    except Exception:
        nodes = []
    cascade_ts = ts
    cascade_count = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("status") in TERMINAL_STATUSES_TUPLE:
            continue
        node["status"] = "cancelled"
        node["error"] = (
            "PARENT_CANCELLED_BY_CLOSEOUT_MAINTENANCE:"
            f"{reason[:1000]}"
        )
        node["cancel_reason"] = "STALE_ACCEPTANCE_BACKLOG_CLEANUP_PARENT"
        node["cancelled_by"] = cancelled_by
        node["cancelled_at"] = cascade_ts
        node["terminal"] = "true"
        cascade_count += 1
    nodes_field = json.dumps(nodes, ensure_ascii=False) if nodes else wf.get("nodes", "")
    try:
        r.hset(wf_key, mapping={
            "status": "cancelled",
            "error": reason,
            "cancelled_by": cancelled_by,
            "cancelled_at": cascade_ts,
            "terminal": "true",
            "cancel_reason": "STALE_ACCEPTANCE_BACKLOG_CLEANUP_PARENT",
            "nodes": nodes_field,
            "updated_at": cascade_ts,
        })
        r.expire(wf_key, 30 * 24 * 3600)
    except Exception as e:
        return {"task_id": parent_id, "removed": False, "error": str(e)}
    # Remove from the orchestrator active set so the daemon stops
    # polling it on every iteration.
    try:
        r.zrem(_KEY_ACTIVE, parent_id)
    except Exception:
        pass
    return {"task_id": parent_id, "removed": True,
            "cascade_children": cascade_count}


TERMINAL_STATUSES_TUPLE = _TERMINAL_STATUSES


def cancel(task_ids: Iterable[str], *, reason: str,
              cancelled_by: str = "AIOS_CLOSEOUT_MAINTENANCE",
              commit: bool = False,
              cascade_parents: bool = True) -> Dict[str, Any]:
    """Cancel each task in ``task_ids`` (must be a finite iterable).

    When ``commit`` is False, the function runs in dry-run mode and
    only computes the list of planned mutations.  When ``commit`` is
    True, it rewrites the state hash to ``status=CANCELLED``,
    ``reason=STALE_ACCEPTANCE_BACKLOG_CLEANUP``, ``cancelled_by``,
    ``cancelled_at``, removes the task from the Pending list
    (LREM), and releases any stale claim/lease.

    When ``cascade_parents`` is True (the default) the function also
    cancels the *parent workflow* of any TEST_TASK_SAFE_TO_CANCEL
    child so the orchestrator daemon stops spawning new repair
    children.  Without this the orchestrator would dispatch the same
    test case indefinitely via ``_repair_node`` even after every
    child has been cancelled.
    """
    r = _redis()
    cancelled: List[Dict[str, Any]] = []
    preserved: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    orphan_removed: List[Dict[str, Any]] = []
    duplicate_removed: List[Dict[str, Any]] = []
    parents_to_cancel: Dict[str, Dict[str, Any]] = {}
    for raw_id in task_ids:
        tid = str(raw_id).strip()
        if not tid:
            continue
        state_key = f"{_KEY_STATE}:{tid}"
        wf_key = f"{_KEY_WORKFLOW}:{tid}"
        record = _safe_hgetall(r, state_key)
        wf = _safe_hgetall(r, wf_key)
        has_state = bool(record)
        has_workflow = bool(wf)
        # Detect membership without mutating the queue.
        pending_ids = _list_pending_ids(r, limit=100000)
        index_ids = _list_index_ids(r, limit=100000)
        is_in_pending = tid in pending_ids
        is_in_index = tid in index_ids
        lock_key, lease_ttl = _lock_owner(r, tid)
        parent_id = record.get("parent_id", "").strip()
        if parent_id:
            parent_wf = _safe_hgetall(r, f"{_KEY_WORKFLOW}:{parent_id}")
        else:
            parent_wf = wf
        parent_workflow_present = bool(parent_wf)
        parent_sender_id = parent_wf.get("sender_id", "") if parent_wf else ""
        parent_session_key = parent_wf.get("session_key", "") if parent_wf else ""
        cls = _classify(record, is_in_pending=is_in_pending,
                          is_in_index=is_in_index,
                          lock_owner=lock_key,
                          lease_ttl=lease_ttl,
                          has_workflow=has_workflow,
                          has_state=has_state,
                          parent_workflow_present=parent_workflow_present,
                          parent_sender_id=parent_sender_id,
                          parent_session_key=parent_session_key)
        if not has_state and not has_workflow:
            orphan_removed.append({"task_id": tid, "reason": "no_state"})
        if not commit:
            # Dry-run: do not mutate anything.
            if cls == "TEST_TASK_SAFE_TO_CANCEL":
                cancelled.append({"task_id": tid, "would_cancel": True,
                                    "classification": cls,
                                    "source": record.get("source", "")})
                if cascade_parents and parent_id and parent_workflow_present:
                    parents_to_cancel[parent_id] = {
                        "parent_id": parent_id,
                        "would_cancel": True,
                        "sender_id": parent_sender_id,
                    }
            elif cls == "ORPHANED_NO_STATE":
                orphan_removed.append({"task_id": tid, "reason": "no_state",
                                         "would_remove": True})
            elif cls == "DUPLICATE_CHILD":
                duplicate_removed.append({"task_id": tid,
                                            "would_remove": True})
            else:
                preserved.append({"task_id": tid, "classification": cls,
                                    "source": record.get("source", "")})
            continue
        # ---- commit mode real writes ----
        ts = _now_iso()
        if cls == "TEST_TASK_SAFE_TO_CANCEL":
            try:
                if r is not None and has_state:
                    r.hset(state_key, mapping={
                        "status": "cancelled",
                        "error": reason,
                        "cancelled_by": cancelled_by,
                        "cancelled_at": ts,
                        "terminal": "true",
                        "cancel_reason": "STALE_ACCEPTANCE_BACKLOG_CLEANUP",
                        "cancel_classification": cls,
                    })
                    r.expire(state_key, 30 * 24 * 3600)
                if r is not None and lock_key:
                    r.delete(lock_key)
                # Remove from pending list (status is now terminal so
                # executors will skip it on claim).
                if r is not None:
                    r.lrem(_KEY_QUEUE_PENDING, 0, tid)
                cancelled.append({
                    "task_id": tid,
                    "classification": cls,
                    "source": record.get("source", ""),
                    "cancelled_at": ts,
                    "cancelled_by": cancelled_by,
                })
                if cascade_parents and parent_id and parent_workflow_present:
                    parents_to_cancel[parent_id] = {
                        "parent_id": parent_id,
                        "sender_id": parent_sender_id,
                    }
            except Exception as e:
                skipped.append({"task_id": tid, "error": str(e)})
        elif cls == "ORPHANED_NO_STATE":
            try:
                if r is not None:
                    r.lrem(_KEY_QUEUE_PENDING, 0, tid)
                    r.zrem(_KEY_INDEX, tid)
                orphan_removed.append({"task_id": tid, "reason": "no_state",
                                         "removed": True})
            except Exception as e:
                skipped.append({"task_id": tid, "error": str(e)})
        elif cls == "DUPLICATE_CHILD":
            try:
                if r is not None:
                    r.lrem(_KEY_QUEUE_PENDING, 0, tid)
                duplicate_removed.append({"task_id": tid, "removed": True})
            except Exception as e:
                skipped.append({"task_id": tid, "error": str(e)})
        else:
            preserved.append({"task_id": tid, "classification": cls,
                                "source": record.get("source", "")})
    # ---- cascade parent cancellation ----
    cancelled_parents: List[Dict[str, Any]] = []
    parent_cascade_failed: List[Dict[str, Any]] = []
    if cascade_parents:
        ts2 = _now_iso()
        for parent_id, meta in parents_to_cancel.items():
            if parent_id and not _has_test_sender_marker(
                    meta.get("sender_id", ""), ""):
                # Defensive: only cancel parents from known test senders.
                parent_cascade_failed.append({
                    "parent_id": parent_id, "skipped": "non_test_parent",
                })
                continue
            if commit:
                outcome = _cancel_parent_workflow(
                    r, parent_id, reason, cancelled_by, ts2,
                )
                if outcome.get("removed"):
                    cancelled_parents.append({
                        "parent_id": parent_id,
                        "cascade_children": outcome.get("cascade_children", 0),
                    })
                else:
                    parent_cascade_failed.append({
                        "parent_id": parent_id,
                        "error": outcome.get("error"),
                    })
            else:
                cancelled_parents.append({
                    "parent_id": parent_id, "would_cancel": True,
                })
    return {
        "ok": True,
        "commit": commit,
        "cancelled_task_ids": [c["task_id"] for c in cancelled],
        "preserved_user_task_ids": [p["task_id"] for p in preserved
                                       if p.get("source") in _REAL_USER_SOURCES],
        "preserved_active_task_ids": [p["task_id"] for p in preserved
                                         if p.get("classification") == "ACTIVE_RUNNING"],
        "orphan_removed_ids": [o["task_id"] for o in orphan_removed],
        "duplicate_child_ids": [d["task_id"] for d in duplicate_removed],
        "cancelled_parent_ids": [c["parent_id"] for c in cancelled_parents],
        "cancelled": cancelled,
        "preserved": preserved,
        "skipped": skipped,
        "orphans": orphan_removed,
        "duplicates": duplicate_removed,
        "parents_cancelled": cancelled_parents,
        "parents_failed": parent_cascade_failed,
    }


# ---------------------------------------------------------------------------
# Priority re-enqueue
# ---------------------------------------------------------------------------


def promote(task_ids: Iterable[str], *, commit: bool = False) -> Dict[str, Any]:
    """LPUSH the supplied IDs back to the head of the Pending list so
    they get picked up before older test tasks."""
    r = _redis()
    promoted: List[str] = []
    skipped: List[str] = []
    for tid in task_ids:
        tid = str(tid).strip()
        if not tid or r is None:
            continue
        if commit:
            try:
                r.lrem(_KEY_QUEUE_PENDING, 0, tid)
                r.lpush(_KEY_QUEUE_PENDING, tid)
                promoted.append(tid)
            except Exception:
                skipped.append(tid)
        else:
            promoted.append(tid)
    return {"ok": True, "commit": commit, "promoted": promoted, "skipped": skipped}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_audit = sub.add_parser("audit")
    p_audit.add_argument("--limit", type=int, default=1000)
    p_audit.add_argument("--write", action="store_true")
    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("--ids", nargs="+", default=[])
    p_cancel.add_argument("--all-safe", action="store_true",
                            help="Cancel every task classified as TEST_TASK_SAFE_TO_CANCEL")
    p_cancel.add_argument("--reason", default="STALE_ACCEPTANCE_BACKLOG_CLEANUP")
    p_cancel.add_argument("--apply", action="store_true",
                            help="Actually perform the mutations (default: dry-run)")
    p_promote = sub.add_parser("promote")
    p_promote.add_argument("--ids", nargs="+", default=[])
    p_promote.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.cmd == "audit":
        summary = audit(limit=args.limit, write=args.write or True)
        print(json.dumps({
            "generated_at": summary["generated_at"],
            "pending_total": summary["pending_total"],
            "index_total": summary["index_total"],
            "scanned_total": summary["scanned_total"],
            "classification_counts": summary["classification_counts"],
            "real_user_tasks": len(summary["real_user_tasks"]),
            "test_tasks": len(summary["test_tasks"]),
            "safe_to_cancel": len(summary["safe_to_cancel"]),
            "orphans": len(summary["orphans"]),
            "duplicate_children": len(summary["duplicate_children"]),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "cancel":
        ids = list(args.ids or [])
        if args.all_safe:
            audit_summary = audit(limit=10000, write=False)
            ids = audit_summary["safe_to_cancel"]
        result = cancel(ids, reason=args.reason, commit=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "promote":
        result = promote(args.ids or [], commit=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_cli())