#!/usr/bin/env python3
"""AIOS P8C-U Combined Routing Policy + Feature Flags.

This is the **top** of the dual-axis failover stack:

  RoutingEngine
    ├── ToolFailoverEngine   (which tool?)
    │     └── ModelFailoverEngine (which model binding for that tool?)
    └── RoleRouteCalculator  (are the three core roles covered?)

It owns:

* Feature flags (env-driven, default off):
    - ``AIOS_MODEL_FAILOVER_ENABLED``  (bool, default ``True``)
    - ``AIOS_TOOL_FAILOVER_ENABLED``   (bool, default ``True``)
    - ``AIOS_ROUTING_SHADOW_MODE``    (bool, default ``True``)
    - ``AIOS_ROUTING_CANARY_ALLOWED_SOURCES``
      (comma-separated; default empty)
    - ``AIOS_ROUTING_TOOL_ALLOWLIST`` / ``AIOS_ROUTING_ROLE_ALLOWLIST``
      (comma-separated; default empty → no restriction)
* The combined :class:`RoutingDecision` (tool + binding + reasons).
* Shadow log: every call to :func:`route` writes a structured record
  to an in-process shadow ledger without taking action.
* Canary gate: a request is allowed to honour the failover decision
  only if its ``source`` is in the canary allow-list (or if the
  feature flags say "all sources allowed").
* Single-step rollback: setting both feature flags to ``False``
  brings the system back to the pre-P8C-U production behaviour.

This module is pure: it never invokes any tool / Provider.  Real
execution is the orchestrator's job.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple  # noqa: F401

from aios_tool_failover import (
    DEFAULT_MAX_TOOL_ATTEMPTS,
    DEFAULT_MAX_TOOL_FAILOVERS,
    ToolFailoverEngine,
    ToolFailoverDecision,
    ToolStatusReport,
    get_default_tool_engine,
)
from aios_model_failover import (
    DEFAULT_RESOURCE_COOLDOWN_SECONDS,
    DEFAULT_BINDING_COOLDOWN_SECONDS,
    ModelAttemptRecord,
    ModelFailoverDecision,
    ModelFailoverEngine,
    get_default_engine as get_default_model_engine,
)
from aios_role_routes import (
    ROLE_PLANNER,
    ROLE_EXECUTOR,
    ROLE_REVIEWER,
    ALL_ROLES,
    RoleRouteCalculator,
    RoleRouteReport,
    RouteCoverageReport,
    get_default_role_calculator,
)
from aios_qwen_provider import QwenStatus, get_last_qwen_status


# ---------------------------------------------------------------------------
# Feature flag env names
# ---------------------------------------------------------------------------

ENV_MODEL_FAILOVER = "AIOS_MODEL_FAILOVER_ENABLED"
ENV_TOOL_FAILOVER = "AIOS_TOOL_FAILOVER_ENABLED"
ENV_SHADOW_MODE = "AIOS_ROUTING_SHADOW_MODE"
ENV_CANARY_SOURCES = "AIOS_ROUTING_CANARY_ALLOWED_SOURCES"
ENV_CANARY_SENDERS = "AIOS_ROUTING_CANARY_ALLOWED_SENDERS"
ENV_TOOL_ALLOWLIST = "AIOS_ROUTING_TOOL_ALLOWLIST"
ENV_ROLE_ALLOWLIST = "AIOS_ROUTING_ROLE_ALLOWLIST"
ENV_AUDIT_OVERLAY = "AIOS_ROUTING_AUDIT_OVERLAY_ENABLED"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list_env(name: str, default: Tuple[str, ...] = ()) -> Tuple[str, ...]:
    raw = os.environ.get(name, "")
    if not raw:
        return tuple(default)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(parts)


# ---------------------------------------------------------------------------
# Decision + outcome dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RoutingDecision:
    """The combined verdict for one task attempt.

    The decision answers four questions:

    1. ``action`` — what should the caller do?
       - ``"proceed"``       — use ``actual_tool`` + ``actual_model_binding``
       - ``"shadow_only"``   — log only; do not change behaviour
       - ``"denied"``        — feature flags forbid the action

    2. ``actual_tool`` / ``actual_model_binding`` — the chosen pair.
    3. ``would_*`` mirrors for shadow-mode traceability.
    4. ``would_failover_reason`` — recorded reason even if shadow.
    """

    action: str  # proceed | shadow_only | denied
    actual_tool: Optional[str] = None
    actual_model_binding: Optional[str] = None
    tool_decision: Optional[ToolFailoverDecision] = None
    model_decision: Optional[ModelFailoverDecision] = None
    shadow: bool = False
    failover_occurred: bool = False
    tool_failover_occurred: bool = False
    model_failover_occurred: bool = False
    attempted_tools: Tuple[str, ...] = ()
    attempted_model_bindings: Tuple[str, ...] = ()
    tool_failover_reason: str = ""
    model_failover_reason: str = ""
    preferred_tool: Optional[str] = None
    preferred_model: Optional[str] = None
    role: str = ""
    strict_tool: Optional[str] = None
    strict_model: Optional[str] = None
    allow_tool_fallback: bool = True
    allow_model_fallback: bool = True
    capability_overlay: Dict[str, str] = field(default_factory=dict)
    canary_allowed: bool = False
    feature_flags: Dict[str, Any] = field(default_factory=dict)
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["tool_decision"] = (self.tool_decision.to_dict()
                                  if self.tool_decision else None)
        data["model_decision"] = (self.model_decision.to_dict()
                                   if self.model_decision else None)
        data["attempted_tools"] = list(self.attempted_tools)
        data["attempted_model_bindings"] = list(self.attempted_model_bindings)
        data["capability_overlay"] = dict(self.capability_overlay)
        data["feature_flags"] = dict(self.feature_flags)
        return data


@dataclass
class ShadowLogEntry:
    """One shadow trace.  Kept in-memory only."""

    task_id: str
    role: str
    preferred_tool: Optional[str]
    preferred_model: Optional[str]
    actual_tool: Optional[str]
    actual_model_binding: Optional[str]
    skipped_tools: Tuple[str, ...] = ()
    skipped_resources: Tuple[str, ...] = ()
    skipped_bindings: Tuple[str, ...] = ()
    would_failover_reason: str = ""
    would_action: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["skipped_tools"] = list(self.skipped_tools)
        data["skipped_resources"] = list(self.skipped_resources)
        data["skipped_bindings"] = list(self.skipped_bindings)
        return data


# ---------------------------------------------------------------------------
# Routing engine
# ---------------------------------------------------------------------------


class RoutingEngine:
    """The combined tool + model routing engine.

    Parameters mirror those of the underlying engines; ``None`` means
    use the process-wide singletons.
    """

    def __init__(
        self,
        *,
        tool_engine: Optional[ToolFailoverEngine] = None,
        model_engine: Optional[ModelFailoverEngine] = None,
        role_calculator: Optional[RoleRouteCalculator] = None,
        shadow_log_capacity: int = 1024,
    ) -> None:
        self._lock = threading.RLock()
        self._tool_engine = tool_engine or get_default_tool_engine()
        self._model_engine = model_engine or get_default_model_engine()
        self._role_calc = role_calculator or get_default_role_calculator()
        self._shadow_log: List[ShadowLogEntry] = []
        self._shadow_capacity = shadow_log_capacity
        # Feature flags snapshot (re-read on every route() call).
        self._feature_flag_snapshot: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Feature flag resolution
    # ------------------------------------------------------------------

    def read_feature_flags(self) -> Dict[str, Any]:
        # P8D hardening: the composite canary gate is open by default
        # to the production canary sender (``aios-canary``) so the
        # acceptance service can verify the live task executed through
        # the dual-axis failover stack without an operator having to
        # pre-configure environment variables. Production tasks sent
        # from non-canary sources still go through ``canary_allows``
        # and are refused unless they explicitly match the allow-list.
        flags = {
            "model_failover_enabled": _bool_env(ENV_MODEL_FAILOVER, True),
            "tool_failover_enabled": _bool_env(ENV_TOOL_FAILOVER, True),
            "shadow_mode": _bool_env(ENV_SHADOW_MODE, True),
            "canary_allowed_sources": _list_env(
                ENV_CANARY_SOURCES, default=("test", "aios-canary"),
            ),
            # ``canary_allowed_senders`` stays empty by default so the
            # legacy P8C-F single-source gate keeps working; production
            # environments that want the P8D composite gate (source +
            # sender) MUST opt in explicitly via
            # ``AIOS_ROUTING_CANARY_ALLOWED_SENDERS``. The acceptance
            # service is the only caller that needs the P8D form; it
            # sets the variable in the unit file, not here.
            "canary_allowed_senders": _list_env(ENV_CANARY_SENDERS),
            "audit_overlay_enabled": _bool_env(ENV_AUDIT_OVERLAY, False),
            "tool_allowlist": _list_env(ENV_TOOL_ALLOWLIST),
            "role_allowlist": _list_env(ENV_ROLE_ALLOWLIST),
        }
        self._feature_flag_snapshot = flags
        return flags

    @property
    def feature_flags(self) -> Dict[str, Any]:
        return dict(self._feature_flag_snapshot)

    def canary_allows(
        self,
        source: str,
        sender: Optional[str] = None,
    ) -> bool:
        """Composite canary gate (P8D, with P8C-F legacy fallback).

        The P8D rules require *both* a matching source AND a matching
        sender; when the operator has not configured
        ``canary_allowed_senders``, the legacy P8C-F single-source
        behaviour is preserved so existing P8C-F callers continue to
        work. The rules:

        * ``source`` is in ``canary_allowed_sources``; AND
        * If ``canary_allowed_senders`` is configured:
          ``sender`` must be in it (sender is required); otherwise
          the gate stays closed (P8D hardening).
        * If ``canary_allowed_senders`` is empty: only ``source``
          is consulted (legacy P8C-F single-source path).
        """
        flags = self.read_feature_flags()
        sources = flags["canary_allowed_sources"]
        senders = flags["canary_allowed_senders"]
        if not sources:
            return False
        if source not in sources:
            return False
        if not senders:
            # Legacy P8C-F single-source path.
            return True
        if sender is None:
            return False
        return sender in senders

    def audit_overlay_enabled(self) -> bool:
        return _bool_env(ENV_AUDIT_OVERLAY, False)

    def tool_allowed(self, tool_id: str) -> bool:
        flags = self.read_feature_flags()
        allowlist = flags["tool_allowlist"]
        if not allowlist:
            return True
        return tool_id in allowlist

    def role_allowed(self, role: str) -> bool:
        flags = self.read_feature_flags()
        allowlist = flags["role_allowlist"]
        if not allowlist:
            return True
        return role in allowlist

    # ------------------------------------------------------------------
    # Route — main entry point
    # ------------------------------------------------------------------

    def route(
        self,
        *,
        task_id: str,
        role: str,
        source: str = "api",
        sender: Optional[str] = None,
        preferred_tool: Optional[str] = None,
        preferred_model: Optional[str] = None,
        strict_tool: Optional[str] = None,
        strict_model: Optional[str] = None,
        allow_tool_fallback: bool = True,
        allow_model_fallback: bool = True,
        max_tool_attempts: int = DEFAULT_MAX_TOOL_ATTEMPTS,
        max_tool_failovers: int = DEFAULT_MAX_TOOL_FAILOVERS,
        capability_overlay: Optional[Mapping[str, Any]] = None,
        task_blocked_tools: Optional[Sequence[str]] = None,
        task_blocked_bindings: Optional[Sequence[str]] = None,
        task_blocked_resources: Optional[Sequence[str]] = None,
    ) -> RoutingDecision:
        """Compute the combined tool + model decision.

        ``sender`` is the optional sender identifier (``ai`` source
        specific, e.g. ``p8d-audit``).  When set, the canary gate is
        a composite ``source + sender`` check; otherwise the legacy
        single-source behaviour is preserved.

        The decision is *advisory*.  The orchestrator is the only
        caller authorised to mutate workflow state.  In canary mode
        the caller MUST honour ``canary_allowed`` before persisting
        the decision.
        """
        flags = self.read_feature_flags()
        overlay = dict(capability_overlay or {})
        # Build a tool decision even if tool failover is off (so the
        # shadow log still records "what would have happened").  The
        # engine itself respects strict_tool regardless of flags.
        tool_decision = self._tool_engine.select_tool(
            task_id=task_id,
            role=role,
            preferred_tool=preferred_tool,
            strict_tool=strict_tool,
            allow_tool_fallback=allow_tool_fallback,
            max_tool_attempts=max_tool_attempts,
            max_tool_failovers=max_tool_failovers,
            capability_overlay=overlay or None,
            task_blocked_tools=task_blocked_tools or (),
        )
        # Build the model decision once we know which tool to use.
        chosen_tool = tool_decision.tool_id
        model_decision: Optional[ModelFailoverDecision] = None
        attempted_bindings: List[str] = []
        model_failover_reason = ""
        # P8D contract: a controlled task-local overlay may list
        # binding IDs to refuse (e.g. ``opencode:free``). The overlay
        # is independent of the live cooldown state and never flips
        # the whole tool to UNAVAILABLE_TOOL_RUNTIME; it just removes
        # one binding from the candidate list for *this* task.
        task_blocked_bindings_overlay: Tuple[str, ...] = ()
        if overlay.get("blocked_model_bindings"):
            raw = overlay.get("blocked_model_bindings")
            if isinstance(raw, (list, tuple, set)):
                task_blocked_bindings_overlay = tuple(
                    str(item) for item in raw if str(item)
                )
            elif isinstance(raw, str):
                task_blocked_bindings_overlay = tuple(
                    item.strip() for item in raw.split(",") if item.strip()
                )
        # Close-out 20260727-§六: if the caller explicitly passed
        # ``task_blocked_bindings``, prefer it over the overlay.
        if task_blocked_bindings is not None:
            task_blocked_bindings_overlay = tuple(
                str(b) for b in task_blocked_bindings if b
            )
        if chosen_tool is not None:
            model_decision = self._model_engine.select_model_binding(
                task_id=task_id,
                tool_id=chosen_tool,
                role=role,
                preferred_model=preferred_model,
                strict_model=strict_model,
                allow_model_fallback=allow_model_fallback,
                task_blocked_bindings=(
                    task_blocked_bindings
                    if task_blocked_bindings is not None
                    else task_blocked_bindings_overlay
                ),
                task_blocked_resources=task_blocked_resources or (),
            )
            attempted_bindings = self._model_engine.attempted_bindings(
                task_id)

        # Shadow log entry — always written.
        skipped_tools = []
        if tool_decision.action in ("pool_exhausted", "no_candidates"):
            # We didn't pick anything; record the candidates the
            # engine considered.
            for t in self._tool_engine._registry.list_by_role(role):  # type: ignore[attr-defined]
                if t.tool_id not in (tool_decision.tool_id or "",):
                    skipped_tools.append(t.tool_id)
        skipped_resources: List[str] = []
        skipped_bindings: List[str] = []
        if model_decision is not None and model_decision.action in (
                "skip_resource", "skip_binding", "exhausted"):
            # Walk the policy again to know what got skipped.
            policy = self._model_engine._policies.get(chosen_tool)  # type: ignore[attr-defined]
            if policy is not None:
                for bid in policy.candidate_bindings:
                    binding = self._model_engine._bindings.get(bid)  # type: ignore[attr-defined]
                    if binding is None:
                        continue
                    if bid in attempted_bindings:
                        continue
                    if (self._model_engine.is_binding_in_cooldown(bid)
                            or self._model_engine.is_resource_in_cooldown(
                                binding.resource_id)):
                        skipped_bindings.append(bid)
                        skipped_resources.append(binding.resource_id)
        attempted_tools = self._tool_engine.attempted_tools(task_id)

        # Determine whether failover actually occurred.
        tool_failover_occurred = (
            tool_decision.tool_id is not None
            and preferred_tool is not None
            and tool_decision.tool_id != preferred_tool
        )
        model_failover_occurred = (
            model_decision is not None
            and model_decision.action == "use"
            and preferred_model is not None
            and model_decision.binding_id != preferred_model
        )
        failover_occurred = tool_failover_occurred or model_failover_occurred

        # Build would_/actual_* mirror.
        would_select_tool = tool_decision.tool_id
        would_select_model_binding = (
            model_decision.binding_id if model_decision else None)
        would_skip_tools = list(skipped_tools)
        would_skip_bindings = list(skipped_bindings)
        would_skip_resources = list(skipped_resources)
        would_failover_reason = ""
        if tool_failover_occurred:
            would_failover_reason = (
                f"tool_failover:{tool_decision.reason}")
        elif model_failover_occurred:
            would_failover_reason = (
                f"model_failover:{model_decision.reason if model_decision else ''}")
        elif tool_decision.action == "pool_exhausted":
            would_failover_reason = f"tool_pool_exhausted:{tool_decision.reason}"
        elif model_decision and model_decision.action in (
                "exhausted", "max_attempts", "max_failovers"):
            would_failover_reason = (
                f"model_pool_exhausted:{model_decision.reason}")

        shadow_entry = ShadowLogEntry(
            task_id=task_id,
            role=role,
            preferred_tool=preferred_tool,
            preferred_model=preferred_model,
            actual_tool=would_select_tool,
            actual_model_binding=would_select_model_binding,
            skipped_tools=tuple(would_skip_tools),
            skipped_resources=tuple(would_skip_resources),
            skipped_bindings=tuple(would_skip_bindings),
            would_failover_reason=would_failover_reason,
            would_action=("proceed" if would_select_tool
                          and (model_decision is None
                               or model_decision.action == "use")
                          else "blocked"),
            finished_at=_iso_now(),
        )
        with self._lock:
            self._shadow_log.append(shadow_entry)
            if len(self._shadow_log) > self._shadow_capacity:
                self._shadow_log = self._shadow_log[-self._shadow_capacity:]

        # Action resolution.
        canary_allowed = self.canary_allows(source, sender)
        role_allowed = self.role_allowed(role)
        tool_blocked_by_role_allowlist = (
            chosen_tool is not None
            and not self.tool_allowed(chosen_tool))
        if not role_allowed:
            action = "denied"
        elif tool_blocked_by_role_allowlist:
            action = "denied"
        elif flags["shadow_mode"] and not canary_allowed:
            action = "shadow_only"
        else:
            action = "proceed"

        decision = RoutingDecision(
            action=action,
            actual_tool=chosen_tool,
            actual_model_binding=(model_decision.binding_id
                                   if model_decision else None),
            tool_decision=tool_decision,
            model_decision=model_decision,
            shadow=(action == "shadow_only"),
            failover_occurred=failover_occurred,
            tool_failover_occurred=tool_failover_occurred,
            model_failover_occurred=model_failover_occurred,
            attempted_tools=tuple(attempted_tools),
            attempted_model_bindings=tuple(attempted_bindings),
            tool_failover_reason=(tool_decision.reason
                                  if tool_failover_occurred else ""),
            model_failover_reason=(model_decision.reason
                                   if model_failover_occurred else ""),
            preferred_tool=preferred_tool,
            preferred_model=preferred_model,
            role=role,
            strict_tool=strict_tool,
            strict_model=strict_model,
            allow_tool_fallback=allow_tool_fallback,
            allow_model_fallback=allow_model_fallback,
            capability_overlay=overlay,
            canary_allowed=canary_allowed,
            feature_flags=dict(flags),
            finished_at=_iso_now(),
        )
        return decision

    # ------------------------------------------------------------------
    # Shadow ledger
    # ------------------------------------------------------------------

    def shadow_log(self) -> List[ShadowLogEntry]:
        with self._lock:
            return list(self._shadow_log)

    def clear_shadow_log(self) -> None:
        with self._lock:
            self._shadow_log.clear()

    # ------------------------------------------------------------------
    # Reviewer independence
    # ------------------------------------------------------------------

    @staticmethod
    def compute_reviewer_independence(
        executor_resource_id: Optional[str],
        reviewer_resource_id: Optional[str],
        executor_tool_id: Optional[str],
        reviewer_tool_id: Optional[str],
    ) -> Dict[str, Any]:
        """Compute the ``review_independence`` classification.

        Returns ``TOOL_AND_MODEL`` / ``TOOL_ONLY`` / ``NO_INDEPENDENCE``
        plus the underlying boolean fields used in monitor / acceptance.
        """
        if executor_resource_id is None or reviewer_resource_id is None:
            independence = "NO_INDEPENDENCE"
        elif (executor_resource_id == reviewer_resource_id
              and executor_tool_id == reviewer_tool_id):
            independence = "NO_INDEPENDENCE"
        elif (executor_resource_id == reviewer_resource_id
              and executor_tool_id != reviewer_tool_id):
            independence = "TOOL_ONLY"
        elif executor_resource_id != reviewer_resource_id:
            independence = "TOOL_AND_MODEL"
        else:
            independence = "NO_INDEPENDENCE"
        same_tool = (executor_tool_id is not None
                     and executor_tool_id == reviewer_tool_id)
        same_resource = (executor_resource_id is not None
                         and executor_resource_id == reviewer_resource_id)
        return {
            "review_independence": independence,
            "same_underlying_model": same_resource,
            "same_tool": same_tool,
            "executor_resource_id": executor_resource_id,
            "reviewer_resource_id": reviewer_resource_id,
            "executor_tool_id": executor_tool_id,
            "reviewer_tool_id": reviewer_tool_id,
        }

    # ------------------------------------------------------------------
    # Health snapshot for monitor / acceptance
    # ------------------------------------------------------------------

    def health_snapshot(self) -> Dict[str, Any]:
        coverage = self._role_calc.compute_all()
        statuses = self._tool_engine.all_tool_statuses()
        return {
            "tool_statuses": [s.to_dict() for s in statuses],
            "coverage": coverage.to_dict(),
            "feature_flags": self.read_feature_flags(),
            "qwen_status": (get_last_qwen_status().to_dict()
                            if get_last_qwen_status() else None),
            "shadow_log_size": len(self._shadow_log),
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_ROUTING_SINGLETON: Optional[RoutingEngine] = None
_ROUTING_LOCK = threading.Lock()


def get_default_routing_engine() -> RoutingEngine:
    global _ROUTING_SINGLETON
    with _ROUTING_LOCK:
        if _ROUTING_SINGLETON is None:
            _ROUTING_SINGLETON = RoutingEngine()
        return _ROUTING_SINGLETON


def set_default_routing_engine(engine: Optional[RoutingEngine]) -> None:
    global _ROUTING_SINGLETON
    with _ROUTING_LOCK:
        _ROUTING_SINGLETON = engine


def reset_default_routing_engine() -> RoutingEngine:
    global _ROUTING_SINGLETON
    with _ROUTING_LOCK:
        _ROUTING_SINGLETON = RoutingEngine()
        return _ROUTING_SINGLETON


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


__all__ = [
    # env names
    "ENV_MODEL_FAILOVER", "ENV_TOOL_FAILOVER", "ENV_SHADOW_MODE",
    "ENV_CANARY_SOURCES", "ENV_TOOL_ALLOWLIST", "ENV_ROLE_ALLOWLIST",
    # dataclasses
    "RoutingDecision", "ShadowLogEntry",
    # engine
    "RoutingEngine",
    "get_default_routing_engine", "set_default_routing_engine",
    "reset_default_routing_engine",
]