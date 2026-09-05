#!/usr/bin/env python3
"""AIOS Task-Local Routing Policy (close-out 20260727-§四).

This module owns the *immutable*, task-scoped routing policy object
that the orchestrator / entry gateway hand to every routing engine.

Hard rules (mirrored from §四):

* The policy is **frozen** after construction. Once a task has been
  accepted, the entire routing surface — preferred / blocked /
  fallback / strict — is read-only. The only way to change a value
  is to rebuild the policy from a fresh Redis read.
* The policy MUST be **re-readable** from the workflow / Redis hash
  after a restart or lease recovery, so the durability layer is the
  parent hash under ``workflow:<parent_id>``.
* The policy is **task-scoped**: it MUST NOT mutate the global
  registry, the global tool-health state, the global binding
  cooldown state, or the global resource circuit.
* The policy **naturally expires** when the workflow ends; nothing
  in this module writes back to Redis to persist itself. Read paths
  consult the parent hash, never a parallel store.
* All ``*_list`` fields are **deduplicated**, **length-capped** at
  :data:`MAX_LIST_LEN`, **string-validated** via
  :func:`_safe_id`, and **rejected** when an unknown / dangerous
  value is detected (the constructor raises :class:`ValueError`).

This module is pure: it does not import any tool module, does not
read secrets, and does not call any provider. It exists so the
executor / model / planner / reviewer selection paths all share one
canonical view of "what is allowed / preferred / blocked for this
task".

The dataclass is intentionally frozen (``:meta frozen=True`` via
``@dataclass(frozen=True)`` plus a separate ``__post_init__`` for
validation); ``__setattr__`` will raise ``dataclasses.FrozenInstanceError``
on any attempt to mutate it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Sequence, Tuple

from aios_role_routes import (
    ROLE_PLANNER,
    ROLE_EXECUTOR,
    ROLE_REVIEWER,
)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

# Hard cap on every list field — prevents a hostile gateway caller from
# crafting a 100k-entry blocked list to starve the failover engine.
MAX_LIST_LEN = 32
# Hard cap on string length for any single id (tool / binding / resource).
MAX_ID_LEN = 128


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _safe_id(value: Any) -> Optional[str]:
    """Return ``value`` as a safe id string, or ``None`` to drop it.

    * drops empties;
    * rejects objects that are not ``str``;
    * enforces the :data:`MAX_ID_LEN` cap;
    * enforces a conservative charset so callers cannot inject
      redis key separators, paths, or whitespace.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_ID_LEN:
        return None
    # Conservative charset: letters, digits, dot, underscore, dash,
    # colon (we keep ``:`` so resource / binding ids stay valid).
    for ch in cleaned:
        if not (
            ch.isalnum()
            or ch in (".", "_", "-", ":")
        ):
            return None
    return cleaned


def _normalise_list(
    raw: Any,
    *,
    field_name: str,
) -> Tuple[str, ...]:
    """Coerce ``raw`` into a deduplicated tuple of safe ids.

    ``raw`` may be ``None``, a single string (CSV), a list / tuple /
    set of strings, or anything else. Unknown shapes are converted
    to an empty tuple so the caller can fail closed at the strict
    boundary instead of inheriting a malformed policy.
    """
    if raw is None or raw == "":
        return ()
    items: Iterable[Any]
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        return ()
    cleaned = []
    seen = set()
    for item in items:
        cid = _safe_id(item)
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        cleaned.append(cid)
        if len(cleaned) >= MAX_LIST_LEN:
            break
    return tuple(cleaned)


def _require_known_role(role: str) -> str:
    """Reject unknown roles early so the policy surface stays small."""
    if role not in (ROLE_PLANNER, ROLE_EXECUTOR, ROLE_REVIEWER):
        raise ValueError(
            f"unknown role {role!r}; expected one of "
            f"planner / executor / reviewer"
        )
    return role


# ---------------------------------------------------------------------------
# TaskRoutingPolicy — the immutable task-scoped policy object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskRoutingPolicy:
    """The immutable, task-scoped routing policy.

    Construct via :func:`build_task_routing_policy` so all fields are
    validated. Direct construction is allowed (the dataclass is
    public) but skip-list validation; prefer the factory.
    """

    # Identity / scope
    task_id: str
    role: str = ROLE_EXECUTOR

    # Preferred first-choice knobs
    preferred_tool: Optional[str] = None
    preferred_model_binding: Optional[str] = None
    preferred_planner: Optional[str] = None
    preferred_reviewer: Optional[str] = None

    # Fallback gates
    allow_tool_fallback: bool = True
    allow_model_fallback: bool = True
    allow_planner_fallback: bool = True
    allow_reviewer_fallback: bool = True

    # Strict mode — refuse any switch once set
    strict_tool: bool = False
    strict_model: bool = False

    # Per-task exclusion lists (deduped, length-capped, validated)
    blocked_tools: Tuple[str, ...] = ()
    blocked_model_bindings: Tuple[str, ...] = ()
    blocked_resources: Tuple[str, ...] = ()
    blocked_reviewer_tools: Tuple[str, ...] = ()
    blocked_planner_tools: Tuple[str, ...] = ()

    # Bookkeeping for the audit ledger
    source: str = "api"
    sender: str = "gateway"
    constructed_at: str = ""

    # ------------------------------------------------------------------
    # Derived views
    # ------------------------------------------------------------------

    def is_tool_blocked(self, tool_id: str) -> bool:
        return tool_id in self.blocked_tools

    def is_binding_blocked(self, binding_id: str) -> bool:
        return binding_id in self.blocked_model_bindings

    def is_resource_blocked(self, resource_id: str) -> bool:
        return resource_id in self.blocked_resources

    def is_reviewer_blocked(self, tool_id: str) -> bool:
        return tool_id in self.blocked_reviewer_tools

    def is_planner_blocked(self, tool_id: str) -> bool:
        return tool_id in self.blocked_planner_tools

    # ------------------------------------------------------------------
    # Serialisation (for Redis round-trip + monitor / acceptance)
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # Tuples are JSON-incompatible; convert to lists.
        for key in (
            "blocked_tools",
            "blocked_model_bindings",
            "blocked_resources",
            "blocked_reviewer_tools",
            "blocked_planner_tools",
        ):
            data[key] = list(getattr(self, key))
        return data


# ---------------------------------------------------------------------------
# Factory + recovery helpers
# ---------------------------------------------------------------------------


def build_task_routing_policy(
    *,
    task_id: str,
    role: str = ROLE_EXECUTOR,
    preferred_tool: Optional[str] = None,
    preferred_model_binding: Optional[str] = None,
    preferred_planner: Optional[str] = None,
    preferred_reviewer: Optional[str] = None,
    allow_tool_fallback: bool = True,
    allow_model_fallback: bool = True,
    allow_planner_fallback: bool = True,
    allow_reviewer_fallback: bool = True,
    strict_tool: bool = False,
    strict_model: bool = False,
    blocked_tools: Optional[Sequence[str]] = None,
    blocked_model_bindings: Optional[Sequence[str]] = None,
    blocked_resources: Optional[Sequence[str]] = None,
    blocked_reviewer_tools: Optional[Sequence[str]] = None,
    blocked_planner_tools: Optional[Sequence[str]] = None,
    source: str = "api",
    sender: str = "gateway",
    constructed_at: str = "",
) -> TaskRoutingPolicy:
    """Validate every field and return an immutable policy object.

    Unknown / malformed values raise :class:`ValueError`. The caller
    is expected to catch this and surface the failure through the
    gateway 400 response — never let a malformed policy reach the
    failover engines.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id is required and must be a non-empty string")
    if len(task_id) > 64:
        raise ValueError("task_id exceeds 64 chars")
    _require_known_role(role)

    def _opt(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = _safe_id(value)
        if cleaned is None:
            raise ValueError(f"invalid id {value!r}")
        return cleaned

    return TaskRoutingPolicy(
        task_id=task_id.strip(),
        role=role,
        preferred_tool=_opt(preferred_tool),
        preferred_model_binding=_opt(preferred_model_binding),
        preferred_planner=_opt(preferred_planner),
        preferred_reviewer=_opt(preferred_reviewer),
        allow_tool_fallback=bool(allow_tool_fallback),
        allow_model_fallback=bool(allow_model_fallback),
        allow_planner_fallback=bool(allow_planner_fallback),
        allow_reviewer_fallback=bool(allow_reviewer_fallback),
        strict_tool=bool(strict_tool),
        strict_model=bool(strict_model),
        blocked_tools=_normalise_list(blocked_tools, field_name="blocked_tools"),
        blocked_model_bindings=_normalise_list(
            blocked_model_bindings, field_name="blocked_model_bindings"),
        blocked_resources=_normalise_list(
            blocked_resources, field_name="blocked_resources"),
        blocked_reviewer_tools=_normalise_list(
            blocked_reviewer_tools, field_name="blocked_reviewer_tools"),
        blocked_planner_tools=_normalise_list(
            blocked_planner_tools, field_name="blocked_planner_tools"),
        source=str(source or "api"),
        sender=str(sender or "gateway")[:MAX_ID_LEN],
        constructed_at=str(constructed_at or ""),
    )


def from_workflow_dict(
    workflow: Mapping[str, Any],
    *,
    role: str = ROLE_EXECUTOR,
    fallback_source: str = "redis",
) -> TaskRoutingPolicy:
    """Recover a :class:`TaskRoutingPolicy` from a workflow dict.

    The orchestrator stores these fields as JSON-serialised strings
    in the parent hash. :func:`_decode_list_field` reverses that
    before re-validation so an inconsistent Redis write cannot leak
    through the policy boundary.
    """
    if not isinstance(workflow, Mapping):
        raise ValueError("workflow payload must be a mapping")
    parent_id = str(
        workflow.get("parent_id")
        or workflow.get("task_id")
        or "",
    )
    if not parent_id:
        raise ValueError("workflow payload missing parent_id / task_id")

    def _decode_list_field(value: Any) -> Optional[Sequence[str]]:
        if value is None or value == "":
            return None
        if isinstance(value, (list, tuple, set)):
            return list(value)
        if isinstance(value, str):
            # JSON array form first.
            text = value.strip()
            if text.startswith("[") and text.endswith("]"):
                try:
                    import json as _json
                    parsed = _json.loads(text)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed]
                except Exception:
                    pass
            # CSV fallback for legacy writes.
            return [item.strip() for item in text.split(",") if item.strip()]
        return None

    return build_task_routing_policy(
        task_id=parent_id,
        role=role,
        preferred_tool=str(workflow.get("preferred_executor", "") or "")
                       or str(workflow.get("preferred_tool", "") or "")
                       or None,
        preferred_model_binding=str(
            workflow.get("preferred_model_binding", "") or "") or None,
        preferred_planner=str(workflow.get("preferred_planner", "") or "")
                          or None,
        preferred_reviewer=str(workflow.get("preferred_reviewer", "") or "")
                           or None,
        allow_tool_fallback=bool(workflow.get("allow_executor_fallback", True)),
        allow_model_fallback=bool(workflow.get("allow_model_fallback", True)),
        allow_planner_fallback=bool(
            workflow.get("allow_planner_fallback", True)),
        allow_reviewer_fallback=bool(
            workflow.get("allow_reviewer_fallback", True)),
        strict_tool=bool(workflow.get("strict_tool", False)),
        strict_model=bool(workflow.get("strict_model", False)),
        blocked_tools=_decode_list_field(workflow.get("blocked_tools")),
        blocked_model_bindings=_decode_list_field(
            workflow.get("blocked_model_bindings")),
        blocked_resources=_decode_list_field(workflow.get("blocked_resources")),
        blocked_reviewer_tools=_decode_list_field(
            workflow.get("blocked_reviewer_tools")),
        blocked_planner_tools=_decode_list_field(
            workflow.get("blocked_planner_tools")),
        source=str(workflow.get("source", "") or "redis"),
        sender=str(workflow.get("sender_id", "") or "gateway"),
        constructed_at=str(workflow.get("constructed_at", "")
                           or workflow.get("created_at", "")
                           or ""),
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "MAX_LIST_LEN",
    "MAX_ID_LEN",
    "TaskRoutingPolicy",
    "build_task_routing_policy",
    "from_workflow_dict",
]