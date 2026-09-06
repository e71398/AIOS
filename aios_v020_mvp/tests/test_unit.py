"""Unit tests for the AIOS v0.2.0 MVP.

Run with::

    pytest aios_v020_mvp/tests/test_unit.py -q

These tests are pure unit tests and exercise the role
boundaries, the JSON store, the file result store, the tool
registry and the reviewer evidence gate.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)


@pytest.fixture()
def workdir(monkeypatch: pytest.MonkeyPatch, tmp_path):
    data_dir = tmp_path / "aios_mvp_unit"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIOS_MVP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIOS_MVP_OFFLINE", "1")
    yield data_dir


def test_json_store_atomic(tmp_path):
    from aios_v020_mvp.persistence import JSONStore
    store = JSONStore(tmp_path / "store.json")
    store.set("a", {"x": 1})
    store.update("a", y=2)
    assert store.get("a") == {"x": 1, "y": 2}
    store.set("b", [1, 2, 3])
    assert store.get("b") == [1, 2, 3]
    store.delete("a")
    assert store.get("a") is None


def test_file_store_sandbox(workdir):
    from aios_v020_mvp.persistence import FileResultStore
    store = FileResultStore(workdir)
    wid = "wf-1"
    artefact = store.write(wid, "notes.txt", "hello\n")
    assert artefact.size_bytes == 6
    assert store.exists(wid, "notes.txt")
    assert store.read(wid, "notes.txt") == "hello\n"
    listing = store.list(wid)
    assert len(listing) == 1 and listing[0].rel_path == "notes.txt"


def test_file_store_containment(workdir):
    from aios_v020_mvp.persistence import FileResultStore
    store = FileResultStore(workdir)
    wid = "wf-2"
    with pytest.raises(Exception):
        store.write(wid, "../escape.txt", "nope")


def test_tool_registry_default(workdir):
    from aios_v020_mvp.persistence import FileResultStore
    from aios_v020_mvp.tools import (
        ToolRegistry,
        ToolInvocation,
        register_default_file_tools,
    )
    registry = ToolRegistry()
    register_default_file_tools(registry)
    assert set(registry.names()) == {"file_read", "file_write", "file_list"}
    store = FileResultStore(workdir)
    res = registry.invoke(ToolInvocation("file_write", {"path": "a.txt", "content": "hi"}), "wf-x", store)
    assert res.status == "ok"
    res = registry.invoke(ToolInvocation("file_read", {"path": "a.txt"}), "wf-x", store)
    assert res.status == "ok" and res.result["content"] == "hi"


def test_planner_emits_plan(workdir):
    from aios_v020_mvp.orchestrator import build_orchestrator
    orch = build_orchestrator()
    wf = orch.submit("Write a file called hello.txt with a greeting.")
    orch.planner.build_plan(wf)
    assert wf.plan is not None
    assert "steps" in wf.plan and isinstance(wf.plan["steps"], list)
    assert wf.stage == "planned"


def test_executor_runs_tool_calls(workdir):
    from aios_v020_mvp.orchestrator import build_orchestrator
    orch = build_orchestrator()
    wf = orch.submit("Write a file called plan.txt with some content.")
    orch.planner.build_plan(wf)
    orch.executor.execute(wf)
    artefacts = wf.execution["artefacts"]
    assert any(a["path"] == "plan.txt" and a["size_bytes"] > 0 for a in artefacts)


def test_reviewer_accepts_valid_work(workdir):
    from aios_v020_mvp.orchestrator import build_orchestrator
    orch = build_orchestrator()
    wf = orch.submit("Write a file called review.txt with content.")
    orch.planner.build_plan(wf)
    orch.executor.execute(wf)
    verdict = orch.reviewer.review(wf)
    assert verdict["verdict"] == "accept"
    assert verdict["accepted"] is True
    assert wf.stage == "completed"


class _RejectProvider:
    """Always-reject provider used to validate the reviewer gate."""
    name = "reject-provider"

    def chat(self, request):
        from aios_v020_mvp.providers import ProviderResponse
        text = (request.messages[0]["content"] or "").lower()
        if "planner" in text:
            plan = {
                "plan_id": "x",
                "summary": "trivial plan",
                "steps": [
                    {"id": "s1", "kind": "file_write", "tool": "file_write",
                     "description": "write", "args": {"path": "must.txt"}},
                ],
                "acceptance": ["must.txt was created"],
            }
            body = json.dumps(plan)
        elif "executor" in text:
            body = json.dumps({"actions": [], "summary": "noop", "finish_reason": "stop"})
        else:
            body = json.dumps({
                "verdict": "reject",
                "score": 0.0,
                "checks": [
                    {"criterion": "must.txt was created",
                     "passed": False,
                     "evidence": "no artefact produced"}
                ],
                "notes": "no artefact"
            })
        return ProviderResponse(
            text=body, input_tokens=10, output_tokens=20,
            finish_reason="stop", raw={},
        )

    def health(self):
        return {"ok": True, "provider": self.name}


def test_reviewer_rejects_missing_artefact(workdir):
    """Force a scenario where the executor produces no artefact and the
    reviewer must reject."""
    import aios_v020_mvp.orchestrator as om
    from aios_v020_mvp.config import load_config

    def _force(spec, default_provider, cfg):
        return _RejectProvider()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(om, "_build_provider", _force)
    try:
        cfg = load_config()
        orch = om.build_orchestrator(cfg)
        wf = orch.submit("Trivial task that demands must.txt.")
        orch.run(wf.id)
        assert wf.stage == "failed", f"expected failed, got {wf.stage}"
        assert wf.review is not None and wf.review["verdict"] == "reject"
        assert wf.review.get("accepted") is False
    finally:
        monkeypatch.undo()


def test_health_endpoint_reports_providers(workdir):
    from aios_v020_mvp.config import load_config
    from aios_v020_mvp.server import build_gateway
    cfg = load_config()
    gw = build_gateway(cfg)
    gw.start()
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://{cfg.host}:{cfg.port}/health", timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is True
        assert body["service"] == "aios-v0.2.0-mvp"
        assert body["providers"]["planner"]["provider"] == "local"
        assert body["providers"]["executor"]["provider"] == "local"
        assert body["providers"]["reviewer"]["provider"] == "local"
        tool_names = sorted(t["name"] for t in body["tools"])
        assert tool_names == ["file_list", "file_read", "file_write"]
    finally:
        gw.shutdown()


def test_synchronous_submit_runs_inline(workdir):
    """Submitting a task with async=False should return the final result
    inside the POST response, no polling required."""
    import urllib.request
    from aios_v020_mvp.config import load_config
    from aios_v020_mvp.server import build_gateway
    cfg = load_config()
    gw = build_gateway(cfg)
    gw.start()
    try:
        body = json.dumps({
            "input": "Write a file called sync.txt with content.",
            "async": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://{cfg.host}:{cfg.port}/task",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200
        assert payload["ok"] is True
        assert payload["stage"] == "completed"
        assert payload["review"]["verdict"] == "accept"
        assert any(
            a["path"] == "sync.txt"
            for a in payload["result"]["artefacts"]
        )
    finally:
        gw.shutdown()


def test_http_provider_parses_response():
    """Validate HTTPChatProvider against a tiny in-process OpenAI-compat server."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from aios_v020_mvp.providers import HTTPChatProvider, ProviderRequest

    captured = {}

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a, **k):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            captured["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            captured["auth"] = self.headers.get("Authorization")
            resp = {
                "id": "x", "object": "chat.completion", "created": 0,
                "model": captured["body"].get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            }
            data = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = HTTPChatProvider(
            name="test",
            base_url=f"http://127.0.0.1:{port}",
            api_key="sk-test",
            model="mvp-test",
        )
        response = provider.chat(ProviderRequest(
            messages=[{"role": "user", "content": "hi"}],
            model="mvp-test",
        ))
        assert response.text == "OK"
        assert response.input_tokens == 5
        assert response.output_tokens == 1
        assert captured["auth"] == "Bearer sk-test"
        assert captured["body"]["model"] == "mvp-test"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
