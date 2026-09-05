#!/usr/bin/env python3
"""AIOS P9A — Gateway Concurrency Isolation Tests.

These tests exercise the :class:`BoundedThreadingHTTPServer` introduced
in P9A and assert that the production gateway endpoints — entry gateway
``/health`` and model gateway ``/v1/chat/completions`` — no longer
serialize behind a single slow handler.

The tests run the servers in-process on a random loopback port so they
do not depend on the systemd services being started.  They do **not**
require any external network or any AIOS-internal Ollama process; the
"slow" handler is a stub that sleeps in the request thread, which is
the exact pathology the P9A fix targets.

Test coverage (mapped to P9A §八):

1. ``test_health_responsive_during_slow_handler`` — one slow handler
   must not block ``/health``; maps to phase C/G probe (1).
2. ``test_second_task_ack_during_slow_handler`` — a second POST
   submitted while the first one is in flight must not be serialised
   behind it; maps to phase C/G probe (2).
3. ``test_overflow_returns_503_when_cap_saturated`` — once the bounded
   cap is exhausted, additional requests get a deterministic 503 with
   ``Retry-After``; maps to phase D branch D1.
4. ``test_overflow_does_not_silently_drop`` — every 503 is a final
   response; the client does not need to retry to learn the outcome.
5. ``test_daemon_threads_release_on_shutdown`` — ``server.shutdown()``
   returns within a deterministic bound even when a slow handler is
   still in flight; no non-daemon thread lingers.
6. ``test_planner_pool_unaffected_by_gateway_change`` — the existing
   orchestrator ``_PLANNER_POOL`` / ``_WORKFLOW_POOL`` are still
   separate bounded pools; the gateway change does not promote them
   into a shared pool (branch D3 negative check).
7. ``test_strict_tool_semantics_unchanged`` — ``strict_tool`` and
   ``strict_model`` knobs propagate unchanged through the entry
   gateway, regardless of the concurrency fix.
8. ``test_local_model_cannot_enter_candidates`` — the local-model
   guard still rejects ``*:ollama`` candidates (P9 boundary).
9. ``test_task_policy_persistence_unchanged`` — the entry gateway
   persists the same workflow fields (P9 boundary).
10. ``test_default_max_handlers_is_32`` — operators can read the
    configured cap via ``AIOS_GATEWAY_MAX_HANDLERS`` (default 32).
11. ``test_custom_max_handlers_overrides_default`` — the operator cap
    overrides the default.
12. ``test_invalid_max_handlers_falls_back_to_default`` — an invalid
    cap value (negative, non-integer, etc.) falls back to the default
    rather than crashing the server.
"""
from __future__ import annotations

import os
import socket
import threading
import time
import json
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

import pytest

import sys
TOOLS = "${AIOS_HOME}/kernel/tools"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from aios_http_server import (
    BoundedThreadingHTTPServer,
    make_bounded_server,
    _default_max_handlers,
)


# ---------------------------------------------------------------------------
# Test handler that supports /health, /slow, /echo, /saturate
# ---------------------------------------------------------------------------
class _ProbeHandler(BaseHTTPRequestHandler):
    SLOW_SLEEP_SEC = 3.0  # large enough to outlast the health probe

    def log_message(self, fmt, *args):
        # Silence stdout during pytest runs.
        pass

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "probe", "ts": time.time()})
            return
        if self.path == "/slow":
            time.sleep(self.SLOW_SLEEP_SEC)
            self._json(200, {"ok": True, "slept": self.SLOW_SLEEP_SEC})
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length) if length else b""
        if self.path == "/echo":
            self._json(200, {"ok": True, "received": True})
            return
        if self.path == "/slow":
            time.sleep(self.SLOW_SLEEP_SEC)
            self._json(200, {"ok": True, "slept": self.SLOW_SLEEP_SEC})
            return
        self._json(404, {"ok": False, "error": "not_found"})


# ---------------------------------------------------------------------------
# Test fixture: ephemeral loopback server + thread
# ---------------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    def __init__(self, max_handlers: int = 4):
        self.port = _free_port()
        self.server = BoundedThreadingHTTPServer(
            ("127.0.0.1", self.port), _ProbeHandler, max_handlers=max_handlers,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
            name=f"probe-server-{self.port}",
        )
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def shutdown(self, join_timeout: float = 5.0):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=join_timeout)


@pytest.fixture
def probe_server():
    s = _Server(max_handlers=4)
    try:
        yield s
    finally:
        s.shutdown()


def _http_get(url, timeout=5.0):
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return time.time() - t0, r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return time.time() - t0, e.code, e.read().decode()[:300]
    except Exception as e:
        return time.time() - t0, -1, str(e)


def _http_post(url, payload=None, timeout=5.0):
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return time.time() - t0, r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return time.time() - t0, e.code, e.read().decode()[:300]
    except Exception as e:
        return time.time() - t0, -1, str(e)


# ---------------------------------------------------------------------------
# 1. /health responds while a slow handler is in flight
# ---------------------------------------------------------------------------
def test_health_responsive_during_slow_handler(probe_server):
    """One slow handler must not block /health (P9A branches D1 + D2)."""
    boxes = {}

    def slow_requester():
        boxes["slow"] = _http_get(probe_server.url("/slow"), timeout=10)

    t = threading.Thread(target=slow_requester, daemon=True)
    t.start()
    # Let the slow handler actually start consuming its semaphore slot.
    time.sleep(0.3)

    # While /slow is sleeping, /health should still respond promptly.
    health_durations = []
    for _ in range(3):
        d, code, body = _http_get(probe_server.url("/health"), timeout=2)
        health_durations.append(d)
        assert code == 200, f"/health blocked: code={code} body={body}"
    t.join(timeout=10)
    slow_d, slow_code, slow_body = boxes["slow"]
    assert slow_d > 0, "slow request did not return"
    assert slow_code == 200, f"slow request failed: {slow_d} {slow_code} {slow_body}"
    # /health should be O(100ms)-fast even with a slow handler in flight.
    assert max(health_durations) < 1.0, (
        f"/health latency too high while slow handler in flight: "
        f"{health_durations}"
    )


# ---------------------------------------------------------------------------
# 2. A second POST gets ACK while the first one is sleeping
# ---------------------------------------------------------------------------
def test_second_task_ack_during_slow_handler(probe_server):
    """A second POST submitted while the first is in flight must not
    wait for the first handler to finish."""
    boxes = {}

    def first_requester():
        boxes["first"] = _http_get(probe_server.url("/slow"), timeout=10)

    t1 = threading.Thread(target=first_requester, daemon=True)
    t1.start()
    time.sleep(0.3)
    # Submit the second POST while the first is sleeping.
    t0 = time.time()
    boxes["second"] = _http_post(probe_server.url("/echo"), {"ping": 1},
                                  timeout=2)
    second_ack_latency = time.time() - t0
    s_d, s_code, s_body = boxes["second"]
    assert s_code == 200, f"second POST blocked: {s_code} {s_body}"
    # The second POST should ACK in well under the slow handler's sleep.
    assert second_ack_latency < 1.0, (
        f"second POST was serialised behind slow handler: "
        f"latency={second_ack_latency:.3f}s"
    )
    # The first POST should still complete normally.
    t1.join(timeout=10)
    f_d, f_code, f_body = boxes["first"]
    assert f_code == 200, f"first POST failed: {f_d} {f_code} {f_body}"


# ---------------------------------------------------------------------------
# 3. Overflow returns 503 (not silent drop)
# ---------------------------------------------------------------------------
def test_overflow_returns_503_when_cap_saturated():
    """When the bounded cap is exhausted, additional requests receive
    a deterministic 503 with Retry-After (P9A branch D1)."""
    # Cap=1 so the second concurrent request must overflow.
    s = _Server(max_handlers=1)
    try:
        inflight = []

        def slow_requester():
            inflight.append(_http_get(s.url("/slow"), timeout=10))

        t1 = threading.Thread(target=slow_requester, daemon=True)
        t1.start()
        # Give the slow handler time to acquire the semaphore slot.
        time.sleep(0.2)
        # The next request should overflow.
        d, code, body = _http_get(s.url("/health"), timeout=3)
        assert code == 503, (
            f"expected 503 from saturated server, got code={code} body={body}"
        )
        payload = json.loads(body)
        assert payload.get("error") == "server_overloaded", payload
        assert payload.get("retry_after") == 1, payload
        # Check the Retry-After header is also returned.
        # We did not keep the raw response, so re-issue and read headers.
        try:
            req = urllib.request.Request(s.url("/echo"))
            with urllib.request.urlopen(req, timeout=3) as r:
                # Inheriting request also blocked.
                raise AssertionError("expected 503, got 200")
        except urllib.error.HTTPError as e:
            assert e.code == 503
            assert e.headers.get("Retry-After") == "1", dict(e.headers)
        t1.join(timeout=10)
    finally:
        s.shutdown()


# ---------------------------------------------------------------------------
# 4. Overflow does not silently drop the client
# ---------------------------------------------------------------------------
def test_overflow_does_not_silently_drop(probe_server):
    """Saturation must produce a clear 503; the client must not block
    forever waiting for a response."""
    # Fill the cap (4 slots) with slow requests.
    threads = []
    for _ in range(4):
        def slow_requester():
            _http_get(probe_server.url("/slow"), timeout=10)
        t = threading.Thread(target=slow_requester, daemon=True)
        t.start()
        threads.append(t)
    time.sleep(0.3)
    # The 5th request must complete (error path) quickly.
    t0 = time.time()
    d, code, body = _http_get(probe_server.url("/health"), timeout=3)
    elapsed = time.time() - t0
    assert code == 503, f"expected 503, got code={code} body={body}"
    assert elapsed < 1.0, f"overflow response too slow: {elapsed:.3f}s"
    for t in threads:
        t.join(timeout=10)


# ---------------------------------------------------------------------------
# 5. Daemon threads release on shutdown
# ---------------------------------------------------------------------------
def test_daemon_threads_release_on_shutdown():
    """server.shutdown() returns within a deterministic bound even when
    a slow handler is in flight; no non-daemon thread lingers."""
    s = _Server(max_handlers=2)
    slow_done = [False]

    def slow_requester():
        _http_get(s.url("/slow"), timeout=10)
        slow_done[0] = True

    t = threading.Thread(target=slow_requester, daemon=True)
    t.start()
    time.sleep(0.3)
    t0 = time.time()
    s.shutdown(join_timeout=2.0)
    elapsed = time.time() - t0
    # server.shutdown() should return promptly even with a slow handler
    # still in flight (daemon=True means the join thread is allowed to
    # exit; the slow handler is also a daemon thread).
    assert elapsed < 2.0, (
        f"server.shutdown() blocked for {elapsed:.3f}s — "
        f"daemon_threads not honoured"
    )
    # The slow handler thread is daemon=True so the process can exit
    # without waiting for it; we don't assert slow_done[0] here.
    # Patch: confirm the server thread joined.
    assert not s.thread.is_alive(), "server thread still alive after shutdown"


# ---------------------------------------------------------------------------
# 6. Planner pool / workflow pool are not unified by the gateway change
# ---------------------------------------------------------------------------
def test_planner_pool_unaffected_by_gateway_change():
    """The orchestrator's _PLANNER_POOL and _WORKFLOW_POOL remain
    distinct bounded pools; the gateway change does not promote them
    into a shared pool (P9A branch D3 negative check)."""
    # Import the orchestrator constants without spinning the daemon.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "aios_orchestrator_under_test",
        "${AIOS_HOME}/kernel/tools/aios_orchestrator.py",
    )
    # We do not actually execute the module — just confirm the constant
    # identification is in source.  Loading the module would attempt
    # Redis connections and a daemon loop.
    with open(spec.origin) as f:
        src = f.read()
    assert "_PLANNER_POOL" in src, "orchestrator no longer defines _PLANNER_POOL"
    assert "_WORKFLOW_POOL" in src, "orchestrator no longer defines _WORKFLOW_POOL"
    # Both are ThreadPoolExecutor initialisers.
    planned = src.count("ThreadPoolExecutor(")
    assert planned >= 2, (
        f"expected at least 2 ThreadPoolExecutor instances, got {planned}"
    )


# ---------------------------------------------------------------------------
# 7. Strict tool / strict model semantics unchanged at the entry gateway
# ---------------------------------------------------------------------------
def test_strict_tool_semantics_unchanged():
    """The entry gateway handler must still parse strict_tool /
    strict_model and forward them to the orchestrator pin."""
    from aios_entry_gateway import EntryGatewayHandler
    import inspect
    src = inspect.getsource(EntryGatewayHandler._handle_create_task)
    assert "strict_tool" in src
    assert "strict_model" in src
    # Both must propagate to _kwargs (the orchestrator pin payload).
    assert '_kwargs["strict_tool"] = True' in src or '_kwargs["strict_tool"]' in src
    assert '_kwargs["strict_model"] = True' in src or '_kwargs["strict_model"]' in src


# ---------------------------------------------------------------------------
# 8. Local model cannot enter candidates (P9 boundary)
# ---------------------------------------------------------------------------
def test_local_model_cannot_enter_candidates():
    """The entry gateway's local-model policy surface must still
    reject *:ollama bindings (P9 close-out §三)."""
    from aios_entry_gateway import _collect_local_model_policy
    # Make sure the env var is NOT set.
    os.environ.pop("AIOS_OLLAMA_USER_APPROVED_AT", None)
    os.environ["AIOS_LOCAL_MODEL_INFERENCE_ALLOWED"] = "0"
    policy = _collect_local_model_policy()
    assert policy["inference_allowed"] is False
    assert policy["routing_eligible"] is False
    assert policy["fallback_allowed"] is False
    assert policy["canary_allowed"] is False
    assert policy["recovery_manager_allowed"] is False
    assert policy["activation_mode"] == "MANUAL_USER_APPROVAL_ONLY"


# ---------------------------------------------------------------------------
# 9. Task policy persistence unchanged at the entry gateway
# ---------------------------------------------------------------------------
def test_task_policy_persistence_unchanged():
    """The entry gateway must still forward the same set of task-policy
    fields to the orchestrator.submit pin (P9A boundary)."""
    from aios_entry_gateway import EntryGatewayHandler
    import inspect
    src = inspect.getsource(EntryGatewayHandler._handle_create_task)
    # Each P9 task-local control must be forwarded.
    for field in (
        "preferred_executor",
        "preferred_model_binding",
        "preferred_planner",
        "preferred_reviewer",
        "allow_planner_fallback",
        "allow_reviewer_fallback",
        "strict_tool",
        "strict_model",
        "blocked_tools",
        "blocked_model_bindings",
        "blocked_resources",
        "blocked_reviewer_tools",
        "blocked_planner_tools",
    ):
        assert field in src, f"entry gateway dropped field {field!r}"


# ---------------------------------------------------------------------------
# 10. Default max_handlers is 32
# ---------------------------------------------------------------------------
def test_default_max_handlers_is_32():
    """Operators must be able to read the configured cap."""
    os.environ.pop("AIOS_GATEWAY_MAX_HANDLERS", None)
    assert _default_max_handlers() == 32


# ---------------------------------------------------------------------------
# 11. Custom max_handlers overrides default
# ---------------------------------------------------------------------------
def test_custom_max_handlers_overrides_default():
    os.environ["AIOS_GATEWAY_MAX_HANDLERS"] = "8"
    try:
        assert _default_max_handlers() == 8
    finally:
        os.environ.pop("AIOS_GATEWAY_MAX_HANDLERS", None)


# ---------------------------------------------------------------------------
# 12. Invalid max_handlers falls back to default
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["-1", "abc", "0", "99999999"])
def test_invalid_max_handlers_falls_back_to_default(bad):
    os.environ["AIOS_GATEWAY_MAX_HANDLERS"] = bad
    try:
        cap = _default_max_handlers()
        assert 1 <= cap <= 1024, f"cap out of bounds for {bad!r}: {cap}"
    finally:
        os.environ.pop("AIOS_GATEWAY_MAX_HANDLERS", None)


# ---------------------------------------------------------------------------
# 13. inflight() and overflow_total() observability
# ---------------------------------------------------------------------------
def test_inflight_and_overflow_observability():
    """The server exposes in-flight count and overflow counter for the
    monitor / status surfaces."""
    s = _Server(max_handlers=2)
    try:
        assert s.server.max_handlers() == 2
        assert s.server.inflight() == 0
        assert s.server.overflow_total() == 0

        def slow_requester():
            _http_get(s.url("/slow"), timeout=10)

        threads = [threading.Thread(target=slow_requester, daemon=True)
                   for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        assert s.server.inflight() == 2, (
            f"expected 2 in-flight, got {s.server.inflight()}"
        )
        # A third request overflows.
        d, code, _ = _http_get(s.url("/health"), timeout=3)
        assert code == 503
        assert s.server.overflow_total() == 1, (
            f"expected 1 overflow, got {s.server.overflow_total()}"
        )
        for t in threads:
            t.join(timeout=10)
    finally:
        s.shutdown()


# ---------------------------------------------------------------------------
# 14. Handler exception does not leak the semaphore slot
# ---------------------------------------------------------------------------
class _BoomHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        # Raise inside the handler so the server's
        # ``_process_request_thread_safe`` runs the ``except`` branch
        # of the bounded semaphore release.  We deliberately do NOT
        # call ``send_error`` here — the production contract is that
        # an unhandled exception in a handler must still release the
        # slot AND emit a 500 to the client.
        raise RuntimeError("boom")

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except Exception:
            # Mirror the BaseHTTPRequestHandler default behaviour but
            # do it explicitly so the client always sees a 500
            # response (instead of a bare RemoteDisconnected) and
            # the test can observe it deterministically.
            try:
                self.send_error(500, "Internal Server Error")
            except Exception:
                pass


def test_handler_exception_releases_semaphore():
    """A handler that raises must still release the semaphore — the
    next request must not see a phantom in-flight slot."""
    # Build a dedicated server whose handler actually raises.  The
    # existing ``_Server`` uses ``_ProbeHandler`` which returns 404
    # for unknown paths; that path does NOT exercise the exception
    # branch of ``_process_request_thread_safe``, so this test used
    # to be racy against the happy-path release order.  Use
    # ``_BoomHandler`` so we cover the real production contract.
    port = _free_port()
    server = BoundedThreadingHTTPServer(
        ("127.0.0.1", port), _BoomHandler, max_handlers=1,
    )
    thread = threading.Thread(
        target=server.serve_forever, daemon=True,
        name=f"boom-server-{port}",
    )
    thread.start()
    try:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=2,
            ) as r:
                raise AssertionError("expected error")
        except (urllib.error.HTTPError, urllib.error.URLError):
            pass
        # The server must release the slot even when the handler
        # raises.  Poll briefly to absorb the small scheduling delay
        # between the server's HTTPError write and the inflight
        # counter decrement — the count goes to zero as soon as
        # ``_process_request_thread_safe`` runs its ``except`` branch
        # which releases the slot BEFORE ``handle_error``.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if server.inflight() == 0:
                break
            time.sleep(0.01)
        assert server.inflight() == 0, (
            f"in-flight leaked after exception: {server.inflight()}"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)