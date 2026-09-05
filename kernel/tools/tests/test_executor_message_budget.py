#!/usr/bin/env python3
"""
Targeted tests for ``aios_executor_message_budget`` and the
``aios_orchestrator._execution_text`` integration that consumes it.

These tests are the message-budget contract.  They MUST stay
narrowly scoped to the executor-prompt-length budget; no routing,
sandbox, planner or reviewer invariants are touched here.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest

# Make the kernel/tools package importable when pytest runs from
# the repository root.
_HERE = os.path.abspath(__file__)
_TESTS_DIR = os.path.dirname(_HERE)
_TOOLS = os.path.dirname(_TESTS_DIR)
_REPO = os.path.dirname(os.path.dirname(_TOOLS))
for _p in (_TOOLS, _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _import_budget():
    return importlib.import_module("aios_executor_message_budget")


def _import_orchestrator():
    return importlib.import_module("aios_orchestrator")


def _import_host_inject():
    return importlib.import_module("aios_orchestrator_host_evidence_injection")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE_LEAD = (
    "Original user goal (authoritative):\n"
    "{goal}\n\n"
    "Assigned node:\nComplete the entire original user goal.\n\n"
    "Acceptance summary:\n{acceptance}\n\n"
    "Evidence mode: semantic\n"
    "System metadata:\n"
    "- Parent task ID: PT-test\n"
    "- Actual executor: codex\n"
    "- Successful final status: completed\n"
    "Copy requested metadata exactly. Obey every original literal/path. "
    "Return only a complete, comprehensive result with concrete evidence. "
    "Never invent live facts. Recompute numeric/comparative claims. "
    "Stay under 7000 characters with no thinking transcript."
)


def _make_base(goal: str = "检查当前 AIOS 核心生产状态并用一句简洁中文总结。严格只读。",
               acceptance: str = "用一句简洁中文总结当前 AIOS 核心生产状态。严格只读。",
               extras: str = "") -> str:
    body = _BASE_LEAD.format(goal=goal, acceptance=acceptance)
    body += (
        "\n\nSANDBOX CONSTRAINT: this Codex sandbox has NO direct access "
        "to host loopback services (127.0.0.1:18801 etc.). Do NOT "
        "attempt to curl localhost, call systemctl / journalctl, or "
        "read /proc. Use the AUTHORITATIVE HOST EVIDENCE block AIOS "
        "supplies as the only fact source for host state."
        "\n\nFACT-USE: every concrete fact (PID, timestamp, status, "
        "count) in your answer MUST appear verbatim in the "
        "AUTHORITATIVE HOST EVIDENCE block. Do NOT extrapolate "
        "'stable'/'healthy'/'broken' from a single field unless the "
        "evidence explicitly states that. End with one line of the "
        "form '当前状态判定: <NORMAL|PARTIAL_ANOMALY|ANOMALY> — <reason>'."
        "\n\nOUTPUT CONTRACT (binding): the final answer MUST be exactly "
        "ONE concise Chinese sentence. NO heading, NO table, NO bullet list, "
        "NO evidence dump, NO host-evidence reproduction, NO extra explanation."
    )
    if extras:
        body += "\n\n" + extras
    return body


def _make_evidence(profile: str = "GENERAL", n_items: int = 9,
                   include_failed: bool = True) -> dict:
    items = []
    caps = (
        "SYSTEMD_USER_STATUS", "SYSTEMD_USER_SHOW", "SYSTEMD_USER_FAILED",
        "JOURNAL_USER_UNIT_RECENT", "PROCESS_LOOKUP", "LISTENING_PORTS",
        "LOCAL_HTTP_GET", "AIOS_HEALTH_SNAPSHOT", "AIOS_TASK_STATUS",
    )
    for i in range(n_items):
        cap = caps[i % len(caps)]
        items.append({
            "capability": cap,
            "ok": True,
            "unit": f"aios-{cap.lower()}.service",
            "status": 200 if "HTTP" in cap or "AIOS" in cap else "active",
            "body": ("lorem ipsum " * 80),
        })
    if include_failed:
        items[2]["body"] = ("FAILED unit log excerpt " * 40)
    return {
        "profile": profile,
        "generated_at": "2026-08-11T20:00:00Z",
        "summary": {"total": n_items, "ok": n_items, "error": 0,
                    "by_capability": {c: "ok" for c in caps[:n_items]}},
        "items": items,
    }


def _patch_workflow(parent_id: str, evidence: dict) -> None:
    """Wire the supplied evidence dict into the ``get_workflow`` shim."""
    orch = _import_orchestrator()
    inj = _import_host_inject()
    orch.get_workflow = lambda pid: {"host_evidence": evidence}
    inj.load_workflow_host_evidence = lambda pid: evidence if pid else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBudgetConstants(unittest.TestCase):
    """1-3. HARD_LIMIT, TARGET_LIMIT, priority order are explicit."""

    def test_hard_limit_is_4096(self):
        b = _import_budget()
        self.assertEqual(b.HARD_LIMIT, 4096)

    def test_target_limit_is_3800(self):
        b = _import_budget()
        self.assertEqual(b.TARGET_LIMIT, 3800)

    def test_priority_order_includes_all_required_tags(self):
        b = _import_budget()
        self.assertEqual(
            b.PRIORITY_ORDER,
            ("base", "hf_instruction", "host_evidence",
             "failed_unit", "repair_context"),
        )


class TestProfileCaps(unittest.TestCase):
    """Profile-scoped caps must be distinct and bounded."""

    def test_general_ops_audit_code_have_distinct_caps(self):
        b = _import_budget()
        general = b.profile_caps("GENERAL")
        ops = b.profile_caps("OPS")
        audit = b.profile_caps("AUDIT")
        code = b.profile_caps("CODE")
        # GENERAL must be tighter or equal on every cap.
        self.assertLessEqual(general[0], ops[0])
        self.assertLessEqual(general[0], audit[0])
        self.assertLessEqual(general[0], code[0])

    def test_unknown_profile_falls_back_to_general(self):
        b = _import_budget()
        self.assertEqual(b.profile_caps("MADEUP"), b.profile_caps("GENERAL"))


class TestAssemblyAllProfiles(unittest.TestCase):
    """2. GENERAL / OPS / AUDIT / CODE / repair share one budget helper."""

    def _run(self, profile: str, repair: str = "") -> int:
        b = _import_budget()
        ev = _make_evidence(profile=profile, n_items=9)
        return b.assemble_executor_message(
            _make_base(extras=repair),
            host_evidence=ev,
            profile=profile,
        ).final_length

    def test_general_under_target(self):
        self.assertLessEqual(self._run("GENERAL"), 3800)

    def test_ops_under_target(self):
        self.assertLessEqual(self._run("OPS"), 3800)

    def test_audit_under_target(self):
        self.assertLessEqual(self._run("AUDIT"), 3800)

    def test_code_under_target(self):
        self.assertLessEqual(self._run("CODE"), 3800)

    def test_repair_attempt_under_target(self):
        b = _import_budget()
        ev = _make_evidence(profile="OPS")
        report = b.assemble_executor_message(
            _make_base(extras="Bounded repair (truncated to 1800 chars):\n"
                       + ("PREVIOUS " * 250)),
            host_evidence=ev,
            repair_context=("PREVIOUS_RESULT " * 250),
            profile="OPS",
        )
        self.assertLessEqual(report.final_length, 3800)
        self.assertTrue(report.passed)


class TestPriorityPreservation(unittest.TestCase):
    """4. Critical content survives trimming, less-critical content is clipped."""

    def test_user_goal_anchor_survives_huge_base(self):
        b = _import_budget()
        huge = _make_base() + ("A" * 12000)
        report = b.assemble_executor_message(huge)
        self.assertIn("Original user goal (authoritative):", report.final_message)
        self.assertLessEqual(len(report.final_message), b.HARD_LIMIT)

    def test_repair_context_dropped_when_budget_full(self):
        b = _import_budget()
        ev = _make_evidence(profile="OPS", n_items=9)
        report = b.assemble_executor_message(
            _make_base(extras="X" * 4000),
            host_evidence=ev,
            repair_context=("R" * 2000),
            profile="OPS",
        )
        self.assertLessEqual(report.final_length, 3800)

    def test_output_format_and_acceptance_preserved(self):
        b = _import_budget()
        base = _make_base()
        report = b.assemble_executor_message(base)
        self.assertIn("Acceptance summary:", report.final_message)
        self.assertIn("OUTPUT CONTRACT", report.final_message)
        self.assertIn("System metadata:", report.final_message)


class TestHardGuard(unittest.TestCase):
    """5 + 6. P0 oversize must be flagged; post-construction guard never exceeded."""

    def test_post_construction_hard_limit_enforced(self):
        b = _import_budget()
        report = b.assemble_executor_message(_make_base())
        self.assertLessEqual(report.final_length, b.HARD_LIMIT)

    def test_p0_oversize_flagged_not_silently_dropped(self):
        b = _import_budget()
        huge = _make_base() + ("Q" * 22000)
        report = b.assemble_executor_message(huge)
        if report.final_length > b.HARD_LIMIT:
            self.assertTrue(report.overflow)

    def test_hard_clip_helper(self):
        b = _import_budget()
        msg = "X" * 5000
        clipped = b.hard_clip_for_protocol(msg)
        self.assertEqual(len(clipped), b.HARD_LIMIT)


class TestCompactHostEvidence(unittest.TestCase):
    """7. Host Evidence uses compact representation, not full JSON dump."""

    def test_full_body_not_in_compact_index(self):
        b = _import_budget()
        ev = _make_evidence(profile="OPS", n_items=9)
        report = b.assemble_executor_message(
            _make_base(), host_evidence=ev, profile="OPS",
        )
        self.assertNotIn("lorem ipsum lorem ipsum lorem ipsum",
                         report.final_message)

    def test_compact_index_lists_only_named_fields(self):
        b = _import_budget()
        ev = _make_evidence(profile="OPS", n_items=9)
        idx = b.render_compact_host_evidence_index(
            ev, section_cap=1300, items_cap=10,
        )
        self.assertIn("AUTHORITATIVE HOST EVIDENCE (compact index", idx)
        self.assertNotIn("FAILED unit log excerpt", idx)


class TestOrchestratorIntegration(unittest.TestCase):
    """``_execution_text`` must hit the helper and never exceed HARD_LIMIT."""

    def setUp(self):
        self.orch = _import_orchestrator()
        self.b = _import_budget()

    def test_no_host_evidence_under_limit(self):
        text = self.orch._execution_text(
            goal="检查当前 AIOS 核心生产状态并用一句简洁中文总结。严格只读。",
            node={
                "task": "检查当前 AIOS 核心生产状态并用一句简洁中文总结。严格只读。",
                "acceptance": ["用一句简洁中文总结当前 AIOS 核心生产状态。严格只读。"],
                "evidence_mode": "semantic", "role": "opencode",
            },
            repair_reason="", previous_result="",
            parent_id="", actual_executor="codex", single_node=True,
        )
        self.assertLessEqual(len(text), self.b.HARD_LIMIT)

    def test_with_general_evidence_under_limit(self):
        ev = _make_evidence(profile="GENERAL", n_items=9)
        _patch_workflow("PT-test", ev)
        text = self.orch._execution_text(
            goal="检查当前 AIOS 核心生产状态并用一句简洁中文总结。严格只读。",
            node={
                "task": "检查当前 AIOS 核心生产状态并用一句简洁中文总结。严格只读。",
                "acceptance": ["用一句简洁中文总结当前 AIOS 核心生产状态。严格只读。"],
                "evidence_mode": "semantic", "role": "opencode",
            },
            repair_reason="", previous_result="",
            parent_id="PT-test", actual_executor="codex", single_node=True,
        )
        self.assertLessEqual(len(text), self.b.HARD_LIMIT)
        self.assertLessEqual(len(text), 3800)
        self.assertIn("Original user goal (authoritative):", text)

    def test_with_ops_evidence_under_limit(self):
        ev = _make_evidence(profile="OPS", n_items=9)
        _patch_workflow("PT-ops", ev)
        text = self.orch._execution_text(
            goal="audit /var/log/aios",
            node={
                "task": "audit /var/log/aios",
                "acceptance": ["summarise"],
                "evidence_mode": "semantic", "role": "opencode",
            },
            repair_reason="", previous_result="",
            parent_id="PT-ops", actual_executor="codex", single_node=True,
        )
        self.assertLessEqual(len(text), self.b.HARD_LIMIT)

    def test_huge_previous_result_under_limit(self):
        text = self.orch._execution_text(
            goal="report",
            node={
                "task": "report the count",
                "acceptance": ["report the exact count"],
                "evidence_mode": "semantic", "role": "opencode",
            },
            repair_reason="material_false_numeric_claim",
            previous_result=("X" * 10240),
            parent_id="", actual_executor="codex", single_node=True,
        )
        self.assertLessEqual(len(text), self.b.HARD_LIMIT)


class TestUnicodePreservation(unittest.TestCase):
    """CJK / multi-byte content must survive; budget counts chars not bytes."""

    def test_chinese_user_goal_survives(self):
        b = _import_budget()
        report = b.assemble_executor_message(_make_base())
        self.assertIn("检查当前 AIOS 核心生产状态", report.final_message)
        self.assertIn("严格只读", report.final_message)
        self.assertIn("当前状态判定", report.final_message)

    def test_chinese_under_target(self):
        b = _import_budget()
        ev = _make_evidence(profile="GENERAL", n_items=9)
        report = b.assemble_executor_message(
            _make_base(), host_evidence=ev, profile="GENERAL",
        )
        self.assertLessEqual(report.final_length, b.TARGET_LIMIT)


class TestStressInput(unittest.TestCase):
    """20,000+ char input must clamp down to target <=3800 / hard <=4096."""

    def test_20000_chars_input_under_target(self):
        b = _import_budget()
        stress_base = _make_base() + ("Z" * 22000)
        self.assertGreater(len(stress_base), 20000)
        report = b.assemble_executor_message(stress_base)
        self.assertLessEqual(report.final_length, b.TARGET_LIMIT)

    def test_final_always_under_hard_limit(self):
        b = _import_budget()
        ev = _make_evidence(profile="OPS", n_items=9)
        stress_base = _make_base(extras="Q" * 22000)
        report = b.assemble_executor_message(
            stress_base, host_evidence=ev, profile="OPS",
        )
        self.assertLessEqual(report.final_length, b.HARD_LIMIT)


class TestRoutingAndSandboxUnchanged(unittest.TestCase):
    """The budget helper MUST NOT mutate routing, sandbox, or reviewer config."""

    def test_budget_module_does_not_import_routing(self):
        b = _import_budget()
        src_path = os.path.join(_TOOLS, "aios_executor_message_budget.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        for banned in ("routing", "sandbox", "reviewer", "hermes",
                       "planner", "provider"):
            self.assertNotIn(
                f"import aios_{banned}", src,
                f"budget module must not import aios_{banned}",
            )

    def test_assembly_has_no_sandbox_or_reviewer_knobs(self):
        import inspect
        b = _import_budget()
        sig = inspect.signature(b.assemble_executor_message)
        for name in sig.parameters:
            self.assertNotIn("sandbox", name)
            self.assertNotIn("reviewer", name)
            self.assertNotIn("planner", name)


if __name__ == "__main__":
    unittest.main()

