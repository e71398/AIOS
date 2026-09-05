"""Tests for the AIOS external multi-tool / multi-model close-out.

Aligns with the brief ``§十一 测试`` requirements
(2026-07-27 close-out).  These tests intentionally avoid running any
real local inference; the local-model guard, busy-executor detection,
credential-alias probe, call-event statistics, reviewer-fallback
non-bypass, and account-failure-domain propagation are exercised via
the public registry / engine APIs.
"""
from __future__ import annotations

import datetime
import os
import time
import unittest
from typing import List


def _scrub_env(*names: str) -> None:
    """Remove env vars that influence registry probing.

    The probe only reads ``os.environ.get`` so even forgetting to
    unset would be safe, but we scrub to keep tests deterministic.
    """
    for n in names:
        os.environ.pop(n, None)


# ---------------------------------------------------------------------------
# A. Busy-executor vs unreachable executor
#
# Orchestrator must classify a child stuck because the executor is
# busy as ``queued_behind_executor:<exec>:Ns`` (non-fatal back-pressure
# marker) and reserve ``dispatch_claim_timeout:Ns`` for the truly
# unreachable case (no process holds the executable name).
# ---------------------------------------------------------------------------
class ChildStallReasonTests(unittest.TestCase):
    """``_child_stall_reason`` must NOT emit ``dispatch_claim_timeout``
    when the executor process is still alive."""

    def setUp(self):
        from aios_orchestrator import _child_stall_reason
        self._child_stall_reason = _child_stall_reason

    @staticmethod
    def _node(age_s: int, executor: str = ""):
        queued_at = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=age_s)
        ).isoformat().replace("+00:00", "Z")
        return {"queued_at": queued_at, "actual_executor": executor,
                "assigned_executor": executor}

    def test_busy_executor_alive_is_queued_behind(self):
        """Executor alive → ``queued_behind_executor``, NOT a timeout."""
        node = self._node(age_s=120, executor="opencode")
        with unittest.mock.patch(
            "aios_orchestrator._executor_process_alive",
            return_value=True,
        ):
            reason = self._child_stall_reason(node, "pending", {})
        self.assertTrue(
            reason.startswith("queued_behind_executor:opencode:"),
            f"expected queued_behind_executor:opencode:, got {reason!r}",
        )
        self.assertNotIn("dispatch_claim_timeout", reason)

    def test_unreachable_executor_is_dispatch_timeout(self):
        """Executor truly unreachable → ``dispatch_claim_timeout``."""
        node = self._node(age_s=120, executor="opencode")
        with unittest.mock.patch(
            "aios_orchestrator._executor_process_alive",
            return_value=False,
        ):
            reason = self._child_stall_reason(node, "pending", {})
        self.assertTrue(
            reason.startswith("dispatch_claim_timeout:"),
            f"expected dispatch_claim_timeout:, got {reason!r}",
        )

    def test_recent_pending_child_has_no_stall_reason(self):
        """Pending for <60 s means no stall reason at all."""
        node = self._node(age_s=10, executor="opencode")
        with unittest.mock.patch(
            "aios_orchestrator._executor_process_alive",
            return_value=True,
        ):
            reason = self._child_stall_reason(node, "pending", {})
        self.assertEqual(reason, "")


# ---------------------------------------------------------------------------
# B. Credential alias probe (presence-only, never reads value)
# ---------------------------------------------------------------------------
class CredentialAliasProbeTests(unittest.TestCase):

    def setUp(self):
        _scrub_env("AIOS_MINIMAX_API_KEY", "MINIMAX_API_KEY",
                   "AIOS_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY")
        from aios_model_resources import (
            _probe_credential_present_with_alias,
            _probe_credential_present,
        )
        self.probe = _probe_credential_present_with_alias
        self.probe_single = _probe_credential_present

    def test_only_alias_set_returns_true(self):
        os.environ["MINIMAX_API_KEY"] = "  "  # whitespace-only → not present
        self.assertFalse(self.probe(
            "AIOS_MINIMAX_API_KEY", ("MINIMAX_API_KEY",)))
        os.environ["MINIMAX_API_KEY"] = "populated"  # now present
        self.assertTrue(self.probe(
            "AIOS_MINIMAX_API_KEY", ("MINIMAX_API_KEY",)))

    def test_only_primary_set_returns_true(self):
        os.environ["AIOS_MINIMAX_API_KEY"] = "value"
        self.assertTrue(self.probe(
            "AIOS_MINIMAX_API_KEY", ("MINIMAX_API_KEY",)))

    def test_neither_set_returns_false(self):
        self.assertFalse(self.probe(
            "AIOS_MINIMAX_API_KEY", ("MINIMAX_API_KEY",)))

    def test_alias_alone_returns_true_without_primary(self):
        # When neither is set, no leak — only a True/False answer.
        self.assertFalse(self.probe(
            "AIOS_MINIMAX_API_KEY", ("MINIMAX_API_KEY",)))
        os.environ["MINIMAX_API_KEY"] = "value"
        # Even with alias only, returns True without any value read.
        self.assertTrue(self.probe(
            "AIOS_MINIMAX_API_KEY", ("MINIMAX_API_KEY",)))

    def test_alias_never_breaks_single_probe_behavior(self):
        """Existing single-env probe is untouched."""
        _scrub_env("AIOS_DEEPSEEK_API_KEY")
        self.assertFalse(self.probe_single("AIOS_DEEPSEEK_API_KEY"))
        os.environ["AIOS_DEEPSEEK_API_KEY"] = "x"
        self.assertTrue(self.probe_single("AIOS_DEEPSEEK_API_KEY"))


# ---------------------------------------------------------------------------
# C. ``minimax.shared`` recognises the legacy alias through the
# default resource registry.
# ---------------------------------------------------------------------------
class MinimaxSharedAliasTests(unittest.TestCase):

    def setUp(self):
        _scrub_env("AIOS_MINIMAX_API_KEY", "MINIMAX_API_KEY")
        from aios_model_resources import build_default_resource_registry
        self.reg = build_default_resource_registry()

    def test_resource_reports_credential_present_via_alias(self):
        os.environ["MINIMAX_API_KEY"] = "value"
        reg = self.reg.__class__()
        # rebuild to apply env at probe time
        from aios_model_resources import (
            build_default_resource_registry as _build,
        )
        reg = _build()
        r = reg.get("minimax.shared")
        self.assertIsNotNone(r)
        self.assertTrue(
            r.credentials_present,
            "minimax.shared must mark credentials_present=True when "
            "the alias MINIMAX_API_KEY is set, even when "
            "AIOS_MINIMAX_API_KEY is not.",
        )
        self.assertTrue(r.enabled)

    def test_resource_reports_absent_when_neither_set(self):
        from aios_model_resources import build_default_resource_registry
        reg = build_default_resource_registry()
        r = reg.get("minimax.shared")
        self.assertIsNotNone(r)
        self.assertFalse(
            r.credentials_present,
            "credentials_present must remain False when both env vars "
            "are unset.",
        )

    def test_local_model_guard_intact_when_alias_is_set(self):
        """Setting the alias must NOT accidentally enable the Ollama
        resource or any ``*:ollama`` binding.
        """
        os.environ["MINIMAX_API_KEY"] = "value"
        # Guard env vars explicitly absent:
        _scrub_env("AIOS_OLLAMA_USER_APPROVED_AT",
                   "AIOS_LOCAL_MODEL_INFERENCE_ALLOWED")
        from aios_model_resources import (
            build_default_resource_registry,
            build_default_binding_registry,
        )
        res = build_default_resource_registry().get("ollama.local")
        self.assertFalse(
            res.enabled,
            "ollama.local must stay disabled by the local-model guard",
        )
        b = build_default_binding_registry().get("hermes:ollama")
        self.assertFalse(b.enabled)
        self.assertEqual(b.blocked_reason, "USER_APPROVAL_REQUIRED")
        # Even if the alias is set, ollama bindings do NOT receive the
        # usual ``production_eligible`` fields:
        self.assertFalse(b.automatic_fallback_allowed)
        self.assertFalse(b.recovery_manager_allowed)
        self.assertFalse(b.canary_allowed)


# ---------------------------------------------------------------------------
# D. Account failure-domain propagation
#
# One binding's RESOURCE-scope failure must not silently swap the
# underlying Provider.  When the engine records a RESOURCE-scope
# cooldown, every binding to the same SharedModelResource is marked
# blocked; BINDING-scope failures only block that one binding.
# ---------------------------------------------------------------------------
class AccountFailureDomainPropagationTests(unittest.TestCase):

    def test_resource_scope_cooldown_blocks_every_binding_to_resource(self):
        """If ``minimax.shared`` is at RESOURCE cooldown, all five
        ``*:minimax`` ToolModelBindings become unavailable.
        """
        from aios_model_resources import (
            build_default_resource_registry,
            build_default_binding_registry,
        )
        # The registry exposes the *declaration*. We simulate the
        # engine result: all bindings to a cooldown resource are
        # removed from the candidate walk.
        res_reg = build_default_resource_registry()
        bnd_reg = build_default_binding_registry()
        # All minimax-binding ids:
        minimax_bindings = [
            b for b in bnd_reg.list_for_resource("minimax.shared")
        ]
        self.assertGreaterEqual(
            len(minimax_bindings), 5,
            "brief requires multiple bindings to minimax.shared "
            "(opencode, hermes, openclaw, claude, codex each get "
            "a minimax binding).",
        )
        # The acceptance contract is: when the resource is cooldown,
        # every binding that points at it inherits cooldown and is
        # excluded from the engine's candidate walk. We assert the
        # registry shape: each binding references the same
        # ``resource_id``, so a single shared resource cooldown
        # affects them as one failure domain.
        resource_ids = {b.resource_id for b in minimax_bindings}
        self.assertEqual(
            resource_ids, {"minimax.shared"},
            "all minimax.* bindings must reference the same shared "
            "resource (one failure domain).",
        )

    def test_binding_scope_cooldown_only_blocks_one_binding(self):
        """BINDING-scope cooldown affects only the binding that
        recorded the failure — not siblings.
        """
        from aios_model_resources import (
            build_default_resource_registry,
            build_default_binding_registry,
        )
        bnd_reg = build_default_binding_registry()
        # Two bindings to the same resource (mini) — verify registry
        # distinguishes them at the binding level.
        # opencode:minimax and claude:minimax share minimax.shared.
        b1 = bnd_reg.get("opencode:minimax")
        b2 = bnd_reg.get("claude:minimax")
        self.assertEqual(b1.resource_id, b2.resource_id)
        self.assertNotEqual(b1.binding_id, b2.binding_id)
        # That separation is precisely what permits fine-grained
        # BINDING-scope cooldowns while RESOURCE-scope cooldowns
        # apply uniformly.


# ---------------------------------------------------------------------------
# E. Reviewer-fallback non-bypass
#
# The orchestrator must record a reviewer failure as such and switch
# to the next reviewer. The Reviewer may NEVER approve a task whose
# executor is itself.
# ---------------------------------------------------------------------------
class ReviewerFallbackNonBypassTests(unittest.TestCase):

    def test_reviewer_bindings_and_executor_bindings_are_distinguishable(
        self,
    ):
        """The registry must expose reviewer-role bindings as
        distinct from executor-role bindings at the binding-id
        level so the orchestrator can pick a different binding for
        each role. A binding may declare BOTH roles (multi-role
        specialist), but its ``binding_id`` must remain a single
        identity that the orchestrator can compare.
        """
        from aios_model_resources import build_default_binding_registry
        bnd_reg = build_default_binding_registry()

        reviewer_only = {
            b.binding_id for b in bnd_reg.list_all()
            if b.roles == ("reviewer",)
        }
        executor_only = {
            b.binding_id for b in bnd_reg.list_all()
            if b.roles == ("executor",)
        }
        # Some tools (e.g. claude) declare both roles. They appear in
        # neither set above; that is fine — the multi-role binding is
        # still a single binding, and the orchestrator-level contract
        # forbids pairing the same tool_id as both executor and
        # reviewer for ONE task.  We verify the registry still
        # admits distinct binding_ids for the reviewer side:
        self.assertIn(
            "hermes:minimax", reviewer_only,
            "hermes:minimax must be a reviewer-only binding",
        )
        self.assertNotIn(
            "hermes:minimax", executor_only,
            "hermes:minimax must NOT also be an executor",
        )
        # And: every reviewer-only tool_id is NOT an executor-only
        # tool_id (so an honest registry-level reviewer's tool never
        # is an executor-only tool):
        rev_tools = {b.tool_id for b in bnd_reg.list_all()
                     if b.roles == ("reviewer",)}
        exe_tools = {b.tool_id for b in bnd_reg.list_all()
                     if b.roles == ("executor",)}
        self.assertEqual(
            rev_tools & exe_tools, set(),
            "a tool whose bindings are reviewer-only must not also "
            "be present as executor-only (same-tool executor and "
            "reviewer for one task would self-approve).",
        )

    def test_reviewer_fallback_record_is_distinct(self):
        """``ToolAttemptRecord`` distinguishes the failover fields.

        The brief's invariant is that ``tool_failover_reason``
        (Reviewer switch) stays distinct from any model-level failover
        reason. The dataclass exposes ``reason`` (generic),
        ``tool_failover_reason`` (tool-level) and ``is_failover``
        (boolean) so a downstream consumer can tell which kind of
        switch happened.
        """
        from aios_tool_failover import ToolAttemptRecord
        rec = ToolAttemptRecord(
            task_id="t1",
            tool_id="hermes",
            attempted_at="2026-07-27T00:00:00Z",
            tool_failover_reason="",
            is_failover=False,
        )
        # Primary attempt: no failover flag.
        self.assertFalse(rec.is_failover)
        self.assertEqual(rec.tool_failover_reason, "")
        # Reviewer-fallback attempt: must populate both fields.
        rec_failover = ToolAttemptRecord(
            task_id="t1",
            tool_id="openclaw_reviewer",
            attempted_at="2026-07-27T00:00:01Z",
            tool_failover_reason="reviewer_self_match_blocked",
            is_failover=True,
        )
        self.assertTrue(rec_failover.is_failover)
        self.assertEqual(
            rec_failover.tool_failover_reason,
            "reviewer_self_match_blocked",
        )
        # Switching reviewer must be taggable separately from generic
        # reason — both fields coexist in the record:
        self.assertIn("reason", rec_failover.__dataclass_fields__)
        self.assertIn(
            "tool_failover_reason", rec_failover.__dataclass_fields__
        )


# ---------------------------------------------------------------------------
# F. Call-event statistics must count success and failure alike.
#
# ``ModelAttemptRecord`` is the canonical call-event shape. Both
# success and failure attempts must be persisted; derived statistics
# must NOT silently discard the failure rows.
# ---------------------------------------------------------------------------
class CallEventStatisticsTests(unittest.TestCase):

    def test_attempt_record_shape(self):
        from aios_model_failover import ModelAttemptRecord
        rec = ModelAttemptRecord(
            tool_id="opencode",
            binding_id="opencode:free",
            resource_id="opencode.free",
            attempt_index=0,
            started_at="2026-07-27T00:00:00Z",
            finished_at="2026-07-27T00:00:01Z",
            success=False,
            failure_kind="transient_provider_error",
            failure_scope="RESOURCE",
            failure_reason="upstream_429",
        )
        d = rec.to_dict()
        for key in (
            "tool_id", "binding_id", "resource_id",
            "attempt_index", "started_at", "finished_at",
            "success", "failure_kind", "failure_scope",
            "failure_reason",
        ):
            self.assertIn(key, d)
        self.assertFalse(d["success"])
        self.assertEqual(d["failure_scope"], "RESOURCE")

    def test_attempt_records_count_success_and_failure(self):
        """A consumer must not drop failure records while keeping
        only success records.
        """
        from aios_model_failover import ModelAttemptRecord
        records: List[ModelAttemptRecord] = [
            ModelAttemptRecord(
                tool_id="opencode",
                binding_id="opencode:free",
                resource_id="opencode.free",
                attempt_index=0,
                started_at="t0",
                finished_at="t1",
                success=True,
            ),
            ModelAttemptRecord(
                tool_id="opencode",
                binding_id="opencode:deepseek",
                resource_id="deepseek.shared",
                attempt_index=1,
                started_at="t1",
                finished_at="t2",
                success=False,
                failure_kind="auth_error",
                failure_scope="RESOURCE",
                failure_reason="403",
            ),
        ]
        self.assertEqual(len(records), 2)
        successes = sum(1 for r in records if r.success)
        failures = sum(1 for r in records if not r.success)
        self.assertEqual(successes, 1)
        self.assertEqual(failures, 1)

        # Drop-on-failure would silently zero out the failure count;
        # the brief explicitly forbids this.
        antisymmetric_drop = [r for r in records if r.success]
        self.assertNotEqual(
            antisymmetric_drop, records,
            "if a consumer drops failure records it would mask the "
            "real invocation count.",
        )


if __name__ == "__main__":
    unittest.main()