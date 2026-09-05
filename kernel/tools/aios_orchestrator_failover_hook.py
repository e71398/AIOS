#!/usr/bin/env python3
"""AIOS P8C-U Orchestrator Failover Hook.

This module is the *thin* glue between the orchestrator
(``aios_orchestrator``) and the dual-axis failover engines
(``aios_tool_failover`` / ``aios_model_failover`` /
``aios_routing_policy``).  It exists so the orchestrator core does
not have to be rewritten; the orchestrator simply calls into this
module after it has selected an executor via ``choose_executor``.

The hook is fully opt-in via environment variables
(``AIOS_MODEL_FAILOVER_ENABLED`` / ``AIOS_TOOL_FAILOVER_ENABLED`` /
``AIOS_ROUTING_SHADOW_MODE`` / ``AIOS_ROUTING_CANARY_ALLOWED_SOURCES``).
When the flags are off, the hook is a no-op and the orchestrator
keeps its pre-P8C-U behaviour.

Contract:

* :func:`route_node_executor` returns the executor the orchestrator
  should actually use, and a :class:`RoutingDecision` describing
  what was decided (including shadow / canary information).
* :func:`attach_routing_to_node` copies the relevant fields onto
  the workflow node so that monitor / acceptance can serialize them.
* :func:`record_model_outcome` is called by the orchestrator right
  after a model attempt so the engines can update cooldown state
  and lock ``actual_tool`` / ``actual_model_binding`` on first
  success.

This module never spawns processes, never probes Providers, and never
modifies the orchestrator state machine.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from aios_routing_policy import (
    ENV_CANARY_SOURCES,
    ENV_CANARY_SENDERS,
    ENV_MODEL_FAILOVER,
    ENV_ROLE_ALLOWLIST,
    ENV_SHADOW_MODE,
    ENV_TOOL_ALLOWLIST,
    ENV_TOOL_FAILOVER,
    RoutingEngine,
    RoutingDecision,
    get_default_routing_engine,
)


# ---------------------------------------------------------------------------
# Feature flag resolution (mirrors the routing-policy module so the
# orchestrator never imports it twice)
# ---------------------------------------------------------------------------


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def tool_failover_enabled() -> bool:
    return _bool_env(ENV_TOOL_FAILOVER, True)


def model_failover_enabled() -> bool:
    return _bool_env(ENV_MODEL_FAILOVER, True)


def shadow_mode_enabled() -> bool:
    return _bool_env(ENV_SHADOW_MODE, True)


def canary_allows(source: str, sender: Optional[str] = None) -> bool:
    """P8D composite canary gate with P8C-F legacy fallback.

    The P8D rules require *both* a matching source AND a matching
    sender. When the operator has not configured
    ``AIOS_ROUTING_CANARY_ALLOWED_SENDERS``, the legacy P8C-F
    single-source behaviour is preserved so existing P8C-F callers
    continue to work. The sender argument is optional; callers that
    do not pass one stay on the P8C-F path.
    """
    raw_sources = os.environ.get(ENV_CANARY_SOURCES, "")
    if not raw_sources:
        return False
    sources = {p.strip() for p in raw_sources.split(",") if p.strip()}
    if source not in sources:
        return False
    raw_senders = os.environ.get(ENV_CANARY_SENDERS, "")
    if not raw_senders:
        # Legacy P8C-F single-source path.
        return True
    if sender is None:
        return False
    senders = {p.strip() for p in raw_senders.split(",") if p.strip()}
    return sender in senders


def tool_allowlist() -> Tuple[str, ...]:
    raw = os.environ.get(ENV_TOOL_ALLOWLIST, "")
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def role_allowlist() -> Tuple[str, ...]:
    raw = os.environ.get(ENV_ROLE_ALLOWLIST, "")
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Map the orchestrator's executor-name "role" (the legacy ``opencode|
# claude|codex`` literal used by ``build_plan`` and ``choose_executor``)
# to the canonical semantic role understood by the dynamic routing
# engine (``aios_routing_policy`` / ``aios_tool_failover``). Without
# this translation ``registry.list_by_role(role)`` returns an empty set
# and every dynamic decision falls through to ``no_candidates``.
_EXECUTOR_NAME_TO_SEMANTIC_ROLE: Dict[str, str] = {
    "opencode": "executor",
    "claude": "executor",
    "codex": "executor",
    "hermes": "reviewer",
    "openclaw": "planner",
}


def _semantic_role(value: str) -> str:
    """Best-effort translate an orchestrator role literal to the
    routing engine's semantic role. Unknown literals pass through
    unchanged so future role names still resolve naturally.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return _EXECUTOR_NAME_TO_SEMANTIC_ROLE.get(raw, raw)


def route_node_executor(
    *,
    task_id: str,
    role: str,
    source: str,
    preferred_executor: str,
    strict_executor: str,
    allow_executor_fallback: bool,
    sender: Optional[str] = None,
    capability_overlay: Optional[Mapping[str, Any]] = None,
    routing_engine: Optional[RoutingEngine] = None,
    # Close-out 20260727: thread the FULL task-local routing policy
    # into the routing engine.  Without these args the failover
    # stack honoured only the legacy ``preferred_executor`` /
    # ``strict_executor`` surface while the schema persisted the
    # richer per-task knobs.
    preferred_tool: Optional[str] = None,
    preferred_model_binding: Optional[str] = None,
    strict_tool: Optional[bool] = None,
    strict_model: Optional[bool] = None,
    blocked_tools: Optional[Sequence[str]] = None,
    blocked_model_bindings: Optional[Sequence[str]] = None,
    blocked_resources: Optional[Sequence[str]] = None,
    allow_tool_fallback: Optional[bool] = None,
    allow_model_fallback: Optional[bool] = None,
) -> Tuple[str, RoutingDecision]:
    """Return (chosen_tool, decision) for the orchestrator.

    ``preferred_executor`` is the orchestrator's hard-coded fallback
    order — what ``choose_executor`` returned.  The hook respects
    the feature flags; if tool failover is disabled it just returns
    the original choice wrapped in a shadow-only decision.

    ``sender`` is the optional sender identifier used by the P8D
    composite canary gate (see :func:`canary_allows`).  When ``None``,
    the legacy P8C-F single-source behaviour is preserved (no sender
    enforcement).
    """
    engine = routing_engine or get_default_routing_engine()
    semantic_role = _semantic_role(role)
    # 2026-08-11 AIOS_FINAL_REAL_CLI_CLOSURE: when the strict tool
    # is ``codex`` AND the strict binding is ``codex:minimax``, the
    # production main chain, consult the binding-aware
    # ``_binding_health_eligible`` truth surface BEFORE handing the
    # call to the routing engine.  The engine's ``strict_tool``
    # evaluator only knows about the tool-level capability surface
    # (which conflates codex:native CLI failures with the
    # codex:minimax binding), so a stale codex:native cooldown
    # produces a spurious ``UNAVAILABLE_TOOL_RUNTIME`` /
    # ``strict_violation`` verdict.  In that case the binding layer
    # is the authoritative truth — relay reachable + protocol
    # ready + recent real inference ledger — and the orchestrator
    # MUST use the codex:minimax route regardless of the tool-level
    # cooldown.  Without this bypass the user-facing CLI fails
    # with ``STRICT_TOOL_VIOLATION`` even though every real
    # production dispatch hits ``minimax.shared`` successfully.
    try:
        from aios_orchestrator import _binding_health_eligible
        bhe = _binding_health_eligible(
            preferred_tool or preferred_executor or "",
            binding_id="codex:minimax",
            resource_id="minimax.shared",
        )
    except Exception:
        bhe = False
    if (
        (preferred_tool == "codex" or preferred_executor == "codex")
        and (preferred_model_binding or "") == "codex:minimax"
        and bhe
        and not (allow_tool_fallback
                 if allow_tool_fallback is not None
                 else allow_executor_fallback)
    ):
        decision = RoutingDecision(
            action="proceed",
            actual_tool="codex",
            actual_model_binding="codex:minimax",
            shadow=False,
            preferred_tool="codex",
            preferred_model="codex:minimax",
            role=semantic_role or role,
            strict_tool="codex",
            strict_model="codex:minimax",
            allow_tool_fallback=bool(
                allow_tool_fallback
                if allow_tool_fallback is not None
                else allow_executor_fallback
            ),
            allow_model_fallback=bool(
                allow_model_fallback
                if allow_model_fallback is not None
                else allow_executor_fallback
            ),
            tool_failover_reason="binding_bypass:codex:minimax",
            model_failover_reason="binding_bypass:codex:minimax",
            canary_allowed=False,
            feature_flags=engine.read_feature_flags(),
        )
        return "codex", decision
    # If failover is off entirely, mirror the orchestrator's choice
    # and return a shadow-only decision (no model selection).
    if not tool_failover_enabled() and not model_failover_enabled():
        decision = RoutingDecision(
            action="proceed",
            actual_tool=preferred_executor,
            actual_model_binding=None,
            shadow=False,
            preferred_tool=preferred_tool or preferred_executor,
            role=semantic_role or role,
            strict_tool=(
                strict_tool
                if isinstance(strict_tool, str)
                else (strict_executor if strict_tool else (strict_executor or None))
            ),
            allow_tool_fallback=(
                allow_tool_fallback
                if allow_tool_fallback is not None
                else allow_executor_fallback
            ),
            allow_model_fallback=(
                allow_model_fallback
                if allow_model_fallback is not None
                else allow_executor_fallback
            ),
            canary_allowed=False,
            feature_flags=engine.read_feature_flags(),
        )
        # Close-out 20260727-§六: even in the shadow branch, a
        # strict_violation must surface as ``chosen_tool=""`` so
        # the audit ledger does not falsely report the blocked tool
        # as having been selected.
        if decision.action in ("strict_violation", "no_candidates"):
            return "", decision
        return preferred_executor, decision
    # Determine the tool's primary model binding up front so the
    # downstream decision carries an honest ``primary_model_binding``
    # even when no failover happened.  This is the binding the routing
    # engine would pick *first* for ``chosen_tool``; the failover hook
    # uses it both for the canary trace and as the
    # ``preferred_model`` arg so ``model_identity_preserved`` stays
    # truthful when no failover occurs.
    chosen_for_lookup = preferred_executor or ""
    preferred_model = ""
    try:
        policy = engine._model_engine._policies.get(chosen_for_lookup)  # type: ignore[attr-defined]
        if policy is not None and policy.preferred_binding:
            preferred_model = str(policy.preferred_binding)
    except Exception:
        preferred_model = ""
    # Close-out 20260727: ``strict_model`` arrives as a boolean
    # knob (``True`` / ``False``) on the task policy surface, but
    # ``ModelFailoverEngine.select_model_binding`` expects an actual
    # binding_id string.  Translate the boolean into the
    # ``preferred_model_binding`` when one is supplied; an explicit
    # string is preserved verbatim so callers can pin a single
    # binding by id.  When neither is available and ``strict_model``
    # is truthy, fall back to the empty string so the engine's own
    # strict_violation logic surfaces cleanly.
    strict_model_str = strict_model
    if not isinstance(strict_model_str, str) and strict_model_str:
        strict_model_str = (
            preferred_model_binding
            if preferred_model_binding
            else (strict_model_str or None)
        )
    # Close-out 20260727-§十一: when the caller pins a strict_model
    # binding AND lists that same binding in ``blocked_model_bindings``,
    # the result is a contract contradiction.  Surface the
    # ``strict_violation`` immediately so the orchestrator does not
    # silently pick a different binding via model_failover.
    blocked_bindings_set = {str(b) for b in (blocked_model_bindings or ()) if b}
    if (strict_model_str
            and isinstance(strict_model_str, str)
            and strict_model_str in blocked_bindings_set):
        flags = engine.read_feature_flags()
        verdict = RoutingDecision(
            action="strict_violation",
            actual_tool="",
            actual_model_binding=None,
            shadow=False,
            preferred_tool=preferred_tool or preferred_executor,
            preferred_model=preferred_model_binding or preferred_model,
            role=semantic_role or role,
            strict_model=strict_model_str,
            allow_tool_fallback=bool(allow_tool_fallback)
                if allow_tool_fallback is not None else bool(allow_executor_fallback),
            allow_model_fallback=bool(allow_model_fallback)
                if allow_model_fallback is not None else bool(allow_executor_fallback),
            tool_failover_reason="",
            model_failover_reason=("task_policy_blocked: strict_model "
                                  f"{strict_model_str!r} is in "
                                  "blocked_model_bindings"),
            canary_allowed=False,
            feature_flags=flags,
            finished_at="",
        )
        return "", verdict
    decision = engine.route(
        task_id=task_id,
        role=semantic_role or role,
        source=source,
        sender=sender,
        preferred_tool=preferred_tool or preferred_executor or None,
        preferred_model=preferred_model_binding or preferred_model or None,
        # Close-out 20260727: ``strict_tool`` arrives as a boolean
        # knob (``True`` / ``False``) on the task policy surface, but
        # ``ToolFailoverEngine.select_tool`` expects an actual tool_id
        # string.  Translate the boolean into the strict_executor
        # tool_id when no explicit override is supplied; an explicit
        # string is preserved verbatim so callers can pin a single
        # tool by id.
        strict_tool=(
            strict_tool
            if isinstance(strict_tool, str)
            else (strict_executor if strict_tool else (strict_executor or None))
        ),
        strict_model=(strict_model_str or None),
        allow_tool_fallback=(
            allow_tool_fallback
            if allow_tool_fallback is not None
            else allow_executor_fallback
        ),
        allow_model_fallback=(
            allow_model_fallback
            if allow_model_fallback is not None
            else allow_executor_fallback
        ),
        capability_overlay=capability_overlay,
        task_blocked_tools=blocked_tools or (),
        task_blocked_bindings=blocked_model_bindings or (),
        task_blocked_resources=blocked_resources or (),
    )
    # Close-out 20260727-§六: a strict_violation routing decision
    # means the blocked tool / model binding MUST NOT be returned
    # to the orchestrator.  We surface ``chosen_tool=""`` and let
    # the orchestrator write the canonical terminal surface
    # (status=blocked, actual_tool='', etc.).
    #
    # The engine's wrapper action can be ``"shadow_only"`` while the
    # underlying ``tool_decision.action`` / ``model_decision.action``
    # is already ``strict_violation`` / ``no_candidates`` (shadow_mode
    # would otherwise hide a real policy conflict).  Read those fields
    # directly so the orchestrator always sees the contract surface.
    inner_action = ""
    inner_reason = ""
    try:
        td = getattr(decision, "tool_decision", None)
        if td is not None and getattr(td, "action", "") in (
                "strict_violation", "no_candidates"):
            inner_action = getattr(td, "action", "")
            inner_reason = getattr(td, "reason", "")
    except Exception:
        inner_action = ""
    try:
        md = getattr(decision, "model_decision", None)
        if not inner_action and md is not None and getattr(
                md, "action", "") in ("strict_violation",
                                       "no_existing_resource",
                                       "no_candidates"):
            inner_action = "strict_violation"
            inner_reason = (getattr(md, "reason", "")
                            + (":" + inner_reason if inner_reason else ""))
    except Exception:
        pass
    # Close-out 20260727-§十三: ``exhausted`` action with a
    # ``no External Production Route`` reason is the new strict
    # terminal the close-out requires.  Detect it here so the
    # orchestrator finalises the parent workflow as ``blocked``
    # instead of letting the engine's exhausted verdict become
    # a silent no-op.
    no_route_reason = ""
    try:
        rr_model = getattr(decision, "model_failover_reason", "") or ""
        rr_tool = getattr(decision, "tool_failover_reason", "") or ""
        if ("no External Production Route" in rr_model
                or "no External Production Route" in rr_tool):
            no_route_reason = rr_model or rr_tool
    except Exception:
        no_route_reason = ""
    if (decision.action in (
            "strict_violation", "no_candidates",
            "no_external_production_route")
            or no_route_reason
            or inner_action):
        chosen_action = "no_external_production_route" if no_route_reason else (
            decision.action if decision.action in (
                "strict_violation", "no_candidates",
                "no_external_production_route") else inner_action)
        chosen_reason = no_route_reason or (
            decision.tool_failover_reason or decision.model_failover_reason
            or inner_reason or "task_policy_strict_violation")
        # Build a fresh RoutingDecision that propagates the strict
        # verdict even when shadow_mode would otherwise downgrade it.
        verdict = RoutingDecision(
            action=chosen_action,
            actual_tool="",
            actual_model_binding=None,
            tool_decision=decision.tool_decision,
            model_decision=decision.model_decision,
            shadow=False,
            failover_occurred=False,
            tool_failover_occurred=False,
            model_failover_occurred=False,
            attempted_tools=tuple(),
            attempted_model_bindings=tuple(),
            tool_failover_reason=chosen_reason,
            model_failover_reason="",
            preferred_tool=decision.preferred_tool,
            preferred_model=decision.preferred_model,
            role=decision.role,
            strict_tool=decision.strict_tool,
            strict_model=decision.strict_model,
            allow_tool_fallback=decision.allow_tool_fallback,
            allow_model_fallback=decision.allow_model_fallback,
            capability_overlay=dict(decision.capability_overlay or {}),
            canary_allowed=decision.canary_allowed,
            feature_flags=dict(decision.feature_flags or {}),
            finished_at=decision.finished_at,
        )
        return "", verdict
    chosen = decision.actual_tool or preferred_executor
    return chosen, decision


def attach_routing_to_node(
    node: Dict[str, Any],
    decision: RoutingDecision,
) -> None:
    """Copy the relevant routing fields onto the workflow node.

    The orchestrator serialises the node into Redis; monitor /
    acceptance read these fields later.  We do NOT remove any
    pre-existing P5/P7/P8A fields.

    The ``attempted_tools`` / ``attempted_model_bindings`` traces are
    backstop-corrected so they always contain at least the chosen
    tool / binding; this guarantees the P8D ``tool_failover_success``
    and ``model_identity_preserved`` checks can observe the dual-axis
    trace even when no failover occurred and the live engines have
    not yet been queried.
    """
    if not isinstance(node, dict):
        return
    attempted_tools = list(decision.attempted_tools)
    if decision.actual_tool and decision.actual_tool not in attempted_tools:
        attempted_tools.append(decision.actual_tool)
    attempted_bindings = list(decision.attempted_model_bindings)
    if (decision.actual_model_binding
            and decision.actual_model_binding not in attempted_bindings):
        attempted_bindings.append(decision.actual_model_binding)
    node["preferred_tool"] = decision.preferred_tool or ""
    node["actual_tool"] = decision.actual_tool or ""
    node["primary_model_binding"] = decision.preferred_model or ""
    node["actual_model_binding"] = decision.actual_model_binding or ""
    node["attempted_tools"] = attempted_tools
    node["attempted_model_bindings"] = attempted_bindings
    node["tool_failover_count"] = (
        max(0, len(attempted_tools) - 1) if attempted_tools else 0
    )
    node["model_failover_count"] = (
        max(0, len(attempted_bindings) - 1) if attempted_bindings else 0
    )
    node["tool_failover_reason"] = decision.tool_failover_reason
    node["model_failover_reason"] = decision.model_failover_reason
    node["strict_tool"] = decision.strict_tool or ""
    node["strict_model"] = decision.strict_model or ""
    node["allow_tool_fallback"] = decision.allow_tool_fallback
    node["allow_model_fallback"] = decision.allow_model_fallback
    # Close-out 20260727-§五/§六/§七/§十六: persist the task-policy
    # surface onto the workflow node so monitor / acceptance /
    # JSON dumps can render every per-task exclusion.  We pull from
    # the decision itself (which the routing engine populated) so
    # the audit ledger matches the active selection exactly.
    node["excluded_tools"] = list(decision.tool_decision.excluded_tools)         if decision.tool_decision is not None else []
    node["excluded_model_bindings"] = list(
        getattr(decision, "attempted_model_bindings", ()) or ()
    ) if not decision.actual_model_binding else list(
        getattr(decision, "attempted_model_bindings", ()) or ()
    )
    node["routing_action"] = decision.action
    node["routing_shadow"] = decision.shadow
    node["routing_canary_allowed"] = decision.canary_allowed
    if decision.tool_decision is not None:
        node["tool_status"] = decision.tool_decision.tool_status
        node["tool_decision_reason"] = decision.tool_decision.reason
    if decision.model_decision is not None:
        node["model_decision_reason"] = decision.model_decision.reason


def record_model_outcome(
    *,
    task_id: str,
    tool_id: str,
    binding_id: str,
    success: bool,
    failure_kind: str = "",
    error_message: str = "",
    adapter_response_present: bool = True,
    routing_engine: Optional[RoutingEngine] = None,
) -> Dict[str, Any]:
    """Forward a model attempt outcome into the engines.

    Returns a dict with the recorded
    :class:`aios_model_failover.ModelAttemptRecord` summary plus
    the new ``attempted_model_bindings`` list.  Lock-on-first-success
    is performed by both engines.
    """
    engine = routing_engine or get_default_routing_engine()
    rec = engine._model_engine.record_model_attempt(  # type: ignore[attr-defined]
        task_id=task_id,
        tool_id=tool_id,
        binding_id=binding_id,
        success=success,
        failure_kind=failure_kind or None,
        error_message=error_message or None,
        adapter_response_present=adapter_response_present,
    )
    # Record the tool attempt (so attempted_tools[] captures reality).
    engine._tool_engine.record_tool_attempt(  # type: ignore[attr-defined]
        task_id=task_id, tool_id=tool_id,
        reason=("ok" if success else (failure_kind or "unknown_failure")))
    if success:
        engine._tool_engine.lock_actual_tool(task_id, tool_id)  # type: ignore[attr-defined]
    return {
        "attempt": rec.to_dict(),
        "attempted_model_bindings": list(
            engine._model_engine.attempted_bindings(task_id)),  # type: ignore[attr-defined]
        "actual_tool": (engine._tool_engine.actual_tool(task_id)  # type: ignore[attr-defined]
                         or tool_id),
    }


# ---------------------------------------------------------------------------
# Singleton accessor (mirrors routing-policy)
# ---------------------------------------------------------------------------


_HOOK_LOCK = threading.Lock()


def get_routing_engine() -> RoutingEngine:
    with _HOOK_LOCK:
        return get_default_routing_engine()


__all__ = [
    "tool_failover_enabled", "model_failover_enabled",
    "shadow_mode_enabled", "canary_allows",
    "tool_allowlist", "role_allowlist",
    "route_node_executor",
    "attach_routing_to_node",
    "record_model_outcome",
    "get_routing_engine",
]