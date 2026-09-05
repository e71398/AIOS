#!/usr/bin/env python3
"""AIOS P9B — Health Truth Publisher.

Background, daemon-free, in-process publisher that periodically writes
**lightweight** health observations to the tool-health cache so that the
adapter's ``health()`` always returns a fresh record instead of an
11+ hour-stale failure.

Why this exists
---------------

The legacy ``health()`` path in :mod:`aios_tool_adapter` reads its
state from a JSON cache file in ``cache/tool_health/<tool>.json`` that
is updated by the slow, full-inference :py:meth:`ToolAdapter.probe`
method.  Production has no scheduled ``probe()``; the cache is only
written when a *real* task happens to invoke the probe path.  As a
result, between real tasks the ``checked_at`` timestamp drifts hours
or days into the past and ``stale: true`` permanently pins
``fully_operational`` to ``false`` even when the underlying tool
service is alive and serving requests.

This module splits the freshness and the inference verification into
two independent dimensions:

* **lightweight health** (this module) — a cheap, non-inference HTTP
  ping against the tool service.  Updates the cache every
  ``AIOS_HEALTH_PUBLISHER_INTERVAL_SECONDS`` (default 60).  Drives
  ``reachable`` / ``protocol_ready`` / ``fresh``.
* **inference health** (the existing ``probe()`` path) — a real
  canary prompt.  Drives ``inference_verified`` / ``provider_ready``.
  Still respects ``probe_max_age_seconds`` and the
  ``probe_failure_backoff_seconds`` cooldown.

The adapter merges both dimensions into the new
``fully_operational`` and ``failure_scope`` fields without ever
hard-coding ``fully_operational = True``; both checks must pass
on their own merits.

Design constraints (P9B §E4, §E1)
---------------------------------

* The publisher NEVER calls a real model API; it only does
  ``HTTP GET`` against the locally-known service health URL or
  ``subprocess.run`` of an explicit ``--ping`` command.
* A single tool failure does not block the rest of the loop.
* Exceptions are logged, counted, and the loop continues.
* No second registry, no second health store; the JSON file in
  ``cache/tool_health/`` is the single source of truth.
* Timestamps are ISO-8601 UTC with explicit ``+00:00`` offset.
* ``failure_scope`` distinguishes ``tool_process`` /
  ``tool_protocol`` / ``tool_configuration`` / ``provider_auth`` /
  ``provider_quota`` / ``provider_network`` / ``provider_model`` /
  ``health_publisher`` / ``health_stale`` / ``unknown`` so the
  orchestrator can route around a single failure class.

This module is intentionally self-contained: it does not import
``aios_bus`` or any Redis client, so the lightweight probe is
runnable from the Monitor, from a one-shot test, or from a CI job.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

HOME = Path(os.getenv("AIOS_HOME", "${AIOS_HOME}")).resolve()
CONFIG = HOME / "config/tool_adapters.json"
CACHE = HOME / "cache/tool_health"
SUPPORTED_CONTRACTS = {"1.0", "1.1"}

_DEFAULT_INTERVAL = 60           # seconds between full publisher sweeps
_DEFAULT_TIMEOUT  = 5            # seconds per lightweight probe
_DEFAULT_MAX_AGE  = 300          # lightweight probe freshness TTL (5 min)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: str, limit: int = 500) -> str:
    """Redact ``api_key=…`` / ``token=…`` style substrings before persisting."""
    if not value:
        return ""
    return re.sub(
        r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+",
        r"\1=[redacted]",
        value.strip(),
    )[-limit:]


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically; never leave a partial file on disk."""
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Lightweight probe — pure HTTP / subprocess, no model API call
# ---------------------------------------------------------------------------
def _http_ping(url: str, timeout: float) -> Tuple[bool, str, int, str]:
    """Return ``(ok, reason, latency_ms, body)`` for a GET against ``url``."""
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "aios-health-publisher/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:500]
            elapsed_ms = int((time.monotonic() - started) * 1000)
            ok = resp.status == 200
            if ok and ("healthy" in body or "ok" in body or "status" in body):
                return True, "ok", elapsed_ms, _safe_text(body)
            if ok:
                return True, "protocol_unknown", elapsed_ms, _safe_text(body)
            return False, f"http_{resp.status}", elapsed_ms, _safe_text(body)
    except urllib.error.HTTPError as exc:
        return False, f"http_{exc.code}", int((time.monotonic() - started) * 1000), ""
    except (urllib.error.URLError, socket.timeout, ConnectionRefusedError) as exc:
        return False, f"{type(exc).__name__}: {exc}", int((time.monotonic() - started) * 1000), ""
    except Exception as exc:  # pragma: no cover — defensive
        return False, f"{type(exc).__name__}: {exc}", int((time.monotonic() - started) * 1000), ""


def _cmd_ping(cmd: list, timeout: float) -> Tuple[bool, str, int, str]:
    started = time.monotonic()
    try:
        env = dict(os.environ)
        env["PATH"] = "${HOME}/.n/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            shell=False, env=env,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body = (result.stdout or result.stderr or "").strip()[:500]
        ok = result.returncode == 0
        return ok, "ok" if ok else f"rc={result.returncode}", elapsed_ms, _safe_text(body)
    except subprocess.TimeoutExpired:
        return False, "timeout", int((time.monotonic() - started) * 1000), ""
    except Exception as exc:  # pragma: no cover — defensive
        return False, f"{type(exc).__name__}: {exc}", int((time.monotonic() - started) * 1000), ""


def _classify_unreachable(text: str) -> str:
    """Classify a failure into a ``failure_scope`` enum value."""
    lower = text.lower()
    if any(x in lower for x in ("refused", "reset", "unreachable",
                                "name or service not known", "dns",
                                "no route", "timed out", "timeout")):
        return "tool_process"
    if "401" in lower or "403" in lower or "auth" in lower:
        return "provider_auth"
    if "429" in lower or "quota" in lower or "rate" in lower:
        return "provider_quota"
    if "404" in lower or "500" in lower or "502" in lower or "503" in lower:
        return "tool_protocol"
    if "no provider" in lower or "model not found" in lower:
        return "provider_model"
    return "tool_process"


def probe_lightweight(name: str, cfg: dict, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """One lightweight observation for ``name`` based on its ``cfg``.

    Returns a record with the **freshness** fields the adapter merges
    into its cache.  This call never invokes a real model API.
    """
    now = now_iso()
    ping_url = cfg.get("lightweight_ping_url")
    ping_cmd = cfg.get("lightweight_ping_cmd")
    started_iso = now
    if ping_url:
        ok, reason, latency_ms, body = _http_ping(ping_url, timeout=timeout)
        kind = "http"
    elif ping_cmd:
        ok, reason, latency_ms, body = _cmd_ping(ping_cmd, timeout=timeout)
        kind = "command"
    else:
        # No lightweight probe configured; fall back to the binary check.
        return {
            "observed_at": now,
            "lightweight_checked_at": now,
            "last_success_at": None,
            "expires_at": None,
            "fresh": False,
            "reachable": None,
            "protocol_ready": None,
            "provider_ready": None,
            "inference_verified": None,
            "fully_operational": None,
            "failure_scope": "health_stale",
            "reason": "no_lightweight_ping_configured",
            "kind": "none",
        }

    failure_scope = _classify_unreachable(reason) if not ok else "unknown"
    record = {
        "observed_at": now,
        "lightweight_checked_at": now,
        "last_success_at": now if ok else None,
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(seconds=int(cfg.get(
                           "lightweight_max_age_seconds", _DEFAULT_MAX_AGE)))
                       ).isoformat(),
        "fresh": ok,
        "reachable": ok,
        "protocol_ready": ok,
        "provider_ready": None,
        "inference_verified": None,
        "fully_operational": None,  # filled by adapter.health()
        "failure_scope": failure_scope,
        "reason": reason,
        "latency_ms": latency_ms,
        "kind": kind,
        "evidence": body,
    }
    return record


# ---------------------------------------------------------------------------
# Cache merge
# ---------------------------------------------------------------------------
def _load_cache(name: str) -> dict:
    path = CACHE / f"{name}.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def merge_lightweight(name: str, lightweight: dict) -> dict:
    """Merge a new lightweight record into the cache file for ``name``.

    The cached inference / model data is preserved verbatim; only the
    new fields are updated.  Returns the merged record on disk.
    """
    path = CACHE / f"{name}.json"
    cache = _load_cache(name)
    cache["lightweight_observed_at"] = lightweight.get("observed_at")
    cache["lightweight_checked_at"] = lightweight.get("lightweight_checked_at")
    cache["lightweight_last_success_at"] = lightweight.get("last_success_at")
    cache["lightweight_expires_at"] = lightweight.get("expires_at")
    cache["lightweight_fresh"] = bool(lightweight.get("fresh"))
    cache["lightweight_reachable"] = lightweight.get("reachable")
    cache["lightweight_protocol_ready"] = lightweight.get("protocol_ready")
    cache["lightweight_failure_scope"] = lightweight.get("failure_scope")
    cache["lightweight_reason"] = lightweight.get("reason")
    cache["lightweight_kind"] = lightweight.get("kind")
    cache["lightweight_latency_ms"] = lightweight.get("latency_ms")
    cache["lightweight_evidence"] = lightweight.get("evidence", "")
    # We never overwrite the legacy ``checked_at`` / ``model_state``
    # fields; the inference path owns those.
    _atomic_write_json(path, cache)
    return cache


# ---------------------------------------------------------------------------
# Iteration across registered tools
# ---------------------------------------------------------------------------
def _load_adapters() -> dict:
    if not CONFIG.is_file():
        return {}
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        if str(data.get("contract_version")) not in SUPPORTED_CONTRACTS:
            return {}
        tools = data.get("tools") or {}
        return {n: cfg for n, cfg in tools.items() if cfg.get("enabled", True)}
    except Exception:
        return {}


def publish_once(timeout_per_tool: float = _DEFAULT_TIMEOUT,
                 only: Optional[Iterable[str]] = None) -> Dict[str, dict]:
    """Publish one sweep across all enabled tools.

    Returns a mapping ``tool_name → merged_cache``.  A single tool
    failure does not stop the rest of the sweep.
    """
    out: Dict[str, dict] = {}
    adapters = _load_adapters()
    targets = [only] if only is not None else list(adapters.keys())
    for name in targets:
        cfg = adapters.get(name)
        if cfg is None:
            continue
        try:
            lightweight = probe_lightweight(name, cfg, timeout=timeout_per_tool)
            merged = merge_lightweight(name, lightweight)
            out[name] = merged
        except Exception as exc:  # pragma: no cover — defensive
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------
class HealthPublisherThread:
    """Long-running daemon thread that publishes lightweight health."""

    def __init__(self, interval_seconds: float = _DEFAULT_INTERVAL,
                 timeout_per_tool: float = _DEFAULT_TIMEOUT,
                 started_event: Optional[threading.Event] = None) -> None:
        self._interval = max(5.0, float(interval_seconds))
        self._timeout = max(1.0, float(timeout_per_tool))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_event = started_event or threading.Event()
        self._last_publish: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._failure_count = 0

    @property
    def interval(self) -> float:
        return self._interval

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def last_published_at(self, name: str) -> Optional[float]:
        with self._lock:
            return self._last_publish.get(name)

    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="aios-health-publisher", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        # Publish once immediately so the cache is fresh on start-up.
        try:
            self._publish_all()
        except Exception:  # pragma: no cover
            with self._lock:
                self._failure_count += 1
        self._started_event.set()
        while not self._stop.is_set():
            # Sleep in small chunks so stop() is responsive.
            slept = 0.0
            while slept < self._interval and not self._stop.is_set():
                time.sleep(min(0.5, self._interval - slept))
                slept += 0.5
            if self._stop.is_set():
                break
            try:
                self._publish_all()
            except Exception:  # pragma: no cover
                with self._lock:
                    self._failure_count += 1

    def _publish_all(self) -> None:
        result = publish_once(timeout_per_tool=self._timeout)
        with self._lock:
            for name in result:
                self._last_publish[name] = time.time()


# ---------------------------------------------------------------------------
# Public singleton
# ---------------------------------------------------------------------------
_PUBLISHER: Optional[HealthPublisherThread] = None
_PUBLISHER_LOCK = threading.Lock()


def get_publisher() -> HealthPublisherThread:
    """Return the process-wide singleton publisher (lazily created)."""
    global _PUBLISHER
    with _PUBLISHER_LOCK:
        if _PUBLISHER is None:
            interval = float(os.environ.get(
                "AIOS_HEALTH_PUBLISHER_INTERVAL_SECONDS", _DEFAULT_INTERVAL))
            timeout = float(os.environ.get(
                "AIOS_HEALTH_PUBLISHER_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT))
            _PUBLISHER = HealthPublisherThread(
                interval_seconds=interval, timeout_per_tool=timeout,
            )
        return _PUBLISHER


def start_publisher() -> HealthPublisherThread:
    """Start the publisher if it is not already running and return it."""
    pub = get_publisher()
    if not pub.is_alive:
        pub.start()
    return pub


__all__ = [
    "CACHE",
    "CONFIG",
    "HealthPublisherThread",
    "merge_lightweight",
    "probe_lightweight",
    "publish_once",
    "start_publisher",
]