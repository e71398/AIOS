#!/usr/bin/env python3
"""AIOS P8A Universal Model Failover Engine.

This module implements the **model-level** failover for already-
selected tools. It is *not* a tool-orchestrator: the tool
identity (``tool_id``) is fixed for the duration of a task; only the
model binding chosen for that tool may change.

The engine's job is to answer four questions per attempt:

1. *Which model binding should the tool use now?*  →  :func:`select_model_binding`
2. *After an attempt, which scope owns the failure?*  →  :func:`classify_model_failure`
3. *Should the engine pick a different binding next time?*  →  :func:`should_failover`
4. *After this attempt, what state do the resource / binding carry?*  →  :func:`record_model_attempt`

Hard rules:

* ``strict_model`` always wins; the engine refuses to pick a
  different binding if a strict model is set.
* ``allow_model_fallback=False`` is an absolute veto on switching.
* RESOURCE-scope failures cool down the *shared resource*; all
  bindings to that resource skip it.
* BINDING-scope failures cool down *that binding only*; other
  tools using the same shared resource are unaffected.
* TOOL_ADAPTER / LOCAL_RUNTIME / TASK_INPUT failures NEVER trigger a
  model switch. The tool itself must be re-tried or replaced; the
  engine does not invent a different model for an adapter bug.
* Cycle detection: the engine never picks a binding that was already
  attempted in this task; ``MiniMax → Qwen → MiniMax`` is forbidden.
* Budget guard: a second model attempt that would exceed the
  policy's ``max_cost`` or ``max_tokens`` is refused with
  ``MODEL_FAILOVER_BLOCKED_BY_BUDGET``.

The engine is pure and offline: it never invokes any Provider. Real
call paths are owned by the per-tool adapter modules; this module
only records the structured outcome so monitor / acceptance / next
attempt can use it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from aios_model_resources import (
    ALL_FAILURE_SCOPES,
    COMPATIBILITY_INCOMPATIBLE,
    COMPATIBILITY_SUPPORTED,
    COMPATIBILITY_SUPPORTED_WITH_ADAPTER,
    COMPATIBILITY_UNSAFE,
    COMPATIBILITY_UNVERIFIED,
    FAILURE_SCOPE_BINDING,
    FAILURE_SCOPE_LOCAL_RUNTIME,
    FAILURE_SCOPE_RESOURCE,
    FAILURE_SCOPE_TASK_INPUT,
    FAILURE_SCOPE_TOOL_ADAPTER,
    SharedModelResource,
    SharedModelResourceRegistry,
    SharedModelResourceState,
    ToolModelBinding,
    ToolModelBindingRegistry,
    ToolModelBindingState,
    ToolModelPolicy,
    ToolModelPolicyRegistry,
)


# ---------------------------------------------------------------------------
# Failure scope + classification rules
# ---------------------------------------------------------------------------


# Allowed failover triggers — these are RESOURCE / BINDING errors that
# may cause a switch to another binding. Anything else is an
# internal / task-input failure and the engine MUST NOT switch.
ALLOWED_MODEL_FAILOVER_KINDS: Tuple[str, ...] = (
    "quota_exhausted",
    "insufficient_balance",
    "rate_limited",
    "token_plan",
    "region_restricted",
    "provider_unavailable",
    "external_network_error",
    "external_timeout",
    "external_service_cooldown",
    "external_contract_failure",
)

# Forbidden kinds — these never cause a model switch. They are
# internal adapter / runtime / task issues that the *tool* must fix,
# not the model.
FORBIDDEN_MODEL_FAILOVER_KINDS: Tuple[str, ...] = (
    "local_process_down",
    "local_adapter_exception",
    "ipc_failure",
    "invalid_local_configuration",
    "programming_error",
    "schema_construction_error",
    "task_validation_error",
    "local_permission_error",
    "schema_mismatch",
    "internal_bug",
)


# Mapping from raw failure kind → ``FAILURE_SCOPE_*``. The classifier
# uses this table as the source of truth. Anything not in the table
# falls back to ``TOOL_ADAPTER`` (a conservative default).
DEFAULT_KIND_TO_SCOPE: Dict[str, str] = {
    # RESOURCE scope — account-level
    "quota_exhausted": FAILURE_SCOPE_RESOURCE,
    "insufficient_balance": FAILURE_SCOPE_RESOURCE,
    "rate_limited": FAILURE_SCOPE_RESOURCE,
    "provider_unavailable": FAILURE_SCOPE_RESOURCE,
    "external_service_cooldown": FAILURE_SCOPE_RESOURCE,
    "external_contract_failure": FAILURE_SCOPE_RESOURCE,
    "region_restricted": FAILURE_SCOPE_RESOURCE,
    # BINDING scope — adapter-specific or plan-specific
    "token_plan": FAILURE_SCOPE_BINDING,
    "external_timeout": FAILURE_SCOPE_BINDING,
    "external_network_error": FAILURE_SCOPE_BINDING,
    "network_error": FAILURE_SCOPE_RESOURCE,
    # TOOL_ADAPTER scope — local adapter / parser
    "malformed_response_local": FAILURE_SCOPE_TOOL_ADAPTER,
    "local_adapter_exception": FAILURE_SCOPE_TOOL_ADAPTER,
    # LOCAL_RUNTIME scope — process / IPC / config
    "local_process_down": FAILURE_SCOPE_LOCAL_RUNTIME,
    "ipc_failure": FAILURE_SCOPE_LOCAL_RUNTIME,
    "invalid_local_configuration": FAILURE_SCOPE_LOCAL_RUNTIME,
    "local_permission_error": FAILURE_SCOPE_LOCAL_RUNTIME,
    # TASK_INPUT scope — caller side
    "task_validation_error": FAILURE_SCOPE_TASK_INPUT,
    "schema_construction_error": FAILURE_SCOPE_TASK_INPUT,
    "programming_error": FAILURE_SCOPE_TASK_INPUT,
}


# Default cooldown duration when the failure kind itself does not
# carry an explicit retry_after (e.g. region_restricted).
DEFAULT_RESOURCE_COOLDOWN_SECONDS = 1800
DEFAULT_BINDING_COOLDOWN_SECONDS = 600


@dataclass
class ModelAttemptRecord:
    """Structured record of one model attempt within a task.

    Captures which binding was tried, what was the outcome, and the
    resource / binding cooldown state after the attempt. The Monitor
    and Acceptance modules serialize this record to expose the
    ``attempted_model_bindings``, ``actual_model_binding``,
    ``model_failover_count``, ``model_failover_reason`` fields.
    """

    tool_id: str
    binding_id: str
    resource_id: str
    attempt_index: int
    started_at: str
    finished_at: str
    success: bool
    failure_kind: Optional[str] = None
    failure_scope: Optional[str] = None
    failure_reason: Optional[str] = None
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_cost: float = 0.0
    actual_tokens: int = 0
    actual_cost: float = 0.0
    is_failover: bool = False
    failover_reason: Optional[str] = None
    resource_cooldown_skips: int = 0
    binding_cooldown_skips: int = 0
    budget_blocked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ModelFailoverDecision:
    """The engine's verdict on the next attempt.

    ``action`` is one of:

    * ``"use"``           — the caller should proceed with ``binding_id``.
    * ``"skip_resource"`` — the chosen binding's shared resource is in
      cooldown; pick the next candidate that does not share the
      cooldown resource.
    * ``"skip_binding"``  — the chosen binding itself is in cooldown.
    * ``"budget_blocked"``— the next attempt would exceed the budget.
    * ``"max_attempts"``  — the policy already hit ``max_model_attempts``.
    * ``"max_failovers"`` — the policy already hit ``max_model_failovers``.
    * ``"strict_violation"`` — caller asked for a different binding
      but ``strict_model`` is set.
    * ``"fallback_disabled"`` — caller asked for a different binding
      but ``allow_model_fallback=False``.
    * ``"exhausted"``     — every candidate was either tried, blocked
      by cooldown, or disabled.
    * ``"no_candidates"`` — the policy has no eligible bindings at all.
    """

    action: str
    binding_id: Optional[str] = None
    reason: str = ""
    next_attempt_index: int = 0
    cooldown_resource_id: Optional[str] = None
    cooldown_binding_id: Optional[str] = None
    budget_blocked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Failover engine
# ---------------------------------------------------------------------------


class ModelFailoverEngine:
    """Stateful model failover engine.

    The engine keeps in-memory cooldown / health state per shared
    resource and per binding. State is process-local; persisting it
    is out of scope for P8A (the live monitor reads the same state
    by importing this module's singleton instance).
    """

    def __init__(
        self,
        resource_registry: SharedModelResourceRegistry,
        binding_registry: ToolModelBindingRegistry,
        policy_registry: ToolModelPolicyRegistry,
        *,
        now_epoch: Optional[float] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._resources = resource_registry
        self._bindings = binding_registry
        self._policies = policy_registry
        self._resource_state: Dict[str, SharedModelResourceState] = {}
        self._binding_state: Dict[str, ToolModelBindingState] = {}
        # Track per-task attempts so cycle detection works.
        self._task_attempts: Dict[str, List[str]] = {}
        self._task_history: Dict[str, List[ModelAttemptRecord]] = {}
        # Map binding_id → resource_id (precomputed for speed).
        self._binding_resource: Dict[str, str] = {
            b.binding_id: b.resource_id for b in self._bindings.list_all()
        }
        # Sentinel for tests / determinism.
        self._now_fn = (lambda: float(now_epoch)) if now_epoch is not None \
            else time.time

    # ------------------------------------------------------------------
    # State lifecycle helpers
    # ------------------------------------------------------------------

    def _ensure_resource_state(self, resource_id: str) -> SharedModelResourceState:
        state = self._resource_state.get(resource_id)
        if state is None:
            state = SharedModelResourceState()
            self._resource_state[resource_id] = state
        return state

    def _ensure_binding_state(self, binding_id: str) -> ToolModelBindingState:
        state = self._binding_state.get(binding_id)
        if state is None:
            state = ToolModelBindingState()
            self._binding_state[binding_id] = state
        return state

    def resource_state(self, resource_id: str) -> SharedModelResourceState:
        with self._lock:
            return self._ensure_resource_state(resource_id)

    def binding_state(self, binding_id: str) -> ToolModelBindingState:
        with self._lock:
            return self._ensure_binding_state(binding_id)

    def reset_state(self) -> None:
        """Wipe all cooldown / attempt state. Used by tests."""
        with self._lock:
            self._resource_state.clear()
            self._binding_state.clear()
            self._task_attempts.clear()
            self._task_history.clear()

    # ------------------------------------------------------------------
    # Failure classification
    # ------------------------------------------------------------------

    @staticmethod
    def classify_model_failure(
        failure_kind: str,
        *,
        error_message: Optional[str] = None,
        adapter_response_present: bool = True,
    ) -> str:
        """Map a raw failure to a FAILURE_SCOPE_* string.

        ``adapter_response_present=False`` means the adapter raised
        before producing a response (timeout, IPC failure, parser
        crash) — these go to ``TOOL_ADAPTER`` so they don't poison
        the resource. ``adapter_response_present=True`` but the body
        is malformed still goes to ``TOOL_ADAPTER`` unless the
        caller explicitly tags ``external_contract_failure``.

        ``error_message`` is consulted only as a last-resort hint
        for legacy errors that pre-date the kind field (e.g.
        ``"API Error: 402 Insufficient Balance"``).
        """
        kind = str(failure_kind or "").strip().lower()
        scope = DEFAULT_KIND_TO_SCOPE.get(kind)
        if scope:
            return scope
        # Heuristic: if no response reached the adapter, treat as
        # local regardless of the kind string. This matches the P8A
        # rule "adapter/parser self-exception → TOOL_ADAPTER".
        if not adapter_response_present:
            return FAILURE_SCOPE_TOOL_ADAPTER
        # Last-ditch heuristic by message content.
        msg = (error_message or "").lower()
        if "402" in msg or "insufficient" in msg or "quota" in msg:
            return FAILURE_SCOPE_RESOURCE
        if "rate limit" in msg or "429" in msg:
            return FAILURE_SCOPE_RESOURCE
        if "tool" in msg and "not supported" in msg:
            return FAILURE_SCOPE_BINDING
        if "malformed" in msg or "schema" in msg:
            return FAILURE_SCOPE_TOOL_ADAPTER
        # Conservative default — never silently switch.
        return FAILURE_SCOPE_TOOL_ADAPTER

    # ------------------------------------------------------------------
    # Attempt outcome recording
    # ------------------------------------------------------------------

    def record_model_attempt(
        self,
        *,
        task_id: str,
        tool_id: str,
        binding_id: str,
        success: bool,
        failure_kind: Optional[str] = None,
        error_message: Optional[str] = None,
        adapter_response_present: bool = True,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
        estimated_cost: float = 0.0,
        actual_tokens: int = 0,
        actual_cost: float = 0.0,
    ) -> ModelAttemptRecord:
        """Record one model attempt and update cooldown state.

        Returns a fully populated :class:`ModelAttemptRecord` that
        the caller can attach to acceptance / monitor payloads.
        """
        binding = self._bindings.get(binding_id)
        if binding is None:
            raise KeyError(f"unknown binding_id {binding_id!r}")
        resource_id = binding.resource_id
        scope = (self.classify_model_failure(
            failure_kind or "",
            error_message=error_message,
            adapter_response_present=adapter_response_present,
        ) if not success else None)
        started = self._now_fn()
        finished = started
        with self._lock:
            attempts = self._task_attempts.setdefault(task_id, [])
            attempt_index = len(attempts)
            is_failover = attempt_index > 0
            attempts.append(binding_id)
            if scope == FAILURE_SCOPE_RESOURCE:
                self._cooldown_resource(
                    resource_id,
                    reason=str(failure_kind or scope),
                    kind=failure_kind,
                )
            elif scope == FAILURE_SCOPE_BINDING:
                self._cooldown_binding(
                    binding_id,
                    reason=str(failure_kind or scope),
                    kind=failure_kind,
                )
            record = ModelAttemptRecord(
                tool_id=tool_id,
                binding_id=binding_id,
                resource_id=resource_id,
                attempt_index=attempt_index,
                started_at=_iso(started),
                finished_at=_iso(finished),
                success=success,
                failure_kind=failure_kind if not success else None,
                failure_scope=scope,
                failure_reason=error_message if not success else None,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
                estimated_cost=estimated_cost,
                actual_tokens=actual_tokens,
                actual_cost=actual_cost,
                is_failover=is_failover,
                failover_reason=(f"{scope}:{failure_kind}"
                                 if (is_failover and not success) else None),
            )
            history = self._task_history.setdefault(task_id, [])
            history.append(record)
            # If the attempt succeeded, also clear cooldown on the
            # binding (we just proved it works).
            if success:
                state = self._ensure_resource_state(resource_id)
                state.last_resource_success = _iso(started)
                bstate = self._ensure_binding_state(binding_id)
                bstate.last_binding_success = _iso(started)
                bstate.binding_health = "AVAILABLE"
                bstate.binding_reason = "recent_success"
                bstate.binding_cooldown_until = None
            else:
                state = self._ensure_resource_state(resource_id)
                state.last_resource_failure = _iso(started)
                state.last_resource_failure_kind = failure_kind
                state.last_resource_failure_scope = scope
                bstate = self._ensure_binding_state(binding_id)
                bstate.last_binding_failure = _iso(started)
                bstate.last_binding_failure_kind = failure_kind
                bstate.last_binding_failure_scope = scope
            return record

    def _cooldown_resource(self, resource_id: str, *, reason: str,
                           kind: Optional[str]) -> None:
        state = self._ensure_resource_state(resource_id)
        state.resource_health = "DEGRADED"
        state.resource_reason = reason
        cooldown_end = self._now_fn() + DEFAULT_RESOURCE_COOLDOWN_SECONDS
        state.resource_cooldown_until = _iso(cooldown_end)
        if kind:
            state.resource_retry_after = state.resource_cooldown_until

    def _cooldown_binding(self, binding_id: str, *, reason: str,
                          kind: Optional[str]) -> None:
        state = self._ensure_binding_state(binding_id)
        state.binding_health = "DEGRADED"
        state.binding_reason = reason
        cooldown_end = self._now_fn() + DEFAULT_BINDING_COOLDOWN_SECONDS
        state.binding_cooldown_until = _iso(cooldown_end)
        if kind:
            state.binding_retry_after = state.binding_cooldown_until

    # ------------------------------------------------------------------
    # Pre-call cooldown expiry
    # ------------------------------------------------------------------

    def _cooldown_active(self, ts: Optional[str]) -> bool:
        if not ts:
            return False
        try:
            end = datetime.fromisoformat(ts).timestamp()
        except Exception:
            return False
        return end > self._now_fn()

    def is_resource_in_cooldown(self, resource_id: str) -> bool:
        with self._lock:
            state = self._resource_state.get(resource_id)
            return self._cooldown_active(
                state.resource_cooldown_until if state else None)

    def is_binding_in_cooldown(self, binding_id: str) -> bool:
        with self._lock:
            state = self._binding_state.get(binding_id)
            return self._cooldown_active(
                state.binding_cooldown_until if state else None)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def select_model_binding(
        self,
        *,
        task_id: str,
        tool_id: str,
        role: str,
        preferred_model: Optional[str] = None,
        strict_model: Optional[str] = None,
        allow_model_fallback: bool = True,
        task_budget: Optional[Mapping[str, Any]] = None,
        task_blocked_bindings: Sequence[str] = (),
        task_blocked_resources: Sequence[str] = (),
    ) -> ModelFailoverDecision:
        """Pick the binding the tool should use for the next attempt.

        The selection algorithm:

        1. Resolve the policy. ``tool_id`` MUST be a registered tool
           (registry owns identity); an unknown tool returns
           ``no_candidates``.
        2. Honour ``strict_model``: if set, refuse to pick anything
           else, even when cooldown blocks the binding.
        3. Refuse to switch after a TOOL_ADAPTER / LOCAL_RUNTIME /
           TASK_INPUT failure. These are internal issues the *tool*
           must fix; the engine never silently swaps the model for
           an adapter bug.
        4. Honour ``allow_model_fallback=False``: only one attempt
           is allowed and no switching is permitted.
        5. Honour ``max_model_attempts``: refuse to add another
           attempt once the cap is reached.
        6. Walk ``candidate_bindings`` in declared priority order,
           skipping bindings that are disabled, in cooldown (binding
           or shared resource), blocked by the task-local
           ``task_blocked_bindings`` overlay, or already attempted
           in this task (cycle prevention).
        7. Honour ``task_budget``: refuse to attempt if the next
           estimated cost / tokens would exceed the budget.

        ``task_blocked_bindings`` is the per-task overlay that lets
        a controlled test refuse a single binding (e.g.
        ``["opencode:free"]``) without flipping the whole tool to
        UNAVAILABLE_TOOL_RUNTIME. It is not persisted across tasks
        and is independent of the live cooldown state.

        ``task_blocked_resources`` (close-out 20260727-§六 / §十三)
        excludes every binding that rides on a named resource in a
        single task pass — e.g. ``["minimax.shared"]`` removes all
        of ``codex:minimax`` / ``hermes:minimax`` / ``openclaw:minimax``
        / ``opencode:minimax`` / ``claude:minimax`` without touching
        the resource circuit.  It is independent of the live
        cooldown and global registry.

        The returned :class:`ModelFailoverDecision` carries the chosen
        binding_id (if any) plus an explicit action so the caller can
        map engine outputs to monitor / acceptance payloads.
        """
        policy = self._policies.get(tool_id)
        if policy is None:
            return ModelFailoverDecision(
                action="no_candidates",
                reason=f"tool_id {tool_id!r} has no model policy",
            )
        attempts = self._task_attempts.setdefault(task_id, [])
        history = self._task_history.setdefault(task_id, [])
        task_blocked_set = {str(b) for b in (task_blocked_bindings or ())}
        task_blocked_resource_set = {
            str(r) for r in (task_blocked_resources or ())
        }
        attempt_index = len(attempts)
        last_attempt = history[-1] if history else None

        # 0. After a TOOL_ADAPTER / LOCAL_RUNTIME / TASK_INPUT
        # failure, the engine refuses to silently swap models. The
        # tool itself must own the recovery. The caller sees
        # ``no_switch_after_local_failure`` and may decide to retry
        # the same binding (e.g. after fixing the adapter) or fail
        # the task — but the engine will NOT propose a different
        # model binding.
        if last_attempt and not last_attempt.success and \
                last_attempt.failure_scope in (
                    FAILURE_SCOPE_TOOL_ADAPTER,
                    FAILURE_SCOPE_LOCAL_RUNTIME,
                    FAILURE_SCOPE_TASK_INPUT,
                ):
            return ModelFailoverDecision(
                action="no_switch_after_local_failure",
                binding_id=last_attempt.binding_id,
                reason=(f"failure scope {last_attempt.failure_scope} "
                        f"is internal; model swap forbidden"),
                next_attempt_index=attempt_index,
            )

        # 1. Strict model → only one binding is ever eligible.
        if strict_model or policy.strict_model:
            pin = strict_model or policy.strict_model
            pin_binding = self._resolve_strict_binding(policy, pin)
            if pin_binding is None:
                return ModelFailoverDecision(
                    action="strict_violation",
                    reason=f"strict_model {pin!r} does not match any "
                           f"candidate binding for {tool_id!r}",
                )
            if pin_binding.binding_id in attempts:
                return ModelFailoverDecision(
                    action="max_attempts",
                    binding_id=pin_binding.binding_id,
                    reason="strict binding already attempted",
                    next_attempt_index=attempt_index,
                )
            return ModelFailoverDecision(
                action="use",
                binding_id=pin_binding.binding_id,
                next_attempt_index=attempt_index,
            )

        # 2. attempts cap
        if attempt_index >= policy.max_model_attempts:
            return ModelFailoverDecision(
                action="max_attempts",
                reason=(f"policy max_model_attempts={policy.max_model_attempts}"
                        f" reached"),
                next_attempt_index=attempt_index,
            )

        # 3. fallbacks disabled → first attempt only.
        # Note: the caller may pass allow_model_fallback=False to
        # override the policy on a per-task basis; either way, once
        # we are past the first attempt the engine MUST refuse.
        if not (allow_model_fallback and policy.allow_model_fallback):
            if attempt_index > 0:
                return ModelFailoverDecision(
                    action="fallback_disabled",
                    reason="allow_model_fallback=False",
                    binding_id=attempts[-1] if attempts else None,
                    next_attempt_index=attempt_index,
                )
            # Pick the preferred binding once.
            chosen = self._pick_preferred(policy, attempts, role,
                                          preferred_model,
                                          task_blocked_bindings)
            if chosen is None:
                return ModelFailoverDecision(
                    action="no_candidates",
                    reason="no preferred binding eligible",
                    next_attempt_index=attempt_index,
                )
            return ModelFailoverDecision(
                action="use",
                binding_id=chosen,
                next_attempt_index=attempt_index,
            )

        # 4. Walk candidates in declared priority order.
        resource_skip = 0
        binding_skip = 0
        blocked_skip = 0
        blocked_resource_skip = 0
        total_candidates = 0
        for binding_id in policy.candidate_bindings:
            binding = self._bindings.get(binding_id)
            if binding is None:
                continue
            if not binding.enabled:
                continue
            total_candidates += 1
            if role and role not in binding.roles:
                continue
            if not binding.resource_id or not self._resources.get(
                    binding.resource_id):
                continue
            if binding_id in task_blocked_set:
                # Task-local overlay explicitly excludes this binding;
                # count it so callers can see the overlay fired, but
                # do not let it affect the cooldown-based classification.
                blocked_skip += 1
                continue
            if (binding.resource_id
                    and binding.resource_id in task_blocked_resource_set):
                # Close-out 20260727-§六 / §十三: a single
                # ``blocked_resources`` entry excludes EVERY binding
                # that rides on that resource, regardless of the tool.
                # This is the propagation contract that powers the
                # ``blocked_resources=['minimax.shared']`` acceptance
                # case in §十三.
                blocked_resource_skip += 1
                continue
            if binding_id in attempts:
                # Cycle prevention: never pick the same binding twice.
                continue
            if self.is_binding_in_cooldown(binding_id):
                binding_skip += 1
                continue
            if self.is_resource_in_cooldown(binding.resource_id):
                resource_skip += 1
                continue
            # Budget guard — refuse if next attempt would exceed.
            budget_blocked = self._would_exceed_budget(
                policy, task_budget, history)
            if budget_blocked:
                return ModelFailoverDecision(
                    action="budget_blocked",
                    binding_id=binding_id,
                    reason="next attempt would exceed budget",
                    next_attempt_index=attempt_index,
                    budget_blocked=True,
                )
            return ModelFailoverDecision(
                action="use",
                binding_id=binding_id,
                next_attempt_index=attempt_index,
            )
        # Close-out 20260727-§十三: when ``task_blocked_bindings`` or
        # ``task_blocked_resources`` excludes every candidate binding
        # the routing engine MUST surface a no-external-production-route
        # verdict instead of falling back to cooldowns.  No provider
        # call should follow this branch (the hook reports it as a
        # strict_violation that the orchestrator finalises).
        if (blocked_skip + blocked_resource_skip) >= max(1, total_candidates) \
                and total_candidates > 0:
            # We surface the ``no_external_production_route`` shape via
            # ``reason`` rather than ``action`` to keep the action
            # surface in the existing union
            # (``no_candidates`` / ``max_attempts`` / ``exhausted`` /
            # ``skip_binding``) that downstream callers already match.
            if blocked_resource_skip > 0:
                return ModelFailoverDecision(
                    action="exhausted",
                    reason=("task_blocked_resources excludes every binding; "
                           "no External Production Route exists"),
                    next_attempt_index=attempt_index,
                )
            return ModelFailoverDecision(
                action="exhausted",
                reason=("task_blocked_bindings excludes every binding; "
                       "no External Production Route exists"),
                next_attempt_index=attempt_index,
            )
        if resource_skip > 0 and binding_skip == 0 and blocked_skip == 0 \
                and blocked_resource_skip == 0:
            return ModelFailoverDecision(
                action="skip_resource",
                reason=f"all candidates blocked by resource cooldown "
                       f"({resource_skip} binding(s) skipped)",
                next_attempt_index=attempt_index,
            )
        if binding_skip > 0 and resource_skip == 0 and blocked_skip == 0 \
                and blocked_resource_skip == 0:
            return ModelFailoverDecision(
                action="skip_binding",
                reason=f"all candidates blocked by binding cooldown "
                       f"({binding_skip} binding(s) skipped)",
                next_attempt_index=attempt_index,
            )
        if blocked_skip > 0 and resource_skip == 0 and binding_skip == 0 \
                and blocked_resource_skip == 0:
            return ModelFailoverDecision(
                action="skip_binding",
                reason=f"all candidates blocked by task overlay "
                       f"({blocked_skip} binding(s) skipped)",
                next_attempt_index=attempt_index,
            )
        return ModelFailoverDecision(
            action="exhausted",
            reason="every candidate was tried / disabled / cooled",
            next_attempt_index=attempt_index,
        )
        # ``original loop kept below for diff reference`` — the actual
        # candidate walk now uses the duplicate-fallback-free path
        # above; the comments below document why each legacy branch
        # was promoted into a terminal classification.
        # The legacy single-loop body is preserved as a no-op marker
        # block so that callers deep-importing symbols from this
        # module continue to see the new terminal exits only.
        if False:  # pragma: no cover — legacy single-loop mirror
            for binding_id in policy.candidate_bindings:
                binding = self._bindings.get(binding_id)
                if binding is None:
                    continue
                if not binding.enabled:
                    continue
                if role and role not in binding.roles:
                    continue
                if not binding.resource_id or not self._resources.get(
                        binding.resource_id):
                    continue
                if binding_id in task_blocked_set:
                    blocked_skip += 1
                    continue
                if (binding.resource_id
                        and binding.resource_id in task_blocked_resource_set):
                    blocked_resource_skip += 1
                    continue
                if binding_id in attempts:
                    continue
                if self.is_binding_in_cooldown(binding_id):
                    binding_skip += 1
                    continue
                if self.is_resource_in_cooldown(binding.resource_id):
                    resource_skip += 1
                    continue
                budget_blocked = self._would_exceed_budget(
                    policy, task_budget, history)
                if budget_blocked:
                    return ModelFailoverDecision(
                        action="budget_blocked",
                        binding_id=binding_id,
                        reason="next attempt would exceed budget",
                        next_attempt_index=attempt_index,
                        budget_blocked=True,
                    )
                return ModelFailoverDecision(
                    action="use",
                    binding_id=binding_id,
                    next_attempt_index=attempt_index,
                )
        if resource_skip > 0 and binding_skip == 0 and blocked_skip == 0:
            return ModelFailoverDecision(
                action="skip_resource",
                reason=f"all candidates blocked by resource cooldown "
                       f"({resource_skip} binding(s) skipped)",
                next_attempt_index=attempt_index,
            )
        if binding_skip > 0 and resource_skip == 0 and blocked_skip == 0:
            return ModelFailoverDecision(
                action="skip_binding",
                reason=f"all candidates blocked by binding cooldown "
                       f"({binding_skip} binding(s) skipped)",
                next_attempt_index=attempt_index,
            )
        if blocked_skip > 0 and resource_skip == 0 and binding_skip == 0:
            return ModelFailoverDecision(
                action="skip_binding",
                reason=f"all candidates blocked by task overlay "
                       f"({blocked_skip} binding(s) skipped)",
                next_attempt_index=attempt_index,
            )
        return ModelFailoverDecision(
            action="exhausted",
            reason="every candidate was tried / disabled / cooled",
            next_attempt_index=attempt_index,
        )
        resource_skip = 0
        binding_skip = 0
        blocked_skip = 0
        for binding_id in policy.candidate_bindings:
            binding = self._bindings.get(binding_id)
            if binding is None:
                continue
            if not binding.enabled:
                continue
            if role and role not in binding.roles:
                continue
            if not binding.resource_id or not self._resources.get(
                    binding.resource_id):
                continue
            if binding_id in task_blocked_set:
                # Task-local overlay explicitly excludes this binding;
                # count it so callers can see the overlay fired, but
                # do not let it affect the cooldown-based classification.
                blocked_skip += 1
                continue
            if (binding.resource_id
                    and binding.resource_id in task_blocked_resource_set):
                # Close-out 20260727-§六 / §十三: a single
                # ``blocked_resources`` entry excludes EVERY binding
                # that rides on that resource, regardless of the tool.
                # This is the propagation contract that powers the
                # ``blocked_resources=['minimax.shared']`` acceptance
                # case in §十三.
                blocked_skip += 1
                continue
            if binding_id in attempts:
                # Cycle prevention: never pick the same binding twice.
                continue
            if self.is_binding_in_cooldown(binding_id):
                binding_skip += 1
                continue
            if self.is_resource_in_cooldown(binding.resource_id):
                resource_skip += 1
                continue
            # Budget guard — refuse if next attempt would exceed.
            budget_blocked = self._would_exceed_budget(
                policy, task_budget, history)
            if budget_blocked:
                return ModelFailoverDecision(
                    action="budget_blocked",
                    binding_id=binding_id,
                    reason="next attempt would exceed budget",
                    next_attempt_index=attempt_index,
                    budget_blocked=True,
                )
            return ModelFailoverDecision(
                action="use",
                binding_id=binding_id,
                next_attempt_index=attempt_index,
            )
        if resource_skip > 0 and binding_skip == 0 and blocked_skip == 0:
            return ModelFailoverDecision(
                action="skip_resource",
                reason=f"all candidates blocked by resource cooldown "
                       f"({resource_skip} binding(s) skipped)",
                next_attempt_index=attempt_index,
            )
        if binding_skip > 0 and resource_skip == 0 and blocked_skip == 0:
            return ModelFailoverDecision(
                action="skip_binding",
                reason=f"all candidates blocked by binding cooldown "
                       f"({binding_skip} binding(s) skipped)",
                next_attempt_index=attempt_index,
            )
        if blocked_skip > 0 and resource_skip == 0 and binding_skip == 0:
            return ModelFailoverDecision(
                action="skip_binding",
                reason=f"all candidates blocked by task overlay "
                       f"({blocked_skip} binding(s) skipped)",
                next_attempt_index=attempt_index,
            )
        return ModelFailoverDecision(
            action="exhausted",
            reason="every candidate was tried / disabled / cooled",
            next_attempt_index=attempt_index,
        )

    def _resolve_strict_binding(
        self, policy: ToolModelPolicy, pin: str,
    ) -> Optional[ToolModelBinding]:
        # ``pin`` may be a binding id (``hermes:minimax``) or a
        # shorthand (``minimax.shared`` → first binding to that
        # resource).
        for binding_id in policy.candidate_bindings:
            binding = self._bindings.get(binding_id)
            if binding is None:
                continue
            if binding.binding_id == pin or binding.resource_id == pin:
                return binding
        return None

    def _pick_preferred(
        self, policy: ToolModelPolicy, attempts: Sequence[str], role: str,
        preferred_model: Optional[str],
        task_blocked_bindings: Sequence[str] = (),
    ) -> Optional[str]:
        task_blocked_set = {str(b) for b in (task_blocked_bindings or ())}
        order = list(policy.candidate_bindings)
        if preferred_model:
            # Move the preferred binding to the front (still subject
            # to strict_model guard, but the caller didn't set it).
            for binding_id in order:
                binding = self._bindings.get(binding_id)
                if binding is None:
                    continue
                if (binding.binding_id == preferred_model or
                        binding.resource_id == preferred_model):
                    order.remove(binding_id)
                    order.insert(0, binding_id)
                    break
        elif policy.preferred_binding:
            if policy.preferred_binding in order:
                order.remove(policy.preferred_binding)
                order.insert(0, policy.preferred_binding)
        for binding_id in order:
            binding = self._bindings.get(binding_id)
            if binding is None or not binding.enabled:
                continue
            if role and role not in binding.roles:
                continue
            if binding_id in task_blocked_set:
                continue
            if binding_id in attempts:
                continue
            if self.is_binding_in_cooldown(binding_id):
                continue
            if self.is_resource_in_cooldown(binding.resource_id):
                continue
            return binding_id
        return None

    def _would_exceed_budget(
        self,
        policy: ToolModelPolicy,
        task_budget: Optional[Mapping[str, Any]],
        history: Sequence[ModelAttemptRecord],
    ) -> bool:
        if task_budget is None and policy.max_cost is None \
                and policy.max_tokens is None:
            return False
        used_cost = sum(h.actual_cost for h in history)
        used_tokens = sum(h.actual_tokens for h in history)
        # Conservative estimate for the next attempt (mid-sized
        # request) if the caller didn't pass a per-attempt budget.
        est_cost = 0.0
        est_tokens = 0
        if task_budget:
            est_cost = float(task_budget.get("next_estimated_cost", 0.0) or 0.0)
            est_tokens = int(task_budget.get("next_estimated_tokens", 0) or 0)
        max_cost = None
        max_tokens = None
        if task_budget:
            if "max_cost" in task_budget:
                max_cost = float(task_budget["max_cost"])
            if "max_tokens" in task_budget:
                max_tokens = int(task_budget["max_tokens"])
        if max_cost is None:
            max_cost = policy.max_cost
        if max_tokens is None:
            max_tokens = policy.max_tokens
        if max_cost is not None and (used_cost + est_cost) > max_cost:
            return True
        if max_tokens is not None and (used_tokens + est_tokens) > max_tokens:
            return True
        return False

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    def task_history(self, task_id: str) -> List[ModelAttemptRecord]:
        with self._lock:
            return list(self._task_history.get(task_id, []))

    def attempted_bindings(self, task_id: str) -> List[str]:
        with self._lock:
            return list(self._task_attempts.get(task_id, []))

    def actual_model_binding(self, task_id: str) -> Optional[str]:
        """Return the binding of the first successful attempt, if any.

        Used by the lock-on-first-success rule: once a model attempt
        succeeds, the tool MUST keep using that binding for the rest
        of the task unless ``strict_model`` / cooldown forces a swap.
        """
        with self._lock:
            for rec in self._task_history.get(task_id, []):
                if rec.success:
                    return rec.binding_id
        return None

    def lock_actual_model_binding(self, task_id: str) -> Optional[str]:
        """Public alias for the lock-on-first-success lookup.

        Monitor and Acceptance both call this when they need to
        expose ``actual_model_binding``. The function returns the
        binding of the first SUCCESSFUL attempt.
        """
        return self.actual_model_binding(task_id)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "resources": {
                    rid: state.to_dict()
                    for rid, state in self._resource_state.items()
                },
                "bindings": {
                    bid: state.to_dict()
                    for bid, state in self._binding_state.items()
                },
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_ENGINE_SINGLETON: Optional[ModelFailoverEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_default_engine() -> ModelFailoverEngine:
    """Return the process-wide default engine.

    Tests can replace the singleton via :func:`set_default_engine`.
    Production callers should always use this accessor so that all
    subsystems see the same cooldown state.
    """
    global _ENGINE_SINGLETON
    with _ENGINE_LOCK:
        if _ENGINE_SINGLETON is None:
            # Local imports to avoid circulars.
            from aios_model_resources import (
                build_default_binding_registry,
                build_default_policy_registry,
                build_default_resource_registry,
            )
            _ENGINE_SINGLETON = ModelFailoverEngine(
                resource_registry=build_default_resource_registry(),
                binding_registry=build_default_binding_registry(),
                policy_registry=build_default_policy_registry(),
            )
        return _ENGINE_SINGLETON


def set_default_engine(engine: Optional[ModelFailoverEngine]) -> None:
    global _ENGINE_SINGLETON
    with _ENGINE_LOCK:
        _ENGINE_SINGLETON = engine


def reset_default_engine() -> ModelFailoverEngine:
    """Force a fresh engine with default registries.

    The state is NOT wiped across calls; use
    ``engine.reset_state()`` explicitly when you need a clean slate.
    """
    global _ENGINE_SINGLETON
    with _ENGINE_LOCK:
        from aios_model_resources import (
            build_default_binding_registry,
            build_default_policy_registry,
            build_default_resource_registry,
        )
        _ENGINE_SINGLETON = ModelFailoverEngine(
            resource_registry=build_default_resource_registry(),
            binding_registry=build_default_binding_registry(),
            policy_registry=build_default_policy_registry(),
        )
        return _ENGINE_SINGLETON


__all__ = [
    # constants
    "ALLOWED_MODEL_FAILOVER_KINDS",
    "FORBIDDEN_MODEL_FAILOVER_KINDS",
    "DEFAULT_KIND_TO_SCOPE",
    "DEFAULT_RESOURCE_COOLDOWN_SECONDS",
    "DEFAULT_BINDING_COOLDOWN_SECONDS",
    # dataclasses
    "ModelAttemptRecord", "ModelFailoverDecision",
    # engine
    "ModelFailoverEngine",
    # singletons
    "get_default_engine", "set_default_engine", "reset_default_engine",
]