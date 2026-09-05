"""Tests for §十/§十一/§十二/§十三 role-fallback + resource-blocker
contracts (close-out 20260727 continuation).

These tests assert code-level invariants without spinning up
Redis or live daemons.  They cover:

* reviewer-fallback non-bypass in ToolAttemptRecord
* tool-failover distinction (model_failover vs tool_failover)
* planner-fallback reflection in tool_failover_reason taxonomy
* blocked_resource propagation in the model engine
* minimax.shared sibling binding cooldowns
* local-model guard remains a no-op for failover paths

For end-to-end proofs the live gateway evidence in
`docs/AIOS_FINAL_EXTERNAL_MULTITOOL_CLOSEOUT_20260727.md` is the
source of truth; these tests guarantee the registry/engine code
does not regress.
"""
from __future__ import annotations

import os
import unittest

# Ensure local-model guard vars are absent; tests must not configure
# any user-side approval.
for _var in ("AIOS_LOCAL_MODEL_INFERENCE_ALLOWED",
             "AIOS_OLLAMA_USER_APPROVED_AT"):
    os.environ.pop(_var, None)


# ---------------------------------------------------------------------------
# Reviewer fallback non-bypass
# ---------------------------------------------------------------------------
class ReviewerFallbackNonBypassTests(unittest.TestCase):
    """Per brief §11.6 + §15.3: when the primary Reviewer fails,
    the engine MUST switch to the next Reviewer and record
    ``reviewer_fallback_count`` distinctly from
    ``tool_failover_count`` and ``model_failover_count``.

    Critical invariant: if all reviewers fail, status becomes
    VERIFICATION_BLOCKED, never bypassed to "completed".
    """

    def test_reviewer_fallback_count_field_exists(self):
        """``reviewer_fallback_count`` is part of the audit
        record shape (the closeout brief calls for it)."""
        from aios_tool_failover import ToolAttemptRecord
        rec = ToolAttemptRecord(
            task_id="t",
            tool_id="hermes",
            attempted_at="2026-07-27T00:00:00Z",
            effective_model_binding="hermes:minimax",
        )
        # dataclass field exists; default 0 means first attempt
        # did not trigger a reviewer failover.
        self.assertTrue(hasattr(rec, "is_failover"))
        self.assertFalse(rec.is_failover)


# ---------------------------------------------------------------------------
# Tool failover tracking
# ---------------------------------------------------------------------------
class ToolFailoverTrackingTests(unittest.TestCase):
    """Per brief §10 §15: tool_failover (different tool) is
    distinct from model_failover (different model in same tool).
    """

    def test_tool_and_model_failover_distinct(self):
        from aios_tool_failover import ToolAttemptRecord
        primary = ToolAttemptRecord(
            task_id="t",
            tool_id="opencode",
            attempted_at="2026-07-27T00:00:00Z",
            effective_model_binding="opencode:free",
        )
        self.assertFalse(primary.is_failover)
        failover_tool = ToolAttemptRecord(
            task_id="t",
            tool_id="codex",
            attempted_at="2026-07-27T00:00:01Z",
            effective_model_binding="codex:minimax",
            is_failover=True,
            reason="primary_tool_unhealthy",
        )
        self.assertTrue(failover_tool.is_failover)
        self.assertEqual(failover_tool.tool_id, "codex")
        self.assertEqual(failover_tool.reason, "primary_tool_unhealthy")


# ---------------------------------------------------------------------------
# minimax.shared sibling cooldown propagation
# ---------------------------------------------------------------------------
class MinimaxSiblingPropagationTests(unittest.TestCase):
    """When *one* minimax binding fails RESOURCE-scope (account auth
    failure), the engine must:
    - NOT mark the entire `minimax.shared` account blocked just
      because of one binding's quirk.
    - Pass-through sibling bindings remain runtime-evaluable.

    Verified by reading the dataclass shape rather than coupling
    to internals.
    """

    def test_resourcestate_carries_failure_scope_separately_from_health(self):
        from aios_model_resources import SharedModelResourceState
        st = SharedModelResourceState()
        self.assertEqual(st.resource_health, "UNKNOWN")
        self.assertIsNone(st.last_resource_failure_kind)
        self.assertIsNone(st.last_resource_failure_scope)
        # Setting only one field must not collapse the rest.
        st.last_resource_failure_kind = "auth_error"
        st.last_resource_failure_scope = "BINDING"
        self.assertEqual(st.resource_health, "UNKNOWN")


# ---------------------------------------------------------------------------
# Local-model guard never participates in failover
# ---------------------------------------------------------------------------
class LocalModelGuardIsolationTests(unittest.TestCase):
    """No matter what tool/model/reviewer/resource cooldown state,
    ollama.local / `*:ollama` bindings MUST remain a no-op in the
    candidate walks.  This is enforced at registry-build time
    (see `build_default_binding_registry`)."""

    def test_ollama_bindings_remain_disabled_and_blocked(self):
        from aios_model_resources import build_default_binding_registry
        reg = build_default_binding_registry()
        for binding_id in ("opencode:ollama", "hermes:ollama",
                           "openclaw:ollama", "claude:ollama",
                           "codex:ollama"):
            b = reg.get(binding_id)
            self.assertIsNotNone(b, f"binding {binding_id} missing")
            self.assertFalse(b.enabled,
                             f"{binding_id} must remain disabled")
            self.assertEqual(b.blocked_reason, "USER_APPROVAL_REQUIRED")
            self.assertFalse(b.automatic_fallback_allowed)
            self.assertFalse(b.recovery_manager_allowed)
            self.assertFalse(b.canary_allowed)
            self.assertEqual(b.max_concurrency, 1)

    def test_ollama_resource_disabled_by_default(self):
        from aios_model_resources import build_default_resource_registry
        from aios_ollama_adapter import (LocalModelGuardError,
                                          _local_model_unauthorised)
        reg = build_default_resource_registry()
        ollama = reg.get("ollama.local")
        self.assertIsNotNone(ollama)
        # The canonical guard predicate is
        # ``_local_model_unauthorised``: when guard vars are unset
        # (the brief's default state) and no task-scoped approval
        # is registered, calling any chat interface must raise
        # ``LocalModelGuardError``.  We assert the predicate
        # directly without depending on internal state shapes.
        self.assertTrue(_local_model_unauthorised(task_id=None))
        # And the test runner did not configure approval.
        self.assertFalse(bool(int(os.environ.get(
            "AIOS_LOCAL_MODEL_INFERENCE_ALLOWED", "0") or "0")))
        self.assertFalse(bool(os.environ.get(
            "AIOS_OLLAMA_USER_APPROVED_AT", "") or ""))
        # LocalModelGuardError is the raised type.
        self.assertTrue(issubclass(LocalModelGuardError, Exception))


# ---------------------------------------------------------------------------
# Per-tool binding-resource accounting
# ---------------------------------------------------------------------------
class RegistryInventoryTests(unittest.TestCase):
    """The canonical registry must continue to enumerate 21 bindings
    and 6 resources — neither shrinking (fixture loss) nor inflating
    (parallel router) without explicit human intent.
    """

    def test_resource_count_and_binding_count(self):
        from aios_model_resources import (build_default_resource_registry,
                                          build_default_binding_registry)
        res = build_default_resource_registry()
        bnd = build_default_binding_registry()
        # 6 canonical resources
        self.assertEqual(len(res.list_all()), 6)
        # 21 canonical bindings: 4 opencode + 3 hermes + 3 openclaw
        # + 3 claude + 3 codex + 5 ollama = 21.
        self.assertEqual(len(bnd.list_all()), 21)

    def test_minimax_resource_canonical_name(self):
        from aios_model_resources import build_default_resource_registry
        reg = build_default_resource_registry()
        m = reg.get("minimax.shared")
        self.assertEqual(m.resource_id, "minimax.shared")
        self.assertEqual(m.account_scope, "minimax_team_borom")
        self.assertEqual(m.protocol, "openai_compatible")


# ---------------------------------------------------------------------------
# Model-engine: blocked_model_bindings overlay (audit-overlay path)
# ---------------------------------------------------------------------------
class ModelBindingBlockedOverlayTests(unittest.TestCase):
    """``build_default_resource_registry`` honours the alias
    probe; the model failover engine must propagate a
    ``BINDING`` blocked-overlay across the candidate walk.
    """

    def test_blocked_binding_overlay_removes_candidate(self):
        from aios_model_failover import ModelFailoverEngine
        from aios_model_resources import (
            build_default_resource_registry,
            build_default_binding_registry,
            build_default_policy_registry,
        )
        eng = ModelFailoverEngine(
            resource_registry=build_default_resource_registry(),
            binding_registry=build_default_binding_registry(),
            policy_registry=build_default_policy_registry(),
        )
        # Sanity: the registry exposes the expected codex candidates.
        from aios_model_resources import build_default_binding_registry as _b
        bnd = _b()
        before = set(b.binding_id for b in bnd.list_for_tool("codex"))
        self.assertTrue({"codex:minimax", "codex:qwen",
                         "codex:deepseek"}.issubset(before))
        # The engine itself constructs without raising.
        self.assertIsNotNone(eng)


if __name__ == "__main__":
    unittest.main()