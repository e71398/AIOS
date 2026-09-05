#!/usr/bin/env python3
"""AIOS shared HTTP server primitives.

This module provides :class:`BoundedThreadingHTTPServer`, a thin
wrapper around :class:`http.server.ThreadingHTTPServer` that:

* runs **one** Python thread per accepted request (so the gateway's
  slow handlers — currently ``_handle_aggregate``, ``_handle_status``
  and ``_handle_verify`` — cannot block ``/health``);
* caps the number of **concurrent** handler threads via a
  :class:`threading.BoundedSemaphore` so a flood of slow requests
  cannot spawn an unbounded thread population.  When the cap is
  reached, the server returns a deterministic ``503 Service
  Unavailable`` with ``Retry-After: 1`` and a JSON error body; it
  does **not** silently drop the request and it does **not** leave
  the connection half-open;
* uses ``daemon_threads=True`` so service shutdown cannot be held
  hostage by a non-daemon handler thread that is still in flight;
* keeps the existing ``BaseHTTPRequestHandler`` semantics, the
  default listen address resolution, and the JSON response helpers
  defined alongside each gateway — i.e. this module intentionally
  does not re-implement any handler or wrap any user code.

This is the smallest change that satisfies the P9A concurrent-isolation
requirement.  It does **not** introduce a second registry, a second
router, or a second executor pool; it does not modify the request
schema, the listen ports, or the public API surface.
"""
from __future__ import annotations

import os
import threading
from http.server import ThreadingHTTPServer


def _default_max_handlers() -> int:
    """Read the bounded-handler cap from the environment.

    Default ``32`` matches the historical ``_WORKFLOW_POOL`` worker
    count and is well below the typical systemd user-service thread
    limit.  Operators can override via ``AIOS_GATEWAY_MAX_HANDLERS``.
    """
    raw = os.environ.get("AIOS_GATEWAY_MAX_HANDLERS", "32")
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        cap = 32
    return max(1, min(cap, 1024))


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server with a bounded concurrent-handler cap.

    The cap is enforced via a :class:`threading.BoundedSemaphore`.
    When the cap is exhausted, the server synthesises a
    ``503 Service Unavailable`` response and closes the accepted
    socket.  No silent drops, no half-open sockets, no orphan
    threads.

    We deliberately do **not** touch ``self._threads`` here.  In
    Python 3.12, ``ThreadingMixIn`` exposes ``_threads`` as a
    ``_NoThreads``/``_Threads`` helper whose mutation methods
    changed between minor versions; we own our own thread tracking
    via ``self._live_threads`` so the implementation is portable
    across CPython 3.11 / 3.12 / 3.13 / 3.14.
    """

    # Threads spawned per request MUST be daemon threads so that
    # ``server.shutdown()`` does not block on a long-running handler
    # while systemd is trying to stop the unit.
    daemon_threads = True

    def __init__(self, address, handler_cls, max_handlers: int | None = None):
        cap = max_handlers if max_handlers is not None else _default_max_handlers()
        super().__init__(address, handler_cls)
        self._max_handlers = cap
        self._sem = threading.BoundedSemaphore(cap)
        # Track the number of in-flight handlers for observability.
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._overflow_total = 0
        # Owned thread tracking set; do NOT rely on ThreadingMixIn._threads.
        self._live_threads: set[threading.Thread] = set()
        self._live_threads_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Concurrency primitives
    # ------------------------------------------------------------------
    def inflight(self) -> int:
        """Return the current number of in-flight handler threads.

        Close-out 20260728 P9D-R: ``inflight()`` returns the count of
        accepted handler slots that have not yet been released by
        :meth:`_unregister_inflight`.  Both the happy path and the
        exception path of :meth:`_process_request_thread_safe` release
        the slot *before* the worker thread returns to the runtime,
        so a follow-up ``inflight()`` query observes the decrement as
        soon as the HTTPError / 500 response has been written to the
        socket — i.e. exactly when the client side can resume
        sending the next request.  ``is_alive()`` was racy here
        because the worker thread is still alive while
        ``handle_error`` / ``shutdown_request`` are running, so the
        slot would appear leaked to a fast client.  ``_live_threads``
        is preserved for the debug-only :meth:`live_threads` accessor.
        """
        with self._inflight_lock:
            return self._inflight

    def overflow_total(self) -> int:
        """Return the cumulative number of 503 overflow responses."""
        with self._inflight_lock:
            return self._overflow_total

    def max_handlers(self) -> int:
        """Return the configured concurrency cap."""
        return self._max_handlers

    def live_threads(self) -> int:
        """Return the number of tracked handler threads still alive."""
        with self._live_threads_lock:
            # Drop dead threads so the count does not monotonically grow.
            alive = {t for t in self._live_threads if t.is_alive()}
            self._live_threads = alive
            return len(alive)

    # ------------------------------------------------------------------
    # ThreadingMixIn overrides
    # ------------------------------------------------------------------
    def process_request(self, request, client_address):
        """Spawn a daemon handler thread iff the cap allows it.

        If the cap is exhausted, the override sends a 503 response
        on the accepted socket and closes it.  No thread is spawned
        and the semaphore is left untouched.
        """
        if not self._sem.acquire(blocking=False):
            self._send_overflow(request, client_address)
            return
        try:
            self._register_inflight()
            t = threading.Thread(
                target=self._process_request_thread_safe,
                args=(request, client_address),
                name=f"aios-gw-{self._inflight}/{self._max_handlers}",
                daemon=True,
            )
            with self._live_threads_lock:
                self._live_threads.add(t)
            t.start()
        except Exception:
            # Spawn failure → release both observers and the semaphore
            # so the connection does not become a leak.
            self._unregister_inflight()
            self._sem.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        """Compatibility shim — never reached in our override.

        :class:`ThreadingMixIn` calls back into this method from the
        spawned thread.  We route everything through
        :meth:`_process_request_thread_safe` so the semaphore release
        and inflight bookkeeping always run, even if the handler
        raises.
        """
        try:
            self.finish_request(request, client_address)
        finally:
            self.shutdown_request(request)

    def _process_request_thread_safe(self, request, client_address):
        """Per-request entry point with hard release semantics.

        Close-out 20260728 P9D-R: every exit path through this method
        runs a single ``finally`` that:

        1. releases the in-flight counter (so a follow-up ``inflight()``
           query returns ``0`` as soon as the response has been
           written to the socket);
        2. releases the bounded semaphore (so the next request gets
           the slot);
        3. drops this thread from ``_live_threads`` (so ``live_threads()``
           stays accurate even if the test re-uses the server).

        Without this, the Python-level ``except`` / ``else`` split can
        leave the worker thread in a state where ``shutdown_request``
        is still finishing while the client has already received the
        HTTPError / RemoteDisconnected and queried ``inflight()`` —
        that race produced the intermittent ``1 != 0`` failures of
        ``test_handler_exception_releases_semaphore`` under full
        repository load.
        """
        current_thread = threading.current_thread()
        try:
            try:
                self.finish_request(request, client_address)
            except Exception:
                # Defensive: never let an unhandled handler exception
                # escape into the spawned thread and silently swallow
                # the semaphore slot.  Release the slot *before*
                # ``handle_error`` writes the error response so a
                # follow-up client request never sees a phantom
                # ``inflight=1`` after the HTTPError has been raised
                # on the client side.
                self._unregister_inflight()
                self._sem.release()
                self.handle_error(request, client_address)
            else:
                # Happy path: release the slot *before* ``shutdown_request``
                # so a follow-up request observes ``inflight() == 0`` as
                # soon as the response body has been sent.
                self._unregister_inflight()
                self._sem.release()
        finally:
            # Single funnel for socket teardown and live-thread cleanup
            # — covers both the happy and exception paths so the
            # _live_threads set cannot leak entries across requests.
            try:
                self.shutdown_request(request)
            except Exception:
                pass
            with self._live_threads_lock:
                self._live_threads.discard(current_thread)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _register_inflight(self) -> None:
        with self._inflight_lock:
            self._inflight += 1

    def _unregister_inflight(self) -> None:
        with self._inflight_lock:
            if self._inflight > 0:
                self._inflight -= 1

    def _send_overflow(self, request, client_address) -> None:
        """Emit a deterministic 503 response for a saturated server."""
        with self._inflight_lock:
            self._overflow_total += 1
        body = (
            b'{"ok":false,"error":"server_overloaded",'
            b'"retry_after":1,'
            b'"hint":"minimax or local executor saturating the gateway; '
            b'a retry of up to 1s is acceptable"}'
        )
        try:
            response = (
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Retry-After: 1\r\n"
                b"Connection: close\r\n"
                b"X-Content-Type-Options: nosniff\r\n\r\n"
            )
            request.sendall(response + body)
        except Exception:
            # The client may have already closed; the OS will reap
            # the socket either way.  We never let this raise out.
            pass
        finally:
            try:
                self.shutdown_request(request)
            except Exception:
                pass


# ----------------------------------------------------------------------
# Module-level helper for callers that do not want to know the cap
# ----------------------------------------------------------------------
def make_bounded_server(address, handler_cls, max_handlers: int | None = None):
    """Factory: build a :class:`BoundedThreadingHTTPServer`.

    Centralising the construction makes it harder for future code
    paths to fall back to the bare ``HTTPServer`` constructor and
    silently re-introduce the single-thread bottleneck.
    """
    return BoundedThreadingHTTPServer(address, handler_cls,
                                       max_handlers=max_handlers)