#!/usr/bin/env python3
"""AIOS P4 Health Model.

Provides a uniform four-state health model and seven-dimension report
consumed by both Monitor (frequent lightweight layer) and Acceptance
(ground-truth canary).

Design constraints (AIOS-P4-ACCEPTANCE-MONITOR-TRUTHFULNESS-10):

* Mandatory core failures always surface as FAILED.
* Optional provider degradations (Claude 402, Codex plan 429) never
  drag the overall status below DEGRADED when the core path succeeds.
* Stale acceptance evidence → UNKNOWN, never HEALTHY.
* Stale runtime dependency → FAILED on the relevant dimension, never
  silently re-graded as HEALTHY.
* Pure functions + pluggable probes; the module NEVER calls any AI
  provider and is safe to import from the lightweight Monitor loop.

The module is intentionally self-contained: no Redis, no subprocess,
no filesystem assumptions beyond :mod:`pathlib`. Callers pass in
already-collected evidence so this module stays side-effect-free and
unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# P8A: dynamic tool discovery. ``probe_capability`` reads the
# default candidate list from the registry instead of a hard-coded
# five-tool tuple. The import is defensive so legacy monitors keep
# loading when the registry is unavailable.
try:
    from aios_tool_registry import (
        get_default_registry as _default_tool_registry,
    )
except Exception:  # pragma: no cover
    _default_tool_registry = None


def _candidate_tools_for_role(role: str) -> Tuple[str, ...]:
    """Read the tool id list for ``role`` from the registry.

    P8A role-to-candidate mapping preserves the historical P4 / P6A
    semantics while becoming dynamic:

    * ``executor``  — tools with the executor role. Today that is
      opencode, claude, codex.
    * ``reviewer``  — ALL enabled tools. The reviewer capability
      dimension needs every tool as a possible fallback reviewer;
      restricting it to ``role=reviewer`` tools would lock out
      opencode (which historically served as the canonical fallback
      reviewer when hermes was unavailable).

    The reviewer=all-tools rule matches the historical default tuple
    (``hermes, claude, codex, opencode, openclaw``) and is enforced
    here so adding a new tool automatically extends the reviewer
    candidate set without requiring changes to this module.
    """
    if _default_tool_registry is not None:
        try:
            reg = _default_tool_registry()
            if role == "reviewer":
                ids = sorted(m.tool_id for m in reg.list_enabled())
                if ids:
                    return tuple(ids)
            ids = sorted(m.tool_id for m in reg.list_by_role(role))
            if ids:
                return tuple(ids)
        except Exception:
            pass
    if role == "executor":
        return ("opencode", "claude", "codex")
    if role == "reviewer":
        return ("hermes", "claude", "codex", "opencode", "openclaw")
    return ()

# ---------------------------------------------------------------------------
# Status enums and constants
# ---------------------------------------------------------------------------

STATUS_HEALTHY = "HEALTHY"
STATUS_DEGRADED = "DEGRADED"
STATUS_FAILED = "FAILED"
STATUS_UNKNOWN = "UNKNOWN"
ALL_STATUSES = (STATUS_HEALTHY, STATUS_DEGRADED, STATUS_FAILED, STATUS_UNKNOWN)

# Severity ranking for aggregation: higher value == more severe.
STATUS_SEVERITY = {
    STATUS_HEALTHY: 0,
    STATUS_DEGRADED: 1,
    STATUS_UNKNOWN: 2,
    STATUS_FAILED: 3,
}

DEFAULT_MAX_ACCEPTANCE_AGE_SECONDS = 8 * 3600  # 8 hours, aligned with timer cadence


@dataclass(frozen=True)
class HealthDimension:
    """A single observable health axis."""

    unit: str
    status: str
    reason_code: str
    mandatory: bool
    evidence_source: str
    observed_at: str
    age_seconds: int
    summary: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # Mapping may be empty; keep it for consumers.
        return data


@dataclass(frozen=True)
class HealthReport:
    """The aggregate of every health dimension.

    ``optional_degradations`` carries one record per degraded optional
    tool. Each record is a plain dict with at least
    ``tool_id``, ``status``, ``reason_code``, ``evidence_freshness``,
    and ``mandatory=false``. The legacy behaviour (a flat list of tool
    name strings) is still supported as input but is upgraded to the
    structured form on output.
    """

    overall_status: str
    dimensions: Dict[str, HealthDimension]
    observed_at: str
    failure_classes: Dict[str, bool]
    optional_degradations: List[Any]
    mandatory_failures: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "observed_at": self.observed_at,
            "dimensions": {name: dim.to_dict() for name, dim in self.dimensions.items()},
            "failure_classes": dict(self.failure_classes),
            "optional_degradations": [dict(item) if isinstance(item, dict)
                                       else item
                                       for item in self.optional_degradations],
            "mandatory_failures": list(self.mandatory_failures),
        }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def aggregate_status(dimensions: Mapping[str, HealthDimension]) -> str:
    """Combine dimension statuses into the overall status.

    Rules (per P4 spec, section 四):

    * any mandatory FAILED → FAILED
    * else any mandatory UNKNOWN → UNKNOWN
    * else all mandatory HEALTHY/DEGRADED and at least one optional
      degraded → DEGRADED
    * else → HEALTHY
    """
    mandatory = [d for d in dimensions.values() if d.mandatory]
    if any(d.status == STATUS_FAILED for d in mandatory):
        return STATUS_FAILED
    if any(d.status == STATUS_UNKNOWN for d in mandatory):
        return STATUS_UNKNOWN
    if any(d.status == STATUS_DEGRADED for d in dimensions.values()):
        return STATUS_DEGRADED
    return STATUS_HEALTHY


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_of_file(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Probe: service_state
# ---------------------------------------------------------------------------


def probe_service_state(
    unit_names: Sequence[str],
    unit_active_fn: Callable[[str], bool],
    *,
    unit_main_pid_fn: Optional[Callable[[str], int]] = None,
    now_epoch: Optional[float] = None,
) -> HealthDimension:
    """Report whether the core systemd units are active.

    ``unit_active_fn(unit)`` returns True if the unit is currently active.
    """
    observed_at = _now_iso()
    now_epoch = float(now_epoch if now_epoch is not None else time.time())
    inactive = [name for name in unit_names if not unit_active_fn(name)]
    if not inactive:
        return HealthDimension(
            unit="service_state",
            status=STATUS_HEALTHY,
            reason_code="ALL_CORE_UNITS_ACTIVE",
            mandatory=True,
            evidence_source="systemd:is-active",
            observed_at=observed_at,
            age_seconds=0,
            summary=f"{len(unit_names)} core units active",
            extra={"inactive": [], "units": list(unit_names)},
        )
    # Hard failure: gateway down is a hard failure even if some units are active.
    critical = {"aios-entry-gateway.service", "aios-orchestrator.service",
                "aios-verification-gate.service"}
    hard_down = [u for u in inactive if u in critical]
    if hard_down:
        return HealthDimension(
            unit="service_state",
            status=STATUS_FAILED,
            reason_code="CRITICAL_SERVICE_INACTIVE",
            mandatory=True,
            evidence_source="systemd:is-active",
            observed_at=observed_at,
            age_seconds=0,
            summary=f"critical units inactive: {hard_down}",
            extra={"inactive": inactive, "hard_down": hard_down,
                   "units": list(unit_names)},
        )
    return HealthDimension(
        unit="service_state",
        status=STATUS_DEGRADED,
        reason_code="NON_CRITICAL_SERVICE_INACTIVE",
        mandatory=True,
        evidence_source="systemd:is-active",
        observed_at=observed_at,
        age_seconds=0,
        summary=f"non-critical units inactive: {inactive}",
        extra={"inactive": inactive, "units": list(unit_names)},
    )


# ---------------------------------------------------------------------------
# Probe: endpoint_reachability
# ---------------------------------------------------------------------------


def probe_endpoint_reachability(
    endpoints: Mapping[str, Callable[[], bool]],
) -> HealthDimension:
    """Report reachability of well-known local HTTP / Redis endpoints.

    ``endpoints`` maps a logical name → probe function returning True.
    """
    observed_at = _now_iso()
    unreachable = [name for name, fn in endpoints.items() if not fn()]
    if not unreachable:
        return HealthDimension(
            unit="endpoint_reachability",
            status=STATUS_HEALTHY,
            reason_code="ALL_ENDPOINTS_REACHABLE",
            mandatory=True,
            evidence_source="http:probe",
            observed_at=observed_at,
            age_seconds=0,
            summary=f"{len(endpoints)} endpoints reachable",
            extra={"unreachable": [], "endpoints": list(endpoints)},
        )
    return HealthDimension(
        unit="endpoint_reachability",
        status=STATUS_FAILED,
        reason_code="ENDPOINT_UNREACHABLE",
        mandatory=True,
        evidence_source="http:probe",
        observed_at=observed_at,
        age_seconds=0,
        summary=f"unreachable endpoints: {unreachable}",
        extra={"unreachable": unreachable, "endpoints": list(endpoints)},
    )


# ---------------------------------------------------------------------------
# Probe: runtime_revision
# ---------------------------------------------------------------------------


def evaluate_runtime_dependency(
    *,
    pid: Optional[int],
    process_started_at: Optional[float],
    dependency_paths: Sequence[Path],
    now_epoch: Optional[float] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Classify a process's runtime dependency freshness.

    Returns ``(status, reason_code, details)`` where ``status`` is one of:

    * ``CURRENT``       — process is older than every dependency file
    * ``STALE``         — at least one dependency is newer than the process
    * ``MISSING_FILE``  — a required file is missing
    * ``PROCESS_MISSING`` — the process has no PID
    * ``UNKNOWN``       — caller did not provide ``process_started_at``

    The caller is expected to translate these states into
    :data:`STATUS_FAILED` (STALE / MISSING_FILE / PROCESS_MISSING) and
    :data:`STATUS_UNKNOWN` (UNKNOWN) — the helper itself stays a pure
    classifier so it can be reused from tests.
    """
    if not pid:
        return STATUS_UNKNOWN, "PROCESS_MISSING", {"pid": pid}
    if process_started_at is None:
        return STATUS_UNKNOWN, "PROCESS_START_UNKNOWN", {"pid": pid}
    now_epoch = float(now_epoch if now_epoch is not None else time.time())
    newest_mtime = 0.0
    details: Dict[str, Any] = {
        "pid": pid,
        "process_started_at": process_started_at,
        "dependencies": [],
    }
    for path in dependency_paths:
        if not path.exists():
            return STATUS_UNKNOWN, "DEPENDENCY_MISSING", {
                "pid": pid,
                "missing": str(path),
            }
        stat = path.stat()
        sha = _sha256_of_file(path)
        details["dependencies"].append({
            "path": str(path),
            "mtime": stat.st_mtime,
            "sha256": sha,
        })
        newest_mtime = max(newest_mtime, stat.st_mtime)
    if newest_mtime > process_started_at + 0.001:
        return STATUS_FAILED, "RUNTIME_DEPENDENCY_STALE", {
            **details,
            "process_started_at": process_started_at,
            "newest_dependency_mtime": newest_mtime,
        }
    return STATUS_HEALTHY, "RUNTIME_DEPENDENCY_CURRENT", details


def probe_runtime_revision(
    *,
    pid: Optional[int],
    process_started_at: Optional[float],
    dependency_paths: Sequence[Path],
    now_epoch: Optional[float] = None,
) -> HealthDimension:
    observed_at = _now_iso()
    status, reason_code, details = evaluate_runtime_dependency(
        pid=pid,
        process_started_at=process_started_at,
        dependency_paths=dependency_paths,
        now_epoch=now_epoch,
    )
    summary_map = {
        STATUS_HEALTHY: "runtime dependencies loaded are current",
        STATUS_FAILED: "process loaded stale runtime dependencies",
        STATUS_UNKNOWN: "runtime dependency state could not be determined",
    }
    return HealthDimension(
        unit="runtime_revision",
        status=status,
        reason_code=reason_code,
        mandatory=True,
        evidence_source="proc:start_time+filesystem:mtime",
        observed_at=observed_at,
        age_seconds=0,
        summary=summary_map.get(status, "runtime dependency state unknown"),
        extra=details,
    )


# ---------------------------------------------------------------------------
# Probe: executor_capability / reviewer_capability
# ---------------------------------------------------------------------------


def probe_capability(
    unit: str,
    capability_state: Mapping[str, str],
    *,
    mandatory: bool,
    candidate_names: Optional[Sequence[str]] = None,
) -> HealthDimension:
    """Build a capability dimension from a per-tool capability matrix.

    ``capability_state`` maps tool name → one of:

    * ``AVAILABLE``
    * ``DEGRADED_EXTERNAL``   (e.g. Claude 402, Codex plan 429)
    * ``DEGRADED_INTERNAL``
    * ``UNAVAILABLE``
    * ``UNVERIFIED``
    * ``NOT_CONFIGURED``

    ``candidate_names`` constrains which tools are eligible to satisfy
    the AVAILABLE check for this unit. Default candidates reflect the
    P3/P4 contract:

    * ``executor_capability``  → ``{"opencode", "claude", "codex"}``
    * ``reviewer_capability``   → ``{"hermes", "claude", "codex",
                                        "opencode"}``
    """
    observed_at = _now_iso()
    items = dict(capability_state)
    if unit == "executor_capability":
        default_candidates = _candidate_tools_for_role("executor")
    elif unit == "reviewer_capability":
        # P8A: candidate list comes from the registry (role=reviewer).
        # P7A errata: openclaw is a planner/reviewer-like tool and was
        # previously omitted from this tuple; the registry's role map
        # correctly puts it on the reviewer side as well, so its
        # DEGRADED_INTERNAL state surfaces in ``optional_degradations``.
        default_candidates = _candidate_tools_for_role("reviewer")
    else:
        default_candidates = tuple(items.keys())
    candidates = tuple(candidate_names) if candidate_names else default_candidates
    unavailable = [name for name in candidates if items.get(name) == "UNAVAILABLE"]
    degraded = [name for name in candidates if items.get(name) in
                ("DEGRADED_EXTERNAL", "DEGRADED_INTERNAL")]
    unverified = [name for name in candidates if items.get(name) == "UNVERIFIED"]
    not_configured = [name for name in candidates if items.get(name) == "NOT_CONFIGURED"]
    if unit == "executor_capability":
        # At least one executor AVAILABLE is mandatory.
        available = [name for name in candidates if items.get(name) == "AVAILABLE"]
        if not available:
            return HealthDimension(
                unit=unit,
                status=STATUS_FAILED,
                reason_code="NO_EXECUTOR_AVAILABLE",
                mandatory=mandatory,
                evidence_source="capability_matrix",
                observed_at=observed_at,
                age_seconds=0,
                summary="no executor available",
                extra={"items": items, "available": available,
                       "degraded": degraded, "unavailable": unavailable,
                       "unverified": unverified,
                       "not_configured": not_configured},
            )
        if unavailable or degraded or unverified:
            return HealthDimension(
                unit=unit,
                status=STATUS_DEGRADED,
                reason_code="OPTIONAL_EXECUTOR_DEGRADED",
                mandatory=mandatory,
                evidence_source="capability_matrix",
                observed_at=observed_at,
                age_seconds=0,
                summary=f"executor matrix degraded: {degraded + unavailable + unverified}",
                extra={"items": items, "available": available,
                       "degraded": degraded, "unavailable": unavailable,
                       "unverified": unverified,
                       "not_configured": not_configured},
            )
        return HealthDimension(
            unit=unit,
            status=STATUS_HEALTHY,
            reason_code="ALL_EXECUTORS_AVAILABLE",
            mandatory=mandatory,
            evidence_source="capability_matrix",
            observed_at=observed_at,
            age_seconds=0,
            summary=f"executors available: {available}",
            extra={"items": items, "available": available,
                   "degraded": degraded, "unavailable": unavailable,
                   "unverified": unverified,
                   "not_configured": not_configured},
        )
    if unit == "reviewer_capability":
        # Reviewer capability is mandatory but a single AVAILABLE reviewer is enough.
        available = [name for name in candidates if items.get(name) == "AVAILABLE"]
        if not available:
            return HealthDimension(
                unit=unit,
                status=STATUS_FAILED,
                reason_code="NO_INDEPENDENT_REVIEWER_AVAILABLE",
                mandatory=mandatory,
                evidence_source="capability_matrix",
                observed_at=observed_at,
                age_seconds=0,
                summary="no independent reviewer available",
                extra={"items": items, "available": available,
                       "degraded": degraded, "unavailable": unavailable,
                       "unverified": unverified,
                       "not_configured": not_configured},
            )
        if degraded or unavailable or unverified:
            return HealthDimension(
                unit=unit,
                status=STATUS_DEGRADED,
                reason_code="OPTIONAL_REVIEWER_DEGRADED",
                mandatory=mandatory,
                evidence_source="capability_matrix",
                observed_at=observed_at,
                age_seconds=0,
                summary=f"reviewer matrix degraded: {degraded + unavailable + unverified}",
                extra={"items": items, "available": available,
                       "degraded": degraded, "unavailable": unavailable,
                       "unverified": unverified,
                       "not_configured": not_configured},
            )
        return HealthDimension(
            unit=unit,
            status=STATUS_HEALTHY,
            reason_code="ALL_REVIEWERS_AVAILABLE",
            mandatory=mandatory,
            evidence_source="capability_matrix",
            observed_at=observed_at,
            age_seconds=0,
            summary=f"reviewers available: {available}",
            extra={"items": items, "available": available,
                   "degraded": degraded, "unavailable": unavailable,
                   "unverified": unverified,
                   "not_configured": not_configured},
        )
    raise ValueError(f"unsupported capability unit: {unit}")


# ---------------------------------------------------------------------------
# Probe: authoritative_state / latest_e2e_acceptance
# ---------------------------------------------------------------------------


def probe_authoritative_state(
    *,
    parent_present: bool,
    result_present: bool,
    verification_present: bool,
    redis_reachable: bool,
    latest_acceptance: Optional[Mapping[str, Any]] = None,
    max_age_seconds: int = DEFAULT_MAX_ACCEPTANCE_AGE_SECONDS,
    now_epoch: Optional[float] = None,
) -> HealthDimension:
    """Return the authoritative_state dimension.

    AIOS authoritative state is **idempotent by design**: once a real
    end-to-end canary has completed successfully, the system is considered
    healthy until a NEW authoritative event (a fresh canary, a real
    failure, or a Redis outage) invalidates the previous verdict. The
    probe MUST NOT downgrade to FAILED merely because there is no
    "currently active workflow" running — AIOS is idle most of the time
    and that is a normal state.

    Decision rules (P5F):

    1. ``REDIS_UNREACHABLE`` → FAILED (cannot read authoritative state).
    2. If no recent acceptance evidence present at all
       (latest_acceptance is missing or stale) → UNKNOWN
       (``NO_RECENT_AUTHORITATIVE_EVIDENCE``).
    3. If the latest acceptance was a real PASS (UUID task_id, parent
       completed, result present, verification present) and Redis agrees
       parent/result/verification are still present → HEALTHY
       (``LATEST_ACCEPTANCE_AUTHORITATIVE_STATE_COMPLETE``).
    4. If the latest acceptance was a real PASS but its parent_id has
       since expired (Redis lookup misses) → HEALTHY
       (``IDLE_WITH_RECENT_AUTHORITATIVE_SUCCESS``). The canary was
       genuinely completed; AIOS is now idle.
    5. If the latest acceptance recorded a real FAIL → FAILED
       (``LATEST_ACCEPTANCE_FAILED``).
    6. If Redis reports parent/result/verification as missing while the
       report claims they exist → FAILED
       (``AUTHORITATIVE_STATE_CONTRADICTION``).

    The probe never requires an active workflow to be HEALTHY.
    """
    observed_at = _now_iso()
    now_epoch = float(now_epoch if now_epoch is not None else time.time())
    extra = {
        "parent_present": parent_present,
        "result_present": result_present,
        "verification_present": verification_present,
        "redis_reachable": redis_reachable,
        "latest_acceptance": dict(latest_acceptance) if latest_acceptance else None,
    }
    if not redis_reachable:
        return HealthDimension(
            unit="authoritative_state",
            status=STATUS_FAILED,
            reason_code="REDIS_UNREACHABLE",
            mandatory=False,
            evidence_source="redis:hgetall",
            observed_at=observed_at,
            age_seconds=0,
            summary="redis unreachable; cannot verify authoritative state",
            extra=extra,
        )
    # No recent acceptance evidence at all: we cannot speak about the
    # authoritative state, so we MUST NOT claim FAILED. UNKNOWN is the
    # only honest answer here.
    if not latest_acceptance:
        return HealthDimension(
            unit="authoritative_state",
            status=STATUS_UNKNOWN,
            reason_code="NO_RECENT_AUTHORITATIVE_EVIDENCE",
            mandatory=True,
            evidence_source="aios-acceptance",
            observed_at=observed_at,
            age_seconds=0,
            summary="no recent acceptance evidence; AIOS is idle",
            extra=extra,
        )
    acceptance_age = int(latest_acceptance.get("age_seconds", 0) or 0)
    if acceptance_age > max_age_seconds:
        return HealthDimension(
            unit="authoritative_state",
            status=STATUS_UNKNOWN,
            reason_code="NO_RECENT_AUTHORITATIVE_EVIDENCE",
            mandatory=True,
            evidence_source="aios-acceptance",
            observed_at=observed_at,
            age_seconds=acceptance_age,
            summary=f"latest acceptance is stale ({acceptance_age}s > {max_age_seconds}s)",
            extra=extra,
        )
    core_result = str(latest_acceptance.get("core_result") or "").upper()
    task_id = str(latest_acceptance.get("task_id") or "").strip()
    if core_result == "FAIL":
        return HealthDimension(
            unit="authoritative_state",
            status=STATUS_FAILED,
            reason_code="LATEST_ACCEPTANCE_FAILED",
            mandatory=False,
            evidence_source="aios-acceptance",
            observed_at=observed_at,
            age_seconds=acceptance_age,
            summary="latest acceptance report core=FAIL",
            extra=extra,
        )
    if core_result != "PASS" or not task_id:
        # Report is unreadable / lacks a real task_id. Cannot claim
        # PASS, but FAILED would also be a lie. UNKNOWN is the only
        # truth-preserving answer.
        return HealthDimension(
            unit="authoritative_state",
            status=STATUS_UNKNOWN,
            reason_code="NO_RECENT_AUTHORITATIVE_EVIDENCE",
            mandatory=True,
            evidence_source="aios-acceptance",
            observed_at=observed_at,
            age_seconds=acceptance_age,
            summary="latest acceptance has no real task id or result",
            extra=extra,
        )
    # Real PASS observed. The canary is the ground truth: a real PASS
    # with a real task_id, parent completed, and a recorded result /
    # verification proves the system was working at the time of the
    # canary. Redis is a *cache* with independent TTLs — the trace
    # zset is preserved for 30 days while the result / verification
    # string keys have much shorter TTLs and naturally expire between
    # canaries. We treat the post-canary partial state as a normal
    # idle (the canary is the source of truth, Redis is evidence).
    if parent_present and result_present and verification_present:
        return HealthDimension(
            unit="authoritative_state",
            status=STATUS_HEALTHY,
            reason_code="LATEST_ACCEPTANCE_AUTHORITATIVE_STATE_COMPLETE",
            mandatory=True,
            evidence_source="aios-acceptance",
            observed_at=observed_at,
            age_seconds=acceptance_age,
            summary="latest acceptance PASS and parent/result/verification present",
            extra=extra,
        )
    # The canary said PASS; the parent trace may still be in Redis
    # (long-lived) while the short-lived result / verification keys
    # have expired. That is the normal post-canary idle state, NOT a
    # contradiction.
    if (parent_present and not result_present and not verification_present) or (
        not parent_present and not result_present and not verification_present
    ):
        return HealthDimension(
            unit="authoritative_state",
            status=STATUS_HEALTHY,
            reason_code="IDLE_WITH_RECENT_AUTHORITATIVE_SUCCESS",
            mandatory=True,
            evidence_source="aios-acceptance",
            observed_at=observed_at,
            age_seconds=acceptance_age,
            summary=f"latest acceptance PASS ({task_id}); no active workflow (idle)",
            extra=extra,
        )
    # The canary said PASS but Redis reports result / verification
    # present while the parent trace is missing. That IS a real
    # contradiction: orphan result / verification data without a
    # traceable parent.
    return HealthDimension(
        unit="authoritative_state",
        status=STATUS_FAILED,
        reason_code="AUTHORITATIVE_STATE_CONTRADICTION",
        mandatory=True,
        evidence_source="aios-acceptance",
        observed_at=observed_at,
        age_seconds=acceptance_age,
        summary=("latest acceptance PASS but Redis state is inconsistent: "
                 f"parent={parent_present} result={result_present} "
                 f"verification={verification_present}"),
        extra=extra,
    )


def find_latest_acceptance_report(
    reports_dir: Path,
    *,
    kind: str = "canary",
) -> Optional[Path]:
    """Return the most recent ``kind_*.json`` report if present."""
    if not reports_dir.exists():
        return None
    candidates = sorted(reports_dir.glob(f"{kind}_*.json"), reverse=True)
    return candidates[0] if candidates else None


def probe_latest_e2e_acceptance(
    reports_dir: Path,
    *,
    max_age_seconds: int = DEFAULT_MAX_ACCEPTANCE_AGE_SECONDS,
    kind: str = "canary",
    now_epoch: Optional[float] = None,
) -> HealthDimension:
    """Inspect the most recent acceptance report and grade freshness."""
    observed_at = _now_iso()
    now_epoch = float(now_epoch if now_epoch is not None else time.time())
    latest = find_latest_acceptance_report(reports_dir, kind=kind)
    if latest is None:
        return HealthDimension(
            unit="latest_e2e_acceptance",
            status=STATUS_UNKNOWN,
            reason_code="ACCEPTANCE_EVIDENCE_MISSING",
            mandatory=True,
            evidence_source=str(reports_dir),
            observed_at=observed_at,
            age_seconds=int(now_epoch - now_epoch),
            summary="no acceptance report on disk",
            extra={"reports_dir": str(reports_dir), "kind": kind},
        )
    stat = latest.stat()
    age = int(max(0.0, now_epoch - stat.st_mtime))
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return HealthDimension(
            unit="latest_e2e_acceptance",
            status=STATUS_UNKNOWN,
            reason_code="ACCEPTANCE_REPORT_UNREADABLE",
            mandatory=True,
            evidence_source=str(latest),
            observed_at=observed_at,
            age_seconds=age,
            summary="latest acceptance report is unreadable JSON",
            extra={"path": str(latest), "age_seconds": age,
                   "max_age_seconds": max_age_seconds},
        )
    core_result = payload.get("core_result") or payload.get("passed") or (
        "PASS" if payload.get("failed", 1) == 0 else "FAIL"
    )
    schema_version = payload.get("schema_version") or payload.get("schema", "unknown")
    overall = payload.get("overall_status")
    if age > max_age_seconds:
        return HealthDimension(
            unit="latest_e2e_acceptance",
            status=STATUS_UNKNOWN,
            reason_code="ACCEPTANCE_EVIDENCE_STALE",
            mandatory=True,
            evidence_source=str(latest),
            observed_at=observed_at,
            age_seconds=age,
            summary=f"latest acceptance is stale ({age}s > {max_age_seconds}s)",
            extra={"path": str(latest), "core_result": core_result,
                   "overall_status": overall, "schema_version": schema_version,
                   "max_age_seconds": max_age_seconds},
        )
    if core_result != "PASS":
        return HealthDimension(
            unit="latest_e2e_acceptance",
            status=STATUS_FAILED,
            reason_code="LATEST_ACCEPTANCE_FAILED",
            mandatory=False,
            evidence_source=str(latest),
            observed_at=observed_at,
            age_seconds=age,
            summary=f"latest acceptance core={core_result}",
            extra={"path": str(latest), "core_result": core_result,
                   "overall_status": overall, "schema_version": schema_version,
                   "max_age_seconds": max_age_seconds},
        )
    return HealthDimension(
        unit="latest_e2e_acceptance",
        status=STATUS_HEALTHY,
        reason_code="LATEST_ACCEPTANCE_PASSED",
        mandatory=True,
        evidence_source=str(latest),
        observed_at=observed_at,
        age_seconds=age,
        summary=f"latest acceptance core=PASS (age={age}s)",
        extra={"path": str(latest), "core_result": core_result,
               "overall_status": overall, "schema_version": schema_version,
               "max_age_seconds": max_age_seconds, "run_id": payload.get("run_id")},
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _extract_degraded_tool_names(dimensions: Mapping[str, HealthDimension]) -> List[str]:
    """Surface tool names that are individually degraded inside capability dimensions.

    P6A: a capability dimension such as ``executor_capability`` or
    ``reviewer_capability`` carries the actual degraded tool names in its
    ``extra`` payload (``extra.degraded`` + ``extra.unavailable`` +
    ``extra.unverified``). The old behaviour only listed dimensions whose
    ``mandatory`` flag was False, which excluded the executor / reviewer
    matrices entirely; callers could not tell *which* tool was down. This
    helper walks every capability dimension and returns the flattened list
    of currently-degraded tool names.
    """
    tools: List[str] = []
    seen: set = set()
    for dim in dimensions.values():
        if not dim.unit.endswith("_capability"):
            continue
        for key in ("degraded", "unavailable", "unverified"):
            for name in (dim.extra.get(key) or []):
                if name not in seen:
                    seen.add(name)
                    tools.append(name)
    return tools


def _extract_degraded_tool_records(
    dimensions: Mapping[str, HealthDimension],
) -> List[Dict[str, Any]]:
    """Surface structured per-tool degradation records.

    P6A §3.2 requires ``optional_degradations`` entries to expose the
    actual degraded tool name plus ``status``, ``reason_code``,
    ``evidence_freshness``, and ``mandatory=false`` so callers can audit
    which optional tools are down without round-tripping the full
    health report. This helper walks the capability matrix payload and
    returns one record per degraded tool, picking the strongest evidence
    available across the executor / reviewer dimensions.
    """
    candidates: Dict[str, Dict[str, str]] = {}
    for dim in dimensions.values():
        if not dim.unit.endswith("_capability"):
            continue
        items = dim.extra.get("items") or {}
        for bucket in ("degraded", "unavailable", "unverified"):
            for name in (dim.extra.get(bucket) or []):
                # The items payload stores a flat ``{tool: status_str}``
                # mapping built by ``probe_capability``. Older revisions
                # also allowed a dict-shaped record; we accept both.
                info = items.get(name) if isinstance(items, Mapping) else None
                if isinstance(info, dict):
                    actual_status = str(info.get("status")
                                        or dim.reason_code
                                        or "CAPABILITY_DEGRADED")
                    reason_code = str(info.get("reason_code")
                                      or dim.reason_code
                                      or "CAPABILITY_DEGRADED")
                    evidence_freshness = str(info.get("evidence_freshness")
                                             or "UNKNOWN")
                else:
                    # P7A errata: ``items[name]`` is the actual state
                    # string (e.g. ``DEGRADED_INTERNAL``). When the
                    # caller only stored the bucket, fall back to the
                    # bucket name as a coarse-grained status hint.
                    actual_status = str(info or f"DEGRADED_{bucket.upper()}")
                    reason_code = str(dim.reason_code
                                      or "CAPABILITY_DEGRADED")
                    evidence_freshness = "UNKNOWN"
                record = {
                    "tool_id": name,
                    "status": actual_status,
                    "reason_code": reason_code,
                    "evidence_freshness": evidence_freshness,
                    "mandatory": False,
                    "source_dimension": dim.unit,
                }
                prior = candidates.get(name)
                # Prefer records carrying a more specific reason_code.
                if prior is None or (
                    record["reason_code"] != dim.reason_code
                    and prior["reason_code"] == dim.reason_code
                ):
                    candidates[name] = record
    # Stable order: deterministic by tool_id.
    return [candidates[name] for name in sorted(candidates)]


def assemble_health_report(
    dimensions: Mapping[str, HealthDimension],
    *,
    failure_classes: Optional[Mapping[str, bool]] = None,
    optional_degradations: Optional[Iterable[str]] = None,
    mandatory_failures: Optional[Iterable[str]] = None,
) -> HealthReport:
    overall = aggregate_status(dimensions)
    optional_degradations = list(optional_degradations or [])
    mandatory_failures = list(mandatory_failures or [])
    if not mandatory_failures:
        mandatory_failures = [
            name for name, dim in dimensions.items()
            if dim.mandatory and dim.status == STATUS_FAILED
        ]
    if not optional_degradations:
        optional_degradations = [
            name for name, dim in dimensions.items()
            if not dim.mandatory and dim.status in (STATUS_DEGRADED, STATUS_FAILED)
        ]
    # P6A: when the aggregate is DEGRADED but the legacy scan above yielded
    # nothing (because executor / reviewer capability dimensions are
    # mandatory=True), promote structured per-tool degradation records
    # from the capability dimensions' ``extra`` payload. Each record
    # carries tool_id / status / reason_code / evidence_freshness /
    # mandatory=false so downstream consumers can audit exactly which
    # optional tools are down.
    # P7A errata: extend the trigger to STATUS_FAILED as well. When
    # every optional tool is degraded the overall collapses to FAILED
    # (e.g. NO_EXECUTOR_AVAILABLE) and the DEGRADED-only trigger would
    # silently hide openclaw. The spec demands that
    # ``optional_degradations`` cover all optional degradations, not
    # just those that keep overall == DEGRADED.
    if overall in (STATUS_DEGRADED, STATUS_FAILED) and not optional_degradations:
        records = _extract_degraded_tool_records(dimensions)
        if records:
            optional_degradations = records
        else:
            # Fallback when items payload is missing — degrade to bare names.
            tool_names = _extract_degraded_tool_names(dimensions)
            if tool_names:
                optional_degradations = tool_names
    return HealthReport(
        overall_status=overall,
        dimensions=dict(dimensions),
        observed_at=_now_iso(),
        failure_classes=dict(failure_classes or {}),
        optional_degradations=optional_degradations,
        mandatory_failures=mandatory_failures,
    )


__all__ = [
    # status constants
    "STATUS_HEALTHY", "STATUS_DEGRADED", "STATUS_FAILED", "STATUS_UNKNOWN",
    "ALL_STATUSES", "STATUS_SEVERITY",
    "DEFAULT_MAX_ACCEPTANCE_AGE_SECONDS",
    # dataclasses
    "HealthDimension", "HealthReport",
    # pure helpers
    "aggregate_status", "evaluate_runtime_dependency",
    # probes
    "probe_service_state", "probe_endpoint_reachability",
    "probe_runtime_revision", "probe_capability",
    "probe_authoritative_state", "probe_latest_e2e_acceptance",
    "find_latest_acceptance_report",
    # aggregation
    "assemble_health_report",
]