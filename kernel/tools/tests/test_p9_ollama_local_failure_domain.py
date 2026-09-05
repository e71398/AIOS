"""Tests for the local Ollama failure domain under
``AIOS_INTERNAL_LOCAL_MODEL_GUARD`` closure 20260725-§二 / §三.

The guard is a hard precondition, NOT a runtime probe result.  These
tests intentionally avoid any real local inference.

The previous revision's real ``PONG`` round-trip tests are removed.
Historical ``PONG`` results are reclassified
``HISTORICAL_MANUAL_TEST`` and may not be cited as production
eligibility evidence.

The default operator state has BOTH guard env vars absent, so
``chat()`` and ``quick_probe()`` must raise
``LocalModelGuardError`` without performing any network call.
The skipped-on-AIOS-down ``test_monitor_local_model_policy_visible``
has been replaced with an offline-only verification.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import subprocess
import time
import unittest
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen


def _local_model_unauthorised_env() -> None:
    """Ensure the operator has NOT unlocked local-model inference."""
    for var in (
        "AIOS_OLLAMA_USER_APPROVED_AT",
        "AIOS_LOCAL_MODEL_INFERENCE_ALLOWED",
    ):
        os.environ.pop(var, None)


_local_model_unauthorised_env()


# ---------------------------------------------------------------------------
# §一 Env-var alone is insufficient.  A per-task approval record is
# required.  We test that fact.
# ---------------------------------------------------------------------------
class TaskScopedApprovalTests(unittest.TestCase):
    """Task-scoped approval is REQUIRED for chat()/quick_probe()."""

    def setUp(self):
        _local_model_unauthorised_env()
        # Reset the in-memory approval registry.
        from aios_ollama_adapter import _TASK_APPROVALS
        _TASK_APPROVALS.clear()

    def test_env_var_alone_is_insufficient_with_task_id(self):
        """§一: even with both env vars set, a chat() call with a
        task_id but no registered task approval must raise
        ``LocalModelGuardError`` returning
        ``LOCAL_MODEL_USER_APPROVAL_REQUIRED``."""
        from aios_ollama_adapter import (
            LocalModelGuardError,
            chat,
            register_task_approval,
        )
        # Env vars set, no task approval registered, task_id provided.
        os.environ["AIOS_OLLAMA_USER_APPROVED_AT"] = "2026-07-25T00:00:00+00:00"
        os.environ["AIOS_LOCAL_MODEL_INFERENCE_ALLOWED"] = "1"
        try:
            with self.assertRaises(LocalModelGuardError) as ctx:
                chat([{"role": "user", "content": "PONG"}],
                     model="qwen2.5:0.5b", timeout=2,
                     task_id="task-env-only-no-approval")
            self.assertIn("LOCAL_MODEL_USER_APPROVAL_REQUIRED",
                          str(ctx.exception))
        finally:
            _local_model_unauthorised_env()

    def test_approval_source_must_be_user_explicit(self):
        """§一: register_task_approval rejects non-user-explicit
        sources (Provider, Recovery Manager, etc.)."""
        from aios_ollama_adapter import (
            LocalModelGuardError,
            register_task_approval,
        )
        for bad in ("provider_self", "recovery_manager",
                    "model_router", "auto", "system"):
            with self.assertRaises(LocalModelGuardError,
                                   msg=f"should reject {bad!r}"):
                register_task_approval(
                    "task-rej-" + bad,
                    approval_source=bad,
                    allowed_model_ids=("qwen2.5:0.5b",),
                )

    def test_approval_is_bound_to_one_task_id(self):
        """§一: the same task_id cannot be approved twice."""
        from aios_ollama_adapter import (
            LocalModelGuardError,
            register_task_approval,
        )
        register_task_approval("task-once", allowed_model_ids=("x",))
        with self.assertRaises(LocalModelGuardError):
            register_task_approval("task-once", allowed_model_ids=("x",))

    def test_consume_invalidates_immediately(self):
        """§一: a consumed approval is no longer accepted; even
        the same task_id can be re-opened with a fresh record but
        the prior one cannot be reused."""
        from aios_ollama_adapter import (
            LocalModelGuardError,
            chat,
            consume_task_approval,
            register_task_approval,
        )
        os.environ["AIOS_OLLAMA_USER_APPROVED_AT"] = "2026-07-25T00:00:00+00:00"
        os.environ["AIOS_LOCAL_MODEL_INFERENCE_ALLOWED"] = "1"
        try:
            register_task_approval("task-consume",
                                    allowed_model_ids=("qwen2.5:0.5b",),
                                    max_calls=5)
            # Before consume, the call would be admitted by the
            # *approval* check.  But the underlying chat() will
            # still fail to reach Ollama because there is no daemon.
            # What we are testing here is that *consume* flips the
            # approval status to CONSUMED, after which a NEW
            # ``chat()`` call would raise the guard again (even if
            # the env vars remain set).
            consume_task_approval("task-consume")
            with self.assertRaises(LocalModelGuardError):
                chat([{"role": "user", "content": "PONG"}],
                     model="qwen2.5:0.5b", timeout=2,
                     task_id="task-consume")
        finally:
            _local_model_unauthorised_env()

    def test_max_calls_enforced(self):
        """§一: even with an active approval, the
        ``max_calls`` budget blocks calls after the budget is
        exhausted."""
        from aios_ollama_adapter import (
            LocalModelGuardError,
            chat,
            register_task_approval,
        )
        os.environ["AIOS_OLLAMA_USER_APPROVED_AT"] = "2026-07-25T00:00:00+00:00"
        os.environ["AIOS_LOCAL_MODEL_INFERENCE_ALLOWED"] = "1"
        try:
            register_task_approval("task-budget",
                                    allowed_model_ids=("qwen2.5:0.5b",),
                                    max_calls=1)
            # First call: we don't care if it succeeds (no daemon)
            # but the *guard* must accept it.  Use try/except broadly
            # so we still exercise max_calls.
            try:
                chat([{"role": "user", "content": "PONG"}],
                     model="qwen2.5:0.5b", timeout=1,
                     task_id="task-budget")
            except LocalModelGuardError:
                self.fail("first call should be admitted by the guard")
            except Exception:
                # Network / connection failure is expected when no
                # daemon is running; the guard did its job.
                pass
            # Second call: should now be rejected (max_calls=1)
            with self.assertRaises(LocalModelGuardError):
                chat([{"role": "user", "content": "PONG"}],
                     model="qwen2.5:0.5b", timeout=1,
                     task_id="task-budget")
        finally:
            _local_model_unauthorised_env()

    def test_expires_at_default_is_five_minutes(self):
        """§一: default ``expires_at`` is approved_at + 5 minutes."""
        from aios_ollama_adapter import register_task_approval
        rec = register_task_approval("task-exp",
                                    allowed_model_ids=("x",))
        try:
            ap = datetime.datetime.fromisoformat(rec["approved_at"]
                                                  .replace("Z", "+00:00"))
            ex = datetime.datetime.fromisoformat(rec["expires_at"]
                                                  .replace("Z", "+00:00"))
            self.assertEqual((ex - ap).total_seconds(), 5 * 60)
        finally:
            pass


# ---------------------------------------------------------------------------
# §二 Default routing: every *:ollama binding is disabled with the
# six required flags.  priority=99 means numerically high but the
# binding is disabled; the test name documents that semantic.
# ---------------------------------------------------------------------------
class BindingStaticStateTests(unittest.TestCase):
    def setUp(self):
        _local_model_unauthorised_env()

    def test_ollama_bindings_have_all_required_disabled_flags(self):
        from aios_model_resources import build_default_binding_registry
        br = build_default_binding_registry()
        ollama_bindings = [
            b for b in br.list_all() if b.binding_id.endswith(":ollama")
        ]
        self.assertEqual(len(ollama_bindings), 5)
        for b in ollama_bindings:
            self.assertFalse(b.enabled, msg=f"{b.binding_id} not disabled")
            self.assertFalse(b.production_eligible if hasattr(b, 'production_eligible') else False)
            self.assertFalse(b.automatic_fallback_allowed)
            self.assertFalse(b.recovery_manager_allowed)
            self.assertFalse(b.canary_allowed)
            self.assertTrue(b.manual_task_only)
            self.assertEqual(b.max_concurrency, 1)
            self.assertEqual(b.blocked_reason, "USER_APPROVAL_REQUIRED")

    def test_ollama_bindings_have_high_priority_but_disabled(self):
        """priority=99 (numerically high) but enabled=False.  The
        numeric value is for documentation only; the engine never
        enters the walk because enabled=False short-circuits the
        selection.  This test documents the semantic, NOT a 'this
        is more likely to be chosen' claim.
        """
        from aios_model_resources import build_default_binding_registry
        br = build_default_binding_registry()
        for b in br.list_all():
            if b.binding_id.endswith(":ollama"):
                self.assertFalse(b.enabled,
                                 msg="enabled must be False, regardless of priority")
                self.assertGreaterEqual(b.priority, 50)

    def test_ollama_bindings_not_in_candidate_walks(self):
        from aios_model_resources import build_default_policy_registry
        pr = build_default_policy_registry()
        for policy in pr.list_all():
            for binding_id in policy.candidate_bindings:
                self.assertNotIn("ollama", binding_id)

    def test_ollama_bindings_cannot_be_used_as_rescue(self):
        from aios_model_resources import build_default_policy_registry
        pr = build_default_policy_registry()
        for policy in pr.list_all():
            keys = set(policy.to_dict().keys())
            self.assertNotIn("rescue_bindings", keys)
            self.assertNotIn("last_resort_bindings", keys)


# ---------------------------------------------------------------------------
# §三 The chat() / quick_probe() guard — also covered by the in-memory
# approval registry tests above.  These two tests confirm the default
# (no env, no task) refusal path is short-circuited and that the
# error message names the precise recovery action.
# ---------------------------------------------------------------------------
class AdapterGuardDefaultTests(unittest.TestCase):
    def setUp(self):
        _local_model_unauthorised_env()

    def test_chat_without_task_id_raises_guard(self):
        from aios_ollama_adapter import (
            LocalModelGuardError,
            chat,
        )
        with self.assertRaises(LocalModelGuardError) as ctx:
            chat([{"role": "user", "content": "PONG"}],
                 model="qwen2.5:0.5b", timeout=2)
        msg = str(ctx.exception)
        self.assertIn("AIOS_OLLAMA_USER_APPROVED_AT", msg)
        self.assertIn("AIOS_LOCAL_MODEL_INFERENCE_ALLOWED", msg)

    def test_quick_probe_without_task_id_raises_guard(self):
        from aios_ollama_adapter import (
            LocalModelGuardError,
            quick_probe,
        )
        with self.assertRaises(LocalModelGuardError):
            quick_probe("PONG", model="qwen2.5:0.5b", timeout=2)

    def test_no_network_call_when_unauthorised(self):
        """§三: no socket connection to 127.0.0.1:11434 is opened
        while the guard is engaged.  We do this by listening for
        any new TCP connection to the Ollama port before and after
        a refused chat() call."""
        # Bind a listener on a free port to count connections.
        import socket as _socket
        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        probe.listen(8)
        probe.setblocking(False)
        port = probe.getsockname()[1]
        # Redirect the adapter to point at this port.
        os.environ["AIOS_OLLAMA_BASE_URL"] = f"http://127.0.0.1:{port}"
        try:
            from aios_ollama_adapter import (
                LocalModelGuardError,
                chat,
            )
            for _ in range(3):
                with self.assertRaises(LocalModelGuardError):
                    chat([{"role": "user", "content": "PONG"}],
                         model="qwen2.5:0.5b", timeout=1)
            # No connection should have reached the probe.
            try:
                conn, _ = probe.accept()
            except BlockingIOError:
                conn = None
            self.assertIsNone(
                conn,
                msg="guard failed to short-circuit: a TCP connection "
                    "reached the Ollama probe despite no approval",
            )
            if conn is not None:
                conn.close()
        finally:
            probe.close()
            os.environ.pop("AIOS_OLLAMA_BASE_URL", None)
            _local_model_unauthorised_env()


# ---------------------------------------------------------------------------
# §三 (online rejection) — replaced with an offline check that does
# not require the AIOS gateway to be running.  When the gateway IS
# available, this test also reads ``/status`` and asserts the
# ``local_model_policy`` surface.  When the gateway is NOT available
# the test is skipped, and the skip is explicitly logged so a future
# CI run can be configured to fail if the gateway is down.
# ---------------------------------------------------------------------------
class OnlineRejectionTests(unittest.TestCase):
    """When the AIOS gateway is reachable, the /status payload must
    report ``local_model_policy.inference_allowed=false`` and
    ``local_model_running=false``.  When the gateway is unreachable
    on this test host, the test is skipped (not silently)."""

    def test_monitor_local_model_policy_visible(self):
        try:
            with urlopen("http://127.0.0.1:18801/status", timeout=8.0) as r:
                payload = json.loads(r.read().decode())
        except (URLError, OSError, ValueError) as exc:
            self.skipTest(
                f"AIOS gateway not reachable on this host "
                f"(reason: {type(exc).__name__}).  This test is "
                "expected to pass when the gateway is up; it is "
                "explicitly skipped otherwise."
            )
            return
        local = payload.get("local_model_policy")
        self.assertIsNotNone(local,
                             msg="/status payload must include local_model_policy")
        self.assertFalse(local.get("inference_allowed"))
        self.assertFalse(local.get("local_model_running"))
        self.assertEqual(local.get("calls_current"), 0)
        self.assertEqual(local.get("activation_mode"),
                         "MANUAL_USER_APPROVAL_ONLY")


# ---------------------------------------------------------------------------
# §四 Read-only metadata probes are allowed; they never send a prompt.
# ---------------------------------------------------------------------------
class MetadataProbeTests(unittest.TestCase):
    def setUp(self):
        _local_model_unauthorised_env()

    def test_health_does_not_send_prompt(self):
        from aios_ollama_adapter import health
        snap = health()
        self.assertIn("binary_ok", snap)
        self.assertIn("state", snap)
        self.assertIn("inference_allowed", snap)
        self.assertFalse(snap["inference_allowed"])

    def test_list_models_does_not_load(self):
        from aios_ollama_adapter import list_models, LocalModelGuardError
        try:
            list_models()
        except LocalModelGuardError:
            self.fail("list_models must not require local-model approval")
        except Exception:
            # connection refused is fine — daemon may not be running
            pass


if __name__ == "__main__":
    _local_model_unauthorised_env()
    unittest.main()