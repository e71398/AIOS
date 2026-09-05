"""AIOS P9D-R OpenClaw Planner Adapter — thin Planner boundary.

This module is the *only* executable that backs the
``aios-planner-openclaw.service`` unit.  It is intentionally a
THIN Planner adapter:

  1.  ``/health``  — local health probe: openclaw binary
      present, openclaw-gateway reachable, and aios-model-gateway
      still healthy.  Returns 200 only when ALL three legs are
      green; 503 otherwise.  The failure scope is split:

      * openclaw CLI missing or non-executable → ``OPENCLAW_BIN``
      * openclaw-gateway:18789 unreachable → ``OPENCLAW_GATEWAY``
      * aios-model-gateway:9998 unreachable → ``MINIMAX_GATEWAY``

      No single leg's failure can poison the others; the JSON
      body lists the per-leg status so the orchestrator can
      record the correct ``failure_scope`` without collapsing
      three independent signals into one.

  2.  ``/plan``    — accept the same JSON request body that the
      planner dispatch already builds (parent_id, prompt) and
      return a plan schema that the existing
      ``aios_orchestrator.build_plan`` can consume.  The actual
      text inference is delegated to
      ``aios_model_gateway.call_model`` (the existing path) so
      we do NOT introduce a second Planner router, a second
      Registry, a second Queue, a second Workflow store, or a
      second Repair engine.  This service only stands up the
      PROCESS BOUNDARY the P9D-R close-out requires.

The module is invoked by the user-level systemd unit
``aios-planner-openclaw.service`` (``ExecStart=/usr/bin/python3 -m
aios.planner_openclaw_adapter``).  Stopping that unit MUST cause
``_is_executor_available("openclaw", role="planner")`` (and the
new ``_is_planner_available("openclaw")`` orchestrator helper)
to return False WITHOUT affecting:

  * ``minimax.shared`` capability (Provider layer)
  * ``claude``/``hermes`` reviewer
  * ``opencode`` Planner

The single-process ``http.server`` here is sufficient for the
close-out: it is a process boundary, not a router.  If the
system ever needs horizontal scale or health-check from an
external monitor, the same module can be wrapped in
``aios_planner_openclaw_adapter.app`` for a real WSGI server;
the on-disk contract (paths, JSON body, schema) is unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

HOST = os.environ.get("AIOS_PLANNER_OPENCLAW_HOST", "127.0.0.1")
PORT = int(os.environ.get("AIOS_PLANNER_OPENCLAW_PORT", "18899"))
OPENCLAW_BIN = os.environ.get(
    "AIOS_PLANNER_OPENCLAW_HEALTH_OPENCLAW_BIN",
    "${HOME}/.n/bin/openclaw",
)
OPENCLAW_GATEWAY_URL = os.environ.get(
    "AIOS_PLANNER_OPENCLAW_HEALTH_OPENCLAW_GATEWAY",
    "http://127.0.0.1:18789/health",
)
MINIMAX_GATEWAY_URL = os.environ.get(
    "AIOS_PLANNER_OPENCLAW_HEALTH_MINIMAX",
    "http://127.0.0.1:18801/health",
)
CONNECT_TIMEOUT = float(os.environ.get(
    "AIOS_PLANNER_OPENCLAW_HEALTH_TIMEOUT", "2") or "2")


# FIX_ONE 2026-08-12: aios-planner-openclaw.service inherits a
# systemd-default PATH that does NOT include the user's nvm shim
# directory (~/.local/bin) where the Node v24 binary that OpenClaw
# 2026.7.1+ requires lives.  Without an explicit PATH prepend the
# `openclaw` wrapper script (shebang `#!/usr/bin/env node`) falls
# back to /usr/bin/node v18 and exits with rc=1 + a Node-version
# error message, which the orchestrator reports as
# FAILED_EXTERNAL_ROUTE_PLANNER_TIMEOUT.  Prepending the user
# node-shim path (when present) here keeps the fix entirely inside
# AIOS-tracked code without touching the user-level systemd unit.
_NODE_SHIM_CANDIDATES: Tuple[str, ...] = (
    "${HOME}/.local/bin",
    "${HOME}/.n/bin",
)


def _openclaw_subprocess_env() -> Dict[str, str]:
    """Return a copy of os.environ with the user node-shim directory
    prepended to PATH so the ``openclaw`` wrapper script resolves
    the OpenClaw-required Node binary.
    """
    env = dict(os.environ)
    current_path = env.get("PATH", "")
    extras = [
        p for p in _NODE_SHIM_CANDIDATES
        if os.path.isdir(p) and p not in current_path.split(":")
    ]
    if extras:
        env["PATH"] = ":".join(extras + [current_path]) if current_path else ":".join(extras)
    return env


def _probe_openclaw_bin() -> Tuple[bool, str]:
    """Return (ok, evidence).  The openclaw CLI must be present and
    executable; missing-or-not-executable is a Planner failure
    (failure_scope=TOOL_PROCESS) but does NOT mean the Provider
    is down.
    """
    path = OPENCLAW_BIN
    if not path:
        return False, "openclaw_bin_path_empty"
    if not os.path.isfile(path):
        return False, f"openclaw_bin_missing:{path}"
    if not os.access(path, os.X_OK):
        return False, f"openclaw_bin_not_executable:{path}"
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            timeout=3, shell=False,
            env=_openclaw_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return False, "openclaw_bin_timeout"
    except Exception as exc:
        return False, f"openclaw_bin_error:{type(exc).__name__}"
    if proc.returncode != 0:
        return False, f"openclaw_bin_nonzero:{proc.returncode}"
    if not (proc.stdout or proc.stderr):
        return False, "openclaw_bin_empty"
    return True, (proc.stdout or proc.stderr).strip()[:200]


def _probe_url(url: str) -> Tuple[bool, str]:
    """Return (ok, evidence).  Pure HTTP GET; we never let a slow
    Provider block the Planner health probe.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
            ok = 200 <= resp.status < 300
            body = resp.read(512).decode("utf-8", errors="replace")[:200]
            return ok, f"http_{resp.status}:{body}"
    except urllib.error.URLError as exc:
        return False, f"url_error:{exc.reason}"
    except Exception as exc:
        return False, f"http_error:{type(exc).__name__}"


def _probe_port(host: str, port: int) -> Tuple[bool, str]:
    """Lightweight TCP probe used for the openclaw-gateway health
    leg.  We avoid the full HTTP roundtrip so a slow gateway
    does not block the orchestrator's recovery probe.
    """
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT):
            return True, "tcp_open"
    except Exception as exc:
        return False, f"tcp_closed:{type(exc).__name__}"


def collect_health() -> Dict[str, Any]:
    """Run all three recovery legs in parallel-shape (sequential
    here) and return a single JSON object.  Each leg is reported
    independently so the orchestrator's
    ``_attempt_tool_recovery("openclaw", role="planner")`` can
    record the correct ``failure_scope`` without poisoning the
    other legs.
    """
    openclaw_ok, openclaw_evidence = _probe_openclaw_bin()
    openclaw_gw_ok, openclaw_gw_evidence = _probe_url(OPENCLAW_GATEWAY_URL)
    minimax_ok, minimax_evidence = _probe_url(MINIMAX_GATEWAY_URL)
    overall_ok = openclaw_ok and openclaw_gw_ok and minimax_ok
    return {
        "ok": overall_ok,
        "planner_tool": "openclaw",
        "planner_role": "planner",
        "checked_at": time.time(),
        "legs": {
            "openclaw_bin": {
                "ok": openclaw_ok,
                "evidence": openclaw_evidence,
                "scope": "TOOL_PROCESS",
                "bin": OPENCLAW_BIN,
            },
            "openclaw_gateway": {
                "ok": openclaw_gw_ok,
                "evidence": openclaw_gw_evidence,
                "scope": "TOOL_ADAPTER",
                "url": OPENCLAW_GATEWAY_URL,
            },
            "minimax_gateway": {
                "ok": minimax_ok,
                "evidence": minimax_evidence,
                "scope": "PROVIDER",
                "url": MINIMAX_GATEWAY_URL,
            },
        },
    }


def _call_provider_via_adapter(parent_id: str, prompt: str) -> Dict[str, Any]:
    """Forward the actual text inference to the existing
    ``aios_model_gateway.call_model`` so this adapter does not
    stand up a second model gateway.  The returned shape is the
    same envelope the existing aios_model_gateway returns, so
    the orchestrator's ``build_plan`` loop can consume it
    unchanged.

    The single-process adapter intentionally re-uses the parent
    orchestrator's model-gateway module: keeping the inference
    pipeline in one place is what makes the OpenClaw Planner a
    THIN boundary rather than a second router.
    """
    from aios_model_gateway import call_model as _call_model
    response = _call_model(
        "minimax", "MiniMax-M3",
        [
            {"role": "system",
             "content": "You are an AIOS planner. Output strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        agent="openclaw",
        task_id=parent_id,
        max_tokens=2400,
        reasoning_split=True,
        connect_timeout=int(CONNECT_TIMEOUT * 5),
        read_timeout=int(CONNECT_TIMEOUT * 25),
    )
    return response


class _Handler(BaseHTTPRequestHandler):
    """Single endpoint surface: /health and /plan.

    The ``/plan`` body MUST be JSON with ``parent_id`` and
    ``prompt`` keys; we forward the inference to
    ``aios_model_gateway`` and return the same envelope so the
    orchestrator's ``build_plan`` loop is unchanged.
    """

    def log_message(self, format, *args):  # noqa: A002
        sys.stderr.write("[planner-openclaw] " + (format % args) + "\n")

    def do_GET(self):  # noqa: N802
        if self.path == "/health" or self.path.startswith("/health?"):
            body = collect_health()
            self._json(200 if body["ok"] else 503, body)
            return
        self._json(404, {"ok": False, "error": "not_found",
                          "available": ["/health", "/plan"]})

    def do_POST(self):  # noqa: N802
        if self.path != "/plan":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception as exc:
            self._json(400, {"ok": False, "error":
                              f"invalid_json:{type(exc).__name__}:{exc}"[:200]})
            return
        parent_id = str(payload.get("parent_id", "") or "")
        prompt = str(payload.get("prompt", "") or "")
        if not parent_id or not prompt:
            self._json(400, {"ok": False,
                              "error": "missing_parent_id_or_prompt"})
            return
        health = collect_health()
        # Hard-block when the OPENCLAW tool boundary is down: do
        # NOT silently route through the Provider.  The
        # orchestrator's Planner-fallback layer will see this
        # ``tool_blocked`` signal and pick the next candidate
        # (opencode planner) without calling minimax.
        if not health["legs"]["openclaw_bin"]["ok"]:
            self._json(503, {"ok": False, "error":
                              "openclaw_tool_unreachable",
                              "scope": "TOOL_PROCESS",
                              "health": health})
            return
        if not health["legs"]["openclaw_gateway"]["ok"]:
            self._json(503, {"ok": False, "error":
                              "openclaw_gateway_unreachable",
                              "scope": "TOOL_ADAPTER",
                              "health": health})
            return
        if not health["legs"]["minimax_gateway"]["ok"]:
            self._json(503, {"ok": False, "error":
                              "provider_unreachable",
                              "scope": "PROVIDER",
                              "health": health})
            return
        # Healthy path: delegate the actual text inference to
        # the existing aios_model_gateway.  This is the
        # single-process Planner boundary the P9D-R close-out
        # requires; we do not introduce a second router.
        try:
            response = _call_provider_via_adapter(parent_id, prompt)
        except Exception as exc:
            self._json(503, {"ok": False, "error":
                              f"planner_call_failed:{type(exc).__name__}:{exc}"[:200]})
            return
        # Forward the upstream envelope unchanged so the
        # orchestrator's build_plan loop is satisfied.
        self._json(200, response)

    def _json(self, status: int, body: Dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    sys.stderr.write(
        f"[planner-openclaw] listening on {HOST}:{PORT} "
        f"pid={os.getpid()}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())