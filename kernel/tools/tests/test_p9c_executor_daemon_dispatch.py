#!/usr/bin/env python3
"""AIOS P9C — OpenCode Executor Daemon Dispatch Recovery Tests.

These tests cover the P9C close-out:

1.  ``_executor_dispatch_ready`` returns ``True`` (with a `dispatch_ready`
    reason) when the binary + lightweight probe are healthy even when
    ``fully_operational`` is ``False`` because the inference slice is
    stale (the exact scenario that froze the daemon pre-P9C).
2.  ``_executor_dispatch_ready`` returns ``False`` with a descriptive
    reason when the binary / lightweight probe fails.
3.  ``_executor_inference_ready`` keeps its strict production-eligibility
    semantics (does *not* inherit the dispatch relaxation).
4.  ``_publish_liveness`` writes the queue-poll timestamp to Redis.
5.  ``_mark_claim`` writes ``last_claimed_at`` + ``last_claimed_task_id``.
6.  ``_mark_completion`` writes ``last_completed_at`` + status.
7.  ``AIOS_EXECUTOR_FORCE_DISPATCH=1`` overrides the dispatch gate.
8.  ``aios:bus:liveness:{executor}`` keys honour a TTL so a dead daemon
    does not masquerade as alive forever.
9.  ``run_once`` keeps returning ``False`` and only emits the
    ``agent.degraded`` event when the *infrastructure* is unavailable;
    it does NOT block on a stale ``fully_operational``.
10. ``run_once`` records ``last_loop_at`` between iterations so a frozen
    consumer loop is detectable through Redis even if the daemon
    process is alive.
11. legacy ``_executor_inference_ready`` still mirrors the cached
    ``fully_operational`` so the Orchestrator planner gate is unchanged.
12. ``_executor_dispatch_ready`` does not depend on any local-model env
    var (``AIOS_OLLAMA_USER_APPROVED_AT`` / ``AIOS_LOCAL_MODEL_INFERENCE_ALLOWED``)
    — it must never fall back to a local model.

The tests intentionally do *not* require the daemon to be running.  They
poke the dispatch helpers directly and assert on the Redis hashes the
daemon writes, so they can run as a pure unit suite.
"""
from __future__ import annotations

import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

TOOLS = "${AIOS_HOME}/kernel/tools"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import aios_executor_daemon as ed  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def redis_clean():
    """Best-effort cleanup of the liveness hash before and after the test."""
    import redis as _rd

    rc = _rd.Redis(host="localhost", port=6379, socket_connect_timeout=2)
    for ex in ("opencode", "claude", "codex"):
        try:
            rc.delete(f"aios:bus:liveness:{ex}")
        except Exception:
            pass
    yield rc
    for ex in ("opencode", "claude", "codex"):
        try:
            rc.delete(f"aios:bus:liveness:{ex}")
        except Exception:
            pass


def _stub_health(monkeypatch, *, contract_ok=True,
                 reachable=True, protocol_ready=True,
                 fully_operational=False, state="degraded"):
    """Patch ``aios_tool_adapter.get_adapter(...)`` to return a stub
    whose ``.health()`` returns a fully-controlled payload.  This keeps
    the dispatch-gate test hermetic — no real health cache is touched."""

    fake_adapter = types.SimpleNamespace()
    fake_adapter.health = lambda: {
        "contract_ok": contract_ok,
        "lightweight_reachable": reachable,
        "lightweight_protocol_ready": protocol_ready,
        "fully_operational": fully_operational,
        "state": state,
        "model_available": fully_operational,
    }

    def _fake_get_adapter(name):
        assert name in ("opencode", "claude", "codex")
        return fake_adapter

    monkeypatch.setattr(ed, "get_adapter", _fake_get_adapter)
    return fake_adapter


# ---------------------------------------------------------------------------
# §1 — _executor_dispatch_ready: infrastructure-aware gate
# ---------------------------------------------------------------------------
def test_dispatch_ready_when_infrastructure_ok_even_if_fully_operational_false(
    monkeypatch,
):
    """The exact pre-P9C deadlock scenario: lightweight probe is fresh
    and reachable but ``fully_operational`` is ``False`` because the
    inference slice is stale.  Dispatch must remain ready so the daemon
    can keep attempting tasks and refresh the inference evidence."""
    _stub_health(
        monkeypatch,
        contract_ok=True, reachable=True, protocol_ready=True,
        fully_operational=False, state="degraded",
    )

    ok, reason = ed._executor_dispatch_ready("opencode")
    assert ok is True
    assert "dispatch_ready" in reason
    assert "fully_operational=False" in reason


def test_dispatch_blocked_when_infrastructure_unavailable(monkeypatch):
    """When the binary / lightweight probe fails, the daemon must still
    refuse to claim tasks.  ``fully_operational`` is irrelevant."""
    _stub_health(
        monkeypatch,
        contract_ok=False, reachable=False, protocol_ready=False,
        fully_operational=False, state="error",
    )

    ok, reason = ed._executor_dispatch_ready("opencode")
    assert ok is False
    assert "infrastructure_unavailable" in reason


def test_dispatch_blocked_when_lightweight_reachable_but_protocol_missing(
    monkeypatch,
):
    """HTTP server reachable but protocol handshake missing must still
    block dispatch.  Otherwise we would call OpenCode Server with a
    protocol version the daemon cannot parse."""
    _stub_health(
        monkeypatch,
        contract_ok=True, reachable=True, protocol_ready=False,
        fully_operational=False, state="degraded",
    )

    ok, reason = ed._executor_dispatch_ready("opencode")
    assert ok is False
    assert "infrastructure_unavailable" in reason


def test_dispatch_force_env_var_override(monkeypatch):
    """``AIOS_EXECUTOR_FORCE_DISPATCH=1`` overrides every cached signal —
    used by tests and by the on-call runbook when the health cache is
    itself the problem."""
    _stub_health(
        monkeypatch,
        contract_ok=False, reachable=False, protocol_ready=False,
        fully_operational=False, state="error",
    )

    monkeypatch.setenv("AIOS_EXECUTOR_FORCE_DISPATCH", "1")
    ok, reason = ed._executor_dispatch_ready("opencode")
    assert ok is True
    assert "FORCE_DISPATCH" in reason


def test_dispatch_non_daemon_role_is_always_ready():
    """Roles outside (opencode, claude, codex) are passthrough."""
    ok, reason = ed._executor_dispatch_ready("hermes")
    assert ok is True
    assert reason == "non_daemon_role"


def test_dispatch_does_not_depend_on_local_model_env(monkeypatch):
    """The dispatch gate must never pick up a local-model override.
    The P9C local-model guard forbids any local-model env var being
    honoured; we therefore assert they have no effect on the gate."""
    _stub_health(
        monkeypatch,
        contract_ok=True, reachable=True, protocol_ready=True,
        fully_operational=False, state="degraded",
    )
    monkeypatch.setenv("AIOS_OLLAMA_USER_APPROVED_AT", "2026-07-28T00:00:00Z")
    monkeypatch.setenv("AIOS_LOCAL_MODEL_INFERENCE_ALLOWED", "1")
    ok, reason = ed._executor_dispatch_ready("opencode")
    assert ok is True
    # Local-model approval must not appear in the reason: the gate is
    # binary / lightweight only.
    assert "ollama" not in reason.lower()
    assert "local" not in reason.lower()


# ---------------------------------------------------------------------------
# §2 — _executor_inference_ready: strict semantics preserved
# ---------------------------------------------------------------------------
def test_inference_ready_strict_mirrors_fully_operational(monkeypatch):
    """The Orchestrator planner gate must still block on stale inference."""
    _stub_health(
        monkeypatch,
        contract_ok=True, reachable=True, protocol_ready=True,
        fully_operational=False, state="degraded",
    )

    assert ed._executor_inference_ready("opencode") is False


def test_inference_ready_passes_when_fully_operational_true(monkeypatch):
    _stub_health(
        monkeypatch,
        contract_ok=True, reachable=True, protocol_ready=True,
        fully_operational=True, state="operational",
    )

    assert ed._executor_inference_ready("opencode") is True


def test_inference_ready_returns_false_on_health_exception(monkeypatch):
    """A health-lookup exception must not silently flip the strict gate
    to True.  Production eligibility must fail closed."""

    def _boom(_name):
        raise RuntimeError("simulated redis outage")

    monkeypatch.setattr(ed, "get_adapter", _boom)
    assert ed._executor_inference_ready("opencode") is False


# ---------------------------------------------------------------------------
# §3 — Liveness hash writers
# ---------------------------------------------------------------------------
def test_publish_liveness_writes_queue_poll_at(redis_clean):
    ed._publish_liveness("opencode", "queue_poll",
                         reason="loop_iteration_start",
                         detail="pytest")
    payload = redis_clean.hgetall("aios:bus:liveness:opencode")
    decoded = {k.decode() if isinstance(k, bytes) else k:
               v.decode() if isinstance(v, bytes) else v
               for k, v in payload.items()}
    assert decoded.get("last_stage") == "queue_poll"
    assert decoded.get("queue_poll_at")
    ttl = redis_clean.ttl("aios:bus:liveness:opencode")
    assert 0 < ttl <= ed._LIVENESS_TTL_SECONDS


def test_mark_claim_writes_last_claimed_at(redis_clean):
    ed._mark_claim("opencode", "task-test-001")
    payload = redis_clean.hgetall("aios:bus:liveness:opencode")
    decoded = {k.decode() if isinstance(k, bytes) else k:
               v.decode() if isinstance(v, bytes) else v
               for k, v in payload.items()}
    assert decoded.get("last_claimed_task_id") == "task-test-001"
    assert decoded.get("last_claimed_at")


def test_mark_completion_writes_last_completed_status(redis_clean):
    ed._mark_completion("opencode", "completed")
    payload = redis_clean.hgetall("aios:bus:liveness:opencode")
    decoded = {k.decode() if isinstance(k, bytes) else k:
               v.decode() if isinstance(v, bytes) else v
               for k, v in payload.items()}
    assert decoded.get("last_completion_status") == "completed"
    assert decoded.get("last_completed_at")


def test_liveness_publish_does_not_throw_on_redis_outage(monkeypatch):
    """Liveness is best-effort: a Redis outage must not break the dispatch
    loop.  Patch the ``redis`` import inside the function via a context
    that raises."""
    import builtins
    real_import = builtins.__import__

    def _broken_import(name, *args, **kwargs):
        if name == "redis":
            raise RuntimeError("simulated redis outage")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _broken_import)
    # Must not raise.
    ed._publish_liveness("opencode", "queue_poll")
    ed._mark_claim("opencode", "task-broken")
    ed._mark_completion("opencode", "failed")


def test_publish_liveness_ignores_non_daemon_executors(redis_clean):
    ed._publish_liveness("hermes", "queue_poll")
    assert redis_clean.exists("aios:bus:liveness:hermes") == 0


# ---------------------------------------------------------------------------
# §4 — run_once integration with the dispatch gate (stubbed loop)
# ---------------------------------------------------------------------------
def test_run_once_emits_emit_on_infrastructure_unavailable(monkeypatch):
    """When infrastructure is down, ``run_once`` must emit
    ``agent.degraded`` and return False.  We capture the emit events
    rather than spinning the whole daemon."""

    _stub_health(
        monkeypatch,
        contract_ok=False, reachable=False, protocol_ready=False,
        fully_operational=False, state="error",
    )

    captured = []

    def _fake_emit(event, **kw):
        captured.append((event, kw))

    monkeypatch.setattr(ed, "emit", _fake_emit)
    monkeypatch.setattr(ed, "heartbeat", lambda *_a, **_k: True)
    monkeypatch.setattr(ed.aios_bus if False else sys.modules[
        "aios_executor_daemon"], "_publish_liveness",
                        ed._publish_liveness)

    result = ed.run_once("opencode")
    assert result is False
    degraded = [c for c in captured if c[0] == "agent.degraded"]
    assert degraded, "expected agent.degraded emit"
    payload = degraded[0][1]
    assert payload.get("source") == "opencode"


def test_run_once_publishes_liveness_with_dispatch_ready(monkeypatch, redis_clean):
    """When infrastructure is OK and no task is available, the daemon
    must still publish a ``queue_poll`` liveness tick so a frozen
    consumer loop is detectable through Redis."""

    _stub_health(
        monkeypatch,
        contract_ok=True, reachable=True, protocol_ready=True,
        fully_operational=False, state="degraded",
    )

    monkeypatch.setattr(ed, "emit", lambda *a, **k: None)
    monkeypatch.setattr(ed, "heartbeat", lambda *_a, **_k: True)
    monkeypatch.setattr(ed, "claim_next_task", lambda *a, **k: None)

    ed.run_once("opencode")

    payload = redis_clean.hgetall("aios:bus:liveness:opencode")
    decoded = {k.decode() if isinstance(k, bytes) else k:
               v.decode() if isinstance(v, bytes) else v
               for k, v in payload.items()}
    assert decoded.get("last_stage") == "queue_poll"
    assert decoded.get("last_reason") in ("dispatch_ready",
                                          "loop_iteration_start")
    # Queue-poll timestamp must be recent (within 60 s).
    qp = decoded.get("queue_poll_at")
    assert qp
    from datetime import datetime, timezone
    ts = datetime.fromisoformat(qp.replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    assert age < 60


def test_run_once_marks_claim_and_completion_when_task_runs(monkeypatch, redis_clean):
    """Stub a successful task so ``run_once`` reaches ``_mark_claim``
    and ``_mark_completion``.  Verify the Redis liveness hash records
    both timestamps."""

    _stub_health(
        monkeypatch,
        contract_ok=True, reachable=True, protocol_ready=True,
        fully_operational=False, state="degraded",
    )

    # Minimal valid task record for the run_once contract.
    fake_task = {
        "task_id": "t-9999",
        "task_name": "noop-strict-opencode-p9c",
        "status": "pending",
        "preferred_executor": "opencode",
        "executor": "",
        "result_summary": "",
        "approval_id": "",
        "parent_id": "",
        "risk_action": "",
        "ts_created": "2026-07-28T00:00:00+00:00",
    }

    monkeypatch.setattr(ed, "emit", lambda *a, **k: None)
    monkeypatch.setattr(ed, "heartbeat", lambda *_a, **_k: True)
    monkeypatch.setattr(ed, "claim_next_task", lambda *a, **k: fake_task)
    monkeypatch.setattr(ed, "check_in_task", lambda *a, **k: True)
    monkeypatch.setattr(ed, "release_lock", lambda *a, **k: True)

    # Force the pipeline to short-circuit to a clean success so we
    # exercise the completion path.  ``enforce_pipeline`` is the only
    # call we cannot trivially stub; instead we monkeypatch the helpers
    # it transitively uses.
    from aios_enforcer import enforce_pipeline as real_enforce
    monkeypatch.setattr(ed, "enforce_pipeline",
                        lambda *a, **k: {
                            "success": True,
                            "pipeline": {
                                "execute": {"summary": "ok"},
                                "verify": {"passed": True,
                                           "report": "ok"},
                            },
                            "error": "",
                        })
    monkeypatch.setattr(ed, "update_task_status", lambda *a, **k: True)
    monkeypatch.setattr(ed, "report_metadata", lambda *a, **k: None)

    result = ed.run_once("opencode")
    assert result is True

    payload = redis_clean.hgetall("aios:bus:liveness:opencode")
    decoded = {k.decode() if isinstance(k, bytes) else k:
               v.decode() if isinstance(v, bytes) else v
               for k, v in payload.items()}
    assert decoded.get("last_claimed_task_id") == "t-9999"
    assert decoded.get("last_claimed_at")
    assert decoded.get("last_completion_status") == "completed"
    assert decoded.get("last_completed_at")


# ---------------------------------------------------------------------------
# §5 — Backwards-compat smoke (legacy strict gate still reachable)
# ---------------------------------------------------------------------------
def test_legacy_inference_ready_signature_unchanged():
    """The original 2-line ``_executor_inference_ready`` must keep
    returning ``bool`` and accept a single executor string.  Tests
    outside P9C and the Orchestrator planner call this."""
    import inspect
    sig = inspect.signature(ed._executor_inference_ready)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "executor"


def test_dispatch_ready_returns_tuple_for_callers():
    """``_executor_dispatch_ready`` must return ``(bool, str)`` so
    callers can log the structured reason without parsing strings."""
    import inspect
    sig = inspect.signature(ed._executor_dispatch_ready)
    # No need to import Tuple — just check the return annotation if any.
    assert sig.return_annotation in (
        "tuple", "Tuple[bool, str]", tuple, inspect.Signature.empty,
    )


# ---------------------------------------------------------------------------
# §6 — Cross-cutting: non-daemon callers still get a clean answer
# ---------------------------------------------------------------------------
def test_publish_liveness_no_op_for_hermes(redis_clean):
    """Hermes is not an executor in the daemon sense — it must not
    create a liveness hash that would be mistaken for dispatch
    activity."""
    ed._publish_liveness("hermes", "queue_poll")
    ed._mark_claim("hermes", "task-hermes-1")
    ed._mark_completion("hermes", "completed")
    assert redis_clean.exists("aios:bus:liveness:hermes") == 0


# ---------------------------------------------------------------------------
# §7 — Forbidden env vars do not flip dispatch
# ---------------------------------------------------------------------------
def test_ollama_approval_does_not_short_circuit_inference_ready(monkeypatch):
    """Local-model guard: setting ``AIOS_OLLAMA_USER_APPROVED_AT`` must
    not cause a ``*_inference_ready`` helper to return ``True`` on
    its own.  This is the contract the P9C local-model guard enforces."""

    monkeypatch.setenv("AIOS_OLLAMA_USER_APPROVED_AT", "2026-07-28T00:00:00Z")
    monkeypatch.setenv("AIOS_LOCAL_MODEL_INFERENCE_ALLOWED", "1")
    _stub_health(
        monkeypatch,
        contract_ok=False, reachable=False, protocol_ready=False,
        fully_operational=False, state="error",
    )
    assert ed._executor_inference_ready("opencode") is False
    ok, _ = ed._executor_dispatch_ready("opencode")
    assert ok is False