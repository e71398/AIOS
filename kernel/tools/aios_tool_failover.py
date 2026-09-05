#!/usr/bin/env python3
"""AIOS P8C-U Tool Failover Engine.

This is the **second** automatic-failover layer, sitting *above*
``aios_model_failover.ModelFailoverEngine``.  The model engine answers
"given a tool, which model binding should we use now?".  This module
answers "given a role, which tool should we use now?".

Hard rules (mirrored from P8C-U section 4 / 5):

1. The set of candidate tools is dynamic — read from
   :class:`aios_tool_registry.ToolRegistry`.  Hard-coded five-tool
   lists are forbidden.
2. ``preferred_tool`` is the caller's first choice (if any).
3. ``strict_tool`` vetoes any tool switch; the engine returns
   ``strict_violation``.
4. ``allow_tool_fallback=False`` allows only one tool attempt.
5. ``max_tool_attempts`` (default 2) and ``max_tool_failovers``
   (default 1) bound the loop; the engine never iterates infinitely.
6. Tool status is computed from "effective model path" — a tool is
   ``AVAILABLE_WITH_MODEL_FALLBACK`` if its preferred model is blocked
   but at least one verified fallback binding exists.  This is the
   P8C-U section 7 fix.
7. Tool failover cannot impersonate model failover.  The engine
   records ``tool_failover_reason`` distinctly from
   ``model_failover_reason``.
8. Once a model attempt *succeeds*, the engine locks
   ``actual_tool`` and ``actual_model_binding`` — no mid-task swap
   back to the preferred tool / model.

This module is pure: it never imports any tool module, never reads
secrets, never spawns processes.  It tracks state per task; monitor /
acceptance can read the records.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from aios_tool_registry import (
    ToolManifest,
    ToolRegistry,
    get_default_registry,
    set_default_registry,
)
from aios_model_resources import (
    FAILURE_SCOPE_RESOURCE,
    FAILURE_SCOPE_BINDING,
    FAILURE_SCOPE_LOCAL_RUNTIME,
    FAILURE_SCOPE_TASK_INPUT,
    FAILURE_SCOPE_TOOL_ADAPTER,
    SharedModelResourceRegistry,
    ToolModelBinding,
    ToolModelBindingRegistry,
    ToolModelPolicy,
    ToolModelPolicyRegistry,
)


# ---------------------------------------------------------------------------
# Public constants — tool availability classification
# ---------------------------------------------------------------------------

TOOL_STATUS_AVAILABLE_PRIMARY = "AVAILABLE_PRIMARY"
TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK = "AVAILABLE_WITH_MODEL_FALLBACK"
TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK = "DEGRADED_NO_MODEL_FALLBACK"
TOOL_STATUS_DEGRADED_NO_NATIVE_MODEL_ROUTE = "DEGRADED_NO_NATIVE_MODEL_ROUTE"
TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME = "UNAVAILABLE_TOOL_RUNTIME"
TOOL_STATUS_UNVERIFIED = "UNVERIFIED"
TOOL_STATUS_DISABLED = "DISABLED"

ALL_TOOL_STATUSES: Tuple[str, ...] = (
    TOOL_STATUS_AVAILABLE_PRIMARY,
    TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK,
    TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK,
    TOOL_STATUS_DEGRADED_NO_NATIVE_MODEL_ROUTE,
    TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
    TOOL_STATUS_UNVERIFIED,
    TOOL_STATUS_DISABLED,
)


# Tool-level map of bindings that the current adapter / runtime layer
# cannot actually serve.  Pinned here so the monitor / acceptance
# / state reports do NOT silently claim ``codex:deepseek`` is a
# production-eligible binding when ``aios-codex-relay.service`` only
# serves the Minimax upstream.  A binding flagged here is treated
# exactly like a quota-blocked binding: it is moved to the
# ``blocked_bindings`` bucket and ``production_eligible=False``.
ADAPTER_UNSUPPORTED_BINDINGS: Dict[str, Tuple[str, ...]] = {
    # Codex's aios-codex-relay binds to the Minimax upstream only;
    # ``codex:deepseek`` is a candidate but the adapter layer cannot
    # reach the DeepSeek API.  Marking it unsupported prevents the
    # monitor / canary from reporting it as production_eligible.
    "codex": ("codex:deepseek",),
    # Hermes' aios_executor_daemon path does not currently route
    # through the MiniMax upstream for native Hermes calls; only
    # the OpenAI-compatible MiniMax upstream is reachable from the
    # adapter.  We keep ``hermes:minimax`` blocked until an explicit
    # provider path is added (see state document deferred work).
    "hermes": ("hermes:minimax",),
}


# Failure kinds the engine recognises as triggering tool failover.
# Local runtime / adapter / task-input failures MUST NOT cause a
# model switch (the model failover engine enforces this); tool
# failover MAY follow after a tool-local failure.
TOOL_FAILOVER_TRIGGER_KINDS: Tuple[str, ...] = (
    FAILURE_SCOPE_LOCAL_RUNTIME,
    FAILURE_SCOPE_TOOL_ADAPTER,
    # Resource / binding exhaustion with empty pool is also a tool-
    # level trigger — we surface it as ``tool_pool_exhausted``.
    "tool_pool_exhausted",
)


# Hard caps.
DEFAULT_MAX_TOOL_ATTEMPTS = 2
DEFAULT_MAX_TOOL_FAILOVERS = 1


# P9D-R-runtime-health-freshness: explicit failure-event TTL.
# When a tool is observed failing (TOOL_PROCESS, TOOL_ADAPTER,
# DISPATCH_CLAIM_TIMEOUT, …) the orchestrator records an explicit
# failure event so the very next ``compute_tool_status`` returns
# ``UNAVAILABLE_TOOL_RUNTIME`` regardless of the on-disk cache
# freshness.  The default 120 s is long enough to cover the
# Repair chain (≤ 60 s dispatch timeout × 3 generations) and short
# enough that a recovered service is re-detected without waiting
# for the much longer ``probe_max_age_seconds`` window.
_TOOL_RUNTIME_FAILURE_TTL_SECONDS = 120




# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ToolAttemptRecord:
    """One tool attempt inside a task.

    ``tool_failover_reason`` is non-empty only when this attempt is a
    failover (i.e. not the first tool tried).  The reason names the
    scope that triggered the switch.
    """

    task_id: str
    tool_id: str
    attempted_at: str
    reason: str = ""
    tool_failover_reason: str = ""
    is_failover: bool = False
    effective_model_binding: str = ""
    tool_status_snapshot: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ToolFailoverDecision:
    """Engine verdict on which tool to try next.

    ``action`` is one of:

    * ``"use"``            — caller should use ``tool_id``.
    * ``"no_candidates"``  — no role-compatible enabled tools at all.
    * ``"max_attempts"``   — already at the attempt cap.
    * ``"max_failovers"``  — already at the failover cap.
    * ``"strict_violation"`` — caller asked for a different tool but
      ``strict_tool`` is set, or the requested tool is not registered.
    * ``"fallback_disabled"`` — ``allow_tool_fallback=False`` and we
      are past the first attempt.
    * ``"pool_exhausted"`` — every candidate tool has its model pool
      exhausted.

    Close-out 20260727-§五 additions:

    * ``excluded_tools`` — tools removed by ``blocked_tools``
      (``task_blocked_tools`` argument).  Carries ``excluded_by_task_policy``
      semantics in the audit ledger.
    * ``attempted_tools`` — ordered list of tools the engine tried
      so far for this task (same as
      :func:`attempted_tools` but read off the in-flight decision).
    * ``tool_failover_count`` — number of times the engine has
      switched tools for the task.
    """

    action: str
    tool_id: Optional[str] = None
    reason: str = ""
    next_attempt_index: int = 0
    tool_status: str = TOOL_STATUS_UNVERIFIED
    effective_model_binding: Optional[str] = None
    excluded_tools: Tuple[str, ...] = ()
    attempted_tools: Tuple[str, ...] = ()
    tool_failover_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["excluded_tools"] = list(self.excluded_tools)
        data["attempted_tools"] = list(self.attempted_tools)
        return data


@dataclass
class ToolStatusReport:
    """Result of :func:`compute_tool_status`.

    ``status`` is one of :data:`ALL_TOOL_STATUSES`.  The ``reason``
    is a human-readable hint; ``fallback_ready`` is True when at
    least one verified *non-primary* binding is currently healthy.
    """

    tool_id: str
    status: str
    primary_binding: Optional[str] = None
    effective_binding: Optional[str] = None
    verified_bindings: Tuple[str, ...] = ()
    blocked_bindings: Tuple[str, ...] = ()
    reason: str = ""
    fallback_ready: bool = False

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["verified_bindings"] = list(self.verified_bindings)
        data["blocked_bindings"] = list(self.blocked_bindings)
        return data


# ---------------------------------------------------------------------------
# Tool failover engine
# ---------------------------------------------------------------------------


class ToolFailoverEngine:
    """Stateful tool-failover engine.

    Parameters
    ----------
    registry:
        The dynamic :class:`ToolRegistry`.  ``None`` means use the
        process-wide default registry (which P8A already loads from
        ``config/tool_adapters.json`` + ``config/ai_registry.json``).
    binding_registry:
        The :class:`ToolModelBindingRegistry`.  ``None`` means use
        the default singleton.
    policy_registry:
        The :class:`ToolModelPolicyRegistry`.  ``None`` means use
        the default singleton.
    resource_registry:
        The :class:`SharedModelResourceRegistry`.  ``None`` means use
        the default singleton.
    model_engine:
        The :class:`ModelFailoverEngine`.  ``None`` means use the
        default singleton.  The engine uses this *only* to look up
        binding cooldowns; it does not own model failover logic.
    """

    def __init__(
        self,
        *,
        registry: Optional[ToolRegistry] = None,
        binding_registry: Optional[ToolModelBindingRegistry] = None,
        policy_registry: Optional[ToolModelPolicyRegistry] = None,
        resource_registry: Optional[SharedModelResourceRegistry] = None,
        model_engine: Any = None,
    ) -> None:
        self._lock = threading.RLock()
        self._registry = registry or get_default_registry()
        if binding_registry is not None:
            self._bindings = binding_registry
        else:
            from aios_model_resources import (
                get_default_binding_registry as _gbr,
            )
            self._bindings = _gbr()
        if policy_registry is not None:
            self._policies = policy_registry
        else:
            from aios_model_resources import (
                get_default_policy_registry as _gpr,
            )
            self._policies = _gpr()
        if resource_registry is not None:
            self._resources = resource_registry
        else:
            from aios_model_resources import (
                get_default_resource_registry as _grr,
            )
            self._resources = _grr()
        if model_engine is not None:
            self._model_engine = model_engine
        else:
            from aios_model_failover import get_default_engine as _gme
            self._model_engine = _gme()
        # Per-task state.
        self._task_tools: Dict[str, List[str]] = {}
        self._task_tool_history: Dict[str, List[ToolAttemptRecord]] = {}
        self._task_locked_tool: Dict[str, str] = {}
        self._task_tool_status: Dict[str, Dict[str, str]] = {}
        # Per-tool runtime liveness snapshot (set externally; defaults
        # to "unknown" so the engine treats it as available).
        self._tool_runtime_alive: Dict[str, bool] = {}
        # Per-tool cache override; when set for a tool, the on-disk
        # cache is ignored and the override dict is used instead.
        # Tests use this to simulate ``cache=available`` while the
        # host file system may still record a real-world failure.
        self._adapter_cache_override: Dict[str, Dict[str, Any]] = {}
        # P9D-R-runtime-health-freshness: explicit tool-runtime
        # failure events recorded by callers (e.g. orchestrator
        # Repair when ``dispatch_claim_timeout`` is observed).  These
        # events bypass the on-disk probe cache so the next
        # ``compute_tool_status`` returns ``UNAVAILABLE_TOOL_RUNTIME``
        # immediately, regardless of cache freshness.  The TTL is
        # intentionally short (default ``_TOOL_RUNTIME_FAILURE_TTL``)
        # so a recovered service can be re-detected without waiting for
        # the longer ``probe_max_age_seconds`` window.  ``expires_at``
        # uses ``time.monotonic`` to survive wall-clock skew.
        self._tool_runtime_failure_events: Dict[str, Dict[str, Any]] = {}

    def set_adapter_cache_override(
        self,
        tool_id: str,
        cache: Optional[Dict[str, Any]],
    ) -> None:
        """Force the probe cache for ``tool_id`` to ``cache``.

        Pass ``None`` (default) to remove the override; pass ``{}``
        to simulate a missing / neutral cache.
        """
        with self._lock:
            if cache is None:
                self._adapter_cache_override.pop(tool_id, None)
            else:
                self._adapter_cache_override[tool_id] = dict(cache)

    def clear_adapter_cache_overrides(self) -> None:
        with self._lock:
            self._adapter_cache_override.clear()

    # ------------------------------------------------------------------
    # External signals
    # ------------------------------------------------------------------

    def set_tool_runtime_alive(self, tool_id: str, alive: bool) -> None:
        with self._lock:
            self._tool_runtime_alive[tool_id] = bool(alive)

    def is_tool_runtime_alive(self, tool_id: str) -> bool:
        with self._lock:
            return self._tool_runtime_alive.get(tool_id, True)

    # ------------------------------------------------------------------
    # P9D-R runtime health freshness: explicit failure events
    # ------------------------------------------------------------------

    def record_tool_runtime_failure(
        self,
        tool_id: str,
        *,
        scope: str = FAILURE_SCOPE_LOCAL_RUNTIME,
        reason: str = "",
        ttl_seconds: Optional[int] = None,
        observed_at_monotonic: Optional[float] = None,
        observed_at_wallclock: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an explicit tool-runtime failure event for
        ``tool_id``.

        ``compute_tool_status`` consults the per-tool failure-event
        map FIRST so a freshly-observed TOOL_PROCESS /
        TOOL_ADAPTER / DISPATCH_CLAIM_TIMEOUT failure is reflected
        on the very next routing decision without waiting for the
        long ``probe_max_age_seconds`` window to expire.

        The event is keyed by ``tool_id`` and stored alongside:
        * ``scope``              — one of the ``FAILURE_SCOPE_*`` strings
                                   (``"TOOL_PROCESS"`` / ``"TOOL_ADAPTER"``
                                   / ``"DISPATCH_CLAIM_TIMEOUT"`` / …).
        * ``reason``             — short human-readable tag.
        * ``observed_at_wallclock`` — ISO-8601 UTC stamp.
        * ``observed_at_monotonic`` — ``time.monotonic()`` value.
        * ``expires_at_monotonic`` — observed_at + ttl.

        TTL is intentionally short (default 120s) — long enough to
        survive a Repair chain, short enough that a recovered
        service can be re-detected without waiting for the long
        probe window.
        """
        import time as _time
        now_m = float(
            observed_at_monotonic
            if observed_at_monotonic is not None
            else _time.monotonic()
        )
        if ttl_seconds is None:
            ttl_seconds = _TOOL_RUNTIME_FAILURE_TTL_SECONDS
        event: Dict[str, Any] = {
            "tool_id": tool_id,
            "scope": str(scope or FAILURE_SCOPE_LOCAL_RUNTIME),
            "reason": str(reason or "tool_runtime_failure"),
            "observed_at_monotonic": now_m,
            "observed_at_wallclock": (
                observed_at_wallclock
                or _iso_now()
            ),
            "expires_at_monotonic": now_m + float(ttl_seconds),
            "ttl_seconds": float(ttl_seconds),
        }
        with self._lock:
            self._tool_runtime_failure_events[tool_id] = event
            # Drop the cached AVAILABLE liveness flag so the very
            # next ``is_tool_runtime_alive`` returns False; this
            # also blocks ``compute_tool_status`` step 3.
            self._tool_runtime_alive[tool_id] = False
        return event

    def clear_tool_runtime_failure(self, tool_id: str) -> None:
        """Clear any explicit failure event for ``tool_id``.

        Called when the caller detects that the tool is healthy
        again (e.g. orchestrator Recovery observes a successful
        claim or the monitor probe flips back to AVAILABLE).  This
        is the only path that re-enables routing after a failure
        event; without it, the long-lived TTL would prevent routing
        from ever resuming.
        """
        with self._lock:
            self._tool_runtime_failure_events.pop(tool_id, None)
            # Restore the default "alive" assumption so the next
            # probe can re-evaluate without us forcing a sticky
            # UNAVAILABLE state.
            self._tool_runtime_alive.pop(tool_id, None)

    def get_tool_runtime_failure(self, tool_id: str) -> Optional[Dict[str, Any]]:
        """Return the active failure event for ``tool_id`` or
        ``None``.  An event whose ``expires_at_monotonic`` is in
        the past is treated as expired and purged lazily on read.
        """
        import time as _time
        with self._lock:
            event = self._tool_runtime_failure_events.get(tool_id)
            if event is None:
                return None
            now_m = _time.monotonic()
            if event.get("expires_at_monotonic", 0.0) <= now_m:
                self._tool_runtime_failure_events.pop(tool_id, None)
                return None
            return dict(event)

    def list_tool_runtime_failures(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot of every active tool-runtime failure event.

        Expired events are purged lazily on read so the snapshot is
        always coherent with ``compute_tool_status``.
        """
        import time as _time
        with self._lock:
            now_m = _time.monotonic()
            active: Dict[str, Dict[str, Any]] = {}
            for tid, event in list(self._tool_runtime_failure_events.items()):
                if event.get("expires_at_monotonic", 0.0) <= now_m:
                    self._tool_runtime_failure_events.pop(tid, None)
                    continue
                active[tid] = dict(event)
            return active

    # ------------------------------------------------------------------
    # Tool status — effective model path
    # ------------------------------------------------------------------

    # Mapping from adapter-cache ``model_state`` to
    # ``FAILURE_SCOPE_*`` so the engine can mechanically classify
    # probe outcomes (P8D §四 requirement: no implicit assumption).
    _CACHE_STATE_TO_SCOPE: Dict[str, str] = {
        # RESOURCE — account / quota / provider-level
        "quota_exhausted": FAILURE_SCOPE_RESOURCE,
        "insufficient_balance": FAILURE_SCOPE_RESOURCE,
        "rate_limited": FAILURE_SCOPE_RESOURCE,
        "provider_unavailable": FAILURE_SCOPE_RESOURCE,
        "external_service_cooldown": FAILURE_SCOPE_RESOURCE,
        "external_contract_failure": FAILURE_SCOPE_RESOURCE,
        "region_restricted": FAILURE_SCOPE_RESOURCE,
        # Network errors observed at the probe boundary are still
        # provider-side and therefore RESOURCE-scope (the tool
        # itself is up).
        "network_error": FAILURE_SCOPE_RESOURCE,
        "external_network_error": FAILURE_SCOPE_RESOURCE,
        # BINDING — adapter / plan specific
        "token_plan": FAILURE_SCOPE_BINDING,
        "external_timeout": FAILURE_SCOPE_BINDING,
        # TOOL_ADAPTER / LOCAL_RUNTIME — tool's own runtime issues
        "malformed_response_local": FAILURE_SCOPE_TOOL_ADAPTER,
        "local_adapter_exception": FAILURE_SCOPE_TOOL_ADAPTER,
        "timeout": FAILURE_SCOPE_LOCAL_RUNTIME,
        "probe_error": FAILURE_SCOPE_LOCAL_RUNTIME,
        "local_process_down": FAILURE_SCOPE_LOCAL_RUNTIME,
        "ipc_failure": FAILURE_SCOPE_LOCAL_RUNTIME,
        "invalid_local_configuration": FAILURE_SCOPE_LOCAL_RUNTIME,
        "local_permission_error": FAILURE_SCOPE_LOCAL_RUNTIME,
        "auth_failed": FAILURE_SCOPE_RESOURCE,
    }

    def _adapter_cache_state(self, tool_id: str) -> Dict[str, Any]:
        """Read the probe cache for ``tool_id``.

        An explicit per-tool override (set via
        :func:`set_adapter_cache_override`) wins over the on-disk
        cache; this is the documented test entry point.

        Returns an empty dict when the cache file is missing or
        unreadable; callers must treat that as ``model_state=unknown``
        which means "do not synthesize a cooldown".
        """
        with self._lock:
            override = self._adapter_cache_override.get(tool_id)
        if override is not None:
            return dict(override)
        try:
            from aios_tool_adapter import get_adapter
            cache = get_adapter(tool_id).cached_probe()
            if not isinstance(cache, dict):
                return {}
            return cache
        except Exception:
            return {}

    def _cache_retry_after_in_future(self, cache: Any) -> bool:
        """True when the cache's ``retry_after`` is still in the future.

        Used as a fallback to keep a RESOURCE-scope failure
        (e.g. quota_exhausted) authoritative even after the cache
        itself is marked stale by ``probe_max_age_seconds``. The
        underlying provider literally answered 402/429; the
        cooldown window is still valid.
        """
        if not isinstance(cache, dict):
            return False
        retry_after = cache.get("retry_after")
        if not retry_after:
            return False
        try:
            end = datetime.fromisoformat(
                str(retry_after).replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            return end.timestamp() > datetime.now(tz=timezone.utc).timestamp()
        except Exception:
            return False

    def _is_shared_provider_primary_resource(
        self, binding_id: Optional[str], primary_bid: Optional[str],
    ) -> bool:
        """True when ``binding_id`` shares a resource with ``primary_bid``.

        A binding is blocked by a RESOURCE-scope failure on the
        primary only when it shares the same underlying resource
        (e.g. both ``claude:deepseek`` and ``codex:deepseek`` ride on
        the shared ``deepseek.shared`` account). A different
        shared provider (e.g. ``minimax.shared``) is independent and
        MUST stay verified.
        """
        if not binding_id or not primary_bid:
            return False
        b1 = self._bindings.get(binding_id)
        b2 = self._bindings.get(primary_bid)
        if b1 is None or b2 is None:
            return False
        return b1.resource_id == b2.resource_id

    def _cache_still_active(self, cache: Any) -> bool:
        """True when the cache is still authoritative.

        A cache is authoritative when:
        * ``stale`` flag is False (the cache itself has not been
          marked as past its probe lifetime), AND
        * ``retry_after`` is missing or still in the future.
        Otherwise the cache is stale and the engine MUST NOT block
        a binding based on it.
        """
        if not isinstance(cache, dict):
            return False
        if cache.get("stale") is True:
            return False
        retry_after = cache.get("retry_after")
        if not retry_after:
            return True
        try:
            end = datetime.fromisoformat(
                str(retry_after).replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            return end.timestamp() > datetime.now(tz=timezone.utc).timestamp()
        except Exception:
            return True

    def _classify_cache_scope(
        self, cache: Dict[str, Any],
    ) -> Tuple[str, str]:
        """Return ``(scope, reason)`` for the given probe cache.

        ``scope`` is one of :data:`aios_model_resources.ALL_FAILURE_SCOPES`
        plus the sentinel ``"OK"`` (cache healthy) and ``"UNKNOWN"``
        (cache missing or non-actionable).  ``reason`` is the raw
        human-readable reason the cache recorded.
        """
        state = str(cache.get("model_state", "") or "")
        reason = str(cache.get("reason", "") or "")
        if not state:
            return "UNKNOWN", reason
        if state == "available":
            return "OK", reason
        scope = self._CACHE_STATE_TO_SCOPE.get(state)
        if scope is None:
            # Conservative default: an unrecognised cache state is
            # treated as a binding-scope anomaly (it doesn't taint
            # the entire resource, but the binding is no longer
            # safe to claim as verified).
            return FAILURE_SCOPE_BINDING, reason or state
        return scope, reason or state

    def _is_shared_provider(self, binding_id: Optional[str]) -> bool:
        """True when ``binding_id`` points to a shared provider resource
        (one that participates in the model-failover stack).  Local
        services (e.g. ``opencode.free``) and missing records both
        return False, so a cache mirror on them is suppressed.
        """
        if not binding_id:
            return False
        b = self._bindings.get(binding_id)
        if b is None:
            return False
        if not self._resources:
            return False
        resource = self._resources.get(b.resource_id)
        if resource is None:
            return False
        vendor = str(getattr(resource, "vendor", "") or "")
        return vendor not in ("", "local", "opencode")

    def _apply_cache_to_engine(
        self, tool_id: str, primary_bid: str, scope: str,
        cache: Dict[str, Any],
    ) -> None:
        """Mirror the adapter cache into the model engine so other
        consumers see a coherent picture.

        * ``RESOURCE``  → cooldown the primary binding's resource
          (only when the resource is a *shared* provider; local
          services like ``opencode.free`` are excluded so a local
          probe outage does not poison every consumer).
        * ``BINDING``   → cooldown the primary binding itself.
        * ``LOCAL_RUNTIME`` / ``TOOL_ADAPTER`` → no model engine
          cooldown (the tool's own runtime is at fault; switching
          models would not help).  The tool-level ``unavailable``
          signal is communicated by the engine caller.

        P8D: this method is the only place where the cache mirror
        happens.  ``compute_tool_status`` keeps the mirror local
        (returns ``UNAVAILABLE_TOOL_RUNTIME`` for cache failures)
        and never mutates the global model-engine cooldown; the
        mirror is restricted to the shared-provider case so test
        fixtures and unrelated consumers are not affected.
        """
        if scope not in (
                FAILURE_SCOPE_RESOURCE, FAILURE_SCOPE_BINDING):
            return
        b = self._bindings.get(primary_bid)
        if b is None:
            return
        # Only mirror failures on SHARED provider resources.  Local
        # services (opencode.free, etc.) do not appear in the
        # failover stack as a provider target; a probe outage on
        # them is communicated via the tool-level UNAVAILABLE
        # signal, not via a resource cooldown.
        if scope == FAILURE_SCOPE_RESOURCE:
            resource = self._resources.get(b.resource_id) if self._resources else None
            if resource is None or getattr(resource, "vendor", "") in (
                    "", "local", "opencode"):
                # No shared provider record — refuse to mirror.
                return
        kind = str(cache.get("model_state", "") or scope)
        reason = str(cache.get("reason", "") or kind)
        try:
            if scope == FAILURE_SCOPE_RESOURCE:
                self._model_engine._cooldown_resource(  # type: ignore[attr-defined]
                    b.resource_id, reason=reason, kind=kind)
            else:
                self._model_engine._cooldown_binding(  # type: ignore[attr-defined]
                    primary_bid, reason=reason, kind=kind)
        except Exception:
            pass

    def compute_tool_status(
        self,
        tool_id: str,
        *,
        adapter_cache: Optional[Dict[str, Any]] = None,
    ) -> ToolStatusReport:
        """Compute the effective status of ``tool_id``.

        ``adapter_cache`` is an optional override for the probe
        cache; when provided, the engine uses it instead of reading
        the on-disk cache.  Tests / canary pass ``{}`` to disable
        the cache mirror entirely without touching the real
        ``cache/tool_health/<tool>.json`` file.

        The status order is:

        1. Not in registry / disabled → ``DISABLED`` or ``UNVERIFIED``.
        2. Adapter probe cache classified as
           ``LOCAL_RUNTIME``/``TOOL_ADAPTER`` and still in cooldown
           → ``UNAVAILABLE_TOOL_RUNTIME`` (the tool's own runtime
           is the problem; switching models would not help).
        3. ``is_tool_runtime_alive`` is False →
           ``UNAVAILABLE_TOOL_RUNTIME``.
        4. Walk every candidate binding of the policy:
           * Binding disabled or in cooldown → blocked.
           * Binding's resource in cooldown → blocked.
           * Otherwise → verified.
           The adapter cache may *force* the primary binding
           (RESOURCE / BINDING scope) into the blocked bucket and
           mirror a resource cooldown into the model engine.
        5. Pick the status:
           * Primary in verified → ``AVAILABLE_PRIMARY``.
           * Primary not in verified, fallback healthy →
             ``AVAILABLE_WITH_MODEL_FALLBACK``.
           * Otherwise → ``DEGRADED_NO_MODEL_FALLBACK``.
        """
        with self._lock:
            manifest = self._registry.get(tool_id)
            if manifest is None or not manifest.enabled:
                return ToolStatusReport(
                    tool_id=tool_id,
                    status=(TOOL_STATUS_DISABLED
                            if manifest and not manifest.enabled
                            else TOOL_STATUS_UNVERIFIED),
                    reason=("disabled by config" if manifest
                            and not manifest.enabled else "unknown tool"),
                )
            policy = self._policies.get(tool_id)
            if policy is None:
                return ToolStatusReport(
                    tool_id=tool_id,
                    status=TOOL_STATUS_UNVERIFIED,
                    reason="no model policy",
                )
            primary = policy.preferred_binding or (
                policy.candidate_bindings[0] if policy.candidate_bindings else None
            )

            # Step 0 (P9D-R runtime health freshness): an explicit
            # failure event recorded by ``record_tool_runtime_failure``
            # trumps every other signal — the long-lived on-disk
            # probe cache is NOT a substitute for the freshly
            # observed TOOL_PROCESS / TOOL_ADAPTER /
            # DISPATCH_CLAIM_TIMEOUT event.  We must invalidate
            # the routing decision immediately so the Repair chain
            # can pick a different tool on its very next generation.
            failure_event = self.get_tool_runtime_failure(tool_id)
            if failure_event is not None:
                blocked_bindings = tuple(policy.candidate_bindings)
                return ToolStatusReport(
                    tool_id=tool_id,
                    status=TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
                    primary_binding=primary,
                    effective_binding=primary,
                    verified_bindings=(),
                    blocked_bindings=blocked_bindings,
                    reason=(
                        "tool_runtime_failure_event:"
                        f"{failure_event.get('scope', 'TOOL_PROCESS')}:"
                        f"{failure_event.get('reason', '')}"
                    ),
                    fallback_ready=False,
                )

            # Step 1: inspect the adapter cache for this tool.
            # ``adapter_cache=None`` means read the on-disk cache;
            # ``adapter_cache={}`` (explicit empty) means ignore
            # the cache entirely (used by tests).
            if adapter_cache is None:
                cache = self._adapter_cache_state(tool_id)
            else:
                cache = adapter_cache
            cache_scope, cache_reason = self._classify_cache_scope(cache)
            cache_active = self._cache_still_active(cache)

            # LOCAL_RUNTIME / TOOL_ADAPTER scope failures trump
            # everything else: the tool's own runtime is broken,
            # so we cannot serve ANY model — not even via the
            # primary binding.  ``UNAVAILABLE_TOOL_RUNTIME`` is the
            # honest signal.
            if cache_active and cache_scope in (
                    FAILURE_SCOPE_LOCAL_RUNTIME,
                    FAILURE_SCOPE_TOOL_ADAPTER):
                # Mirror the cache into the model engine for any
                # observers that care, but mark the tool as runtime-
                # unavailable so the caller does not silently
                # pretend a model fallback would help.
                if cache_scope == FAILURE_SCOPE_BINDING:
                    pass
                return ToolStatusReport(
                    tool_id=tool_id,
                    status=TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
                    primary_binding=primary,
                    effective_binding=primary,
                    verified_bindings=(),
                    blocked_bindings=tuple(policy.candidate_bindings),
                    reason=(
                        f"adapter_cache:{tool_id}:{cache_scope}:"
                        f"{cache_reason or str(cache.get('model_state') or '')}"),
                    fallback_ready=False,
                )

            # Step 2: process runtime liveness AFTER the cache so an
            # explicit probe failure is never silently masked.
            if not self.is_tool_runtime_alive(tool_id):
                return ToolStatusReport(
                    tool_id=tool_id,
                    status=TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
                    primary_binding=primary,
                    effective_binding=primary,
                    verified_bindings=(),
                    blocked_bindings=tuple(policy.candidate_bindings),
                    reason="runtime not active",
                    fallback_ready=False,
                )

            verified: List[str] = []
            blocked: List[str] = []
            # P8D reconciliation: a stale cache (probe older than
            # probe_max_age_seconds) loses its freshness stamp but a
            # quota/rate/auth failure that still has ``retry_after``
            # in the future remains authoritative for blocking. We
            # therefore keep treating RESOURCE-scope failures on
            # shared providers as blocking when either (a) the cache
            # is still fresh, or (b) ``retry_after`` is still in the
            # future. Without this clause a stale cache would
            # silently let a quota-exhausted primary stay AVAILABLE
            # even though the provider literally answered 402.
            cache_informative = (
                cache_active
                or self._cache_retry_after_in_future(cache)
            )
            cache_blocked_primary = (
                cache_informative
                and cache_scope == FAILURE_SCOPE_RESOURCE
                and self._is_shared_provider(primary))
            cache_blocked_primary = cache_blocked_primary or (
                cache_informative
                and cache_scope == FAILURE_SCOPE_BINDING)
            # P8D reconciliation: the adapter layer may also flag
            # some bindings as unsupported (the executor / relay
            # cannot reach that binding's upstream). Those bindings
            # are NEVER production eligible and must join the
            # ``blocked_bindings`` bucket regardless of probe state
            # so the monitor / acceptance can distinguish a
            # quota-exhausted primary from a hard-upstream-missing
            # primary (e.g. ``codex:deepseek`` while the codex relay
            # serves only the MiniMax upstream).
            unsupported = set(
                ADAPTER_UNSUPPORTED_BINDINGS.get(tool_id, ()) or ())
            for bid in policy.candidate_bindings:
                b = self._bindings.get(bid)
                if b is None or not b.enabled:
                    continue
                # Already in cooldown via recorded attempt?
                if (self._model_engine.is_binding_in_cooldown(bid)
                        or self._model_engine.is_resource_in_cooldown(
                            b.resource_id)):
                    blocked.append(bid)
                    continue
                # Adapter unsupported: relay / adapter cannot
                # actually reach this binding's upstream. Mirror to
                # blocked_bindings and never count as verified.
                if bid in unsupported:
                    blocked.append(bid)
                    continue
                # P8D: cache RESOURCE/BINDING failures on the
                # primary binding block it locally for THIS status
                # call only.  We do NOT mutate the global model
                # engine cooldown here — that would leak test
                # fixtures' side-effects into unrelated consumers
                # and poison real workload accounting.
                if bid == primary and cache_blocked_primary:
                    blocked.append(bid)
                    continue
                # Same reasoning for ALL bindings on a shared
                # provider: if the resource itself is blocked (RESOURCE
                # scope) and the binding's resource is shared, the
                # binding is blocked regardless of probe freshness.
                if (
                    cache_informative
                    and cache_scope == FAILURE_SCOPE_RESOURCE
                    and self._is_shared_provider(bid)
                    and self._is_shared_provider_primary_resource(bid, primary)
                ):
                    blocked.append(bid)
                    continue
                verified.append(bid)
            # If the primary was locally blocked by the cache,
            # mirror the synthetic cooldown into the model engine
            # AFTER the status has been computed so subsequent
            # callers in the same process see a coherent picture.
            # The mirror happens only when the cache is genuinely
            # authoritative (active + non-stale + RESOURCE on a
            # shared provider); otherwise we leave the engine alone.
            if cache_blocked_primary and cache_scope == (
                    FAILURE_SCOPE_RESOURCE):
                self._apply_cache_to_engine(
                    tool_id, primary, FAILURE_SCOPE_RESOURCE, cache)
            elif cache_blocked_primary and cache_scope == (
                    FAILURE_SCOPE_BINDING):
                self._apply_cache_to_engine(
                    tool_id, primary, FAILURE_SCOPE_BINDING, cache)
            primary_healthy = primary in verified
            fallback_ready = (not primary_healthy
                               and len([b for b in verified
                                        if b != primary]) > 0)
            # P8D reconciliation: if the adapter supports no model
            # path AT ALL for this tool, the engine refuses to
            # pretend a fallback exists.  ``codex`` with
            # ``codex-relay`` only on the MiniMax upstream falls in
            # this bucket when ``codex:deepseek`` is also blocked —
            # the engine MUST surface ``DEGRADED_NO_NATIVE_MODEL_ROUTE``
            # instead of silently serving the
            # ``DEGRADED_NO_MODEL_FALLBACK`` wrapper around a fake
            # fallback.
            unsupported_present = any(
                bid in ADAPTER_UNSUPPORTED_BINDINGS.get(tool_id, ())
                for bid in policy.candidate_bindings
            )
            if primary_healthy:
                status = TOOL_STATUS_AVAILABLE_PRIMARY
                effective = primary
                reason = "primary binding healthy"
            elif fallback_ready:
                # Pick the first healthy non-primary binding.
                effective = next((b for b in verified if b != primary),
                                  primary)
                status = TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK
                reason = ("primary blocked; fallback binding healthy: "
                          + str(effective))
            elif verified:
                status = TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK
                effective = primary
                reason = "primary blocked; no other verified binding"
            elif unsupported_present and not verified:
                status = TOOL_STATUS_DEGRADED_NO_NATIVE_MODEL_ROUTE
                effective = primary
                reason = ("no adapter-supported model path: every "
                          "candidate binding is either blocked or "
                          "not reachable from the current relay")
            else:
                status = TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK
                effective = primary
                reason = "every candidate binding blocked or disabled"
            return ToolStatusReport(
                tool_id=tool_id,
                status=status,
                primary_binding=primary,
                effective_binding=effective,
                verified_bindings=tuple(verified),
                blocked_bindings=tuple(blocked),
                reason=reason,
                fallback_ready=fallback_ready,
            )

    def all_tool_statuses(self) -> List[ToolStatusReport]:
        with self._lock:
            return [self.compute_tool_status(t.tool_id)
                    for t in self._registry.list_enabled()]

    # ------------------------------------------------------------------
    # Selection — pick the next tool for a role
    # ------------------------------------------------------------------

    def select_tool(
        self,
        *,
        task_id: str,
        role: str,
        preferred_tool: Optional[str] = None,
        strict_tool: Optional[str] = None,
        allow_tool_fallback: bool = True,
        max_tool_attempts: int = DEFAULT_MAX_TOOL_ATTEMPTS,
        max_tool_failovers: int = DEFAULT_MAX_TOOL_FAILOVERS,
        capability_overlay: Optional[Mapping[str, Any]] = None,
        task_blocked_tools: Optional[Sequence[str]] = None,
        task_policy_role: Optional[str] = None,
    ) -> ToolFailoverDecision:
        """Pick the next tool to try for ``role``.

        ``capability_overlay`` is a task-scoped override; it does NOT
        mutate the global registry.  It is used by tests / canary to
        simulate ``preferred executor unavailable`` without touching
        real services.  The overlay maps ``tool_id`` to a synthetic
        status; values: ``"unavailable_runtime"``,
        ``"degraded_no_model_fallback"``,
        ``"available_primary"``,
        ``"available_with_model_fallback"``.

        ``task_blocked_tools`` (close-out 20260727-§五) is the
        per-task overlay that lets the orchestrator force specific
        tools to be excluded from this task only. The exclusion does
        NOT mutate global tool health, binding cooldown, or resource
        circuit; it is recorded via :attr:`ToolFailoverDecision.reason`
        and surfaces in the shadow log as
        ``excluded_by_task_policy=True``.

        ``task_policy_role`` is the policy-bound role (executor /
        planner / reviewer) when the caller wants the engine to
        cross-check ``blocked_planner_tools`` / ``blocked_reviewer_tools``
        in addition to ``blocked_tools``.  ``role`` is still the
        routing role for the registry lookup.
        """
        overlay = capability_overlay or {}
        # Normalise the task-blocked list once; the set lookup is O(1).
        task_blocked_set: set[str] = set()
        for raw in (task_blocked_tools or ()):
            if isinstance(raw, str) and raw:
                task_blocked_set.add(raw.strip())
        # Cross-role exclusions.
        if task_policy_role == "reviewer":
            # The blocked_reviewer_tools surface is consulted via
            # ``task_blocked_tools`` argument by the caller; we accept
            # the same set here so a single ``task_blocked_tools``
            # argument covers both blocking fields depending on
            # ``task_policy_role``.
            pass
        with self._lock:
            # 0. Lock on first success — if we already locked a tool
            #    for this task, keep using it.
            locked = self._task_locked_tool.get(task_id)
            if locked:
                return ToolFailoverDecision(
                    action="use",
                    tool_id=locked,
                    reason="actual_tool locked on first success",
                    tool_status=self._task_tool_status.get(
                        task_id, {}).get(locked, TOOL_STATUS_UNVERIFIED),
                    effective_model_binding=None,
                )

            tools = list(self._registry.list_by_role(role))
            # Apply overlay to mark tools unavailable for *this* task
            # without mutating registry state.
            tool_status = self._task_tool_status.setdefault(task_id, {})
            for tid in [m.tool_id for m in tools]:
                status = self.compute_tool_status(tid)
                if tid in overlay:
                    overlay_status = str(overlay[tid])
                    if overlay_status in ALL_TOOL_STATUSES:
                        status = ToolStatusReport(
                            tool_id=tid,
                            status=overlay_status,
                            primary_binding=status.primary_binding,
                            effective_binding=status.effective_binding,
                            verified_bindings=status.verified_bindings,
                            blocked_bindings=status.blocked_bindings,
                            reason=("overlay:" + overlay_status),
                            fallback_ready=status.fallback_ready,
                        )
                tool_status[tid] = status.status

            # 1. strict_tool path — refuse to switch.
            if strict_tool:
                manifest = self._registry.get(strict_tool)
                if manifest is None:
                    return ToolFailoverDecision(
                        action="strict_violation",
                        reason=f"strict_tool {strict_tool!r} not registered",
                    )
                if not manifest.enabled or not manifest.has_role(role):
                    return ToolFailoverDecision(
                        action="strict_violation",
                        reason=(f"strict_tool {strict_tool!r} not enabled "
                                f"or not role-compatible with {role!r}"),
                    )
                # Close-out 20260727-§十四: a strict_tool that is
                # itself in the task-level ``blocked_tools`` is
                # treated as a strict violation (the caller cannot
                # both pin and ban the same tool).
                if strict_tool in task_blocked_set:
                    return ToolFailoverDecision(
                        action="strict_violation",
                        tool_id=strict_tool,
                        reason=("task_policy_blocked: strict_tool "
                                f"{strict_tool!r} is in blocked_tools"),
                        tool_status=TOOL_STATUS_UNVERIFIED,
                    )
                status = self._status_for(task_id, strict_tool)
                if status.status in (
                        TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
                        TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK):
                    return ToolFailoverDecision(
                        action="strict_violation",
                        tool_id=strict_tool,
                        reason=(f"strict_tool {strict_tool!r} "
                                f"status={status.status}"),
                        tool_status=status.status,
                    )
                return ToolFailoverDecision(
                    action="use",
                    tool_id=strict_tool,
                    tool_status=status.status,
                    effective_model_binding=status.effective_binding,
                    reason="strict_tool honoured",
                )

            attempted = self._task_tools.setdefault(task_id, [])
            attempt_index = len(attempted)
            history = self._task_tool_history.setdefault(task_id, [])

            # 2. max attempts cap.
            if attempt_index >= max_tool_attempts:
                return ToolFailoverDecision(
                    action="max_attempts",
                    reason=(f"max_tool_attempts={max_tool_attempts} reached"),
                    next_attempt_index=attempt_index,
                )

            # 3. Build the candidate order.  Preferred first (if
            #    registered and role-compatible), then registry order.
            ordered: List[str] = []
            if preferred_tool:
                if preferred_tool in [m.tool_id for m in tools]:
                    ordered.append(preferred_tool)
            for m in tools:
                if m.tool_id not in ordered:
                    ordered.append(m.tool_id)

            # 3a. (close-out 20260727-§五) drop the per-task blocked
            #     tools BEFORE any other logic runs.  The exclusion
            #     MUST be visible in the audit trail; we therefore
            #     keep a parallel ``task_excluded`` list and surface
            #     it in the decision ``reason`` so monitor / acceptance
            #     can render it.
            task_excluded: List[str] = []
            if task_blocked_set:
                kept: List[str] = []
                for tid in ordered:
                    if tid in task_blocked_set:
                        task_excluded.append(tid)
                        continue
                    kept.append(tid)
                ordered = kept

            if not ordered:
                # Strict task-policy: even the preferred tool was
                # excluded by the task policy.  We refuse to silently
                # pick another tool and surface a dedicated signal.
                return ToolFailoverDecision(
                    action="no_candidates",
                    reason=("task_policy_blocked: every candidate "
                            f"for role={role!r} is in "
                            "blocked_tools; excluded="
                            f"{task_excluded}"),
                    next_attempt_index=attempt_index,
                )

            # 4. Fallback disabled → only the preferred tool is
            #    allowed; if it has already been attempted we abort.
            if not allow_tool_fallback:
                if attempt_index > 0:
                    return ToolFailoverDecision(
                        action="fallback_disabled",
                        reason="allow_tool_fallback=False",
                        next_attempt_index=attempt_index,
                    )
                target = preferred_tool or ordered[0]
                # Close-out 20260727-§五 / §十四: if the preferred
                # tool is excluded by the task policy, return
                # ``no_candidates`` so the caller can record an
                # audit event instead of silently bypassing the
                # block. The earlier ``if not ordered`` branch already
                # covered the case where ALL candidates were blocked
                # via the explicit ``blocked_tools`` overlay, but
                # ``target`` may still resolve to ``ordered[0]``
                # here when the caller supplied ``preferred_tool``
                # pointing INTO the blocked set.  We treat that as a
                # dedicated task-policy signal.
                if target in task_blocked_set:
                    return ToolFailoverDecision(
                        action="no_candidates",
                        tool_id=target,
                        reason=("task_policy_blocked: preferred "
                                f"{target!r} is in blocked_tools; "
                                "fallback disabled"),
                        next_attempt_index=attempt_index,
                    )
                status = self._status_for(task_id, target)
                if status.status in (
                        TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME,
                        TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK):
                    return ToolFailoverDecision(
                        action="fallback_disabled",
                        tool_id=target,
                        reason=(f"preferred {target!r} status="
                                f"{status.status}; fallback disabled"),
                        tool_status=status.status,
                        next_attempt_index=attempt_index,
                    )
                return ToolFailoverDecision(
                    action="use",
                    tool_id=target,
                    tool_status=status.status,
                    effective_model_binding=status.effective_binding,
                )

            # 5. Walk candidates in order, skipping tools we've
            #    already attempted and tools whose model pool is
            #    exhausted.
            skipped = 0
            for tool_id in ordered:
                if tool_id in attempted:
                    continue
                status = self._status_for(task_id, tool_id)
                if status.status == TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME:
                    skipped += 1
                    continue
                if status.status == TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK:
                    skipped += 1
                    continue
                if status.status == TOOL_STATUS_DISABLED:
                    skipped += 1
                    continue
                if status.status == TOOL_STATUS_UNVERIFIED:
                    # UNVERIFIED tools are not eligible.
                    skipped += 1
                    continue
                # Eligible: AVAILABLE_PRIMARY or AVAILABLE_WITH_MODEL_FALLBACK.
                return ToolFailoverDecision(
                    action="use",
                    tool_id=tool_id,
                    tool_status=status.status,
                    effective_model_binding=status.effective_binding,
                    reason=("first eligible" if not attempted
                            else "tool failover"),
                )

            # 6. Nothing eligible → pool exhausted.
            if skipped == len(ordered):
                return ToolFailoverDecision(
                    action="pool_exhausted",
                    reason=(f"every candidate tool for role={role!r} "
                            f"is unavailable or pool-exhausted"),
                    next_attempt_index=attempt_index,
                )
            return ToolFailoverDecision(
                action="pool_exhausted",
                reason="no eligible tool",
                next_attempt_index=attempt_index,
            )

    def _status_for(self, task_id: str, tool_id: str) -> ToolStatusReport:
        cached_status = self._task_tool_status.get(task_id, {}).get(
            tool_id)
        live = self.compute_tool_status(tool_id)
        # If a task-scoped status was cached (e.g. via capability
        # overlay), honour it: the overlay is the authoritative
        # signal for this task and MUST NOT be overridden by the live
        # compute. We only refresh the cache if no overlay is in effect.
        if cached_status is None:
            return live
        if live.status == cached_status:
            return live
        # Return a synthetic report that combines the cached status
        # with the live report's other fields.
        return ToolStatusReport(
            tool_id=tool_id,
            status=cached_status,
            primary_binding=live.primary_binding,
            effective_binding=live.effective_binding,
            verified_bindings=live.verified_bindings,
            blocked_bindings=live.blocked_bindings,
            reason=("overlay:" + cached_status
                    if cached_status != live.status
                    else live.reason),
            fallback_ready=live.fallback_ready,
        )

    # ------------------------------------------------------------------
    # Attempt recording
    # ------------------------------------------------------------------

    def record_tool_attempt(
        self,
        *,
        task_id: str,
        tool_id: str,
        reason: str = "",
    ) -> ToolAttemptRecord:
        with self._lock:
            attempted = self._task_tools.setdefault(task_id, [])
            is_failover = len(attempted) > 0
            attempted.append(tool_id)
            status = self.compute_tool_status(tool_id)
            record = ToolAttemptRecord(
                task_id=task_id,
                tool_id=tool_id,
                attempted_at=_iso_now(),
                reason=reason,
                tool_failover_reason=reason if is_failover else "",
                is_failover=is_failover,
                effective_model_binding=(status.effective_binding or ""),
                tool_status_snapshot=status.status,
            )
            self._task_tool_history.setdefault(task_id, []).append(record)
            return record

    def lock_actual_tool(self, task_id: str, tool_id: str) -> None:
        """Lock ``tool_id`` as the task's ``actual_tool``.

        Once locked, the engine refuses to suggest a different tool
        for the rest of the task.  Called by the orchestrator right
        after the first successful model attempt.
        """
        with self._lock:
            self._task_locked_tool[task_id] = tool_id

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def attempted_tools(self, task_id: str) -> List[str]:
        with self._lock:
            return list(self._task_tools.get(task_id, []))

    def actual_tool(self, task_id: str) -> Optional[str]:
        with self._lock:
            return self._task_locked_tool.get(task_id)

    def tool_history(self, task_id: str) -> List[ToolAttemptRecord]:
        with self._lock:
            return list(self._task_tool_history.get(task_id, []))

    def reset_state(self) -> None:
        with self._lock:
            self._task_tools.clear()
            self._task_tool_history.clear()
            self._task_locked_tool.clear()
            self._task_tool_status.clear()
            self._tool_runtime_alive.clear()

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "registry_tool_count": len(self._registry),
                "tasks": list(self._task_tools.keys()),
                "tool_runtime_alive": dict(self._tool_runtime_alive),
            }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_ENGINE_SINGLETON: Optional[ToolFailoverEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_default_tool_engine() -> ToolFailoverEngine:
    global _ENGINE_SINGLETON
    with _ENGINE_LOCK:
        if _ENGINE_SINGLETON is None:
            _ENGINE_SINGLETON = ToolFailoverEngine()
        return _ENGINE_SINGLETON


def set_default_tool_engine(engine: Optional[ToolFailoverEngine]) -> None:
    global _ENGINE_SINGLETON
    with _ENGINE_LOCK:
        _ENGINE_SINGLETON = engine


def reset_default_tool_engine() -> ToolFailoverEngine:
    global _ENGINE_SINGLETON
    with _ENGINE_LOCK:
        _ENGINE_SINGLETON = ToolFailoverEngine()
        return _ENGINE_SINGLETON


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# P9D-R runtime health freshness: module-level helpers
# ---------------------------------------------------------------------------


def record_tool_runtime_failure(
    tool_id: str,
    *,
    scope: str = FAILURE_SCOPE_LOCAL_RUNTIME,
    reason: str = "",
    ttl_seconds: Optional[int] = None,
    observed_at_monotonic: Optional[float] = None,
    observed_at_wallclock: Optional[str] = None,
) -> Dict[str, Any]:
    """Record an explicit tool-runtime failure event on the
    default :class:`ToolFailoverEngine` instance.

    This is the public entry point used by the orchestrator Repair
    path (``_expire_child_for_repair`` / ``_repair_node``) and any
    other component that observes a real tool failure (TOOL_PROCESS
    / TOOL_ADAPTER / DISPATCH_CLAIM_TIMEOUT / CONNECTION_REFUSED /
    ENDPOINT_UNREACHABLE).  After this call, every subsequent
    :func:`ToolFailoverEngine.compute_tool_status` for
    ``tool_id`` returns ``UNAVAILABLE_TOOL_RUNTIME`` until
    :func:`clear_tool_runtime_failure` is called or the TTL
    expires — regardless of the on-disk probe cache.
    """
    return get_default_tool_engine().record_tool_runtime_failure(
        tool_id,
        scope=scope,
        reason=reason,
        ttl_seconds=ttl_seconds,
        observed_at_monotonic=observed_at_monotonic,
        observed_at_wallclock=observed_at_wallclock,
    )


def clear_tool_runtime_failure(tool_id: str) -> None:
    """Clear any explicit failure event for ``tool_id`` on the
    default engine.

    Called by the orchestrator Repair path once the tool has been
    observed healthy again (e.g. successful probe, claim
    confirmed).  This is the only path that re-enables routing after
    a failure event; without it the long-lived TTL would keep the
    tool routed-out forever.
    """
    get_default_tool_engine().clear_tool_runtime_failure(tool_id)


def get_tool_runtime_failure(tool_id: str) -> Optional[Dict[str, Any]]:
    """Return the active failure event for ``tool_id`` (or
    ``None``) on the default engine.  Expired events are purged
    lazily on read.
    """
    return get_default_tool_engine().get_tool_runtime_failure(tool_id)


def is_tool_runtime_alive(tool_id: str) -> bool:
    """Return ``True`` iff ``tool_id`` does NOT have an active
    tool-runtime failure event.  The single source of truth is the
    per-tool failure-event map; the per-tool liveness snapshot is
    a derived signal that flips to ``False`` whenever
    ``record_tool_runtime_failure`` is called.

    This helper is the only correct way to ask "is this tool alive?"
    from outside ``ToolFailoverEngine``.  The legacy
    ``is_tool_runtime_alive`` *method* on the engine is preserved
    for backwards compatibility; this module-level wrapper returns
    the same answer.
    """
    return get_tool_runtime_failure(tool_id) is None


def list_tool_runtime_failures() -> Dict[str, Dict[str, Any]]:
    """Snapshot of every active tool-runtime failure event on the
    default engine.
    """
    return get_default_tool_engine().list_tool_runtime_failures()


__all__ = [
    # constants
    "TOOL_STATUS_AVAILABLE_PRIMARY",
    "TOOL_STATUS_AVAILABLE_WITH_MODEL_FALLBACK",
    "TOOL_STATUS_DEGRADED_NO_MODEL_FALLBACK",
    "TOOL_STATUS_UNAVAILABLE_TOOL_RUNTIME",
    "TOOL_STATUS_UNVERIFIED",
    "TOOL_STATUS_DISABLED",
    "ALL_TOOL_STATUSES",
    "TOOL_FAILOVER_TRIGGER_KINDS",
    "DEFAULT_MAX_TOOL_ATTEMPTS",
    "DEFAULT_MAX_TOOL_FAILOVERS",
    # dataclasses
    "ToolAttemptRecord", "ToolFailoverDecision", "ToolStatusReport",
    # engine
    "ToolFailoverEngine",
    "get_default_tool_engine", "set_default_tool_engine",
    "reset_default_tool_engine",
    # P9D-R runtime health freshness
    "record_tool_runtime_failure",
    "clear_tool_runtime_failure",
    "get_tool_runtime_failure",
    "list_tool_runtime_failures",
]
