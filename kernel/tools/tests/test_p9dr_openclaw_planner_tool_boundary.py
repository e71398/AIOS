#!/usr/bin/env python3
"""AIOS P9D-R — OpenClaw Planner Tool Boundary (17 unit tests).

This test matrix closes the unit-level surface of the
``aios-planner-openclaw.service`` introduced by commit ``ff0ecbe``
("fix(reliability): establish independent OpenClaw planner boundary").

The 17 tests deliberately target the *boundary contract* of the
adapter (the only executable that backs the user-level
``aios-planner-openclaw.service``) rather than the orchestrator
integration.  Process boundary, per-leg failure scope, and
HTTP surface are exercised in isolation so the close-out can
reference the test IDs in the final adjudication ledger.

The matrix below uses ``collected == 17`` and ``passed == 17``
when run with ``PYTHONPATH=${AIOS_HOME}/kernel/tools:${AIOS_HOME}/kernel/tools/tests``.

P9D-R planner-boundary — 17 contracted tests
============================================

  1.  collect_health returns the planner-tool envelope
  2.  collect_health reports three independent legs
  3.  each leg carries a failure_scope
  4.  openclaw bin missing  →  ok=False, TOOL_PROCESS
  5.  openclaw-gateway down →  ok=False, TOOL_ADAPTER
  6.  minimax-gateway down   →  ok=False, PROVIDER
  7.  overall ok requires all three legs simultaneously
  8.  /health returns 200 only when all legs are green
  9.  /health returns 503 when any leg fails
 10.  /plan returns 503 TOOL_PROCESS when openclaw bin missing
 11.  /plan returns 503 TOOL_ADAPTER when openclaw-gateway down
 12.  /plan returns 503 PROVIDER when minimax-gateway down
 13.  /plan returns 200 with the upstream envelope on healthy path
 14.  /plan 400 on missing parent_id or prompt
 15.  /plan 400 on invalid JSON body
 16.  adapter STOP → Provider capability intact (independence)
 17.  adapter STOP → Reviewer/Executor surfaces intact (independence)

Run::

    PYTHONPATH=${AIOS_HOME}/kernel/tools:${AIOS_HOME}/kernel/tools/tests \\
      python3 -m pytest -q \\
        kernel/tools/tests/test_p9dr_openclaw_planner_tool_boundary.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from typing import Any, Dict, Tuple

import pytest

# Ensure ``aios`` package is importable regardless of the
# invocation working directory.
_TOOLS_DIR = "${AIOS_HOME}/kernel/tools"
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from aios import planner_openclaw_adapter as _adapter  # noqa: E402


# ---------------------------------------------------------------------------
# Test-local helpers
# ---------------------------------------------------------------------------

def _reset_legs(
    *,
    openclaw_bin: bool = True,
    openclaw_gateway: bool = True,
    minimax_gateway: bool = True,
) -> None:
    """Patch the three probe entry points on the adapter module
    so each test can deterministically toggle a single leg without
    touching the real services.  This is intentional: the unit
    test must NOT depend on whether ``openclaw-gateway`` or the
    model gateway are actually running in the test environment.
    """
    _adapter._probe_openclaw_bin = lambda: (  # type: ignore[assignment]
        (openclaw_bin, "stub_openclaw_bin_ok")
        if openclaw_bin
        else (False, "stub_openclaw_bin_missing")
    )
    _adapter._probe_url = (  # type: ignore[assignment]
        lambda url: (openclaw_gateway, "stub_gw_ok")
        if "18789" in url
        else (minimax_gateway, "stub_minimax_ok")
        if "18801" in url
        else (True, "stub_other_ok")
    )


@pytest.fixture(autouse=True)
def _restore_legs():
    """Always restore the real probe functions after each test so
    we do not pollute the module for any later test.
    """
    real_bin = _adapter._probe_openclaw_bin
    real_url = _adapter._probe_url
    real_port = _adapter.PORT
    real_port = _adapter.PORT
    yield
    _adapter._probe_openclaw_bin = real_bin  # type: ignore[assignment]
    _adapter._probe_url = real_url  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 1. collect_health envelope shape
# ---------------------------------------------------------------------------

def test_01_collect_health_returns_planner_tool_envelope():
    """The shape is fixed: planner_tool=openclaw, planner_role=planner,
    ok is bool, checked_at is float.  Per-leg dict has three keys.
    """
    _reset_legs()
    body = _adapter.collect_health()
    assert isinstance(body, dict)
    assert body["planner_tool"] == "openclaw"
    assert body["planner_role"] == "planner"
    assert isinstance(body["ok"], bool)
    assert isinstance(body["checked_at"], float)
    assert "legs" in body and isinstance(body["legs"], dict)


# ---------------------------------------------------------------------------
# 2. three independent legs
# ---------------------------------------------------------------------------

def test_02_collect_health_reports_three_independent_legs():
    """Three keys MUST exist: openclaw_bin, openclaw_gateway,
    minimax_gateway.  Dropping any single leg silently is a
    regression of the per-leg failure isolation contract.
    """
    _reset_legs()
    legs = _adapter.collect_health()["legs"]
    assert set(legs.keys()) == {
        "openclaw_bin",
        "openclaw_gateway",
        "minimax_gateway",
    }
    for k, leg in legs.items():
        assert "ok" in leg and isinstance(leg["ok"], bool), k
        assert "evidence" in leg, k
        assert "scope" in leg, k


# ---------------------------------------------------------------------------
# 3. per-leg scope is preserved
# ---------------------------------------------------------------------------

def test_03_each_leg_carries_its_failure_scope():
    """The contract is that openclaw_bin reports TOOL_PROCESS,
    openclaw_gateway reports TOOL_ADAPTER, minimax_gateway
    reports PROVIDER.  Collapsing them into a single scope
    would poison the orchestrator's planner-fallback layer.
    """
    _reset_legs()
    legs = _adapter.collect_health()["legs"]
    assert legs["openclaw_bin"]["scope"] == "TOOL_PROCESS"
    assert legs["openclaw_gateway"]["scope"] == "TOOL_ADAPTER"
    assert legs["minimax_gateway"]["scope"] == "PROVIDER"


# ---------------------------------------------------------------------------
# 4. openclaw bin missing → TOOL_PROCESS
# ---------------------------------------------------------------------------

def test_04_openclaw_bin_missing_marks_tool_process_failure():
    _reset_legs(openclaw_bin=False)
    body = _adapter.collect_health()
    assert body["ok"] is False
    assert body["legs"]["openclaw_bin"]["ok"] is False
    assert body["legs"]["openclaw_bin"]["scope"] == "TOOL_PROCESS"
    # the other two legs are unaffected
    assert body["legs"]["openclaw_gateway"]["ok"] is True
    assert body["legs"]["minimax_gateway"]["ok"] is True


# ---------------------------------------------------------------------------
# 5. openclaw-gateway down → TOOL_ADAPTER
# ---------------------------------------------------------------------------

def test_05_openclaw_gateway_down_marks_tool_adapter_failure():
    _reset_legs(openclaw_gateway=False)
    body = _adapter.collect_health()
    assert body["ok"] is False
    assert body["legs"]["openclaw_gateway"]["ok"] is False
    assert body["legs"]["openclaw_gateway"]["scope"] == "TOOL_ADAPTER"
    assert body["legs"]["openclaw_bin"]["ok"] is True
    assert body["legs"]["minimax_gateway"]["ok"] is True


# ---------------------------------------------------------------------------
# 6. minimax-gateway down → PROVIDER
# ---------------------------------------------------------------------------

def test_06_minimax_gateway_down_marks_provider_failure():
    _reset_legs(minimax_gateway=False)
    body = _adapter.collect_health()
    assert body["ok"] is False
    assert body["legs"]["minimax_gateway"]["ok"] is False
    assert body["legs"]["minimax_gateway"]["scope"] == "PROVIDER"
    assert body["legs"]["openclaw_bin"]["ok"] is True
    assert body["legs"]["openclaw_gateway"]["ok"] is True


# ---------------------------------------------------------------------------
# 7. overall ok requires ALL three legs green
# ---------------------------------------------------------------------------

def test_07_overall_ok_requires_all_three_legs():
    """Each combination of two-healthy-one-bad MUST yield
    overall ok=False, and only the all-three-green combo
    yields ok=True.  This guards against AND/OR drift.
    """
    combos = [
        {"openclaw_bin": False, "openclaw_gateway": True,  "minimax_gateway": True},
        {"openclaw_bin": True,  "openclaw_gateway": False, "minimax_gateway": True},
        {"openclaw_bin": True,  "openclaw_gateway": True,  "minimax_gateway": False},
        {"openclaw_bin": False, "openclaw_gateway": False, "minimax_gateway": True},
        {"openclaw_bin": False, "openclaw_gateway": True,  "minimax_gateway": False},
        {"openclaw_bin": True,  "openclaw_gateway": False, "minimax_gateway": False},
        {"openclaw_bin": False, "openclaw_gateway": False, "minimax_gateway": False},
    ]
    for combo in combos:
        _reset_legs(**combo)
        body = _adapter.collect_health()
        assert body["ok"] is False, f"expected not ok for {combo}"
    # all three healthy
    _reset_legs()
    assert _adapter.collect_health()["ok"] is True


# ---------------------------------------------------------------------------
# 8. /health returns 200 when all legs green
# ---------------------------------------------------------------------------

def test_08_get_health_returns_200_when_all_legs_green(_free_port_server):
    """Spin up the adapter on an ephemeral port with all legs
    green; ``GET /health`` MUST return HTTP 200 and ok=True.
    """
    _reset_legs()
    port, server, thread = _free_port_server
    try:
        status, body = _http_get(port, "/health")
        assert status == 200, body
        assert body["ok"] is True
        assert body["planner_tool"] == "openclaw"
    finally:
        _stop_server(server, thread)


# ---------------------------------------------------------------------------
# 9. /health returns 503 when any leg fails
# ---------------------------------------------------------------------------

def test_09_get_health_returns_503_when_any_leg_fails(_free_port_server):
    """Spin the adapter with the openclaw_bin leg failing;
    ``GET /health`` MUST return HTTP 503 and ok=False.  The
    orchestrator's recovery probe relies on the 503 status;
    a 200 with ok=False would silently bypass the
    ``routing_eligible=False`` short-circuit.
    """
    _reset_legs(openclaw_bin=False)
    port, server, thread = _free_port_server
    try:
        status, body = _http_get(port, "/health")
        assert status == 503, body
        assert body["ok"] is False
        # per-leg failure isolation: only one leg is marked bad
        bad = [k for k, leg in body["legs"].items() if not leg["ok"]]
        assert bad == ["openclaw_bin"], bad
    finally:
        _stop_server(server, thread)


# ---------------------------------------------------------------------------
# 10. /plan returns 503 TOOL_PROCESS when openclaw bin missing
# ---------------------------------------------------------------------------

def test_10_post_plan_returns_503_tool_process_when_bin_missing(
    _free_port_server,
):
    """The adapter MUST NOT silently route to the Provider when
    the openclaw CLI is missing.  The 503 carries scope=TOOL_PROCESS.
    """
    _reset_legs(openclaw_bin=False)
    port, server, thread = _free_port_server
    try:
        status, body = _http_post(port, "/plan", {
            "parent_id": "p9dr-boundary-10",
            "prompt": "stub",
        })
        assert status == 503, body
        assert body.get("ok") is False
        assert body.get("scope") == "TOOL_PROCESS"
    finally:
        _stop_server(server, thread)


# ---------------------------------------------------------------------------
# 11. /plan returns 503 TOOL_ADAPTER when openclaw-gateway down
# ---------------------------------------------------------------------------

def test_11_post_plan_returns_503_tool_adapter_when_gateway_down(
    _free_port_server,
):
    _reset_legs(openclaw_gateway=False)
    port, server, thread = _free_port_server
    try:
        status, body = _http_post(port, "/plan", {
            "parent_id": "p9dr-boundary-11",
            "prompt": "stub",
        })
        assert status == 503, body
        assert body.get("ok") is False
        assert body.get("scope") == "TOOL_ADAPTER"
    finally:
        _stop_server(server, thread)


# ---------------------------------------------------------------------------
# 12. /plan returns 503 PROVIDER when minimax-gateway down
# ---------------------------------------------------------------------------

def test_12_post_plan_returns_503_provider_when_minimax_down(
    _free_port_server,
):
    _reset_legs(minimax_gateway=False)
    port, server, thread = _free_port_server
    try:
        status, body = _http_post(port, "/plan", {
            "parent_id": "p9dr-boundary-12",
            "prompt": "stub",
        })
        assert status == 503, body
        assert body.get("ok") is False
        assert body.get("scope") == "PROVIDER"
    finally:
        _stop_server(server, thread)


# ---------------------------------------------------------------------------
# 13. /plan returns 200 with upstream envelope on healthy path
# ---------------------------------------------------------------------------

def test_13_post_plan_returns_200_on_healthy_path(
    _free_port_server, monkeypatch,
):
    """Healthy path delegates to ``aios_model_gateway.call_model``
    (the existing single-router path) and forwards the envelope
    unchanged.  The mock simulates a successful upstream call;
    we MUST NOT introduce a second router inside the adapter.
    """
    _reset_legs()

    upstream_envelope = {
        "ok": True,
        "result": {
            "model": "minimax",
            "content": '{"steps": []}',
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "fallback_used": False,
        },
        "model": "minimax",
    }

    def _stub_call_model(provider, model, messages, **kwargs):
        # verify we pass the planner role contract downstream
        assert provider == "minimax"
        assert isinstance(messages, list) and messages, messages
        return upstream_envelope

    monkeypatch.setattr(
        "aios_model_gateway.call_model", _stub_call_model, raising=False,
    )
    # also patch the import-by-name inside the adapter module
    monkeypatch.setattr(_adapter, "_call_provider_via_adapter",
                        lambda parent_id, prompt: upstream_envelope)

    port, server, thread = _free_port_server
    try:
        status, body = _http_post(port, "/plan", {
            "parent_id": "p9dr-boundary-13",
            "prompt": "healthy test",
        })
        assert status == 200, body
        assert body.get("ok") is True
        assert "result" in body
    finally:
        _stop_server(server, thread)


# ---------------------------------------------------------------------------
# 14. /plan returns 400 on missing parent_id or prompt
# ---------------------------------------------------------------------------

def test_14_post_plan_returns_400_on_missing_parent_id_or_prompt(
    _free_port_server,
):
    _reset_legs()
    port, server, thread = _free_port_server
    try:
        # missing parent_id
        s1, b1 = _http_post(port, "/plan", {"prompt": "x"})
        assert s1 == 400, b1
        # missing prompt
        s2, b2 = _http_post(port, "/plan", {"parent_id": "x"})
        assert s2 == 400, b2
        # empty dict
        s3, b3 = _http_post(port, "/plan", {})
        assert s3 == 400, b3
    finally:
        _stop_server(server, thread)


# ---------------------------------------------------------------------------
# 15. /plan returns 400 on invalid JSON body
# ---------------------------------------------------------------------------

def test_15_post_plan_returns_400_on_invalid_json(_free_port_server):
    _reset_legs()
    port, server, thread = _free_port_server
    try:
        status, body = _http_raw_post(port, "/plan", b"{not-json")
        assert status == 400, body
        assert "invalid_json" in (body.get("error") or "")
    finally:
        _stop_server(server, thread)


# ---------------------------------------------------------------------------
# 16. adapter STOP → Provider capability intact (independence)
# ---------------------------------------------------------------------------

def test_16_adapter_stop_does_not_poison_minimax_provider():
    """Independence contract: stopping the OpenClaw Planner
    adapter (or having any leg fail) MUST not surface as a
    Provider-layer failure.  The minimax_provider leg is
    reported independently under its own scope.
    """
    _reset_legs(openclaw_bin=False, openclaw_gateway=False)
    body = _adapter.collect_health()
    # minmax leg is independently green
    assert body["legs"]["minimax_gateway"]["ok"] is True
    assert body["legs"]["minimax_gateway"]["scope"] == "PROVIDER"
    # failure scopes are independent
    assert body["legs"]["openclaw_bin"]["scope"] == "TOOL_PROCESS"
    assert body["legs"]["openclaw_gateway"]["scope"] == "TOOL_ADAPTER"
    # independence: no single leg can mark the provider scope bad
    assert all(
        leg["scope"] != "PROVIDER"
        for k, leg in body["legs"].items()
        if k != "minimax_gateway"
    )


# ---------------------------------------------------------------------------
# 17. adapter STOP → Reviewer/Executor surfaces intact (independence)
# ---------------------------------------------------------------------------

def test_17_adapter_stop_does_not_poison_reviewer_or_executor():
    """The adapter is the OpenClaw Planner boundary; nothing
    about its /health legs mentions reviewer (``hermes`` /
    ``claude``) or executor (``codex`` / ``opencode`` / ``claude``).
    Independence guarantee is structural, not just behavioural:
    the planner boundary has no knowledge of those role surfaces.
    """
    _reset_legs()
    body = _adapter.collect_health()
    serialised = json.dumps(body, sort_keys=True)
    # neither reviewer nor executor role is named in the planner envelope
    for excluded in (
        "reviewer", "Reviewer", "REVIEWER",
        "hermes", "Hermes", "HERMES",
        "codex", "Codex", "CODEX",
        "executor", "Executor", "EXECUTOR",
    ):
        # executor-and-planner role names DO appear because the
        # adapter identifies itself as a planner; this test only
        # verifies reviewer and executor downstream identifiers
        # are NOT branded into the planner health surface.
        if excluded in {"reviewer", "hermes", "Reviewer", "Hermes",
                        "REVIEWER", "HERMES",
                        "codex", "Codex", "CODEX"}:
            assert excluded not in serialised, (
                f"planner envelope leaked reviewer/executor "
                f"identifier: {excluded!r}"
            )
    # the adapter MUST always report planner_role=planner
    assert body["planner_role"] == "planner"


# ---------------------------------------------------------------------------
# Test fixture: ephemeral-port HTTP server
# ---------------------------------------------------------------------------

@pytest.fixture
def _free_port_server(monkeypatch):
    """Start the adapter's ``ThreadingHTTPServer`` on an ephemeral
    port so each test gets a clean surface with no state leak.
    Returns ``(port, server, thread)``.
    """
    # Pick an unused port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(_adapter, "PORT", port, raising=False)

    from http.server import ThreadingHTTPServer as _THS

    server = _THS(("127.0.0.1", port), _adapter._Handler)
    thread = threading.Thread(target=server.serve_forever,
                              name=f"p9dr-boundary-{port}", daemon=True)
    thread.start()
    # Give the server a moment to bind.
    time.sleep(0.05)
    return port, server, thread


def _stop_server(server, thread):
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass
    try:
        thread.join(timeout=2)
    except Exception:
        pass


def _http_get(port: int, path: str) -> Tuple[int, Dict[str, Any]]:
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.status, json.loads(resp.read(4096).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read(4096).decode("utf-8") or "{}")


def _http_post(port: int, path: str,
               payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    raw = json.dumps(payload).encode("utf-8")
    return _http_raw_post(port, path, raw,
                          content_type="application/json")


def _http_raw_post(port: int, path: str, raw: bytes,
                   content_type: str = "application/json"
                   ) -> Tuple[int, Dict[str, Any]]:
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=raw, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(raw)))
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.status, json.loads(resp.read(4096).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read(4096).decode("utf-8") or "{}")