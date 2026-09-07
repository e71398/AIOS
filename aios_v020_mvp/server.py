"""HTTP entry gateway for the v0.2.0 MVP.

Binds a tiny HTTP server that exposes the normal user
entry-points required by the spec:

    POST   /task                - submit a new task (returns id immediately)
    GET    /task/<id>           - poll a workflow status / read result back
    GET    /task/<id>/artefacts - list artefacts (file tool outputs)
    GET    /task/<id>/artefacts/<path>
                                - read a single artefact back
    GET    /tasks               - list recent workflows
    GET    /health              - health probe
    GET    /                    - service banner

The gateway is intentionally minimal: no websockets, no SSE,
no streaming. Clients submit a task and poll ``/task/<id>``
until the workflow reaches a terminal stage.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .config import MVPConfig, load_config
from .orchestrator import BackgroundWorker, Orchestrator, build_orchestrator


log = logging.getLogger("aios_v020_mvp.gateway")


@dataclass
class Gateway:
    config: MVPConfig
    orchestrator: Orchestrator
    worker: BackgroundWorker
    _http_server: Optional[ThreadingHTTPServer] = field(default=None, init=False)
    _http_thread: Optional[threading.Thread] = field(default=None, init=False)

    def start(self) -> None:
        """Start the HTTP server (and the background worker if needed)."""
        self.worker.start()
        if self._http_server is not None:
            return
        server = ThreadingHTTPServer((self.config.host, self.config.port), _Handler)
        server.gateway = self  # type: ignore[attr-defined]
        self._http_server = server
        thread = threading.Thread(
            target=server.serve_forever,
            name="aios-mvp-http",
            daemon=True,
        )
        self._http_thread = thread
        thread.start()
        log.info(
            "AIOS v0.2.0 MVP listening on http://%s:%d",
            self.config.host,
            self.config.port,
        )

    def serve_forever(self) -> None:
        """Block forever serving HTTP (foreground mode)."""
        self.start()
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:  # pragma: no cover
            self.shutdown()

    def shutdown(self) -> None:
        if self._http_server is not None:
            try:
                self._http_server.shutdown()
                self._http_server.server_close()
            except Exception:
                pass
            self._http_server = None
        if self._http_thread is not None:
            self._http_thread.join(timeout=2.0)
            self._http_thread = None
        self.worker.stop()

    @property
    def base_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}"


def build_gateway(config: Optional[MVPConfig] = None) -> Gateway:
    cfg = config or load_config()
    orch = build_orchestrator(cfg)
    worker = BackgroundWorker(orch)
    return Gateway(config=cfg, orchestrator=orch, worker=worker)


# ----------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def _auth_ok(headers, config: MVPConfig, query) -> bool:
    if not config.require_auth:
        return True
    token = headers.get("X-AIOS-Token") or query.get("token", [""])[0]
    return bool(config.auth_token) and token == config.auth_token


class _Handler(BaseHTTPRequestHandler):
    server_version = "AIOS-v0.2.0-MVP"

    # ---- helpers ----

    @property
    def gateway(self) -> Gateway:
        return self.server.gateway  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002
        log.info("%s - %s", self.address_string(), format % args)

    def _write(self, code: int, payload: Any) -> None:
        body = _json_bytes(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-AIOS-MVP-Version", "0.2.0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str, **extra) -> None:
        body = {"ok": False, "error": message, "code": code}
        body.update(extra)
        self._write(code, body)

    # ---- GET / POST dispatch ----

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        query = parse_qs(url.query)
        if not _auth_ok(self.headers, self.gateway.config, query):
            return self._error(401, "unauthorized")
        try:
            if path == "/" or path == "/health":
                return self._health()
            if path == "/tasks":
                return self._list_tasks()
            if path.startswith("/task/"):
                rest = path[len("/task/"):]
                parts = rest.split("/", 2)
                wid = parts[0]
                if len(parts) == 1:
                    return self._get_task(wid)
                if len(parts) == 2 and parts[1] == "artefacts":
                    return self._list_artefacts(wid)
                if len(parts) == 3 and parts[1] == "artefacts":
                    return self._read_artefact(wid, parts[2])
            return self._error(404, "not_found", path=path)
        except Exception as exc:
            log.exception("GET %s failed", path)
            return self._error(500, f"internal_error: {type(exc).__name__}: {exc}"[:300])

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        query = parse_qs(url.query)
        if not _auth_ok(self.headers, self.gateway.config, query):
            return self._error(401, "unauthorized")
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except json.JSONDecodeError as exc:
                return self._error(400, f"invalid_json: {exc}")
            if path == "/task":
                return self._submit_task(body)
            return self._error(404, "not_found", path=path)
        except Exception as exc:
            log.exception("POST %s failed", path)
            return self._error(500, f"internal_error: {type(exc).__name__}: {exc}"[:300])

    # ---- endpoint implementations ----

    def _health(self) -> None:
        self._write(200, {
            "ok": True,
            "service": "aios-v0.2.0-mvp",
            "version": "0.2.0",
            "offline": self.gateway.config.offline,
            "providers": {
                "planner": {
                    "provider": self.gateway.config.planner.provider,
                    "model": self.gateway.config.planner.model,
                },
                "executor": {
                    "provider": self.gateway.config.executor.provider,
                    "model": self.gateway.config.executor.model,
                },
                "reviewer": {
                    "provider": self.gateway.config.reviewer.provider,
                    "model": self.gateway.config.reviewer.model,
                },
            },
            "tools": self.gateway.orchestrator.tools.describe(),
            "timestamp": time.time(),
        })

    def _submit_task(self, body: Dict[str, Any]) -> None:
        task = str(body.get("input") or body.get("task") or "").strip()
        if not task:
            return self._error(400, "missing_input")
        source = str(body.get("source", "http"))
        sender = str(body.get("sender", ""))
        run_async = bool(body.get("async", True))
        workflow = self.gateway.orchestrator.submit(task, source=source)
        if sender:
            # Stamp the sender on the workflow for later audit.
            self.gateway.orchestrator.json_store.update(
                f"wf:{workflow.id}", **{"sender": sender}
            )
        # Optional input-file ingestion. Callers may seed the workflow
        # sandbox with read-only inputs so the Executor's file_read
        # tool can operate on real content. Paths stay INSIDE the
        # per-workflow sandbox (the store sanitises them), so this
        # never widens the sandbox boundary.
        seed_files = body.get("files")
        ingested = []
        if isinstance(seed_files, dict):
            store = self.gateway.orchestrator.file_store
            for rel, content in list(seed_files.items())[:20]:
                if not isinstance(rel, str) or not isinstance(content, str):
                    continue
                try:
                    art = store.write(workflow.id, rel, content)
                    ingested.append(art.rel_path)
                except Exception as exc:
                    log.warning("seed file %r rejected: %s", rel, exc)
            if ingested:
                self.gateway.orchestrator.json_store.update(
                    f"wf:{workflow.id}", **{"seed_files": ingested}
                )
        if run_async:
            self.gateway.worker.enqueue(workflow.id)
            self._write(202, {
                "ok": True,
                "task_id": workflow.id,
                "stage": workflow.stage,
                "status": "accepted",
                "poll_url": f"/task/{workflow.id}",
            })
            return
        self.gateway.orchestrator.run(workflow.id)
        workflow_doc = self.gateway.orchestrator.get(workflow.id) or {}
        self._write(200, {
            "ok": True,
            "task_id": workflow.id,
            "stage": workflow_doc.get("stage"),
            "result": workflow_doc.get("execution"),
            "review": workflow_doc.get("review"),
        })

    def _get_task(self, workflow_id: str) -> None:
        doc = self.gateway.orchestrator.get(workflow_id)
        if doc is None:
            return self._error(404, "workflow_not_found")
        self._write(200, {"ok": True, "workflow": doc})

    def _list_tasks(self) -> None:
        items = self.gateway.orchestrator.list_recent(limit=50)
        self._write(200, {"ok": True, "count": len(items), "items": items})

    def _list_artefacts(self, workflow_id: str) -> None:
        if self.gateway.orchestrator.get(workflow_id) is None:
            return self._error(404, "workflow_not_found")
        artefacts = self.gateway.orchestrator.list_artefacts(workflow_id)
        self._write(200, {"ok": True, "count": len(artefacts), "items": artefacts})

    def _read_artefact(self, workflow_id: str, rel_path: str) -> None:
        if self.gateway.orchestrator.get(workflow_id) is None:
            return self._error(404, "workflow_not_found")
        try:
            content = self.gateway.orchestrator.read_artefact(workflow_id, rel_path)
        except FileNotFoundError:
            return self._error(404, "artefact_not_found", path=rel_path)
        except Exception as exc:
            return self._error(400, str(exc))
        self._write(200, {
            "ok": True,
            "workflow_id": workflow_id,
            "path": rel_path,
            "content": content,
        })


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------


def main() -> int:  # pragma: no cover - manual entry
    import argparse

    parser = argparse.ArgumentParser(description="AIOS v0.2.0 MVP gateway")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    cfg = load_config()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    gateway = build_gateway(cfg)
    try:
        gateway.serve_forever()
    finally:
        gateway.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
