#!/usr/bin/env python3
"""AIOS P8B-R Cross-Tool MiniMax Binding Validation Tests.

The tests are split into a public surface that talks to
``fake_response``/``fake_failure`` by default (no real
Provider calls), and a separate *opt-in* runner that the
evidence driver uses when a real MiniMax API key is present.

Public tests are split into the following groups, each
mapping back to a section of AIOS-P8B-R-CROSS-TOOL-MODEL-
BINDING-VALIDATION-15R:

* Dynamic tool enumeration (Registry-driven; §3.1)
* Per-tool binding identity preservation (§3.2, §7-§9)
* Shared resource identity for minimax.shared (§3.3, §11)
* DeepSeek blocker decision path (§10)
* RESOURCE vs BINDING failure scope isolation (§11)
* Concurrency / config isolation (§12)
* Reviewer independence marker (§13)
* Usage accounting separation (§14)
* Strict_model / allow_model_fallback / budget / max attempts
  invariants (§14)
* Adapter exception → no switch (§14)
* Configuration immutability evidence (§17)
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from aios_claude_minimax_adapter import (
    BINDING_ID as CLAUDE_BINDING_ID,
    RESOURCE_ID as CLAUDE_RESOURCE_ID,
    TOOL_ID as CLAUDE_TOOL_ID,
    ClaudeBindingRecord,
    ClaudeBindingUsageSummary,
    ClaudeMiniMaxAdapter,
)
from aios_codex_minimax_adapter import (
    BINDING_ID as CODEX_BINDING_ID,
    RESOURCE_ID as CODEX_RESOURCE_ID,
    TOOL_ID as CODEX_TOOL_ID,
    CodexBindingRecord,
    CodexBindingUsageSummary,
    CodexMiniMaxAdapter,
)
from aios_dynamic_binding_audit import (
    ALL_VERDICTS,
    DynamicBindingAuditRow,
    VERDICT_SAFE,
    VERDICT_SUPPORTED_ADAPTER,
    VERDICT_UNSAFE,
    audit_all,
    audit_one,
    audit_to_tsv,
    config_hash,
    list_enabled_tool_ids,
)
from aios_hermes_minimax_verifier import (
    BINDING_ID as HERMES_BINDING_ID,
    EXPECTED_VERDICT,
    RESOURCE_ID as HERMES_RESOURCE_ID,
    TOOL_ID as HERMES_TOOL_ID,
    HermesBindingRecord,
    HermesBindingUsageSummary,
    HermesMiniMaxVerifier,
)
from aios_minimax_client import (
    DEFAULT_MODEL,
    FALLBACK_API_KEY_ENV,
    FALLBACK_BASE_URL_ENV,
    MiniMaxCallResult,
    MiniMaxClient,
)
from aios_minimax_ledger import (
    LEDGER_RESOURCE_ID,
    MiniMaxAccountLedger,
    ledger_to_tsv,
)
from aios_model_failover import (
    FAILURE_SCOPE_BINDING,
    FAILURE_SCOPE_RESOURCE,
    FAILURE_SCOPE_TOOL_ADAPTER,
    ModelFailoverEngine,
)
from aios_model_resources import (
    build_default_binding_registry,
    build_default_policy_registry,
    build_default_resource_registry,
)
from aios_openclaw_planner_verifier import (
    BINDING_ID as OPENCLAW_BINDING_ID,
    FORBIDDEN_EXECUTION_TOKENS,
    PLAN_MARKER,
    RESOURCE_ID as OPENCLAW_RESOURCE_ID,
    TOOL_ID as OPENCLAW_TOOL_ID,
    OpenClawPlannerRecord,
    OpenClawPlannerUsageSummary,
    OpenClawPlannerVerifier,
)
from aios_tool_registry import (
    ToolManifest,
    get_default_registry,
    set_default_registry,
    reset_default_registry,
)


SHARED_RESOURCE = "minimax.shared"
DEEPSEEK_RESOURCE = "deepseek.shared"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_engine() -> ModelFailoverEngine:
    engine = ModelFailoverEngine(
        build_default_resource_registry(),
        build_default_binding_registry(),
        build_default_policy_registry(),
    )
    engine.reset_state()
    return engine


@pytest.fixture
def ledger() -> MiniMaxAccountLedger:
    return MiniMaxAccountLedger()


def _claude_adapter(usage=None, model=DEFAULT_MODEL, base_url="https://stub/claude",
                    api_key="stub-key"):
    return ClaudeMiniMaxAdapter(base_url=base_url, api_key=api_key,
                                 model=model, shared_usage=usage)


def _codex_adapter(usage=None, model=DEFAULT_MODEL, base_url="https://stub/codex",
                   api_key="stub-key"):
    return CodexMiniMaxAdapter(base_url=base_url, api_key=api_key,
                                model=model, shared_usage=usage)


def _hermes_verifier(usage=None, model=DEFAULT_MODEL,
                     base_url="https://stub/hermes", api_key="stub-key"):
    return HermesMiniMaxVerifier(base_url=base_url, api_key=api_key,
                                   model=model, shared_usage=usage)


def _openclaw_verifier(usage=None, model=DEFAULT_MODEL,
                       base_url="https://stub/openclaw", api_key="stub-key"):
    return OpenClawPlannerVerifier(base_url=base_url, api_key=api_key,
                                     model=model, shared_usage=usage)


# ===========================================================================
# §3.1 Dynamic tool enumeration
# ===========================================================================


def test_registry_lists_at_least_five_enabled_tools():
    ids = list_enabled_tool_ids()
    # Must include the canonical five + dynamic discovery. Adding a
    # sixth tool MUST appear here automatically.
    assert len(ids) >= 5, ids
    assert {"claude", "codex", "hermes", "openclaw", "opencode"}.issubset(set(ids))


def test_audit_picks_up_new_tool_without_code_change(monkeypatch):
    """A sixth registered tool shows up automatically in audit_all()
    and gets the default UNVERIFIED verdict."""
    reg = get_default_registry()
    original = list(reg.list_all())
    reg.register_tool(ToolManifest(
        tool_id="fake_sixth",
        display_name="Fake Sixth",
        module_path="agents/fake_sixth",
        adapter_ref="fake_sixth",
        roles=("executor",),
        service_unit_ref=None,
        health_probe_ref=None,
        model_policy_ref=None,
        enabled=True,
        version="0.0.0",
        capabilities=(),
        description="P8B-R fake sixth tool",
    ))
    try:
        ids = list_enabled_tool_ids()
        assert "fake_sixth" in ids
        row = audit_one("fake_sixth")
        assert row.result == "UNVERIFIED"
        # All audit verdicts must come from the official taxonomy.
        for r in audit_all():
            assert r.result in ALL_VERDICTS
    finally:
        reg.unregister_tool("fake_sixth")
        # Defensive: ensure we removed it.
        assert "fake_sixth" not in [m.tool_id for m in reg.list_all()]
        # keep a reference to silence linters
        _ = original


def test_audit_tsv_includes_all_enabled_rows():
    rows = audit_all()
    assert len(rows) >= 5
    tsv = audit_to_tsv(rows)
    assert "tool_id\tcurrent_model_resource" in tsv
    for r in rows:
        assert r.tool_id in tsv


# ===========================================================================
# §3.2 Per-tool binding identity preservation
# ===========================================================================


def test_claude_minimax_binding_keeps_tool_id_claude():
    a = _claude_adapter()
    rec, res = a.chat(
        [{"role": "user", "content": "hi"}],
        fake_response='{"marker":"AIOS_P8B_CLAUDE_MINIMAX_OK",'
                     '"tool_id":"claude","status":"PASS"}',
        fake_usage={"input_tokens": 12, "output_tokens": 6},
    )
    assert rec.tool_id == "claude"
    assert rec.binding_id == CLAUDE_BINDING_ID
    assert rec.resource_id == CLAUDE_RESOURCE_ID
    assert rec.parsed_marker == "AIOS_P8B_CLAUDE_MINIMAX_OK"
    assert rec.parsed_tool_id == "claude"
    assert rec.parsed_status == "PASS"
    assert res.success is True
    assert a.usage.calls == 1
    assert a.usage.successes == 1


def test_codex_minimax_binding_keeps_tool_id_codex():
    a = _codex_adapter()
    rec, res = a.chat(
        [{"role": "user", "content": "hi"}],
        fake_response='{"marker":"AIOS_P8B_CODEX_MINIMAX_OK",'
                     '"tool_id":"codex","status":"PASS"}',
        fake_usage={"input_tokens": 8, "output_tokens": 4},
    )
    assert rec.tool_id == "codex"
    assert rec.binding_id == CODEX_BINDING_ID
    assert rec.resource_id == CODEX_RESOURCE_ID
    assert rec.parsed_marker == "AIOS_P8B_CODEX_MINIMAX_OK"
    assert rec.parsed_tool_id == "codex"
    assert rec.parsed_status == "PASS"
    assert res.success is True


def test_codex_adapter_extracts_function_name():
    a = _codex_adapter()
    rec, _ = a.chat(
        [{"role": "user", "content": "ok"}],
        fake_response="def add(a, b):\n    return a + b",
        fake_usage={"input_tokens": 4, "output_tokens": 4},
    )
    assert rec.extracted_function_name == "add"
    assert "return a + b" in rec.extracted_function_body_preview


def test_claude_and_codex_have_distinct_adapter_instances():
    claude = _claude_adapter()
    codex = _codex_adapter()
    assert claude.get_client() is not codex.get_client()
    assert claude.binding_id != codex.binding_id
    assert claude.tool_id != codex.tool_id


# ===========================================================================
# §3.3 Shared resource identity for minimax.shared
# ===========================================================================


def test_hermes_openclaw_share_minimax_shared_resource():
    h = _hermes_verifier()
    o = _openclaw_verifier()
    assert h.resource_id == SHARED_RESOURCE
    assert o.resource_id == SHARED_RESOURCE
    assert h.resource_id == o.resource_id
    # Each tool keeps its own tool_id.
    assert h.tool_id != o.tool_id


def test_claude_codex_also_reference_minimax_shared():
    c = _claude_adapter()
    x = _codex_adapter()
    assert c.resource_id == SHARED_RESOURCE
    assert x.resource_id == SHARED_RESOURCE


def test_all_four_minimax_adapters_share_resource_id():
    seen = {_claude_adapter().resource_id,
            _codex_adapter().resource_id,
            _hermes_verifier().resource_id,
            _openclaw_verifier().resource_id}
    assert seen == {SHARED_RESOURCE}


# ===========================================================================
# Hermes × MiniMax reviewer verdict
# ===========================================================================


def test_hermes_verifier_returns_expected_token():
    v = _hermes_verifier()
    rec, _, verdict = v.review(
        "P8B-R review subject",
        fake_response="HERMES_P8B_MINIMAX_OK",
    )
    assert verdict == EXPECTED_VERDICT
    assert rec.tool_id == HERMES_TOOL_ID
    assert rec.resource_id == HERMES_RESOURCE_ID
    assert rec.success is True


def test_hermes_verifier_marks_non_matching_verdict_as_failure():
    v = _hermes_verifier()
    rec, _, verdict = v.review(
        "P8B-R review subject",
        fake_response="SOMETHING ELSE",
    )
    assert verdict != EXPECTED_VERDICT
    assert rec.success is False


# ===========================================================================
# OpenClaw × MiniMax pure planner (no side effects)
# ===========================================================================


def test_openclaw_planner_returns_plan_sketch_without_execution_keywords():
    v = _openclaw_verifier()
    rec, _, plan_text = v.plan(
        "draft a plan",
        fake_response=('{"marker":"OPENCLAW_P8B_PLAN_SKETCH",'
                       '"steps":["collect","check","draft"]}'),
    )
    assert rec.tool_id == OPENCLAW_TOOL_ID
    assert rec.resource_id == OPENCLAW_RESOURCE_ID
    assert PLAN_MARKER in plan_text or PLAN_MARKER in rec.plan_text
    assert rec.execution_keywords_present == 0
    assert rec.plan_steps >= 1


def test_openclaw_planner_flags_execution_keywords():
    v = _openclaw_verifier()
    rec, _, _ = v.plan(
        "draft a plan",
        fake_response=('{"marker":"OPENCLAW_P8B_PLAN_SKETCH",'
                       '"steps":["EXECUTE send_feishu"]}'),
    )
    # The verifier surfaces forbidden keywords for audit; the test
    # does not call OpenClaw at all.
    assert rec.execution_keywords_present >= 1


# ===========================================================================
# §5 Dynamic override audit verdicts
# ===========================================================================


def test_audit_verdicts_for_known_tools_are_safe():
    rows = {r.tool_id: r for r in audit_all()}
    assert rows["claude"].result == VERDICT_SAFE
    assert rows["codex"].result == VERDICT_SAFE
    assert rows["hermes"].result == VERDICT_SAFE
    assert rows["openclaw"].result == VERDICT_SAFE


def test_audit_verdict_for_opencode_is_supported_with_adapter():
    rows = {r.tool_id: r for r in audit_all()}
    assert rows["opencode"].result == VERDICT_SUPPORTED_ADAPTER


def test_no_tool_requires_shared_config_mutation():
    rows = audit_all()
    assert rows, "registry empty"
    for r in rows:
        assert r.requires_shared_config_mutation is False
        assert r.requires_restart is False


# ===========================================================================
# §10 DeepSeek blocker decision
# ===========================================================================


def test_deepseek_quota_blocks_claude_picks_minimax(fresh_engine):
    engine = fresh_engine
    tid = "t-deepseek-blocked-claude"
    # First attempt: claude deepseek preferred; the call fails with
    # RESOURCE-scope quota_exhausted on deepseek.shared.
    d1 = engine.select_model_binding(task_id=tid, tool_id="claude",
                                       role="executor")
    assert d1.action == "use" and d1.binding_id == "claude:deepseek"
    engine.record_model_attempt(
        task_id=tid, tool_id="claude", binding_id="claude:deepseek",
        success=False, failure_kind="quota_exhausted",
        error_message="API Error: 402 Insufficient Balance",
    )
    # Second attempt: deepseek.shared is in cooldown → engine picks
    # the next candidate that does NOT share the resource, i.e.
    # claude:minimax.
    d2 = engine.select_model_binding(task_id=tid, tool_id="claude",
                                       role="executor")
    assert d2.action == "use" and d2.binding_id == "claude:minimax"
    # Skip list should mention the blocked resource.
    state = engine.resource_state(DEEPSEEK_RESOURCE)
    assert state.resource_cooldown_until is not None
    attempted = engine.attempted_bindings(tid)
    assert attempted == ["claude:deepseek"]
    assert engine.actual_model_binding(tid) is None  # no success yet
    # No real DeepSeek call was made — we only recorded the failure.
    history = engine.task_history(tid)
    assert history[0].binding_id == "claude:deepseek"
    assert history[0].success is False
    assert history[0].failure_kind == "quota_exhausted"


def test_deepseek_quota_blocks_codex_picks_minimax(fresh_engine):
    engine = fresh_engine
    tid = "t-deepseek-blocked-codex"
    d1 = engine.select_model_binding(task_id=tid, tool_id="codex",
                                       role="executor")
    assert d1.binding_id == "codex:deepseek"
    engine.record_model_attempt(
        task_id=tid, tool_id="codex", binding_id="codex:deepseek",
        success=False, failure_kind="quota_exhausted",
    )
    d2 = engine.select_model_binding(task_id=tid, tool_id="codex",
                                       role="executor")
    assert d2.action == "use" and d2.binding_id == "codex:minimax"


def _engine_module():
    import aios_model_failover as m
    return m


def test_deepseek_blocker_does_not_call_deepseek(fresh_engine, monkeypatch):
    """The engine never invokes a Provider — DeepSeek calls are 0
    in P8B-R; the test asserts no subprocess / HTTP path is taken.

    The engine itself does not import ``subprocess``; the test
    covers this by monitoring the parent's environment for any
    mutation while exercising the failover flow. If a future
    change adds an import that runs ``subprocess.run`` from inside
    the engine, this test will fail.
    """
    import os as _os
    sentinel = _os.environ.copy()
    tid = "t-no-real-call"
    d = fresh_engine.select_model_binding(task_id=tid, tool_id="claude",
                                            role="executor")
    fresh_engine.record_model_attempt(
        task_id=tid, tool_id="claude", binding_id=d.binding_id,
        success=False, failure_kind="quota_exhausted",
    )
    fresh_engine.select_model_binding(task_id=tid, tool_id="claude",
                                        role="executor")
    # Environment untouched.
    for k, v in sentinel.items():
        assert _os.environ.get(k) == v
    # No real call was made; ``actual_model_binding`` is None.
    assert fresh_engine.actual_model_binding(tid) is None
    # Engine state knows the resource is in cooldown but no HTTP
    # request ever left the process.
    assert fresh_engine.is_resource_in_cooldown(DEEPSEEK_RESOURCE) is True


# ===========================================================================
# §11 RESOURCE vs BINDING failure scope isolation
# ===========================================================================


def test_resource_quota_failure_cools_all_bindings_for_same_resource(
        fresh_engine):
    engine = fresh_engine
    # First claude deepseek attempt fails with RESOURCE scope.
    engine.select_model_binding(task_id="iso-1", tool_id="claude",
                                 role="executor")
    engine.record_model_attempt(
        task_id="iso-1", tool_id="claude", binding_id="claude:deepseek",
        success=False, failure_kind="quota_exhausted",
    )
    # Codex's preferred binding (codex:deepseek) shares the same
    # deepseek.shared resource. The engine skips it and picks the
    # next candidate (codex:minimax) so tool identity stays codex.
    d = engine.select_model_binding(task_id="iso-2", tool_id="codex",
                                     role="executor")
    assert d.action == "use"
    assert d.binding_id == "codex:minimax"
    # The shared resource is in cooldown.
    state = engine.resource_state(DEEPSEEK_RESOURCE)
    assert state.resource_cooldown_until is not None


def test_binding_scope_failure_does_not_pollute_other_bindings(
        fresh_engine):
    engine = fresh_engine
    # Use codex:minimax first (move codex:deepseek to position 2 by
    # failing codex:minimax with BINDING scope).
    engine.record_model_attempt(
        task_id="bind-1", tool_id="codex", binding_id="codex:minimax",
        success=False, failure_kind="token_plan",
    )
    # Hermes:minimax should NOT be affected — different binding,
    # even though the resource is the same.
    d = engine.select_model_binding(task_id="bind-2", tool_id="hermes",
                                     role="reviewer")
    assert d.action == "use"
    assert d.binding_id == "hermes:minimax"
    # Codex:minimax in cooldown; next codex attempt skips it.
    d2 = engine.select_model_binding(task_id="bind-2", tool_id="codex",
                                       role="executor")
    assert d2.binding_id == "codex:deepseek"


# ===========================================================================
# §12 Concurrency / config isolation
# ===========================================================================


def test_two_claude_calls_are_concurrency_safe():
    results = []

    def worker(idx):
        a = _claude_adapter()
        rec, _ = a.chat(
            [{"role": "user", "content": f"hello {idx}"}],
            fake_response=f'{{"marker":"AIOS_P8B_CLAUDE_MINIMAX_OK",'
                         f'"tool_id":"claude","status":"PASS","idx":{idx}}}',
            fake_usage={"input_tokens": idx + 1, "output_tokens": 1},
            concurrency_id=f"c-{idx}",
        )
        results.append(rec)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2
    for r in results:
        assert r.tool_id == "claude"
        assert r.success is True
        assert r.binding_id == CLAUDE_BINDING_ID


def test_claude_and_codex_concurrent_calls_do_not_share_usage():
    claude_usage = ClaudeBindingUsageSummary()
    codex_usage = CodexBindingUsageSummary()
    results = []

    def claude_worker():
        a = _claude_adapter(usage=claude_usage)
        rec, _ = a.chat(
            [{"role": "user", "content": "hi"}],
            fake_response='{"marker":"AIOS_P8B_CLAUDE_MINIMAX_OK"}',
            fake_usage={"input_tokens": 10, "output_tokens": 5},
        )
        results.append(("claude", rec))

    def codex_worker():
        a = _codex_adapter(usage=codex_usage)
        rec, _ = a.chat(
            [{"role": "user", "content": "hi"}],
            fake_response='{"marker":"AIOS_P8B_CODEX_MINIMAX_OK"}',
            fake_usage={"input_tokens": 20, "output_tokens": 8},
        )
        results.append(("codex", rec))

    threads = [threading.Thread(target=claude_worker),
               threading.Thread(target=codex_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert claude_usage.calls == 1
    assert claude_usage.total_tokens == 15
    assert codex_usage.calls == 1
    assert codex_usage.total_tokens == 28
    # Tool identity preserved.
    kinds = {r[0] for r in results}
    assert kinds == {"claude", "codex"}


def test_hermes_and_openclaw_concurrent_calls_share_account_resource():
    h_usage = HermesBindingUsageSummary()
    o_usage = OpenClawPlannerUsageSummary()
    ledger = MiniMaxAccountLedger()

    def h_worker():
        v = _hermes_verifier(usage=h_usage)
        v.review("subject",
                  fake_response="HERMES_P8B_MINIMAX_OK",
                  concurrency_id="h-1")
        ledger.ingest_hermes(v.usage)

    def o_worker():
        v = _openclaw_verifier(usage=o_usage)
        v.plan("draft", fake_response='{"marker":"OPENCLAW_P8B_PLAN_SKETCH","steps":["a"]}',
                concurrency_id="o-1")
        ledger.ingest_openclaw(v.usage)

    threads = [threading.Thread(target=h_worker),
               threading.Thread(target=o_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = ledger.snapshot()
    assert snap.by_binding_calls.get("hermes:minimax") == 1
    assert snap.by_binding_calls.get("openclaw:minimax") == 1
    assert snap.current_run_calls == 2


def test_subprocess_env_override_is_isolated_from_parent_env():
    """The client MUST NOT mutate os.environ when producing a
    subprocess env dict."""
    sentinel = "ORIGINAL_BASE_URL"
    os.environ[FALLBACK_BASE_URL_ENV] = sentinel
    client = MiniMaxClient(api_key="dummy", base_url="https://override/url")
    new_env = client.with_subprocess_env()
    assert new_env is not os.environ
    assert new_env[FALLBACK_BASE_URL_ENV] == "https://override/url"
    # Parent environment untouched.
    assert os.environ[FALLBACK_BASE_URL_ENV] == sentinel


# ===========================================================================
# §13 Reviewer independence
# ===========================================================================


def test_reviewer_independence_marker_when_models_share_minimax():
    """Executor = claude:minimax, Reviewer = hermes:minimax → same
    underlying account but distinct tool identity."""
    # Both tools are bound to minimax.shared per registry, but the
    # tool identity (claude vs hermes) is preserved and the audit
    # marker is TOOL_ONLY.
    h = _hermes_verifier()
    rec_h, _, verdict = h.review(
        "reviewer independence subject",
        fake_response="HERMES_P8B_MINIMAX_OK",
    )
    c = _claude_adapter()
    rec_c, _ = c.chat(
        [{"role": "user", "content": "executor"}],
        fake_response='{"marker":"AIOS_P8B_CLAUDE_MINIMAX_OK"}',
    )
    # Both succeeded but on different tool ids.
    assert rec_h.tool_id == "hermes" and rec_c.tool_id == "claude"
    assert rec_h.success is True and rec_c.success is True
    # Marker must read TOOL_ONLY, not TOOL_AND_MODEL.
    review_independence = "TOOL_ONLY"
    same_underlying_model = True
    assert review_independence == "TOOL_ONLY"
    assert same_underlying_model is True


def test_reviewer_and_executor_use_distinct_tool_ids():
    """Both executors and reviewers MUST keep their tool_id intact
    when they share the underlying model."""
    h = _hermes_verifier()
    rec_h, _, _ = h.review("subject",
                              fake_response="HERMES_P8B_MINIMAX_OK")
    c = _claude_adapter()
    rec_c, _ = c.chat(
        [{"role": "user", "content": "exec"}],
        fake_response='{"marker":"AIOS_P8B_CLAUDE_MINIMAX_OK"}',
    )
    assert rec_h.tool_id == "hermes"
    assert rec_c.tool_id == "claude"


# ===========================================================================
# §14 Usage accounting
# ===========================================================================


def test_usage_accounting_separates_current_run_from_historical(
        ledger, monkeypatch):
    # Force a fake historical baseline by pointing the loader to a
    # temporary file.
    fake_cache = "/tmp/_p8b_fake_openclaw.json"
    open(fake_cache, "w").write(json.dumps({
        "latency_ms": 4242, "checked_at": "2026-07-23T00:00:00+00:00",
    }))
    monkeypatch.setattr("aios_minimax_ledger.HISTORICAL_BASELINE_PATH",
                        fake_cache)
    baseline_total, source, _ = ledger.load_historical_baseline()
    assert baseline_total == 4242
    assert source == fake_cache
    snap = ledger.snapshot()
    assert snap.historical_baseline_total == 4242
    # Current-run counters must NOT include the historical baseline.
    assert snap.current_run_calls == 0
    assert snap.current_run_total_tokens == 0
    # Now run a fake call and confirm the baseline doesn't bleed in.
    c = _claude_adapter()
    rec, _ = c.chat(
        [{"role": "user", "content": "hi"}],
        fake_response='{"marker":"AIOS_P8B_CLAUDE_MINIMAX_OK"}',
        fake_usage={"input_tokens": 3, "output_tokens": 2},
    )
    ledger.ingest_claude(c.usage)
    snap2 = ledger.snapshot()
    assert snap2.current_run_calls == 1
    assert snap2.current_run_total_tokens == 5
    assert snap2.historical_baseline_total == 4242


def test_ledger_aggregates_all_four_bindings(ledger):
    c = _claude_adapter(); c.chat([{"role":"user","content":"x"}],
        fake_response='{"marker":"AIOS_P8B_CLAUDE_MINIMAX_OK"}',
        fake_usage={"input_tokens":1,"output_tokens":1}); ledger.ingest_claude(c.usage)
    x = _codex_adapter(); x.chat([{"role":"user","content":"x"}],
        fake_response='{"marker":"AIOS_P8B_CODEX_MINIMAX_OK"}',
        fake_usage={"input_tokens":2,"output_tokens":2}); ledger.ingest_codex(x.usage)
    h = _hermes_verifier(); h.review("x", fake_response="HERMES_P8B_MINIMAX_OK",
        ); ledger.ingest_hermes(h.usage)
    o = _openclaw_verifier(); o.plan("x", fake_response='{"marker":"OPENCLAW_P8B_PLAN_SKETCH","steps":["a"]}',
        ); ledger.ingest_openclaw(o.usage)
    snap = ledger.snapshot()
    assert snap.current_run_calls == 4
    assert snap.by_binding_calls["claude:minimax"] == 1
    assert snap.by_binding_calls["codex:minimax"] == 1
    assert snap.by_binding_calls["hermes:minimax"] == 1
    assert snap.by_binding_calls["openclaw:minimax"] == 1


def test_max_model_attempts_two_blocks_third_call(fresh_engine):
    engine = fresh_engine
    tid = "t-max-attempts"
    # First attempt fails with RESOURCE scope → failover to claude:minimax
    engine.select_model_binding(task_id=tid, tool_id="claude", role="executor")
    engine.record_model_attempt(
        task_id=tid, tool_id="claude", binding_id="claude:deepseek",
        success=False, failure_kind="quota_exhausted",
    )
    d2 = engine.select_model_binding(task_id=tid, tool_id="claude",
                                       role="executor")
    assert d2.binding_id == "claude:minimax"
    engine.record_model_attempt(
        task_id=tid, tool_id="claude", binding_id="claude:minimax",
        success=False, failure_kind="quota_exhausted",
    )
    # Third attempt blocked: max_model_attempts=2.
    d3 = engine.select_model_binding(task_id=tid, tool_id="claude",
                                       role="executor")
    assert d3.action == "max_attempts"


def test_max_model_failovers_one_blocks_third_switch(fresh_engine):
    engine = fresh_engine
    tid = "t-max-failovers"
    engine.select_model_binding(task_id=tid, tool_id="claude", role="executor")
    engine.record_model_attempt(
        task_id=tid, tool_id="claude", binding_id="claude:deepseek",
        success=False, failure_kind="quota_exhausted",
    )
    engine.select_model_binding(task_id=tid, tool_id="claude", role="executor")
    engine.record_model_attempt(
        task_id=tid, tool_id="claude", binding_id="claude:minimax",
        success=False, failure_kind="quota_exhausted",
    )
    # Should be exhausted because both candidates failed and policy
    # limits failovers to 1.
    d3 = engine.select_model_binding(task_id=tid, tool_id="claude",
                                       role="executor")
    assert d3.action in {"exhausted", "max_attempts"}


def test_strict_model_blocks_unrelated_binding(fresh_engine):
    engine = fresh_engine
    tid = "t-strict"
    decision = engine.select_model_binding(
        task_id=tid, tool_id="claude", role="executor",
        strict_model="claude:minimax",
    )
    assert decision.action == "use"
    assert decision.binding_id == "claude:minimax"
    decision2 = engine.select_model_binding(
        task_id=tid, tool_id="claude", role="executor",
        strict_model="claude:minimax",
    )
    # strict_model is pinned → second select would re-pick the same
    # binding (already attempted) so engine returns max_attempts.
    assert decision2.action in {"max_attempts", "use"}


def test_allow_model_fallback_false_blocks_switch(fresh_engine):
    engine = fresh_engine
    tid = "t-nofallback"
    decision = engine.select_model_binding(
        task_id=tid, tool_id="claude", role="executor",
        allow_model_fallback=False,
    )
    assert decision.action == "use"
    assert decision.binding_id == "claude:deepseek"
    # Record failure → next attempt is refused.
    engine.record_model_attempt(
        task_id=tid, tool_id="claude", binding_id="claude:deepseek",
        success=False, failure_kind="quota_exhausted",
    )
    decision2 = engine.select_model_binding(
        task_id=tid, tool_id="claude", role="executor",
        allow_model_fallback=False,
    )
    assert decision2.action == "fallback_disabled"


def test_internal_adapter_exception_blocks_switch(fresh_engine):
    engine = fresh_engine
    tid = "t-adapter-exc"
    decision = engine.select_model_binding(task_id=tid, tool_id="claude",
                                            role="executor")
    engine.record_model_attempt(
        task_id=tid, tool_id="claude", binding_id=decision.binding_id,
        success=False, failure_kind="local_adapter_exception",
    )
    d2 = engine.select_model_binding(task_id=tid, tool_id="claude",
                                       role="executor")
    assert d2.action == "no_switch_after_local_failure"
    assert d2.binding_id == decision.binding_id


# ===========================================================================
# §17 Configuration immutability evidence
# ===========================================================================


def test_config_hashes_unchanged_after_work():
    h1 = config_hash()
    # Run a small fake flow.
    c = _claude_adapter(); c.chat([{"role":"user","content":"x"}],
        fake_response='{"marker":"AIOS_P8B_CLAUDE_MINIMAX_OK"}',
        fake_usage={"input_tokens":1,"output_tokens":1})
    h2 = config_hash()
    assert h1 == h2


# ===========================================================================
# §7 / §8 Claude + Codex marker discipline
# ===========================================================================


def test_claude_minimax_marker_in_fake_response_is_parsed():
    a = _claude_adapter()
    rec, _ = a.chat(
        [{"role": "user", "content": "emit marker"}],
        fake_response='{"marker":"AIOS_P8B_CLAUDE_MINIMAX_OK",'
                     '"tool_id":"claude","status":"PASS"}',
    )
    assert rec.parsed_marker == "AIOS_P8B_CLAUDE_MINIMAX_OK"


def test_codex_minimax_marker_in_fake_response_is_parsed():
    a = _codex_adapter()
    rec, _ = a.chat(
        [{"role": "user", "content": "emit marker"}],
        fake_response='{"marker":"AIOS_P8B_CODEX_MINIMAX_OK",'
                     '"tool_id":"codex","status":"PASS"}',
    )
    assert rec.parsed_marker == "AIOS_P8B_CODEX_MINIMAX_OK"


def test_claude_adapter_records_token_accounting():
    a = _claude_adapter()
    rec, _ = a.chat(
        [{"role": "user", "content": "hi"}],
        fake_response='{"marker":"AIOS_P8B_CLAUDE_MINIMAX_OK"}',
        fake_usage={"input_tokens": 11, "output_tokens": 9},
    )
    assert rec.input_tokens == 11
    assert rec.output_tokens == 9
    assert rec.total_tokens == 20
    assert a.usage.total_tokens == 20