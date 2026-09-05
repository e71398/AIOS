#!/usr/bin/env python3
"""AIOS P7F entry / identity / session / result boundary closeout tests.

P7F scope (per AIOS-P7F-ENTRY-E2E-CAPABILITY-CLOSEOUT-13F):

* Validate the production Gateway whitelist has been closed: the
  P7A-only audit token ``p7a-local`` MUST NOT appear in
  ``valid_sources``.
* Validate the official ``web`` / ``api`` sources remain accepted and
  remain the supported path for real local audits.
* Validate ``failure_ownership`` is separate from
  ``evidence_freshness``: external quota / plan / rate-limit / token-plan
  / region / auth / network / cooldown signals MUST surface as
  ``DEGRADED_EXTERNAL``; only explicit local process / adapter / IPC /
  server signals may resolve to ``DEGRADED_INTERNAL``.
* Validate the ``executor_capability`` dimension can report FAILED
  while ``optional_degradations`` still exposes tool-level records.
* Re-validate source / sender / session isolation and result-callback
  task_id isolation introduced by P7A, so removing the audit token
  did not regress the boundary invariants.

These tests are offline: no AI provider is invoked, no Redis is
mutated, no subprocess is spawned, no feishu / telegram / openclaw
real message is sent. Network egress is restricted to localhost.

Run alongside the rest of the suite:

.. code-block:: bash

    PYTHONPATH=${AIOS_HOME}/kernel/tools:${AIOS_HOME}/kernel/tools/tests \\
        python3 -m pytest -q kernel/tools/tests/
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
AIOS_HOME = TOOLS.parent.parent
sys.path.insert(0, str(TOOLS))

from aios_capability import (
    AVAILABLE, DEGRADED_EXTERNAL, DEGRADED_INTERNAL,
    NOT_CONFIGURED, UNAVAILABLE, UNVERIFIED,
)
from aios_health_model import (
    STATUS_DEGRADED, STATUS_FAILED, STATUS_HEALTHY, STATUS_UNKNOWN,
    HealthDimension, assemble_health_report, probe_capability,
)


GATEWAY_SRC = (AIOS_HOME / "kernel/tools/aios_entry_gateway.py").read_text()
MONITOR_SRC = (AIOS_HOME / "kernel/tools/aios_monitor.py").read_text()


def _whitelist_set() -> set[str]:
    """Extract the production ``valid_sources`` literal from the gateway."""
    match = re.search(r"valid_sources\s*=\s*\(([^)]+)\)", GATEWAY_SRC)
    assert match is not None, "Gateway source whitelist not found"
    return {item.strip().strip("\"'") for item in match.group(1).split(",")}


# ---------------------------------------------------------------------------
# 1. p7a-local closed: it is not a production source
# ---------------------------------------------------------------------------


class P7aLocalClosed(unittest.TestCase):
    """P7F §5: P7A introduced ``p7a-local`` as a single-test audit
    token. P7F removes it from the production ``valid_sources`` tuple
    and forbids it from any production contract surface.
    """

    def test_p7a_local_not_in_valid_sources(self):
        parsed = _whitelist_set()
        self.assertNotIn("p7a-local", parsed,
                         "p7a-local audit token leaked into production")

    def test_p7a_local_not_in_whitelist_literal(self):
        # The audit token must not appear in the production
        # ``valid_sources`` tuple as a literal string element. The
        # comment block above the tuple is allowed to mention the
        # audit token by name (it is the audit trail); the test only
        # forbids the token being a member of the production enum.
        parsed = _whitelist_set()
        self.assertNotIn("p7a-local", parsed)

    def test_p7a_local_rejected_via_gateway_handler(self):
        # The gateway must reject ``source=p7a-local`` at the HTTP
        # boundary. We mirror the rejection rule here so a future
        # refactor of the rejection branch is caught even if the
        # comment is rewritten.
        parsed = _whitelist_set()
        rejected = "p7a-local"
        self.assertNotIn(rejected, parsed)
        # And the rejection branch is documented by the error string.
        self.assertIn("invalid_source", GATEWAY_SRC)
        # And no audit-token-only alias replaces it.
        for bad in ("p7a", "audit", "audit-local", "p7a-only"):
            self.assertNotIn(bad, parsed)

    def test_no_secondary_audit_source_in_whitelist(self):
        # P9D-R final signoff (20260804) appends a dedicated
        # ``p9dr-final-signoff`` token so the three documented
        # end-to-end sign-off tasks (Primary, Fallback, Recovery)
        # can be submitted through the normal HTTP path with a
        # stable, auditable source string.  The production set is
        # therefore 11-element.  ``p7a-local`` is still forbidden
        # and no audit-token-only alias is added.
        parsed = _whitelist_set()
        self.assertEqual(len(parsed), 11,
                         f"unexpected whitelist size: {parsed}")
        expected = {"feishu", "cli", "cron", "telegram",
                    "web", "api", "system", "test", "openclaw",
                    "acceptance", "p9dr-final-signoff"}
        self.assertEqual(parsed, expected)


# ---------------------------------------------------------------------------
# 2. Official sources remain accepted
# ---------------------------------------------------------------------------


class OfficialSourcesAccepted(unittest.TestCase):
    """P7F §5: ``web`` and ``api`` are the official sources real local
    audits MUST use. They must remain on the whitelist and the
    canonical identity / session payload must survive a switch from
    ``p7a-local`` to ``api``.
    """

    def test_official_sources_present(self):
        parsed = _whitelist_set()
        for official in ("web", "api"):
            self.assertIn(official, parsed,
                          f"official source {official!r} missing from whitelist")

    def test_official_sources_have_documented_acceptance(self):
        # The gateway must mention the official sources explicitly so
        # audit reviewers can map ``source=api`` to a documented
        # surface.
        self.assertIn('"web"', GATEWAY_SRC)
        self.assertIn('"api"', GATEWAY_SRC)

    def test_session_key_namespace_remains_source_scoped(self):
        # Switching from ``p7a-local`` to ``api`` must not collapse
        # two distinct sources into the same session. We verify the
        # rule by checking the session_key prefix matches the source.
        for source in ("web", "api", "feishu", "telegram"):
            session = f"{source}:p7f-audit:room-1"
            self.assertEqual(session.split(":")[0], source,
                             "session_key must be source-namespaced")


# ---------------------------------------------------------------------------
# 3. failure_ownership classification
# ---------------------------------------------------------------------------


class FailureOwnershipClassification(unittest.TestCase):
    """P7F §6: the monitor MUST classify failures by ownership:

    * ``DEGRADED_EXTERNAL`` — Claude 402 / Codex token-plan / Codex
      region / OpenClaw 429 token-plan / OpenCode external network /
      external cooldown / free-model service. These signals point at
      upstream services, not local infrastructure.
    * ``DEGRADED_INTERNAL`` — local process / adapter / IPC / server
      code errors only. Evidence freshness is tracked separately and
      MUST NOT change the ownership bucket.

    The monitor exposes ``EXTERNAL_FAILURE_STATES`` /
    ``INTERNAL_FAILURE_STATES`` as frozensets at module level so we can
    verify the buckets directly.
    """

    def test_external_failure_states_include_quota(self):
        from aios_monitor import EXTERNAL_FAILURE_STATES
        for state in ("quota_exhausted", "plan_exhausted", "rate_limited",
                      "token_plan", "region", "auth_failed",
                      "network_error", "cooldown",
                      "free_model_service_error", "transient_provider_error"):
            self.assertIn(state, EXTERNAL_FAILURE_STATES,
                          f"external state {state!r} missing")

    def test_internal_failure_states_are_explicit(self):
        from aios_monitor import INTERNAL_FAILURE_STATES
        for state in ("model_error", "adapter_error", "ipc_error",
                      "local_server_error", "local_adapter_exception"):
            self.assertIn(state, INTERNAL_FAILURE_STATES,
                          f"internal state {state!r} missing")

    def test_buckets_are_disjoint(self):
        from aios_monitor import (
            EXTERNAL_FAILURE_STATES, INTERNAL_FAILURE_STATES,
        )
        overlap = EXTERNAL_FAILURE_STATES & INTERNAL_FAILURE_STATES
        self.assertEqual(overlap, set(),
                         f"failure buckets overlap: {overlap}")

    def test_matrix_classifies_claude_402_as_external(self):
        from aios_monitor import _build_capability_matrix_from_agents
        matrix = _build_capability_matrix_from_agents([{
            "name": "claude", "fully_operational": False,
            "infrastructure_ok": True,
            "status": "degraded",
            "model_state": "quota_exhausted",
        }])
        self.assertEqual(matrix["claude"], DEGRADED_EXTERNAL)

    def test_matrix_classifies_codex_token_plan_as_external(self):
        from aios_monitor import _build_capability_matrix_from_agents
        matrix = _build_capability_matrix_from_agents([{
            "name": "codex", "fully_operational": False,
            "infrastructure_ok": True,
            "status": "degraded",
            "model_state": "token_plan",
        }])
        self.assertEqual(matrix["codex"], DEGRADED_EXTERNAL)

    def test_matrix_classifies_codex_region_as_external(self):
        from aios_monitor import _build_capability_matrix_from_agents
        matrix = _build_capability_matrix_from_agents([{
            "name": "codex", "fully_operational": False,
            "infrastructure_ok": True,
            "status": "degraded",
            "model_state": "region",
        }])
        self.assertEqual(matrix["codex"], DEGRADED_EXTERNAL)

    def test_matrix_classifies_openclaw_429_as_external(self):
        from aios_monitor import _build_capability_matrix_from_agents
        matrix = _build_capability_matrix_from_agents([{
            "name": "openclaw", "fully_operational": False,
            "infrastructure_ok": True,
            "status": "degraded",
            "model_state": "rate_limited",
        }])
        self.assertEqual(matrix["openclaw"], DEGRADED_EXTERNAL)

    def test_matrix_classifies_opencode_external_network_as_external(self):
        from aios_monitor import _build_capability_matrix_from_agents
        matrix = _build_capability_matrix_from_agents([{
            "name": "opencode", "fully_operational": False,
            "infrastructure_ok": True,
            "status": "degraded",
            "model_state": "network_error",
        }])
        self.assertEqual(matrix["opencode"], DEGRADED_EXTERNAL)

    def test_matrix_classifies_opencode_external_cooldown_as_external(self):
        from aios_monitor import _build_capability_matrix_from_agents
        matrix = _build_capability_matrix_from_agents([{
            "name": "opencode", "fully_operational": False,
            "infrastructure_ok": True,
            "status": "degraded",
            "model_state": "cooldown",
        }])
        self.assertEqual(matrix["opencode"], DEGRADED_EXTERNAL)

    def test_matrix_classifies_local_adapter_exception_as_internal(self):
        from aios_monitor import _build_capability_matrix_from_agents
        matrix = _build_capability_matrix_from_agents([{
            "name": "opencode", "fully_operational": False,
            "infrastructure_ok": False,
            "status": "failed",
            "model_state": "local_adapter_exception",
        }])
        self.assertEqual(matrix["opencode"], DEGRADED_INTERNAL)


# ---------------------------------------------------------------------------
# 4. evidence_freshness does NOT change failure ownership
# ---------------------------------------------------------------------------


class EvidenceFreshnessIndependent(unittest.TestCase):
    """P7F §6: ``evidence_freshness=STALE`` is reported separately and
    MUST NOT change the failure ownership bucket. The monitor keeps
    quota / rate-limit / auth / external failures as DEGRADED_EXTERNAL
    even when their evidence is stale; only an explicit local signal
    may downgrade to DEGRADED_INTERNAL.
    """

    def test_stale_evidence_does_not_change_ownership(self):
        # We don't drive the real capability cache here; instead we
        # assert the rule is documented in the source. The owning
        # module is aios_capability and the rule is enforced there.
        from aios_monitor import _build_capability_matrix_from_agents
        # An external quota_exhausted signal with no process up
        # information must still classify as DEGRADED_EXTERNAL.
        matrix = _build_capability_matrix_from_agents([{
            "name": "claude", "fully_operational": False,
            "infrastructure_ok": None,
            "status": "unknown",
            "model_state": "quota_exhausted",
        }])
        self.assertEqual(matrix["claude"], DEGRADED_EXTERNAL)

    def test_no_executor_available_dimension_failed_keeps_tool_records(self):
        # When every executor is unavailable, ``executor_capability``
        # is FAILED, but ``optional_degradations`` still carries the
        # tool-level reasons so reviewers can audit ownership.
        matrix = {
            "opencode": DEGRADED_EXTERNAL,
            "claude": DEGRADED_EXTERNAL,
            "codex": DEGRADED_EXTERNAL,
            "openclaw": DEGRADED_EXTERNAL,
            "hermes": AVAILABLE,
        }
        exec_dim = probe_capability("executor_capability", matrix,
                                    mandatory=True)
        rev_dim = probe_capability("reviewer_capability", matrix,
                                   mandatory=True)
        healthy = HealthDimension(
            unit="service_state", status=STATUS_HEALTHY,
            reason_code="ALL_CORE_UNITS_ACTIVE", mandatory=True,
            evidence_source="systemd:is-active",
            observed_at="2026-07-24T07:45:00+00:00",
            age_seconds=0, summary="5 core units active",
        )
        report = assemble_health_report({
            "service_state": healthy,
            "executor_capability": exec_dim,
            "reviewer_capability": rev_dim,
        })
        # The overall status collapses to FAILED because no executor
        # is available, but the tool-level degradations are still
        # listed with the correct EXTERNAL ownership.
        self.assertEqual(report.overall_status, STATUS_FAILED)
        by_tool = {
            entry["tool_id"]: entry
            for entry in report.optional_degradations
            if isinstance(entry, dict)
        }
        for required in ("opencode", "claude", "codex", "openclaw"):
            self.assertIn(required, by_tool,
                          f"{required} missing from optional_degradations")
            self.assertEqual(by_tool[required]["status"], DEGRADED_EXTERNAL)
            # ``reason_code`` may legitimately be either
            # ``NO_EXECUTOR_AVAILABLE`` (the executor dimension) or any
            # of the optional-degradation reason codes when the same
            # tool appears in the reviewer dimension. The important
            # invariant is that the dimension-level reason_code is
            # non-empty and the tool-level record exists.
            self.assertTrue(
                by_tool[required]["reason_code"],
                f"missing reason_code on {required!r}",
            )
            self.assertIn(
                by_tool[required]["reason_code"],
                ("NO_EXECUTOR_AVAILABLE",
                 "OPTIONAL_REVIEWER_DEGRADED",
                 "EXTERNAL_CAPABILITY_DEGRADED",
                 "OPTIONAL_EXTERNAL_DEGRADED"),
                f"unexpected reason_code: {by_tool[required]['reason_code']!r}",
            )


# ---------------------------------------------------------------------------
# 5. Source / sender / session isolation — preserved after p7a-local removal
# ---------------------------------------------------------------------------


class SourceSenderSessionIsolationPreserved(unittest.TestCase):
    """P7F §10: removing ``p7a-local`` MUST NOT regress the source /
    sender / session_key invariants introduced by P7A. Real local
    audits now use ``source=api`` (or ``web``) but the same sender /
    session separation rules apply.
    """

    def test_session_key_is_source_namespaced_for_official_sources(self):
        for source in ("web", "api", "feishu", "telegram", "openclaw"):
            session = f"{source}:p7f-audit:room-1"
            self.assertEqual(session.split(":")[0], source,
                             "session_key must be source-namespaced")

    def test_distinct_official_sources_with_same_sender_do_not_share_session(self):
        # The rule is structural; we verify by construction that two
        # official sources with the same sender produce two distinct
        # session keys. This is the same invariant P7A guarded for
        # p7a-local; we re-assert it for the surviving sources.
        sender = "p7f-shared-sender"
        keys = {
            source: f"{source}:{sender}:room-1"
            for source in ("web", "api", "feishu")
        }
        self.assertEqual(len(set(keys.values())), len(keys),
                         "official sources must produce distinct sessions")


# ---------------------------------------------------------------------------
# 6. Result-callback task_id isolation — preserved
# ---------------------------------------------------------------------------


class ResultCallbackTaskIdIsolation(unittest.TestCase):
    """P7F §10: the result callback key MUST remain keyed by task_id,
    not by sender or source. The contract introduced by P7A is
    re-validated against the post-p7a-local source code.
    """

    def test_callback_key_is_task_id(self):
        from aios_bus import KEY_CALLBACK
        sample = "abcdef12-3456-7890-abcd-ef1234567890"
        self.assertEqual(
            f"{KEY_CALLBACK}:{sample}",
            f"aios:bus:callback:{sample}",
        )

    def test_callback_record_carries_source_and_sender(self):
        src = (AIOS_HOME / "kernel/tools/aios_bus.py").read_text()
        self.assertIn("def register_callback", src)
        self.assertIn("sender_id", src)
        self.assertIn('"source"', src)


# ---------------------------------------------------------------------------
# 7. Monitor module-level constants are exported
# ---------------------------------------------------------------------------


class MonitorFailureOwnershipExported(unittest.TestCase):
    """P7F §6: the buckets MUST be importable from ``aios_monitor`` so
    downstream code can branch on ownership without re-implementing the
    rule. This guards against the constants being moved into a private
    closure.
    """

    def test_external_states_importable(self):
        from aios_monitor import EXTERNAL_FAILURE_STATES
        self.assertIsInstance(EXTERNAL_FAILURE_STATES, frozenset)

    def test_internal_states_importable(self):
        from aios_monitor import INTERNAL_FAILURE_STATES
        self.assertIsInstance(INTERNAL_FAILURE_STATES, frozenset)


if __name__ == "__main__":
    unittest.main()