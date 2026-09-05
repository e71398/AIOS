#!/usr/bin/env python3
"""AIOS P8C-U Role-Level Route Coverage.

The P8C-U section 8 rule: a system is "operationally closed" only
when every critical role (planner / executor / reviewer) has at
least one valid route.  A route is valid iff:

1. the tool is enabled;
2. the tool is role-compatible (Registry ``has_role``);
3. the local runtime is alive (default: True when not reported);
4. the tool has at least one *verified* healthy model binding
   (binding not in cooldown AND resource not in cooldown AND
   binding is ``enabled``).

This module is a thin layer over
:class:`aios_tool_failover.ToolFailoverEngine`.  It computes the
route set for each role and assigns one of four role statuses:

* ``ROLE_AVAILABLE_PRIMARY``           — preferred route healthy
* ``ROLE_AVAILABLE_FALLBACK``          — only fallback routes
* ``ROLE_SINGLE_POINT_OF_FAILURE``     — only one valid route
* ``ROLE_UNAVAILABLE``                 — zero valid routes

The production core chain may only declare itself "operationally
closed" if ``planner_routes``, ``executor_routes`` and
``reviewer_routes`` are each non-empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from aios_tool_failover import (
    ALL_TOOL_STATUSES,
    TOOL_STATUS_AVAILABLE_PRIMARY,
    TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK,
    TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK,
    TOOL_STATUS_DISABLED,
    TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
    TOOL_STATUS_UNVERIFIED,
    ToolFailoverEngine,
    ToolStatusReport,
    get_default_tool_engine,
)


# ---------------------------------------------------------------------------
# Role taxonomy (mirrors what the orchestrator uses)
# ---------------------------------------------------------------------------

ROLE_PLANNER = "planner"
ROLE_EXECUTOR = "executor"
ROLE_REVIEWER = "reviewer"

ALL_ROLES: Tuple[str, ...] = (ROLE_PLANNER, ROLE_EXECUTOR, ROLE_REVIEWER)

# Role statuses
ROLE_STATUS_AVAILABLE_PRIMARY = "ROLE_AVAILABLE_PRIMARY"
ROLE_STATUS_AVAILABLE_FALLBACK = "ROLE_AVAILABLE_FALLBACK"
ROLE_STATUS_SINGLE_POINT_OF_FAILURE = "ROLE_SINGLE_POINT_OF_FAILURE"
ROLE_STATUS_UNAVAILABLE = "ROLE_UNAVAILABLE"

ALL_ROLE_STATUSES: Tuple[str, ...] = (
    ROLE_STATUS_AVAILABLE_PRIMARY,
    ROLE_STATUS_AVAILABLE_FALLBACK,
    ROLE_STATUS_SINGLE_POINT_OF_FAILURE,
    ROLE_STATUS_UNAVAILABLE,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RoleRoute:
    """One role × tool × effective binding entry.

    ``available`` is True iff the route is healthy today (binding
    not in cooldown AND resource not in cooldown AND binding
    enabled AND runtime alive).
    """

    role: str
    tool_id: str
    tool_status: str
    primary_binding: Optional[str] = None
    effective_binding: Optional[str] = None
    available: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RoleRouteReport:
    """Coverage report for one role."""

    role: str
    routes: Tuple[RoleRoute, ...] = ()
    available_routes: int = 0
    role_status: str = ROLE_STATUS_UNAVAILABLE
    primary_route: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["routes"] = [r.to_dict() for r in self.routes]
        return data


@dataclass
class RouteCoverageReport:
    """Coverage for every role."""

    planner_routes: RoleRouteReport = field(default_factory=lambda: RoleRouteReport(ROLE_PLANNER))
    executor_routes: RoleRouteReport = field(default_factory=lambda: RoleRouteReport(ROLE_EXECUTOR))
    reviewer_routes: RoleRouteReport = field(default_factory=lambda: RoleRouteReport(ROLE_REVIEWER))
    chain_operationally_closed: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "planner_routes": self.planner_routes.to_dict(),
            "executor_routes": self.executor_routes.to_dict(),
            "reviewer_routes": self.reviewer_routes.to_dict(),
            "chain_operationally_closed": self.chain_operationally_closed,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Coverage computation
# ---------------------------------------------------------------------------


class RoleRouteCalculator:
    """Compute role-level route coverage on demand.

    The calculator delegates to a :class:`ToolFailoverEngine` for
    binding / resource cooldown state.  Tests / canary may inject a
    custom engine to simulate "preferred executor unavailable" via
    ``set_tool_runtime_alive``.
    """

    def __init__(self, engine: Optional[ToolFailoverEngine] = None) -> None:
        self._engine = engine or get_default_tool_engine()

    @property
    def engine(self) -> ToolFailoverEngine:
        return self._engine

    def compute_role(self, role: str,
                     *,
                     preferred: Optional[str] = None) -> RoleRouteReport:
        registry = self._engine._registry  # type: ignore[attr-defined]
        manifests = [m for m in registry.list_enabled()
                     if m.has_role(role)]
        routes: List[RoleRoute] = []
        for manifest in manifests:
            status = self._engine.compute_tool_status(manifest.tool_id)
            available = status.status in (
                TOOL_STATUS_AVAILABLE_PRIMARY,
                TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK,
            )
            routes.append(
                RoleRoute(
                    role=role,
                    tool_id=manifest.tool_id,
                    tool_status=status.status,
                    primary_binding=status.primary_binding,
                    effective_binding=status.effective_binding,
                    available=available,
                    reason=status.reason,
                )
            )
        available_routes = sum(1 for r in routes if r.available)
        if available_routes == 0:
            role_status = ROLE_STATUS_UNAVAILABLE
            primary = None
        elif available_routes == 1:
            role_status = ROLE_STATUS_SINGLE_POINT_OF_FAILURE
            primary = next(r.tool_id for r in routes if r.available)
        else:
            # More than one available route.  ``AVAILABLE_PRIMARY``
            # if the preferred tool is in the available set,
            # otherwise ``AVAILABLE_FALLBACK``.
            avail_ids = {r.tool_id for r in routes if r.available}
            if preferred and preferred in avail_ids:
                role_status = ROLE_STATUS_AVAILABLE_PRIMARY
                primary = preferred
            else:
                role_status = ROLE_STATUS_AVAILABLE_FALLBACK
                primary = next(iter(avail_ids))
        return RoleRouteReport(
            role=role,
            routes=tuple(routes),
            available_routes=available_routes,
            role_status=role_status,
            primary_route=primary,
        )

    def compute_all(self) -> RouteCoverageReport:
        planner = self.compute_role(ROLE_PLANNER)
        executor = self.compute_role(ROLE_EXECUTOR)
        reviewer = self.compute_role(ROLE_REVIEWER)
        closed = (planner.available_routes > 0
                  and executor.available_routes > 0
                  and reviewer.available_routes > 0)
        if closed:
            reason = "all three roles have at least one available route"
        else:
            missing = [r.role for r in (planner, executor, reviewer)
                       if r.available_routes == 0]
            reason = (f"unavailable roles: {','.join(missing) or '(none)'} "
                      "; chain not operationally closed")
        return RouteCoverageReport(
            planner_routes=planner,
            executor_routes=executor,
            reviewer_routes=reviewer,
            chain_operationally_closed=closed,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_SINGLETON: Optional[RoleRouteCalculator] = None


def get_default_role_calculator() -> RoleRouteCalculator:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = RoleRouteCalculator()
    return _SINGLETON


def set_default_role_calculator(calc: Optional[RoleRouteCalculator]) -> None:
    global _SINGLETON
    _SINGLETON = calc


def reset_default_role_calculator() -> RoleRouteCalculator:
    global _SINGLETON
    _SINGLETON = RoleRouteCalculator()
    return _SINGLETON


__all__ = [
    "ROLE_PLANNER", "ROLE_EXECUTOR", "ROLE_REVIEWER", "ALL_ROLES",
    "ROLE_STATUS_AVAILABLE_PRIMARY", "ROLE_STATUS_AVAILABLE_FALLBACK",
    "ROLE_STATUS_SINGLE_POINT_OF_FAILURE", "ROLE_STATUS_UNAVAILABLE",
    "ALL_ROLE_STATUSES",
    "RoleRoute", "RoleRouteReport", "RouteCoverageReport",
    "RoleRouteCalculator",
    "get_default_role_calculator",
    "set_default_role_calculator",
    "reset_default_role_calculator",
]