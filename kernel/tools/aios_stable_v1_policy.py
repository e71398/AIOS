#!/usr/bin/env python3
"""AIOS STABLE_V1 routing policy — single source of truth for CLI defaults.

This module is the SINGLE place that owns the STABLE_V1 production
routing defaults used by the four production CLI commands:

    ./aios task
    ./aios audit
    ./aios ops
    ./aios code

Contract (enforced 2026-08-11, AIOS_FINAL_REAL_CLI_CLOSURE — role-specialized
addendum 2026-08-12, AIOS_EXECUTOR_TOPOLOGY_CORRECTION):

    planner        = openclaw
    allow_planner_fallback = False

    reviewer       = hermes
    allow_reviewer_fallback = True

    Executor per profile (restored from config/ai_registry.json
    baseline roles — opencode=primary_general_executor,
    codex=specialist_code_batch):

        GENERAL:  primary=opencode  binding=opencode:free-auto-router
                  fallback=codex    fallback_binding=codex:minimax
                  allow_executor_fallback=True   strict_tool=False
        AUDIT:    same as GENERAL
        OPS:      same as GENERAL
        CODE:     primary=codex     binding=codex:minimax
                  strict_executor=codex  strict_tool=True
                  allow_executor_fallback=False

    The CODE profile keeps the legacy strict-mode contract because
    codex is the canonical ``specialist_code_batch`` tool.  GENERAL /
    AUDIT / OPS default to the original opencode-first role; when
    opencode is unhealthy at runtime, the existing bounded tool
    fallback (``choose_executor`` walks ``(opencode, claude, codex)``
    for requested_role="opencode") routes the task to the next
    healthy executor with codex:minimax as the model binding.  When
    opencode recovers, the next task automatically returns to the
    opencode primary path — no user reconfiguration needed.

Profiles (GENERAL / AUDIT / OPS / CODE) may differ in:

    * prompt templates
    * write/read policy
    * project directory hints
    * verification criteria
    * primary executor / binding / fallback policy

The role-specialized routing surface is returned by
``executor_policy_for_profile(profile)``.  Profile definitions merge
*under* the planner + reviewer + executor surfaces when
constructing a payload.
"""
from __future__ import annotations

from typing import Any, Dict

# ---------------------------------------------------------------------------
# STABLE_V1 production routing — immutable per session, here as module const.
# ---------------------------------------------------------------------------

STABLE_V1_PLANNER: str = "openclaw"
STABLE_V1_ALLOW_PLANNER_FALLBACK: bool = False

STABLE_V1_REVIEWER: str = "hermes"
STABLE_V1_ALLOW_REVIEWER_FALLBACK: bool = True

# CODE-profile executor constants (the specialist_code_batch path).
STABLE_V1_CODE_EXECUTOR: str = "codex"
STABLE_V1_CODE_STRICT_EXECUTOR: str = "codex"
STABLE_V1_CODE_ALLOW_EXECUTOR_FALLBACK: bool = False
STABLE_V1_CODE_STRICT_TOOL: bool = True
STABLE_V1_CODE_BINDING: str = "codex:minimax"

# GENERAL / AUDIT / OPS executor constants (the primary_general_executor path).
STABLE_V1_GENERAL_EXECUTOR: str = "opencode"
STABLE_V1_GENERAL_BINDING: str = "opencode:free-auto-router"
STABLE_V1_GENERAL_STRICT_EXECUTOR: str = ""
STABLE_V1_GENERAL_ALLOW_EXECUTOR_FALLBACK: bool = True
STABLE_V1_GENERAL_STRICT_TOOL: bool = False
# Bounded tool fallback for GENERAL / AUDIT / OPS: when opencode is
# unhealthy, the existing ``choose_executor(role="opencode")`` walks
# ``(opencode, claude, codex)`` — the fallback executor below is the
# one chosen once both opencode and claude fail (i.e. the realistic
# single-provider state observed in 2026-08-10 production readiness).
STABLE_V1_GENERAL_FALLBACK_EXECUTOR: str = "codex"
STABLE_V1_GENERAL_FALLBACK_BINDING: str = "codex:minimax"

# Legacy aliases (kept for external imports; downstream code MAY still
# import the names but must consult ``executor_policy_for_profile``
# for the role-specialized view).
STABLE_V1_EXECUTOR: str = STABLE_V1_CODE_EXECUTOR
STABLE_V1_STRICT_EXECUTOR: str = STABLE_V1_CODE_STRICT_EXECUTOR
STABLE_V1_ALLOW_EXECUTOR_FALLBACK: bool = STABLE_V1_CODE_ALLOW_EXECUTOR_FALLBACK
STABLE_V1_STRICT_TOOL: bool = STABLE_V1_CODE_STRICT_TOOL
STABLE_V1_BINDING: str = STABLE_V1_CODE_BINDING


def executor_policy_for_profile(profile: str) -> Dict[str, Any]:
    """Return the role-specialized executor routing surface for ``profile``.

    The four production profiles split into two role groups:

    * GENERAL / AUDIT / OPS — the ``primary_general_executor``
      opencode path with bounded tool fallback to codex:minimax.
    * CODE — the ``specialist_code_batch`` codex path with the
      strict-mode gate preserved (no executor fallback).

    Returned keys mirror the production workflow surface::

        {
            "preferred_executor": str,
            "preferred_model_binding": str,
            "strict_executor": str,
            "allow_executor_fallback": bool,
            "strict_tool": bool,
            "fallback_executor": str,            # best-effort; "" when None
            "fallback_model_binding": str,
            "fallback_chain": Tuple[str, ...],   # requested-role order
        }

    ``fallback_chain`` is the ``choose_executor`` order consulted when
    the primary is unhealthy.  Profiles that intentionally do NOT
    allow a fallback (CODE) return ``()`` so the consumer can
    distinguish "no fallback" from "fallback to the same primary".
    """
    profile_norm = str(profile or "").upper()
    if profile_norm in ("GENERAL", "AUDIT", "OPS"):
        return {
            "preferred_executor": STABLE_V1_GENERAL_EXECUTOR,
            "preferred_model_binding": STABLE_V1_GENERAL_BINDING,
            "strict_executor": STABLE_V1_GENERAL_STRICT_EXECUTOR,
            "allow_executor_fallback": STABLE_V1_GENERAL_ALLOW_EXECUTOR_FALLBACK,
            "strict_tool": STABLE_V1_GENERAL_STRICT_TOOL,
            "fallback_executor": STABLE_V1_GENERAL_FALLBACK_EXECUTOR,
            "fallback_model_binding": STABLE_V1_GENERAL_FALLBACK_BINDING,
            # choose_executor(requested_role="opencode") walks this
            # order.  When opencode is healthy the primary is picked;
            # otherwise the next available tool is selected.  This is
            # the EXISTING production fallback machinery — no new
            # router, no extra layer.
            "fallback_chain": ("opencode", "claude", "codex"),
        }
    if profile_norm == "CODE":
        return {
            "preferred_executor": STABLE_V1_CODE_EXECUTOR,
            "preferred_model_binding": STABLE_V1_CODE_BINDING,
            "strict_executor": STABLE_V1_CODE_STRICT_EXECUTOR,
            "allow_executor_fallback": STABLE_V1_CODE_ALLOW_EXECUTOR_FALLBACK,
            "strict_tool": STABLE_V1_CODE_STRICT_TOOL,
            "fallback_executor": "",
            "fallback_model_binding": "",
            "fallback_chain": (),
        }
    raise ValueError(f"unknown_profile: {profile}")


def stable_v1_routing_policy(profile: str = "") -> Dict[str, Any]:
    """Return the canonical STABLE_V1 production routing surface.

    When ``profile`` is supplied, the executor / binding fields are
    drawn from :func:`executor_policy_for_profile`.  When ``profile``
    is empty, the legacy CODE-profile constants are returned so the
    pre-addendum ``stable_v1_routing_policy()`` shape (used by tests
    that pre-date the role correction) keeps working.
    """
    if profile:
        ep = executor_policy_for_profile(profile)
        executor_block = {
            "preferred_executor": ep["preferred_executor"],
            "strict_executor": ep["strict_executor"],
            "allow_executor_fallback": ep["allow_executor_fallback"],
            "strict_tool": ep["strict_tool"],
            "preferred_model_binding": ep["preferred_model_binding"],
        }
    else:
        executor_block = {
            "preferred_executor": STABLE_V1_EXECUTOR,
            "strict_executor": STABLE_V1_STRICT_EXECUTOR,
            "allow_executor_fallback": STABLE_V1_ALLOW_EXECUTOR_FALLBACK,
            "strict_tool": STABLE_V1_STRICT_TOOL,
            "preferred_model_binding": STABLE_V1_BINDING,
        }
    return {
        "preferred_planner": STABLE_V1_PLANNER,
        "allow_planner_fallback": STABLE_V1_ALLOW_PLANNER_FALLBACK,
        **executor_block,
        "preferred_reviewer": STABLE_V1_REVIEWER,
        "allow_reviewer_fallback": STABLE_V1_ALLOW_REVIEWER_FALLBACK,
    }


# Profile-only metadata — never touches the routing surface above.
PROFILE_META: Dict[str, Dict[str, Any]] = {
    "GENERAL": {
        "verification_criteria": [],
        "host_evidence_profile": "GENERAL",
    },
    "AUDIT": {
        "verification_criteria": [
            "read-only — no files modified",
            "output cites evidence",
            "prioritized findings",
        ],
        "host_evidence_profile": "AUDIT",
    },
    "OPS": {
        "verification_criteria": [
            "read-only — no services stopped or restarted",
            "output cites service status",
            "no sudo",
        ],
        "host_evidence_profile": "OPS",
    },
    "CODE": {
        "verification_criteria": [
            "code compiles",
            "tests pass",
            "no regressions",
        ],
        "host_evidence_profile": "CODE",
    },
}


def build_cli_payload(
    profile: str,
    user_input: str,
    source: str = "api",
    *,
    project_path: str = "",
) -> Dict[str, Any]:
    """Return the full CLI submit() payload for one of the four profiles.

    The routing surface is the STABLE_V1 production defaults merged
    with the profile-aware executor policy returned by
    :func:`executor_policy_for_profile`.  Planner and reviewer fields
    are shared across profiles; executor / binding / strict-mode
    fields are role-specialized per profile.  Profile-only metadata
    (verification_criteria, host_evidence_profile) is merged
    underneath so profiles cannot accidentally override the routing
    surface.

    ``project_path`` is forwarded as the optional ``--project`` flag
    the host-evidence boundary uses to constrain READ_FILE /
    LIST_DIRECTORY / GIT_* collectors when AUDIT / CODE profiles ask
    about a specific target.
    """
    if profile not in PROFILE_META:
        raise ValueError(f"unknown_profile: {profile}")
    payload: Dict[str, Any] = dict(stable_v1_routing_policy(profile=profile))
    meta = PROFILE_META[profile]
    for k, v in meta.items():
        payload.setdefault(k, v)
    payload["source"] = source
    payload["input"] = user_input
    if project_path:
        payload["project_path"] = project_path
    return payload


__all__ = [
    "STABLE_V1_PLANNER",
    "STABLE_V1_REVIEWER",
    "STABLE_V1_EXECUTOR",
    "STABLE_V1_BINDING",
    "STABLE_V1_CODE_EXECUTOR",
    "STABLE_V1_CODE_BINDING",
    "STABLE_V1_GENERAL_EXECUTOR",
    "STABLE_V1_GENERAL_BINDING",
    "STABLE_V1_GENERAL_FALLBACK_EXECUTOR",
    "STABLE_V1_GENERAL_FALLBACK_BINDING",
    "executor_policy_for_profile",
    "stable_v1_routing_policy",
    "build_cli_payload",
]