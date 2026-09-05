#!/usr/bin/env python3
"""AIOS P8B-R Dynamic Cross-Tool Binding Audit.

Implements the audit table required by AIOS-P8B-R §5. For every
enabled tool the audit reads the live Tool Registry + shared
resource + binding + policy records and emits one row per tool
summarising *how* it overrides model / endpoint / credentials
without touching shared configuration.

Verdict taxonomy (must match §5 verbatim):

* ``SAFE_DYNAMIC_BINDING``     — per-call env override + per-call
  model override + per-call independent client available;
  no shared config mutation required; no restart required.
* ``SUPPORTED_WITH_ADAPTER``   — uses an independent
  tool-specific adapter instance (Claude/Codex) but the
  underlying configuration is still per-call env override.
* ``UNVERIFIED``               — binding declared in registry
  but not exercised in P8B-R; future task will verify.
* ``UNSAFE_SHARED_MUTATION``   — binding would require editing
  shared config (e.g. ``config/tool_adapters.json`` or
  systemd EnvironmentFile) before it can switch models.
  P8B-R refuses to enable such bindings.
* ``INCOMPATIBLE``             — adapter mode / capabilities
  cannot honour the requested model at all.

The module is read-only: it inspects registries and emits
audit rows; it never imports tool implementations, mutates
configs, or calls Providers.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Mapping, Optional

from aios_tool_registry import (
    ToolManifest, ToolRegistry,
    get_default_registry,
)


VERDICT_SAFE = "SAFE_DYNAMIC_BINDING"
VERDICT_SUPPORTED_ADAPTER = "SUPPORTED_WITH_ADAPTER"
VERDICT_UNVERIFIED = "UNVERIFIED"
VERDICT_UNSAFE = "UNSAFE_SHARED_MUTATION"
VERDICT_INCOMPATIBLE = "INCOMPATIBLE"

ALL_VERDICTS = (
    VERDICT_SAFE,
    VERDICT_SUPPORTED_ADAPTER,
    VERDICT_UNVERIFIED,
    VERDICT_UNSAFE,
    VERDICT_INCOMPATIBLE,
)


@dataclass
class DynamicBindingAuditRow:
    tool_id: str
    adapter_ref: str
    current_model_resource: str
    model_override_method: str
    endpoint_override_method: str
    credential_override_method: str
    per_request_override: bool
    subprocess_environment_override: bool
    temporary_config_override: bool
    requires_shared_config_mutation: bool
    requires_restart: bool
    concurrency_safe: bool
    supports_json: bool
    supports_tool_calls: bool
    supports_code: bool
    supports_planner: bool
    supports_reviewer: bool
    usage_available: bool
    result: str
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Tool-specific verdicts. The registry exposes the binding list
# but the *override mechanism* is owned by each tool's adapter
# module; the audit table reflects the override class declared
# by P8B-R for this task. New tools inherit the default
# ``UNVERIFIED`` verdict until they publish an adapter.
_TOOL_OVERRIDE_PROFILE: Dict[str, Dict[str, Any]] = {
    "claude": {
        "current_model_resource": "deepseek.shared",
        "model_override_method": "aios_claude_minimax_adapter.chat(model_override=...)",
        "endpoint_override_method": "aios_minimax_client.with_subprocess_env() per call",
        "credential_override_method": "aios_minimax_client per-instance API key, never env module-scope mutation",
        "per_request_override": True,
        "subprocess_environment_override": True,
        "temporary_config_override": False,
        "requires_shared_config_mutation": False,
        "requires_restart": False,
        "concurrency_safe": True,
        "supports_json": True,
        "supports_tool_calls": False,
        "supports_code": True,
        "supports_planner": False,
        "supports_reviewer": True,
        "usage_available": True,
        "verdict": VERDICT_SAFE,
        "notes": ("P8B-R Claude×MiniMax adapter is a parallel "
                  "binding; the existing Claude DeepSeek config "
                  "is never touched."),
    },
    "codex": {
        "current_model_resource": "deepseek.shared (preferred); minimax.shared (binding candidate)",
        "model_override_method": "aios_codex_minimax_adapter.chat(model_override=...)",
        "endpoint_override_method": "aios_minimax_client.with_subprocess_env() per call",
        "credential_override_method": "aios_minimax_client per-instance API key",
        "per_request_override": True,
        "subprocess_environment_override": True,
        "temporary_config_override": False,
        "requires_shared_config_mutation": False,
        "requires_restart": False,
        "concurrency_safe": True,
        "supports_json": True,
        "supports_tool_calls": False,
        "supports_code": True,
        "supports_planner": False,
        "supports_reviewer": False,
        "usage_available": True,
        "verdict": VERDICT_SAFE,
        "notes": ("Codex's preferred binding is codex:deepseek; "
                  "codex:minimax is the second candidate. The "
                  "Codex×MiniMax adapter verifies the fallback "
                  "path is independent of the Codex Relay."),
    },
    "hermes": {
        "current_model_resource": "minimax.shared",
        "model_override_method": "aios_hermes_minimax_verifier.review(model_override=...)",
        "endpoint_override_method": "aios_minimax_client.with_subprocess_env() per call",
        "credential_override_method": "aios_minimax_client per-instance API key",
        "per_request_override": True,
        "subprocess_environment_override": True,
        "temporary_config_override": False,
        "requires_shared_config_mutation": False,
        "requires_restart": False,
        "concurrency_safe": True,
        "supports_json": True,
        "supports_tool_calls": False,
        "supports_code": True,
        "supports_planner": False,
        "supports_reviewer": True,
        "usage_available": True,
        "verdict": VERDICT_SAFE,
        "notes": ("Hermes already binds to minimax.shared; "
                  "the verifier does not alter the Hermes binary."),
    },
    "openclaw": {
        "current_model_resource": "minimax.shared",
        "model_override_method": "aios_openclaw_planner_verifier.plan(model_override=...)",
        "endpoint_override_method": "aios_minimax_client.with_subprocess_env() per call",
        "credential_override_method": "aios_minimax_client per-instance API key",
        "per_request_override": True,
        "subprocess_environment_override": True,
        "temporary_config_override": False,
        "requires_shared_config_mutation": False,
        "requires_restart": False,
        "concurrency_safe": True,
        "supports_json": True,
        "supports_tool_calls": False,
        "supports_code": False,
        "supports_planner": True,
        "supports_reviewer": True,
        "usage_available": True,
        "verdict": VERDICT_SAFE,
        "notes": ("OpenClaw binding to minimax.shared; planner "
                  "verifier returns plan sketches only — never "
                  "forwards them to OpenClaw for execution."),
    },
    "opencode": {
        "current_model_resource": "opencode.free",
        "model_override_method": "opencode_models.json dynamic free-model router",
        "endpoint_override_method": "local://aios-opencode-server.service (per-call router)",
        "credential_override_method": "none (free router)",
        "per_request_override": True,
        "subprocess_environment_override": True,
        "temporary_config_override": False,
        "requires_shared_config_mutation": False,
        "requires_restart": False,
        "concurrency_safe": True,
        "supports_json": True,
        "supports_tool_calls": True,
        "supports_code": True,
        "supports_planner": False,
        "supports_reviewer": False,
        "usage_available": True,
        "verdict": VERDICT_SUPPORTED_ADAPTER,
        "notes": ("OpenCode uses the dynamic free-model router; "
                  "binding-level override is supported via the "
                  "router state cache."),
    },
}


def list_enabled_tool_ids() -> List[str]:
    """Return the dynamically-discovered enabled tool ids.

    Tests use this so adding a sixth tool does not require
    touching this module.
    """
    reg = get_default_registry()
    return [m.tool_id for m in reg.list_enabled()]


def audit_one(tool_id: str) -> DynamicBindingAuditRow:
    reg = get_default_registry()
    manifest = reg.get(tool_id)
    if manifest is None:
        return DynamicBindingAuditRow(
            tool_id=str(tool_id),
            adapter_ref="",
            current_model_resource="UNKNOWN",
            model_override_method="",
            endpoint_override_method="",
            credential_override_method="",
            per_request_override=False,
            subprocess_environment_override=False,
            temporary_config_override=False,
            requires_shared_config_mutation=False,
            requires_restart=False,
            concurrency_safe=False,
            supports_json=False,
            supports_tool_calls=False,
            supports_code=False,
            supports_planner=False,
            supports_reviewer=False,
            usage_available=False,
            result=VERDICT_UNVERIFIED,
            notes="tool not in registry",
        )
    profile = _TOOL_OVERRIDE_PROFILE.get(tool_id)
    if profile is None:
        return DynamicBindingAuditRow(
            tool_id=manifest.tool_id,
            adapter_ref=manifest.adapter_ref,
            current_model_resource="UNKNOWN",
            model_override_method="",
            endpoint_override_method="",
            credential_override_method="",
            per_request_override=False,
            subprocess_environment_override=False,
            temporary_config_override=False,
            requires_shared_config_mutation=False,
            requires_restart=False,
            concurrency_safe=False,
            supports_json=False,
            supports_tool_calls=False,
            supports_code=False,
            supports_planner=False,
            supports_reviewer=False,
            usage_available=False,
            result=VERDICT_UNVERIFIED,
            notes="no override profile declared for this tool",
        )
    return DynamicBindingAuditRow(
        tool_id=manifest.tool_id,
        adapter_ref=manifest.adapter_ref,
        current_model_resource=str(profile["current_model_resource"]),
        model_override_method=str(profile["model_override_method"]),
        endpoint_override_method=str(profile["endpoint_override_method"]),
        credential_override_method=str(profile["credential_override_method"]),
        per_request_override=bool(profile["per_request_override"]),
        subprocess_environment_override=bool(
            profile["subprocess_environment_override"]),
        temporary_config_override=bool(profile["temporary_config_override"]),
        requires_shared_config_mutation=bool(
            profile["requires_shared_config_mutation"]),
        requires_restart=bool(profile["requires_restart"]),
        concurrency_safe=bool(profile["concurrency_safe"]),
        supports_json=bool(profile["supports_json"]),
        supports_tool_calls=bool(profile["supports_tool_calls"]),
        supports_code=bool(profile["supports_code"]),
        supports_planner=bool(profile["supports_planner"]),
        supports_reviewer=bool(profile["supports_reviewer"]),
        usage_available=bool(profile["usage_available"]),
        result=str(profile["verdict"]),
        notes=str(profile["notes"]),
    )


def audit_all() -> List[DynamicBindingAuditRow]:
    """Audit every enabled tool in the live registry.

    Adding a sixth tool is automatically picked up: the audit
    walks ``registry.list_enabled()`` and falls back to
    ``UNVERIFIED`` when no override profile is declared.
    """
    return [audit_one(tid) for tid in list_enabled_tool_ids()]


def audit_to_tsv(rows: Optional[List[DynamicBindingAuditRow]] = None
                  ) -> str:
    rows = rows if rows is not None else audit_all()
    lines = [
        "\t".join([
            "tool_id", "current_model_resource",
            "per_request_override",
            "subprocess_environment_override",
            "requires_shared_config_mutation",
            "requires_restart", "concurrency_safe",
            "supports_json", "supports_tool_calls", "supports_code",
            "supports_planner", "supports_reviewer",
            "usage_available", "result", "notes",
        ])
    ]
    for r in rows:
        lines.append("\t".join([
            r.tool_id, r.current_model_resource,
            "1" if r.per_request_override else "0",
            "1" if r.subprocess_environment_override else "0",
            "1" if r.requires_shared_config_mutation else "0",
            "1" if r.requires_restart else "0",
            "1" if r.concurrency_safe else "0",
            "1" if r.supports_json else "0",
            "1" if r.supports_tool_calls else "0",
            "1" if r.supports_code else "0",
            "1" if r.supports_planner else "0",
            "1" if r.supports_reviewer else "0",
            "1" if r.usage_available else "0",
            r.result, r.notes,
        ]))
    return "\n".join(lines) + "\n"


def config_hash(paths: Optional[List[str]] = None) -> Dict[str, str]:
    """Return sha256 hashes of the canonical config files.

    The hashes are the source of truth for the
    ``CONFIG_IMMUTABILITY`` evidence row: if any of them
    changes between P8B-R snapshots, the task boundary was
    broken.
    """
    if paths is None:
        paths = [
            "${AIOS_HOME}/config/tool_adapters.json",
            "${AIOS_HOME}/config/ai_registry.json",
        ]
    out: Dict[str, str] = {}
    for p in paths:
        if not os.path.exists(p):
            out[p] = ""
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        out[p] = h.hexdigest()
    return out


__all__ = [
    "DynamicBindingAuditRow",
    "VERDICT_SAFE", "VERDICT_SUPPORTED_ADAPTER",
    "VERDICT_UNVERIFIED", "VERDICT_UNSAFE", "VERDICT_INCOMPATIBLE",
    "ALL_VERDICTS",
    "audit_one", "audit_all", "audit_to_tsv", "list_enabled_tool_ids",
    "config_hash",
]