#!/usr/bin/env python3
"""AIOS P8A Dynamic Tool Registry.

The registry is the single source of truth for which AI tool modules
exist, what roles they serve, what their public contract looks like,
and which model / API resources they bind to. Core subsystems
(``Orchestrator``, ``Monitor``, ``Acceptance``, ``Capability``, the new
``ModelFailover`` engine) read their tool lists from here instead of
hard-coding a five-tool enumeration.

Architecture principles enforced by this module:

* **Tool management layer ≠ tool implementation.** The registry only
  stores metadata (ids, roles, capabilities, references). It never
  imports, executes, or restarts tool internals.
* **Dynamic discovery.** New tools appear without editing this module
  or any of the core subsystems: callers register a new manifest via
  ``register_tool()`` or by dropping a manifest file in the
  configured search directory.
* **Tool identity is preserved.** ``tool_id`` is immutable for the
  lifetime of a registration; the failover engine swaps model
  bindings *within* a tool, never the tool itself.
* **P8A is offline-only.** The registry loads JSON / YAML manifests
  from the repo; it NEVER calls any provider and NEVER reads a
  secret value (only ``credential_ref`` identifiers, which are
  pointers, not values).

Manifests live next to the canonical configs:

* ``config/tool_adapters.json``        — existing per-tool adapter
  contract (executable, probe_args, capabilities, …). The registry
  imports it without modification.
* ``config/ai_registry.json``          — cross-reference table for
  roles and labels.
* ``config/tool_registry.json``        — new manifest overlay written
  by this module if missing. The default values match the canonical
  five tools; users may add entries to register additional tools.

The registry is intentionally append-only: missing tools degrade to
``NOT_CONFIGURED`` rather than raising, so adding a new tool to
production never breaks older modules that only know the original
five.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

HOME = Path(os.getenv("AIOS_HOME", "${AIOS_HOME}"))
TOOLS = HOME / "kernel/tools"
ADAPTERS_CFG = HOME / "config/tool_adapters.json"
REGISTRY_CFG = HOME / "config/ai_registry.json"
REGISTRY_OVERLAY = HOME / "config/tool_registry_overlay.json"

ALL_ROLES = ("executor", "reviewer", "planner")
# Adapter config uses short tool ids (``claude``); ai_registry uses
# the human-readable ``claude_code``. The registry canonicalises on
# the adapter id so we never have two phantom entries pointing at the
# same executable.
_AGENT_ID_TO_ADAPTER_ID: Dict[str, str] = {
    "claude_code": "claude",
    "opencode": "opencode",
    "codex": "codex",
    "hermes": "hermes",
    "openclaw": "openclaw",
}
_ADAPTER_ID_TO_ROLES_HINT: Dict[str, Tuple[str, ...]] = {
    # P9D-R role-closure: opencode is also a registered planner
    # (PLAN_ONLY mode), in addition to its primary executor role.
    "opencode": ("executor", "planner"),
    "hermes": ("reviewer",),
    # P7A errata: openclaw is a planner/reviewer-like tool. The
    # reviewer_capability probe surfaces its DEGRADED_INTERNAL state
    # in ``optional_degradations`` only when it appears in the
    # reviewer candidate tuple; both the historical code and the
    # P7A errata agree on this dual classification.
    "openclaw": ("planner", "reviewer"),
    # P9D-R role-closure: claude is the secondary independent reviewer
    # (it has its own adapter that satisfies the reviewer contract);
    # it keeps its executor role for high-depth tasks.
    "claude": ("executor", "reviewer"),
    "codex": ("executor",),
}
ALL_CAPABILITIES = (
    "conversation", "channel_gateway", "tool_automation", "result_return",
    "shell", "filesystem", "mcp", "cli",
    "code_analysis", "architecture", "debugging", "deep_reasoning",
    "parallel_task", "async_workflow", "batch",
    "knowledge", "provenance", "semantic_review", "post_acceptance_learning",
    "query", "file", "script", "standard_task",
    "refactor", "audit", "complex_task",
    "migration", "scheduled_task",
)


@dataclass(frozen=True)
class ToolManifest:
    """Public contract for one AI tool module.

    The registry stores ONE manifest per ``tool_id``. The fields below
    are the minimal contract every dynamic subsystem (capability
    probe, executor supervisor, monitor, acceptance, failover engine)
    may rely on. Per-tool implementation details (subprocess commands,
    adapter call shape) stay in the tool's own module and are NOT
    duplicated here.
    """

    tool_id: str
    display_name: str
    module_path: str
    adapter_ref: str
    roles: Tuple[str, ...]
    service_unit_ref: Optional[str]
    health_probe_ref: Optional[str]
    model_policy_ref: Optional[str]
    enabled: bool
    version: str
    capabilities: Tuple[str, ...]
    description: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["roles"] = list(self.roles)
        data["capabilities"] = list(self.capabilities)
        return data


class ToolRegistry:
    """In-memory registry of :class:`ToolManifest` instances.

    The registry is a pure data layer: it loads JSON, exposes lookup
    helpers, and lets callers register new tools at runtime (used by
    tests and by future tooling). It NEVER imports tool modules, reads
    secrets, or spawns processes.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._manifests: Dict[str, ToolManifest] = {}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> "ToolRegistry":
        """Load canonical sources from disk. Idempotent."""
        with self._lock:
            self._manifests.clear()
            self._load_from_adapter_config()
            self._load_from_ai_registry()
            self._load_from_overlay()
            return self

    def _load_from_adapter_config(self) -> None:
        if not ADAPTERS_CFG.exists():
            return
        try:
            data = json.loads(ADAPTERS_CFG.read_text(encoding="utf-8"))
        except Exception:
            return
        tools = data.get("tools", {})
        if not isinstance(tools, dict):
            return
        for tool_id, cfg in tools.items():
            manifest = self._manifest_from_adapter(tool_id, cfg or {})
            if manifest is not None:
                self._manifests[tool_id] = manifest

    def _load_from_ai_registry(self) -> None:
        """Cross-reference ``ai_registry.json`` to enrich manifests.

        ``ai_registry.json`` is the canonical role / icon / display
        table. The adapter config does not always list roles, so the
        registry merges the two sources. If a tool is only in the
        overlay / adapter config, it is still registered — registry
        additions never require ai_registry.json to be edited.
        """
        if not REGISTRY_CFG.exists():
            return
        try:
            data = json.loads(REGISTRY_CFG.read_text(encoding="utf-8"))
        except Exception:
            return
        for entry in data.get("agents", []) or []:
            if not isinstance(entry, dict):
                continue
            agent_id = str(entry.get("id", "")).strip()
            if not agent_id:
                continue
            # Canonicalise the agent id to the adapter id so we never
            # register phantom duplicates (``claude`` vs ``claude_code``).
            tool_id = _AGENT_ID_TO_ADAPTER_ID.get(agent_id, agent_id)
            current = self._manifests.get(tool_id)
            if current is None:
                current = ToolManifest(
                    tool_id=tool_id,
                    display_name=str(entry.get("name", tool_id)),
                    module_path=str(entry.get("module_path",
                                              f"agents/{tool_id}")),
                    adapter_ref=tool_id,
                    roles=tuple(),
                    service_unit_ref=None,
                    health_probe_ref=None,
                    model_policy_ref=None,
                    enabled=True,
                    version="unknown",
                    capabilities=tuple(entry.get("capabilities", []) or []),
                    description=str(entry.get("role", "")),
                )
            roles = self._merge_roles(
                current.roles, self._infer_role(entry),
            )
            # Static role hint for the canonical five tools — the
            # adapter config doesn't always declare roles so we
            # cross-reference here.
            if tool_id in _ADAPTER_ID_TO_ROLES_HINT:
                roles = self._merge_roles(
                    roles, _ADAPTER_ID_TO_ROLES_HINT[tool_id],
                )
            display = entry.get("name") or current.display_name
            description = (entry.get("role") or current.description)
            self._manifests[tool_id] = ToolManifest(
                tool_id=current.tool_id,
                display_name=display,
                module_path=current.module_path,
                adapter_ref=current.adapter_ref,
                roles=roles,
                service_unit_ref=current.service_unit_ref,
                health_probe_ref=current.health_probe_ref,
                model_policy_ref=current.model_policy_ref,
                enabled=current.enabled,
                version=current.version,
                capabilities=current.capabilities,
                description=description,
                extra=current.extra,
            )

    def _load_from_overlay(self) -> None:
        """User-edited overlay for additional / future tools.

        The overlay schema mirrors :class:`ToolManifest`. New tools
        register themselves here without editing the canonical configs.
        Existing tools MAY be enriched (roles / capabilities) but their
        ``tool_id`` is immutable.
        """
        if not REGISTRY_OVERLAY.exists():
            return
        try:
            data = json.loads(REGISTRY_OVERLAY.read_text(encoding="utf-8"))
        except Exception:
            return
        for entry in data.get("tools", []) or []:
            if not isinstance(entry, dict):
                continue
            tool_id = str(entry.get("tool_id", "")).strip()
            if not tool_id:
                continue
            existing = self._manifests.get(tool_id)
            manifest = self._manifest_from_overlay(tool_id, entry, existing)
            if manifest is not None:
                self._manifests[tool_id] = manifest

    @staticmethod
    def _manifest_from_adapter(tool_id: str, cfg: Mapping[str, Any]) -> Optional[ToolManifest]:
        if not isinstance(cfg, Mapping):
            return None
        capabilities = tuple(cfg.get("capabilities", []) or ())
        # The adapter config does not declare roles; infer from the
        # ``role`` description string and the capability set. This is
        # best-effort: any tool whose canonical role lives only in
        # ``ai_registry.json`` will be enriched on the second pass.
        role_text = str(cfg.get("role", "")).lower()
        roles = ToolRegistry._infer_role({"role": role_text,
                                          "capabilities": list(capabilities)})
        return ToolManifest(
            tool_id=tool_id,
            display_name=str(cfg.get("label", tool_id)),
            module_path=f"kernel/tools/aios_{tool_id}",
            adapter_ref=tool_id,
            roles=roles,
            service_unit_ref=cfg.get("server_service"),
            health_probe_ref=f"cache/tool_health/{tool_id}.json",
            model_policy_ref=f"kernel/tools/policies/{tool_id}.json",
            enabled=bool(cfg.get("enabled", True)),
            version=str(cfg.get("version", "unknown")),
            capabilities=capabilities,
            description=str(cfg.get("role", "")),
        )

    @staticmethod
    def _manifest_from_overlay(
        tool_id: str, entry: Mapping[str, Any], existing: Optional[ToolManifest],
    ) -> Optional[ToolManifest]:
        if existing is None:
            existing = ToolManifest(
                tool_id=tool_id,
                display_name=tool_id,
                module_path=str(entry.get("module_path",
                                          f"agents/{tool_id}")),
                adapter_ref=tool_id,
                roles=tuple(),
                service_unit_ref=None,
                health_probe_ref=None,
                model_policy_ref=None,
                enabled=bool(entry.get("enabled", True)),
                version=str(entry.get("version", "0.0.0")),
                capabilities=tuple(),
                description=str(entry.get("description", "")),
            )
        roles = tuple(entry.get("roles", existing.roles) or existing.roles)
        capabilities = tuple(
            entry.get("capabilities", existing.capabilities) or existing.capabilities,
        )
        return ToolManifest(
            tool_id=tool_id,
            display_name=str(entry.get("display_name", existing.display_name)),
            module_path=str(entry.get("module_path", existing.module_path)),
            adapter_ref=str(entry.get("adapter_ref", existing.adapter_ref)),
            roles=roles,
            service_unit_ref=entry.get("service_unit_ref",
                                       existing.service_unit_ref),
            health_probe_ref=entry.get("health_probe_ref",
                                       existing.health_probe_ref),
            model_policy_ref=entry.get("model_policy_ref",
                                       existing.model_policy_ref),
            enabled=bool(entry.get("enabled", existing.enabled)),
            version=str(entry.get("version", existing.version)),
            capabilities=capabilities,
            description=str(entry.get("description", existing.description)),
            extra=dict(entry.get("extra", existing.extra) or {}),
        )

    @staticmethod
    def _infer_role(entry: Mapping[str, Any]) -> Tuple[str, ...]:
        text = " ".join(str(entry.get("role", "") or "").lower().split())
        capabilities = [str(c).lower() for c in entry.get("capabilities", []) or []]
        roles: List[str] = []
        if any(s in text for s in ("executor", "执行", "execute")):
            roles.append("executor")
        if any(s in text for s in ("reviewer", "review", "审核", "复审")):
            roles.append("reviewer")
        if any(s in text for s in ("planner", "plan", "规划")):
            roles.append("planner")
        # Fallback heuristics for the canonical five tools based on
        # capability tags. Any new tool that does not set ``role`` in
        # its config must declare roles in the overlay file.
        if not roles:
            if any(c in capabilities for c in ("semantic_review",
                                               "knowledge",
                                               "post_acceptance_learning")):
                roles.append("reviewer")
            if any(c in capabilities for c in ("channel_gateway",
                                               "conversation",
                                               "result_return")):
                roles.append("planner")
            if any(c in capabilities for c in ("shell", "filesystem",
                                               "cli", "query", "script")):
                roles.append("executor")
        return tuple(dict.fromkeys(roles))  # de-duplicate, preserve order

    @staticmethod
    def _merge_roles(a: Tuple[str, ...], b: Tuple[str, ...]) -> Tuple[str, ...]:
        seen = []
        for role in (*a, *b):
            if role and role not in seen:
                seen.append(role)
        return tuple(seen)

    # ------------------------------------------------------------------
    # Registration API
    # ------------------------------------------------------------------

    def register_tool(self, manifest: ToolManifest) -> None:
        """Register or replace a manifest. ``tool_id`` is immutable
        across registrations (replacing an existing tool with the same
        id only refreshes its fields)."""
        if not isinstance(manifest, ToolManifest):
            raise TypeError("register_tool expects a ToolManifest")
        if not manifest.tool_id:
            raise ValueError("tool_id must be non-empty")
        for role in manifest.roles:
            if role not in ALL_ROLES:
                raise ValueError(f"unknown role {role!r}; expected one of {ALL_ROLES}")
        with self._lock:
            self._manifests[manifest.tool_id] = manifest

    def unregister_tool(self, tool_id: str) -> Optional[ToolManifest]:
        """Remove a manifest. Returns the previous manifest, or None."""
        with self._lock:
            return self._manifests.pop(tool_id, None)

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def list_all(self) -> List[ToolManifest]:
        with self._lock:
            return list(self._manifests.values())

    def list_enabled(self) -> List[ToolManifest]:
        return [m for m in self.list_all() if m.enabled]

    def list_by_role(self, role: str) -> List[ToolManifest]:
        return [m for m in self.list_all()
                if m.enabled and m.has_role(role)]

    def list_by_capability(self, capability: str) -> List[ToolManifest]:
        return [m for m in self.list_all()
                if m.enabled and m.has_capability(capability)]

    def get(self, tool_id: str) -> Optional[ToolManifest]:
        with self._lock:
            return self._manifests.get(tool_id)

    def tool_ids(self) -> List[str]:
        return [m.tool_id for m in self.list_all()]

    def __contains__(self, tool_id: object) -> bool:
        return isinstance(tool_id, str) and tool_id in self._manifests

    def __len__(self) -> int:
        return len(self._manifests)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tools": [m.to_dict() for m in sorted(self.list_all(),
                                                   key=lambda x: x.tool_id)],
        }


# Singleton accessor ------------------------------------------------------------

_DEFAULT_REGISTRY: Optional[ToolRegistry] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_registry() -> ToolRegistry:
    """Return the process-wide default registry, loading it on first use.

    Tests can replace the singleton via :func:`set_default_registry`.
    Production callers should always use this accessor so that all
    subsystems see the same tool set.
    """
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = ToolRegistry().load()
        return _DEFAULT_REGISTRY


def set_default_registry(registry: Optional[ToolRegistry]) -> None:
    """Replace the process-wide registry (used by tests)."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = registry


def reset_default_registry() -> ToolRegistry:
    """Force a fresh reload from disk. Returns the new registry."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = ToolRegistry().load()
        return _DEFAULT_REGISTRY


# Manifest data classes for overlays -------------------------------------------


def write_overlay(tools: Sequence[ToolManifest]) -> Path:
    """Persist ``tools`` to the overlay file. Used by tooling that
    registers new tools. Does NOT touch canonical configs."""
    REGISTRY_OVERLAY.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        "description": ("P8A dynamic tool registry overlay. New "
                        "tools register here without editing "
                        "tool_adapters.json or ai_registry.json."),
        "tools": [t.to_dict() for t in tools],
    }
    REGISTRY_OVERLAY.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return REGISTRY_OVERLAY


__all__ = [
    "ToolManifest", "ToolRegistry",
    "get_default_registry", "set_default_registry", "reset_default_registry",
    "write_overlay", "ALL_ROLES", "ALL_CAPABILITIES",
]
