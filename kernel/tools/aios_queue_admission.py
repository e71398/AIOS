#!/usr/bin/env python3
"""
aios_queue_admission.py
========================

Close-out 20260727-§三: per-source Pending-queue admission guard.

The Pending queue at ``aios:bus:queue:pending`` is shared by every
submission channel.  Without a bound, a runaway acceptance / test
runner can enqueue hundreds of identical tasks before any executor
finishes the first one, starving real user / API tasks.

This module adds the *smallest possible* source-level admission
control:

* a configurable set of ``test_backlog_sources`` (default
  ``acceptance``, ``test``, ``pytest``, ``closeout``);
* a per-source pending cap (``test_pending_limit`` default 5);
* a deterministic ``admit()`` interface returned to callers;
* out-of-band audit (4 fields: source, current_pending,
  configured_limit, decision) for every admit call.

The guard reads the live state from Redis (the same Redis the
Executor daemon uses) so it stays accurate even if the queue is
being drained concurrently.  Redis is *not* an authoritative
counter for "running" tasks — it only counts pending and indexed
work — so the limit is intentionally loose.  The point is to
stop the *flood*, not to be a perfect throttler.

The module is exported via ``evaluate()`` and ``admit()`` so the
gateway / dispatcher can use it without coupling to a specific
config schema.  Configuration is read from
``config/features.toml`` under ``[queue.admission]`` *and* via
process env vars (used by the closeout tests):

    AIOS_TEST_PENDING_LIMIT   -> int (default 5)
    AIOS_TEST_BACKLOG_SOURCES -> comma-separated (default
                                acceptance,test,pytest,closeout)
    AIOS_QUEUE_ADMISSION_ENABLED -> "0" disables the guard

The guard is intentionally **fail-open** when Redis is unavailable
or the config file is missing.  The close-out environment is
Redis-backed; if Redis is down, the queue is already broken in
much bigger ways and the operator can flip the env var to 0 to
take the guard out of the picture.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

_DEFAULT_TEST_LIMIT = 5
_DEFAULT_TEST_SOURCES = ("acceptance", "test", "pytest", "closeout")
_DEFAULT_REJECT_REASON = "TEST_BACKLOG_LIMIT_REACHED"
_DEFAULT_REJECT_STATUS = 429
_REAL_USER_SOURCES = ("feishu", "cli", "cron", "telegram", "web",
                       "api", "system", "openclaw")
_TERMINAL_STATUSES = (
    "completed", "failed", "cancelled", "blocked",
    "verification_blocked", "no_external_production_route",
    "superseded", "dead_letter",
)

# Redis keys (mirror of aios_bus — duplicated on purpose so the
# guard has no import-time dependency on aios_bus, which keeps
# unit tests in this module fast).
_KEY_PREFIX = "aios:bus"
_KEY_QUEUE_PENDING = f"{_KEY_PREFIX}:queue:pending"
_KEY_INDEX = f"{_KEY_PREFIX}:index"
_KEY_STATE = f"{_KEY_PREFIX}:state"

# ---------------------------------------------------------------------------
# Config reader
# ---------------------------------------------------------------------------


def _read_toml_field(data: Any, path: str, default: Any) -> Any:
    """Tiny dotted-path lookup helper for a nested dict tree."""
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _try_load_features_toml() -> Dict[str, Any]:
    """Return the parsed ``[queue.admission]`` block or ``{}``."""
    p = Path(__file__).resolve().parents[2] / "config" / "features.toml"
    if not p.exists():
        return {}
    try:
        # tomllib (≥3.11) takes precedence; fall back to ``toml`` if
        # the user is on an older interpreter.
        try:
            import tomllib  # type: ignore[attr-defined]
            with open(p, "rb") as fh:
                return tomllib.load(fh).get("queue", {}).get("admission", {}) or {}
        except Exception:
            try:
                import toml  # type: ignore
                with open(p, "r", encoding="utf-8") as fh:
                    return (toml.load(fh).get("queue", {})
                            .get("admission", {}) or {})
            except Exception:
                return {}
    except Exception:
        return {}


def _load_config() -> Dict[str, Any]:
    """Resolve the active admission config (TOML ⊕ env, with env precedence)."""
    cfg = _try_load_features_toml()
    out: Dict[str, Any] = {
        "enabled": bool(cfg.get("enabled", True)),
        "test_pending_limit": int(cfg.get("test_pending_limit", _DEFAULT_TEST_LIMIT)),
        "test_backlog_sources": list(cfg.get("test_backlog_sources",
                                              list(_DEFAULT_TEST_SOURCES))),
        "real_user_sources": list(cfg.get("real_user_sources",
                                            list(_REAL_USER_SOURCES))),
        "reject_reason": str(cfg.get("reject_reason", _DEFAULT_REJECT_REASON)),
        "reject_status": int(cfg.get("reject_status", _DEFAULT_REJECT_STATUS)),
    }
    # Env overrides
    if os.environ.get("AIOS_QUEUE_ADMISSION_ENABLED") == "0":
        out["enabled"] = False
    raw_limit = os.environ.get("AIOS_TEST_PENDING_LIMIT")
    if raw_limit and raw_limit.strip().isdigit():
        out["test_pending_limit"] = int(raw_limit.strip())
    raw_src = os.environ.get("AIOS_TEST_BACKLOG_SOURCES")
    if raw_src:
        out["test_backlog_sources"] = [
            s.strip() for s in raw_src.split(",") if s.strip()
        ]
    return out


def get_config() -> Dict[str, Any]:
    """Public accessor: returns the current admission config (cached)."""
    return dict(_LOADED_CFG)


def _as_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v)]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return []


_LOADED_CFG: Dict[str, Any] = _load_config()


def reload_config() -> Dict[str, Any]:
    """Force a fresh read of features.toml + env.  Used by tests."""
    global _LOADED_CFG
    _LOADED_CFG = _load_config()
    return dict(_LOADED_CFG)


# ---------------------------------------------------------------------------
# Redis-side counters
# ---------------------------------------------------------------------------


def _redis_client():
    """Lazy Redis client.  Returns ``None`` if Redis is unreachable."""
    try:
        import redis  # type: ignore
        r = redis.Redis(host="localhost", port=6379,
                         socket_connect_timeout=1,
                         socket_timeout=1)
        # Cheap round-trip to avoid masking DNS race issues behind
        # a non-existent client.
        r.ping()
        return r
    except Exception:
        return None


def _count_test_pending(sources: List[str]) -> int:
    """Walk the pending list and count tasks whose source belongs to
    ``sources`` (or empty / missing source — those are considered
    backlog-equivalent for safety).

    We use the *pending* list (LRANGE) rather than the index so
    finished tasks do not inflate the count.  The pending list is
    bounded by the active backlog so the walk is cheap.
    """
    r = _redis_client()
    if r is None:
        return 0
    try:
        # Cap the scan so we don't OOM on a 100k-task queue.
        ids = r.lrange(_KEY_QUEUE_PENDING, 0, 9999) or []
        if not ids:
            return 0
        keys = [f"{_KEY_STATE}:{tid.decode() if isinstance(tid, bytes) else tid}"
                for tid in ids]
        pipe = r.pipeline()
        for k in keys:
            pipe.hget(k, "source")
            pipe.hget(k, "status")
        rows = pipe.execute()
        count = 0
        source_set = set(sources)
        for i in range(0, len(rows), 2):
            raw_src = rows[i]
            raw_status = rows[i + 1]
            src = (raw_src.decode() if isinstance(raw_src, bytes) else raw_src or "").strip()
            status = (raw_status.decode() if isinstance(raw_status, bytes) else raw_status or "").strip()
            if status in _TERMINAL_STATUSES:
                continue
            if not src:
                # Empty source treated as backlog (defensive).
                count += 1
                continue
            if src in source_set:
                count += 1
        return count
    except Exception:
        return 0


def _count_pending_total() -> int:
    """Return the full pending list length (for ``current_pending`` audit)."""
    r = _redis_client()
    if r is None:
        return 0
    try:
        return int(r.llen(_KEY_QUEUE_PENDING) or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate(source: str) -> Dict[str, Any]:
    """Return an admission decision for a single submission.

    Shape::

        {
            "ok": True | False,
            "decision": "ADMIT" | "REJECT",
            "reason": "TEST_BACKLOG_LIMIT_REACHED" | "",
            "http_status": 200 | 429,
            "source": <source>,
            "current_pending": <int>,
            "test_pending": <int>,
            "configured_limit": <int>,
            "timestamp": <iso8601>,
        }

    The gateway only needs ``ok``, ``reason``, ``http_status``;
    the remaining fields are exposed for audit / live close-out
    reporting.
    """
    cfg = _LOADED_CFG
    src = str(source or "").strip().lower()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    base = {
        "source": src,
        "configured_limit": int(cfg.get("test_pending_limit", _DEFAULT_TEST_LIMIT)),
        "timestamp": ts,
    }
    if not cfg.get("enabled", True):
        return {
            **base,
            "ok": True,
            "decision": "ADMIT",
            "reason": "ADMISSION_DISABLED",
            "http_status": 200,
            "current_pending": _count_pending_total(),
            "test_pending": 0,
        }
    real_sources = set(_as_list(cfg.get("real_user_sources"))) | set(_REAL_USER_SOURCES)
    if src in real_sources:
        return {
            **base,
            "ok": True,
            "decision": "ADMIT",
            "reason": "REAL_USER_SOURCE",
            "http_status": 200,
            "current_pending": _count_pending_total(),
            "test_pending": 0,
        }
    test_sources = set(_as_list(cfg.get("test_backlog_sources")))
    if src not in test_sources:
        # Unknown source — treat as real user (default open).  The
        # source whitelist at the gateway is the *real* gate; this
        # guard is a backstop, not a primary auth line.
        return {
            **base,
            "ok": True,
            "decision": "ADMIT",
            "reason": "NON_TEST_SOURCE",
            "http_status": 200,
            "current_pending": _count_pending_total(),
            "test_pending": 0,
        }
    test_pending = _count_test_pending(sorted(test_sources))
    limit = int(cfg.get("test_pending_limit", _DEFAULT_TEST_LIMIT))
    current_pending = _count_pending_total()
    if test_pending >= limit:
        return {
            **base,
            "ok": False,
            "decision": "REJECT",
            "reason": str(cfg.get("reject_reason", _DEFAULT_REJECT_REASON)),
            "http_status": int(cfg.get("reject_status", _DEFAULT_REJECT_STATUS)),
            "current_pending": current_pending,
            "test_pending": test_pending,
        }
    return {
        **base,
        "ok": True,
        "decision": "ADMIT",
        "reason": "TEST_BACKLOG_OK",
        "http_status": 200,
        "current_pending": current_pending,
        "test_pending": test_pending,
    }


def admit(source: str) -> Tuple[bool, Dict[str, Any]]:
    """Return ``(True, decision)`` for accepted, ``(False, decision)`` for rejected."""
    decision = evaluate(source)
    return bool(decision.get("ok")), decision


# ---------------------------------------------------------------------------
# CLI — used by operator + closeout scripts
# ---------------------------------------------------------------------------


def _cli() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show").set_defaults(_show=True)
    sub.add_parser("reload").set_defaults(_reload=True)
    p_check = sub.add_parser("check")
    p_check.add_argument("--source", required=True)
    args = parser.parse_args()
    if getattr(args, "_reload", False):
        cfg = reload_config()
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return 0
    if getattr(args, "_show", False):
        print(json.dumps(get_config(), indent=2, ensure_ascii=False))
        return 0
    decision = evaluate(args.source)
    print(json.dumps(decision, indent=2, ensure_ascii=False))
    return 0 if decision["ok"] else 2


if __name__ == "__main__":
    sys.exit(_cli())