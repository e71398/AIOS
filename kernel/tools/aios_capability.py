#!/usr/bin/env python3
"""AIOS P5 Unified Capability Truth Source.

Provides a single authoritative function to determine per-tool capability
status, consumed by orchestrator (executor selection), acceptance (canary
report), and monitor (health dimensions).

Design constraints (AIOS-P5-CORE-ROUTING-CONFIG-SYSTEMD-CONVERGENCE-11):

* AVAILABLE requires recent real success evidence, not just service active.
* DEGRADED_EXTERNAL for quota/plan/auth failures.
* DEGRADED_INTERNAL for timeout/probe errors.
* UNAVAILABLE for fatal lifecycle states.
* UNVERIFIED when only runtime active but no real success evidence.
* NOT_CONFIGURED when not in registry.
* All consumers use the same function — no duplicate logic.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# P8A: dynamic tool discovery. ``evaluate_all_capabilities`` and
# ``capability_matrix`` no longer assume a fixed five-tool enumeration;
# they read the tool list from the registry. Importing the registry
# lazily avoids a hard import-time dependency at module load (which
# would also break the lightweight Monitor loop).
try:
    from aios_tool_registry import (
        get_default_registry as _default_tool_registry,
        ToolRegistry as _ToolRegistry,
    )
except Exception:  # pragma: no cover - extremely defensive
    _default_tool_registry = None
    _ToolRegistry = None

HOME = Path(os.getenv("AIOS_HOME", "${AIOS_HOME}"))
TOOLS = HOME / "kernel/tools"
CACHE = HOME / "cache/tool_health"
CONFIG = HOME / "config/tool_adapters.json"

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

AVAILABLE = "AVAILABLE"
DEGRADED_EXTERNAL = "DEGRADED_EXTERNAL"
DEGRADED_INTERNAL = "DEGRADED_INTERNAL"
UNAVAILABLE = "UNAVAILABLE"
UNVERIFIED = "UNVERIFIED"
NOT_CONFIGURED = "NOT_CONFIGURED"

ALL_STATUSES = (AVAILABLE, DEGRADED_EXTERNAL, DEGRADED_INTERNAL,
                UNAVAILABLE, UNVERIFIED, NOT_CONFIGURED)

# Evidence validity: how old a successful probe can be before we downgrade
# to UNVERIFIED.
DEFAULT_EVIDENCE_MAX_AGE_SECONDS = 7200  # 2 hours

# Cooldown after failure before rechecking
DEFAULT_FAILURE_COOLDOWN_SECONDS = 21600  # 6 hours

# ---------------------------------------------------------------------------
# Core capability record
# ---------------------------------------------------------------------------

CAPABILITY_KEYS = (
    "tool_id", "roles", "process_state", "adapter_state",
    "last_real_success_at", "last_real_failure_at", "last_failure_kind",
    "evidence_age_seconds", "cooldown_until", "fatal", "status", "reason",
    "source",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> float:
    return time.time()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _read_probe_cache(name: str) -> dict:
    """Read the probe cache file for a tool, returning {} on any error."""
    path = CACHE / f"{name}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_adapter_config(name: str) -> dict:
    """Read the adapter config for a tool from tool_adapters.json."""
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        tools = data.get("tools", {})
        return tools.get(name, {})
    except Exception:
        return {}


def _is_service_active(name: str) -> bool:
    """Check if the systemd service for this tool is active.

    P8A: the registry owns the ``service_unit_ref`` declaration for
    each tool. When the registry is unavailable (very early boot,
    tests that reset the singleton) we fall back to the historical
    inline map so existing capability probes continue to work.
    """
    inline_unit_map = {
        "opencode": "aios-executor-opencode.service",
        "claude": "aios-executor-claude.service",
        "codex": "aios-executor-codex.service",
        # P5 M3 reaudit: hermes unit name is `hermes-gateway.service`,
        # not `aios-hermes-gateway.service`. Both names are accepted for
        # defensive coverage; the canonical name is checked first.
        "hermes": "hermes-gateway.service",
        "openclaw": "openclaw-gateway.service",
    }
    unit_map = inline_unit_map
    if _default_tool_registry is not None:
        try:
            manifest = _default_tool_registry().get(name)
            if manifest is not None and manifest.service_unit_ref:
                unit_map = {name: manifest.service_unit_ref}
        except Exception:
            pass
    unit_aliases = {
        "hermes": ("hermes-gateway.service", "aios-hermes-gateway.service"),
    }
    unit = unit_map.get(name)
    if not unit:
        return False
    candidates = [unit]
    for alias in unit_aliases.get(name, ()):
        if alias not in candidates:
            candidates.append(alias)
    try:
        import subprocess
        for candidate in candidates:
            p = subprocess.run(
                ["systemctl", "--user", "is-active", candidate],
                capture_output=True, text=True, timeout=5,
            )
            if p.stdout.strip() == "active":
                return True
        return False
    except Exception:
        return False


def evaluate_capability(
    name: str,
    *,
    evidence_max_age_seconds: int = DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
    failure_cooldown_seconds: int = DEFAULT_FAILURE_COOLDOWN_SECONDS,
    now_epoch: Optional[float] = None,
) -> dict:
    """Evaluate the capability status of a single tool.

    Returns a dict with all CAPABILITY_KEYS populated.

    Priority (highest first):
    1. Not in registry → NOT_CONFIGURED
    2. Fatal lifecycle (disabled, missing binary) → UNAVAILABLE
    3. Recent real failure with active cooldown → DEGRADED_EXTERNAL/INTERNAL
    4. Recent real success (not expired) → AVAILABLE
    5. Runtime active only (service running, no success evidence) → UNVERIFIED
    6. Everything else → NOT_CONFIGURED
    """
    now_epoch = float(now_epoch if now_epoch is not None else _now_epoch())
    now_dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)

    # Default record
    record = {
        "tool_id": name,
        "roles": [],
        "process_state": "unknown",
        "adapter_state": "unknown",
        "last_real_success_at": None,
        "last_real_failure_at": None,
        "last_failure_kind": None,
        "evidence_age_seconds": -1,
        "cooldown_until": None,
        "fatal": False,
        "status": NOT_CONFIGURED,
        "reason": "not_in_registry",
        "source": "capability_module",
    }

    # Check adapter config
    config = _read_adapter_config(name)
    if not config:
        return record

    enabled = config.get("enabled", True)
    if not enabled:
        record.update({
            "status": UNAVAILABLE,
            "reason": "disabled_in_config",
            "fatal": True,
        })
        return record

    # Check binary existence
    executable = config.get("executable", "")
    if executable and not Path(executable).is_file():
        record.update({
            "status": UNAVAILABLE,
            "reason": f"binary_missing:{executable}",
            "fatal": True,
        })
        return record

    # Read probe cache
    probe = _read_probe_cache(name)
    model_state = str(probe.get("model_state", "") or "")
    model_available = bool(probe.get("model_available", False))
    checked_at = _parse_ts(probe.get("checked_at"))
    retry_after = _parse_ts(probe.get("retry_after"))
    evidence_source = str(probe.get("evidence_source", "") or "")

    # Calculate evidence age
    evidence_age = -1
    if checked_at:
        evidence_age = int((now_dt - checked_at).total_seconds())

    # P5F: explicit evidence freshness field. Separates "what kind of
    # failure we last observed" (status) from "how stale the evidence is"
    # (evidence_freshness). External quota/payment issues do NOT
    # become INTERNAL just because the cached evidence is old.
    if evidence_age < 0:
        evidence_freshness = "FRESHNESS_UNKNOWN"
    elif evidence_age < evidence_max_age_seconds:
        evidence_freshness = "FRESH"
    else:
        evidence_freshness = "STALE"
    record["evidence_freshness"] = evidence_freshness
    record["evidence_age_seconds"] = evidence_age
    record["evidence_max_age_seconds"] = evidence_max_age_seconds

    # Check if service is active
    service_active = _is_service_active(name)

    # Determine roles. P8A: read roles from the registry so the
    # canonical five-tool list is no longer hard-coded here. The
    # inline fallback is the historical P6A mapping, kept so legacy
    # capability probes still resolve when the registry is unavailable.
    inline_role_map = {
        "opencode": ["executor"],
        "claude": ["executor", "reviewer"],
        "codex": ["executor"],
        "hermes": ["reviewer"],
        "openclaw": ["planner"],
    }
    roles = list(inline_role_map.get(name, []))
    if _default_tool_registry is not None:
        try:
            manifest = _default_tool_registry().get(name)
            if manifest is not None and manifest.roles:
                # Union rather than replace: legacy callers may
                # already expect both reviewer and executor for
                # claude, etc. The registry's role set wins on
                # conflict because it's the canonical source.
                roles = list(dict.fromkeys(list(manifest.roles) + roles))
        except Exception:
            pass

    # Priority 1: Fatal lifecycle
    if model_state in ("disabled",):
        record.update({
            "roles": roles,
            "process_state": "active" if service_active else "inactive",
            "adapter_state": model_state,
            "status": UNAVAILABLE,
            "reason": f"fatal_lifecycle:{model_state}",
            "fatal": True,
        })
        return record

    # Priority 2: Recent real failure with active cooldown
    if retry_after and retry_after > now_dt:
        failure_kind = (
            DEGRADED_EXTERNAL if model_state in (
                "quota_exhausted", "rate_limited", "auth_failed",
            ) else DEGRADED_INTERNAL
        )
        record.update({
            "roles": roles,
            "process_state": "active" if service_active else "inactive",
            "adapter_state": model_state,
            "last_real_failure_at": probe.get("checked_at"),
            "last_failure_kind": model_state,
            "evidence_age_seconds": evidence_age,
            "cooldown_until": retry_after.isoformat(),
            "status": failure_kind,
            "reason": str(probe.get("reason", f"cooldown_active:{model_state}")),
            "source": "probe_cache",
        })
        return record

    # Priority 3: Recent real failure (no cooldown, but failed)
    if model_state in ("quota_exhausted", "rate_limited", "auth_failed"):
        record.update({
            "roles": roles,
            "process_state": "active" if service_active else "inactive",
            "adapter_state": model_state,
            "last_real_failure_at": probe.get("checked_at"),
            "last_failure_kind": model_state,
            "evidence_age_seconds": evidence_age,
            "status": DEGRADED_EXTERNAL,
            "reason": str(probe.get("reason", model_state)),
            "source": "probe_cache",
        })
        return record

    if model_state in ("timeout", "probe_error", "network_error"):
        record.update({
            "roles": roles,
            "process_state": "active" if service_active else "inactive",
            "adapter_state": model_state,
            "last_real_failure_at": probe.get("checked_at"),
            "last_failure_kind": model_state,
            "evidence_age_seconds": evidence_age,
            "status": DEGRADED_INTERNAL,
            "reason": str(probe.get("reason", model_state)),
            "source": "probe_cache",
        })
        return record

    # Priority 4: Recent real success (not expired)
    if model_available and evidence_age >= 0 and evidence_age < evidence_max_age_seconds:
        record.update({
            "roles": roles,
            "process_state": "active" if service_active else "inactive",
            "adapter_state": "available",
            "last_real_success_at": probe.get("checked_at"),
            "evidence_age_seconds": evidence_age,
            "status": AVAILABLE,
            "reason": "recent_real_success",
            "source": "probe_cache",
        })
        return record

    # Priority 5: Expired success evidence
    if model_available and evidence_age >= evidence_max_age_seconds:
        record.update({
            "roles": roles,
            "process_state": "active" if service_active else "inactive",
            "adapter_state": "stale",
            "last_real_success_at": probe.get("checked_at"),
            "evidence_age_seconds": evidence_age,
            "status": UNVERIFIED,
            "reason": f"evidence_expired:{evidence_age}s>={evidence_max_age_seconds}s",
            "source": "probe_cache",
        })
        return record

    # Priority 6: Runtime active only
    if service_active:
        record.update({
            "roles": roles,
            "process_state": "active",
            "adapter_state": "unverified",
            "status": UNVERIFIED,
            "reason": "service_active_no_recent_success_evidence",
            "source": "systemd",
        })
        return record

    # Priority 7: Nothing
    record.update({
        "roles": roles,
        "process_state": "inactive",
        "adapter_state": "unverified",
        "status": UNVERIFIED,
        "reason": "no_evidence_service_inactive",
        "source": "systemd",
    })
    return record




def _discover_tool_ids() -> List[str]:
    """Read the dynamic tool id list, falling back to the canonical
    five tools if the registry is unavailable.

    The function is intentionally defensive: the capability module is
    loaded by lightweight Monitor loops that should not crash if a
    registry import misbehaves. The fallback preserves P6A behaviour
    so existing tests and live monitors see the same tool set.
    """
    if _default_tool_registry is not None:
        try:
            registry = _default_tool_registry()
            ids = sorted(registry.tool_ids())
            if ids:
                return ids
        except Exception:
            pass
    return ["opencode", "claude", "codex", "hermes", "openclaw"]


def evaluate_all_capabilities(
    tool_names: Optional[List[str]] = None,
    *,
    evidence_max_age_seconds: int = DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
) -> dict[str, dict]:
    """Evaluate capability for all known tools or a subset.

    P8A: when ``tool_names`` is omitted, the function reads the tool
    list from the dynamic registry instead of a hard-coded five-tool
    tuple. New tools added via the overlay are picked up automatically
    without modifying this module. The inline fallback matches the
    historical five-tool list so legacy behaviour is preserved when
    the registry is unavailable (e.g. very early boot).
    """
    if tool_names is None:
        tool_names = list(_discover_tool_ids())
    return {
        name: evaluate_capability(
            name,
            evidence_max_age_seconds=evidence_max_age_seconds,
        )
        for name in tool_names
    }


def capability_matrix(
    tool_names: Optional[list[str]] = None,
    *,
    evidence_max_age_seconds: int = DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
) -> dict[str, str]:
    """Return a simple name→status mapping for health model consumption."""
    return {
        name: rec["status"]
        for name, rec in evaluate_all_capabilities(
            tool_names, evidence_max_age_seconds=evidence_max_age_seconds,
        ).items()
    }


def is_available(name: str, *, evidence_max_age_seconds: int = DEFAULT_EVIDENCE_MAX_AGE_SECONDS) -> bool:
    """Check if a tool is AVAILABLE for dispatch."""
    rec = evaluate_capability(name, evidence_max_age_seconds=evidence_max_age_seconds)
    return rec["status"] == AVAILABLE


def is_available_for_role(
    name: str, role: str,
    *,
    evidence_max_age_seconds: int = DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
) -> bool:
    """Check if a tool is AVAILABLE and supports the given role."""
    rec = evaluate_capability(name, evidence_max_age_seconds=evidence_max_age_seconds)
    if rec["status"] != AVAILABLE:
        return False
    return role in rec.get("roles", [])


def select_available(
    candidates: list[str],
    *,
    exclude: Optional[set[str]] = None,
    role: Optional[str] = None,
    evidence_max_age_seconds: int = DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
) -> str:
    """Select the first AVAILABLE candidate, respecting exclusion and role.

    Returns empty string if no candidate is available.
    """
    excluded = set(exclude or [])
    for name in candidates:
        if name in excluded:
            continue
        if role:
            if is_available_for_role(name, role, evidence_max_age_seconds=evidence_max_age_seconds):
                return name
        else:
            if is_available(name, evidence_max_age_seconds=evidence_max_age_seconds):
                return name
    return ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", action="store_true", help="Print capability matrix")
    parser.add_argument("--name", default="", help="Single tool to evaluate")
    args = parser.parse_args()

    if args.name:
        result = evaluate_capability(args.name)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.matrix:
        result = capability_matrix()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        result = evaluate_all_capabilities()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())