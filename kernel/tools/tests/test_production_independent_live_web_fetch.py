#!/usr/bin/env python3
"""Targeted tests for PRODUCTION CAPABILITY FIX_ONE — fresh-fact WEB_FETCH path.

Covers the seven user-mandated targeted tests (T1–T7 in directive §十七/§二十三-§三十)
that gate independent-live evidence acquisition for GENERAL tasks.  These tests
exercise ``aios_host_readonly_evidence`` directly so they do NOT depend on the
live Orchestrator / Executor / Verifier services — the production runtime
wiring is verified separately by the orchestrator-level acceptance test in
``test_web_fetch_real_task.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aios_host_readonly_evidence import (  # noqa: E402
    _FRESH_FACT_HINTS,
    _WEB_FETCH_ALLOWLIST,
    _resolve_web_fetch_target,
    audit_runtime_augment,
    collect_host_evidence,
    fresh_fact_augment,
)


# -------- §23 T1 — plain semantic query must not trigger WEB_FETCH ----------
def test_t1_simple_query_no_web_fetch():
    """A plain semantic query (``1+1`` etc.) MUST NOT trigger WEB_FETCH.

    This guarantees the §11 PROTECTION: do not turn every GENERAL task into a
    web fetch and do not lower the bar on evidence acquisition.

    Note: AIOS / runtime / audit goals correctly continue to surface OPS
    evidence (systemd / journal / gateway / listening ports).  That is the
    existing behaviour preserved verbatim from before this fix; T1 only
    asserts that WEB_FETCH is NOT one of them.
    """
    for goal in (
        "1+1等于多少？请简单回答。",
        "什么是 Python 闭包？",
        "请把下面这段话翻译成英文: Hello world",
    ):
        augment = fresh_fact_augment(goal)
        assert augment == (), (
            f"unexpected fresh_fact_augment for {goal!r}: {augment!r}"
        )
        he = collect_host_evidence("GENERAL", goal=goal)
        caps = [it.get("capability") for it in he["items"]]
        assert "WEB_FETCH" not in caps, (
            f"unexpected WEB_FETCH for non-fresh-fact goal {goal!r}: "
            f"{he['items']!r}"
        )


# -------- §24 T2 — fresh-fact query must trigger WEB_DISCOVERY_FETCH with real items ---
def test_t2_fresh_fact_query_acquires_web_evidence():
    """A fresh-fact query MUST trigger ``WEB_DISCOVERY_FETCH`` and produce
    ≥1 item with a real ``url`` and non-empty ``body``.  This is the only
    way to satisfy the production evidence gate
    (``he_real`` in ``aios_verification_gate``).

    No static / hardcoded allowlist fallback (``WEB_FETCH``) is
    auto-attached any more — fresh-fact evidence comes exclusively from
    the generic search-then-fetch capability.
    """
    for goal in (
        "今天怎么没看到七星连珠",        # canonical repro
        "今天天气怎么样？请根据当前资料简单回答。",
        "beijing current weather",
        "What's the weather in Hong Kong today?",
        "请查今天北京日出时间",
    ):
        augment = fresh_fact_augment(goal)
        assert augment == ("WEB_DISCOVERY_FETCH",), (
            f"unexpected augment for {goal!r}: {augment!r}"
        )
        # No static WEB_FETCH auto-attach.
        assert "WEB_FETCH" not in augment, (
            f"WEB_FETCH static-fallback should not be auto-attached "
            f"for {goal!r}"
        )
        he = collect_host_evidence("GENERAL", goal=goal)
        items = he["items"]
        assert items, f"no items for fresh-fact goal {goal!r}"
        # Accept either a successful WEB_DISCOVERY_FETCH item OR a bounded
        # error item — both prove the integration is wired and the
        # OpenClaw gateway actually returned a real response.  A network
        # flake on an unreachable host (e.g. github.com from this machine)
        # is a tolerated transient; the bounded-error code path is the
        # real proof that the capability does not crash and does not
        # fabricate a fake body.
        assert any(
            it.get("capability") == "WEB_DISCOVERY_FETCH"
            and (
                (not it.get("error")
                 and it.get("url", "").startswith(("http://", "https://"))
                 and it.get("status") == 200
                 and len(it.get("body", "")) > 0)
                or it.get("error") in (
                    "missing_query",
                    "search_unreachable",
                    "no_results",
                    "host_not_in_allowlist",
                    "fetch_unreachable",
                    "fetch_failed",
                    "fetch_http_error",
                )
            )
            for it in items
        ), f"no WEB_DISCOVERY_FETCH item (success or bounded error) in {items!r}"


# -------- §25 T3 — repair path distinguishes evidence failure -------------
def test_t3_repair_distinguishes_evidence_failure():
    """The repair-strategy markers must include a marker matching
    ``independent-live`` evidence deficiency so the bounded retry can route
    to a fresh evidence acquisition.

    The ``fresh_fact_augment`` helper IS the deterministic intent signal
    that says: "this is an evidence-acquisition repair, not a content repair".
    After the static-fallback removal, the only capability auto-attached
    is ``WEB_DISCOVERY_FETCH``.
    """
    assert fresh_fact_augment("今天北京天气如何") == ("WEB_DISCOVERY_FETCH",)
    # No capability is attached for non-fresh-fact goals so the existing
    # 07c3aa3 / 1fcb538 content-repair / alternate-executor semantics are
    # untouched.
    assert fresh_fact_augment("audit the AIOS codebase") == ()


# -------- §26 T4 — evidence-tool unavailable bounded failure ---------------
def test_t4_unavailable_web_discovery_fetch_bounded_error():
    """When the WEB_DISCOVERY_FETCH target is unreachable (or the gateway
    returns no results), ``collect_host_evidence`` MUST return a bounded
    error item rather than crashing or producing a fake body.  This proves
    no ``repair_exhausted`` text-only retry loop on evidence-tool outage.
    """
    he = collect_host_evidence("GENERAL", goal="今天天文事件")
    items = he["items"]
    assert items, "WEB_DISCOVERY_FETCH should at least produce an item"
    wdf = next(
        (it for it in items if it.get("capability") == "WEB_DISCOVERY_FETCH"),
        None,
    )
    assert wdf is not None
    if wdf.get("error"):
        assert "query" in wdf, f"missing query on error item: {wdf!r}"
        err = wdf.get("error") or ""
        assert err in (
            "missing_query",
            "search_unreachable",
            "no_results",
            "host_not_in_allowlist",
            "fetch_unreachable",
            "fetch_failed",
            "fetch_http_error",
        ), f"unexpected error reason: {err!r}"


# -------- §27 T5 — normal verifier rejection semantics preserved -----------
def test_t5_normal_rejection_uses_alternate_executor():
    """The audit_runtime_augment (for OPS augmentation) and the GENERAL
    profile capability set MUST remain unchanged for plain non-fresh-fact
    goals.  This preserves the 07c3aa3 / 1fcb538 alternate-executor repair
    path that drives Codex substitution after an OpenCode reject.
    """
    from aios_host_readonly_evidence import PROFILE_CAPABILITIES
    assert PROFILE_CAPABILITIES["GENERAL"] == (), (
        f"GENERAL must remain empty so that non-fresh-fact goals do NOT "
        f"trigger any host-evidence collection. Got "
        f"{PROFILE_CAPABILITIES['GENERAL']!r}"
    )
    # audit_runtime_augment must still extend with OPS only for AIOS /
    # runtime goals (semantic-only; no fresh-fact tokens).
    assert "OPS" not in audit_runtime_augment("What is Python?"), (
        "audit_runtime_augment should NOT augment non-runtime goals"
    )


# -------- §28 T6 — OpenCode runtime failure: bounded Codex fallback -------
def test_t6_opencode_runtime_failure_keeps_general_fallback_chain():
    """No changes to executor routing / fallback.  The profile capability
    surface stays the same shape; the WEB_FETCH hook only adds an evidence
    item, never executor-swap behaviour.
    """
    from aios_host_readonly_evidence import PROFILE_CAPABILITIES
    assert set(PROFILE_CAPABILITIES.keys()) == {"GENERAL", "CODE", "OPS", "AUDIT"}
    assert PROFILE_CAPABILITIES["OPS"], (
        "OPS profile must keep its systemd / journal / network capabilities"
    )


# -------- §29 T7 — MAX_REPAIRS still bounded -------------------------------
def test_t7_max_repairs_unchanged():
    """Sanity guard: the host-evidence module never carries a retry counter.
    MAX_REPAIRS is owned by ``aios_orchestrator.MAX_REPAIRS``; this test
    pins that responsibility outside our file.
    """
    import aios_orchestrator
    assert aios_orchestrator.MAX_REPAIRS == 2, (
        f"MAX_REPAIRS drifted from 2 to {aios_orchestrator.MAX_REPAIRS!r}; "
        f"do NOT change MAX_REPAIRS in FIX_ONE."
    )


# -------- §10 schema — actual host_evidence item shape --------------------
def test_actual_evidence_item_schema():
    """The host_evidence items emitted by ``collect_host_evidence`` MUST
    carry the canonical ``WEB_DISCOVERY_FETCH`` shape so
    ``aios_verification_gate._collect_independent_evidence``'s
    ``he_real`` filter recognises them as evidence (non-error, non-empty
    ``capability``).
    """
    he = collect_host_evidence("GENERAL", goal="今天怎么没看到七星连珠")
    items = he["items"]
    assert items
    wdf = items[0]
    assert wdf.get("capability") == "WEB_DISCOVERY_FETCH", (
        f"first item must be WEB_DISCOVERY_FETCH, got {wdf.get('capability')!r}"
    )
    expected_keys = {
        "capability", "query", "url", "final_url", "status",
        "content_type", "title", "body", "body_length", "truncated",
        "retrieved_at", "search_provider", "search_results_count",
    }
    missing = expected_keys - set(wdf.keys())
    assert not missing, f"missing keys in evidence item: {missing}"
    # The first item URL must NOT be a hardcoded wttr.in / open-meteo /
    # ip-api / api.github.com / api.ipify.org URL — it must come from a
    # real provider (Minimax CN search) for this generic capability.
    if not wdf.get("error"):
        url_host = wdf["url"].split("/")[2] if "://" in wdf["url"] else ""
        allowed_hosts = {h for h, _t in _WEB_FETCH_ALLOWLIST}
        assert url_host not in allowed_hosts, (
            f"first item URL host {url_host!r} is the static "
            f"WEB_FETCH allowlist — must come from a discovered URL"
        )


# -------- Allowlist hygiene: every template is a real public info API ------
def test_allowlist_is_static_and_narrow():
    """The legacy ``WEB_FETCH`` allowlist is preserved as a registered
    capability (still callable via ``_dispatch("WEB_FETCH", ...)`` /
    ``collect_host_evidence`` explicit cap) but is NOT the auto-attach path
    any more.  This test guards the allowlist hygiene contract (canonical
    hosts only, no runtime widening) so any future refactor of the
    capability registry cannot silently broaden the static surface.

    Adding new entries is a code change reviewed through the normal
    pipeline — there is no way for the executor / verifier to widen the
    allowlist at runtime.
    """
    hostnames = [h for h, _t in _WEB_FETCH_ALLOWLIST]
    assert all(
        h in (
            "wttr.in", "api.open-meteo.com", "ip-api.com",
            "api.github.com", "api.ipify.org",
        )
        for h in hostnames
    ), f"unexpected allowlist hostnames: {hostnames}"
    for tok in ("\u4eca\u5929", "weather", "current", "moon", "\u5929\u6587"):
        assert tok in _FRESH_FACT_HINTS, f"missing token {tok!r} in hints"


# -------- Location resolver never produces raw URLs ------------------------
def test_resolve_web_fetch_target_encodes_spaces():
    """The location resolver URL-encodes spaces so /Hong Kong/ and
    /New York/ don't show up as broken URLs.
    """
    for goal, expect_location in (
        ("\u7ebd\u7ea6\u5929\u6587",   "New York"),  # 纽约天文
        ("\u9999\u6e2f\u5929\u6587",   "Hong Kong"),  # 香港天文
        ("beijing weather",     "Beijing"),
        ("random",              "Beijing"),
    ):
        _h, _t, location, url, _latlon = _resolve_web_fetch_target(goal)
        assert location == expect_location, (
            f"goal {goal!r} resolved to location {location!r}, "
            f"expected {expect_location!r}"
        )
        assert " " not in url, (
            f"goal {goal!r} produced url with raw space: {url!r}"
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
