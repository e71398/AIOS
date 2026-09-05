#!/usr/bin/env python3
"""AIOS P8A Shared Model / API Resource Layer.

This module is the single source of truth for *which* real Provider /
account / API resources exist in the system, *which* tool modules
bind to them, and *which* order each tool uses its candidates.

The architecture explicitly forbids the old pattern of one
``minimax.hermes`` / ``minimax.openclaw`` per-tool resource: that
pattern created phantom independent quotas for tools that actually
share the same Provider / API / account / balance / rate-limit. The
P8A design instead models:

* **SharedModelResource**  — a real, account-scoped Provider resource.
  Examples: ``minimax.shared``, ``deepseek.shared``, ``opencode.free``.
  Holds account-level health (quota, balance, region, retry_after,
  cooldown).
* **ToolModelBinding**     — a *binding* of one tool to one shared
  resource, with role / adapter-mode / capability flags. A tool may
  have several bindings (preferred + fallbacks) but each binding
  belongs to one tool and one shared resource. Per-binding issues
  (e.g. OpenClaw asking the account for a feature the plan doesn't
  include) live here, not on the shared resource.
* **ToolModelPolicy**      — the per-tool policy that lists its
  candidate bindings in priority order, plus per-tool limits
  (max_model_attempts, max_model_failovers, max_cost, max_tokens,
  strict_model, allow_model_fallback).

The separation lets the failover engine answer three independent
questions for each task:

1. *Which tools are eligible?*  →  Tool Registry.
2. *Which model bindings can a tool try?*  →  ToolModelPolicy.
3. *Is the underlying account healthy?*  →  SharedModelResource.
4. *Is this binding's adapter mode compatible with the current
   request?*  →  ToolModelBinding.

The registry / policy never imports any tool module or reads any
secret value (only ``credential_ref`` identifiers).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

HOME = Path(os.getenv("AIOS_HOME", "${AIOS_HOME}"))
TOOLS = HOME / "kernel/tools"
SHARED_RESOURCES_FILE = HOME / "config/shared_model_resources.json"
BINDINGS_FILE = HOME / "config/tool_model_bindings.json"
POLICIES_FILE = HOME / "config/tool_model_policies.json"

# ---------------------------------------------------------------------------
# Failure scope taxonomy — used by the failover engine to decide which
# subsystem absorbs a failure. RESOURCE and BINDING are the only scopes
# that may trigger an automatic model failover. Other scopes
# (TOOL_ADAPTER / LOCAL_RUNTIME / TASK_INPUT) are tool-internal issues
# and MUST NOT silently switch the tool's underlying Provider.
# ---------------------------------------------------------------------------

FAILURE_SCOPE_RESOURCE = "RESOURCE"
FAILURE_SCOPE_BINDING = "BINDING"
FAILURE_SCOPE_TOOL_ADAPTER = "TOOL_ADAPTER"
FAILURE_SCOPE_LOCAL_RUNTIME = "LOCAL_RUNTIME"
FAILURE_SCOPE_TASK_INPUT = "TASK_INPUT"

ALL_FAILURE_SCOPES = (
    FAILURE_SCOPE_RESOURCE,
    FAILURE_SCOPE_BINDING,
    FAILURE_SCOPE_TOOL_ADAPTER,
    FAILURE_SCOPE_LOCAL_RUNTIME,
    FAILURE_SCOPE_TASK_INPUT,
)


# ---------------------------------------------------------------------------
# SharedModelResource
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SharedModelResource:
    """One real Provider / account / API resource.

    The resource is the layer that owns account-level state (quota,
    balance, region, global retry_after). All tool bindings to this
    resource see the same account health; cooldown on the resource
    applies to every binding regardless of which tool raised it.

    * ``resource_id``  — opaque, unique within the system.
    * ``vendor``       — provider name (e.g. ``MiniMax``, ``DeepSeek``).
    * ``model_id``     — primary model id used by this resource.
    * ``endpoint_ref`` — pointer to the endpoint config entry, NEVER
      the secret value.
    * ``credential_ref`` — pointer to the credentials config entry,
      NEVER the secret value.
    * ``account_scope`` — logical account scope (e.g.
      ``MiniMax_team_borom``). All bindings to this resource share
      the same quota pool.
    * ``protocol``     — wire protocol (``openai_compatible``,
      ``anthropic_compatible``, ``opencode_free`` …).
    * ``enabled``      — resource is administratively enabled.
    * ``credentials_present`` — whether the credential file referenced
      by ``credential_ref`` actually exists. The P8A build never reads
      secret contents.
    """

    resource_id: str
    vendor: str
    model_id: str
    endpoint_ref: str
    credential_ref: str
    account_scope: str
    protocol: str
    enabled: bool
    credentials_present: bool = False
    implemented: bool = True
    description: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    # Mutable runtime state (cooldown / last success / last failure).
    # P8A does NOT persist these to disk; tests and live probes build
    # a fresh record each time. The dataclass is ``frozen=True`` only
    # for the configuration fields — runtime state is held in the
    # separate ``SharedModelResourceState`` below so the resource
    # object itself remains hashable / immutable.

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return data


@dataclass
class SharedModelResourceState:
    """Mutable runtime state for one shared resource.

    Held alongside the frozen resource declaration. The two are kept
    separate so the declaration can be used as a dict key / set
    member without paying for a mutable container.
    """

    resource_health: str = "UNKNOWN"
    resource_reason: str = ""
    resource_retry_after: Optional[str] = None
    resource_cooldown_until: Optional[str] = None
    last_resource_success: Optional[str] = None
    last_resource_failure: Optional[str] = None
    last_resource_failure_kind: Optional[str] = None
    last_resource_failure_scope: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# ToolModelBinding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolModelBinding:
    """How a single tool binds to a single shared resource.

    Bindings encode *tool-specific* facts about a resource:

    * which roles the binding supports (``executor`` / ``reviewer`` /
      ``planner``);
    * which ``adapter_mode`` the tool uses (e.g. ``openai_chat``,
      ``anthropic_messages``);
    * which ``prompt_profile`` applies (e.g. ``hermes_reviewer``,
      ``openclaw_planner``);
    * timeout / capability flags (``supports_json``, ``supports_tools``,
      ``supports_code``, ``supports_long_context``);
    * priority — used by the failover engine to order candidates.

    Failures that belong to this binding (e.g. ``token_plan`` when
    OpenClaw tries a feature the MiniMax plan doesn't allow) do NOT
    poison other bindings to the same resource — they only cool down
    *this* binding. The shared resource cooldown is only triggered
    when the failure scope is ``RESOURCE`` (account-wide).
    """

    binding_id: str
    tool_id: str
    resource_id: str
    roles: Tuple[str, ...]
    adapter_mode: str
    prompt_profile: str
    timeout: int
    supports_json: bool
    supports_tools: bool
    supports_code: bool
    supports_long_context: bool
    priority: int
    enabled: bool = True
    description: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)
    # Local-model guard fields (P9 close-out 20260725 §二).  Defaults
    # are conservative: cloud bindings get manual_task_only=False and
    # the rest False, so the guard stays a no-op for them.  Local
    # bindings set all four to False / 1 and manual_task_only=True via
    # ``build_default_binding_registry`` below.
    manual_task_only: bool = False
    automatic_fallback_allowed: bool = False
    recovery_manager_allowed: bool = False
    canary_allowed: bool = False
    max_concurrency: int = 0
    blocked_reason: str = ""

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["roles"] = list(self.roles)
        return data


@dataclass
class ToolModelBindingState:
    """Mutable runtime state for one binding.

    A binding may be temporarily disabled (e.g. OpenClaw's specific
    tool-call mode is not supported by the current MiniMax plan). This
    state is independent of the shared resource's cooldown — one
    binding being hot does not imply the other is hot, and vice versa.
    """

    binding_health: str = "UNKNOWN"
    binding_reason: str = ""
    binding_retry_after: Optional[str] = None
    binding_cooldown_until: Optional[str] = None
    last_binding_success: Optional[str] = None
    last_binding_failure: Optional[str] = None
    last_binding_failure_kind: Optional[str] = None
    last_binding_failure_scope: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# ToolModelPolicy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolModelPolicy:
    """The per-tool policy that selects its model candidates.

    The policy is the *only* place where a tool's candidate order is
    declared. The failover engine never invents ordering; it walks the
    candidate_bindings tuple in the declared order.

    Limits:

    * ``max_model_attempts``  — total attempts allowed (default 2).
    * ``max_model_failovers`` — number of *switches* allowed
      (default 1; first attempt does not count as a failover).
    * ``max_cost`` / ``max_tokens`` — task-level budget. Second model
      attempt is blocked if it would exceed either.
    * ``strict_model``        — if set, the tool MUST use the named
      binding; the failover engine refuses to pick a different binding.
    * ``allow_model_fallback`` — when ``False``, even non-strict
      policies cannot swap models.

    ``strict_model`` and ``allow_model_fallback`` are independent
    controls: ``strict_model`` is a hard pin, ``allow_model_fallback``
    is a permission flag. A task may have ``allow_model_fallback=True``
    but ``strict_model="hermes:minimax.shared"`` and the engine will
    refuse to switch. Conversely, ``strict_model=None`` with
    ``allow_model_fallback=False`` means "no model substitution ever".
    """

    tool_id: str
    candidate_bindings: Tuple[str, ...]
    preferred_binding: Optional[str] = None
    max_model_attempts: int = 2
    max_model_failovers: int = 1
    max_cost: Optional[float] = None
    max_tokens: Optional[int] = None
    strict_model: Optional[str] = None
    allow_model_fallback: bool = True
    description: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["candidate_bindings"] = list(self.candidate_bindings)
        return data


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------


class SharedModelResourceRegistry:
    """In-memory registry of :class:`SharedModelResource` declarations.

    State (cooldown, last success, etc.) lives in the matching
    :class:`SharedModelResourceState` objects held by the failover
    engine; this registry is purely declarative.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._resources: Dict[str, SharedModelResource] = {}

    def register(self, resource: SharedModelResource) -> None:
        if not isinstance(resource, SharedModelResource):
            raise TypeError("register expects SharedModelResource")
        if not resource.resource_id:
            raise ValueError("resource_id must be non-empty")
        with self._lock:
            self._resources[resource.resource_id] = resource

    def unregister(self, resource_id: str) -> Optional[SharedModelResource]:
        with self._lock:
            return self._resources.pop(resource_id, None)

    def get(self, resource_id: str) -> Optional[SharedModelResource]:
        with self._lock:
            return self._resources.get(resource_id)

    def list_all(self) -> List[SharedModelResource]:
        with self._lock:
            return list(self._resources.values())

    def list_enabled(self) -> List[SharedModelResource]:
        return [r for r in self.list_all() if r.enabled]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resources": [r.to_dict() for r in sorted(
                self.list_all(), key=lambda x: x.resource_id)],
        }


class ToolModelBindingRegistry:
    """In-memory registry of :class:`ToolModelBinding` declarations."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bindings: Dict[str, ToolModelBinding] = {}

    def register(self, binding: ToolModelBinding) -> None:
        if not isinstance(binding, ToolModelBinding):
            raise TypeError("register expects ToolModelBinding")
        if not binding.binding_id:
            raise ValueError("binding_id must be non-empty")
        with self._lock:
            self._bindings[binding.binding_id] = binding

    def unregister(self, binding_id: str) -> Optional[ToolModelBinding]:
        with self._lock:
            return self._bindings.pop(binding_id, None)

    def get(self, binding_id: str) -> Optional[ToolModelBinding]:
        with self._lock:
            return self._bindings.get(binding_id)

    def list_all(self) -> List[ToolModelBinding]:
        with self._lock:
            return list(self._bindings.values())

    def list_for_tool(self, tool_id: str) -> List[ToolModelBinding]:
        return [b for b in self.list_all() if b.tool_id == tool_id]

    def list_for_resource(self, resource_id: str) -> List[ToolModelBinding]:
        return [b for b in self.list_all() if b.resource_id == resource_id]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bindings": [b.to_dict() for b in sorted(
                self.list_all(), key=lambda x: (x.tool_id, x.priority))],
        }


class ToolModelPolicyRegistry:
    """In-memory registry of :class:`ToolModelPolicy` declarations."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._policies: Dict[str, ToolModelPolicy] = {}

    def register(self, policy: ToolModelPolicy) -> None:
        if not isinstance(policy, ToolModelPolicy):
            raise TypeError("register expects ToolModelPolicy")
        if not policy.tool_id:
            raise ValueError("tool_id must be non-empty")
        with self._lock:
            self._policies[policy.tool_id] = policy

    def unregister(self, tool_id: str) -> Optional[ToolModelPolicy]:
        with self._lock:
            return self._policies.pop(tool_id, None)

    def get(self, tool_id: str) -> Optional[ToolModelPolicy]:
        with self._lock:
            return self._policies.get(tool_id)

    def list_all(self) -> List[ToolModelPolicy]:
        with self._lock:
            return list(self._policies.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policies": [p.to_dict() for p in sorted(
                self.list_all(), key=lambda x: x.tool_id)],
        }


# ---------------------------------------------------------------------------
# Singleton accessors
# ---------------------------------------------------------------------------


_DEFAULT_RESOURCE_REGISTRY: Optional[SharedModelResourceRegistry] = None
_DEFAULT_BINDING_REGISTRY: Optional[ToolModelBindingRegistry] = None
_DEFAULT_POLICY_REGISTRY: Optional[ToolModelPolicyRegistry] = None
_SINGLETON_LOCK = threading.Lock()


def get_default_resource_registry() -> SharedModelResourceRegistry:
    global _DEFAULT_RESOURCE_REGISTRY
    with _SINGLETON_LOCK:
        if _DEFAULT_RESOURCE_REGISTRY is None:
            _DEFAULT_RESOURCE_REGISTRY = build_default_resource_registry()
        return _DEFAULT_RESOURCE_REGISTRY


def set_default_resource_registry(reg: Optional[SharedModelResourceRegistry]) -> None:
    global _DEFAULT_RESOURCE_REGISTRY
    with _SINGLETON_LOCK:
        _DEFAULT_RESOURCE_REGISTRY = reg


def get_default_binding_registry() -> ToolModelBindingRegistry:
    global _DEFAULT_BINDING_REGISTRY
    with _SINGLETON_LOCK:
        if _DEFAULT_BINDING_REGISTRY is None:
            _DEFAULT_BINDING_REGISTRY = build_default_binding_registry()
        return _DEFAULT_BINDING_REGISTRY


def set_default_binding_registry(reg: Optional[ToolModelBindingRegistry]) -> None:
    global _DEFAULT_BINDING_REGISTRY
    with _SINGLETON_LOCK:
        _DEFAULT_BINDING_REGISTRY = reg


def get_default_policy_registry() -> ToolModelPolicyRegistry:
    global _DEFAULT_POLICY_REGISTRY
    with _SINGLETON_LOCK:
        if _DEFAULT_POLICY_REGISTRY is None:
            _DEFAULT_POLICY_REGISTRY = build_default_policy_registry()
        return _DEFAULT_POLICY_REGISTRY


def set_default_policy_registry(reg: Optional[ToolModelPolicyRegistry]) -> None:
    global _DEFAULT_POLICY_REGISTRY
    with _SINGLETON_LOCK:
        _DEFAULT_POLICY_REGISTRY = reg


# ---------------------------------------------------------------------------
# Default declarations — derived strictly from non-secret config.
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _shared_minimax() -> SharedModelResource:
    """The one shared ``minimax.shared`` resource.

    Both Hermes and OpenClaw bind to this resource. The P8A design
    deliberately collapses the old phantom ``minimax.hermes`` /
    ``minimax.openclaw`` accounts into one — they share the same
    account, balance, rate-limit, and cooldown.
    """
    return SharedModelResource(
        resource_id="minimax.shared",
        vendor="MiniMax",
        model_id="MiniMax-M3",
        endpoint_ref="env://AIOS_MINIMAX_BASE_URL",
        credential_ref="env://AIOS_MINIMAX_API_KEY",
        account_scope="minimax_team_borom",
        protocol="openai_compatible",
        enabled=True,
        credentials_present=_probe_credential_present_with_alias(
            "AIOS_MINIMAX_API_KEY",
            ("MINIMAX_API_KEY",),
        ),
        description=("Shared MiniMax account used by Hermes and "
                     "OpenClaw. P8A invariant: never split into "
                     "per-tool resources."),
    )


def _shared_deepseek() -> SharedModelResource:
    """The ``deepseek.shared`` resource.

    ``config/tool_adapters.json`` shows Claude and Codex both listing
    ``provider=DeepSeek`` / ``model=DeepSeek V4 Pro`` — they share the
    same account. P8A models this as one shared resource so a single
    account-wide cooldown affects both bindings.
    """
    return SharedModelResource(
        resource_id="deepseek.shared",
        vendor="DeepSeek",
        model_id="DeepSeek-V4-Pro",
        endpoint_ref="env://AIOS_DEEPSEEK_BASE_URL",
        credential_ref="env://AIOS_DEEPSEEK_API_KEY",
        account_scope="deepseek_team_borom",
        protocol="openai_compatible",
        enabled=True,
        credentials_present=_probe_credential_present("AIOS_DEEPSEEK_API_KEY"),
        description=("Shared DeepSeek account used by Claude and "
                     "Codex adapters."),
    )


def _opencode_free() -> SharedModelResource:
    """OpenCode's free-model routing — NOT a paid provider.

    This is a free, on-prem free-model router; it has no account /
    quota and the registry tags it accordingly so the failover engine
    can short-circuit resource cooldown logic.
    """
    return SharedModelResource(
        resource_id="opencode.free",
        vendor="opencode",
        model_id="free-auto-router",
        endpoint_ref="local://aios-opencode-server.service",
        credential_ref="none",
        account_scope="opencode_local",
        protocol="opencode_local",
        enabled=True,
        credentials_present=True,
        description=("OpenCode local free-model router; no "
                     "account, no quota. Failover semantics differ "
                     "from paid providers."),
    )


def _qwen_primary() -> SharedModelResource:
    """Qwen (阿里云百炼) — APPROVED_AS_THIRD_PROVIDER but NOT enabled.

    P8A only registers the declaration so future code can reference
    the resource_id without crashing; production traffic is forbidden
    until credentials are configured and the policy is enabled.
    """
    return SharedModelResource(
        resource_id="qwen.primary",
        vendor="qwen",
        model_id="qwen-plus",
        endpoint_ref="env://AIOS_QWEN_BASE_URL",
        credential_ref="env://AIOS_QWEN_API_KEY",
        account_scope="qwen_dashscope_borom",
        protocol="openai_compatible",
        enabled=False,
        credentials_present=_probe_credential_present("AIOS_QWEN_API_KEY"),
        implemented=True,
        description=("Qwen / 阿里云百炼 — APPROVED_AS_THIRD_PROVIDER, "
                     "NOT_ENABLED, NO_PRODUCTION_TRAFFIC in P8A."),
    )


def _kimi_cold_standby() -> SharedModelResource:
    """Kimi — RESERVED_COLD_STANDBY only.

    No production call path; the resource is registered for symmetry
    but ``implemented=False`` and ``enabled=False``.
    """
    return SharedModelResource(
        resource_id="kimi.cold_standby",
        vendor="kimi",
        model_id="moonshot-v1",
        endpoint_ref="env://AIOS_KIMI_BASE_URL",
        credential_ref="env://AIOS_KIMI_API_KEY",
        account_scope="kimi_moonshot_borom",
        protocol="openai_compatible",
        enabled=False,
        credentials_present=_probe_credential_present("AIOS_KIMI_API_KEY"),
        implemented=False,
        description=("Kimi / Moonshot — RESERVED_COLD_STANDBY, "
                     "NOT_IMPLEMENTED, NO_SECRET in P8A."),
    )


def _probe_credential_present(env_var: str) -> bool:
    """Return ``True`` iff the referenced env var is set to a non-empty value.

    The P8A build never reads the secret value — only the presence of
    the env var is used to populate ``credentials_present`` for
    monitoring / acceptance. If the var is unset, the resource is
    declared with ``credentials_present=False``; this is a deliberate
    way to keep Qwen / Kimi off the production candidate path without
    ever reading or printing a key.
    """
    raw = os.environ.get(env_var, "")
    return bool(raw and raw.strip())


# Backwards-compatible alias probe for the MiniMax resource.
#
# The original operator-side credential is shipped in
# ``${HOME}/.openclaw/.env`` as the unprefixed ``MINIMAX_API_KEY``
# (a name pre-dating the AIOS_-prefix convention). The P8A registry
# key is the AIOS_-prefixed ``AIOS_MINIMAX_API_KEY``.
#
# The brief explicitly allows fixing env-loading so existing
# credentials become visible to the registry without altering the
# secret file. The existing ``MINIMAX_API_KEY`` line must therefore
# be detected in addition to the AIOS_-prefixed name.
#
# Safety contract (carried over from ``_probe_credential_present``):
#   * presence-only — no value is ever read, logged, or persisted
#   * empty/whitespace is treated as "not present"
#   * false negative on either side produces credentials_present=False
#     exactly as before, preserving the existing accept gate
def _probe_credential_present_with_alias(
    primary_env_var: str, alias_env_vars: tuple[str, ...] = ()
) -> bool:
    if _probe_credential_present(primary_env_var):
        return True
    for alias in alias_env_vars:
        if _probe_credential_present(alias):
            return True
    return False


def _shared_ollama_local() -> SharedModelResource:
    """Local Ollama daemon: independent failure domain.

    STRICT LOCAL-MODEL POLICY (2026-07-25 close-out):
    * ``enabled`` is **always** ``False`` regardless of daemon reachability.
    * ``implemented`` stays ``True`` so the registry continues to expose
      the resource shape for monitoring.
    * Activation is ``MANUAL_USER_APPROVAL_ONLY``: an operator must
      explicitly set ``AIOS_LOCAL_MODEL_INFERENCE_ALLOWED=1`` AND
      ``AIOS_OLLAMA_USER_APPROVED_AT`` to a recorded timestamp before
      any binding pointing at this resource becomes production-eligible.
    * No automatic probe, no auto-start, no fallback implication.
    """
    import os
    base_url = os.environ.get("AIOS_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    user_approved = bool(os.environ.get("AIOS_OLLAMA_USER_APPROVED_AT"))
    policy_allows = bool(int(os.environ.get("AIOS_LOCAL_MODEL_INFERENCE_ALLOWED", "0") or "0"))
    enabled = bool(user_approved and policy_allows)
    implemented = True  # shape preserved for monitoring regardless of daemon reachability
    return SharedModelResource(
        resource_id="ollama.local",
        vendor="ollama",
        model_id="qwen3:8b",
        endpoint_ref=base_url,
        credential_ref="none_local",
        account_scope="ollama_local",
        protocol="openai_compatible",
        enabled=enabled,
        credentials_present=implemented,
        implemented=implemented,
        description=(
            "Local Ollama daemon (qwen3:8b / qwen3:8b-ctx / qwen2.5:0.5b). "
            "Independent local failure domain; no external quota."
        ),
    )


def build_default_resource_registry() -> SharedModelResourceRegistry:
    """Build the canonical shared resource registry for the current run."""
    reg = SharedModelResourceRegistry()
    resources = [_shared_minimax(),
                  _shared_deepseek(),
                  _opencode_free(),
                  _qwen_primary(),
                  _kimi_cold_standby(),
                  _shared_ollama_local()]
    for r in resources:
        reg.register(r)
    return reg


def build_default_binding_registry() -> ToolModelBindingRegistry:
    """Build the canonical binding registry for the current run.

    Every tool's first binding is its preferred one; further bindings
    are fallbacks. Hermes / OpenClaw both bind to ``minimax.shared``;
    Claude / Codex both bind to ``deepseek.shared``. Qwen and Kimi
    bindings exist but are disabled so the failover engine never
    selects them in P8A.
    """
    reg = ToolModelBindingRegistry()
    # OpenCode ----------------------------------------------------------------
    reg.register(ToolModelBinding(
        binding_id="opencode:free",
        tool_id="opencode",
        resource_id="opencode.free",
        roles=("executor",),
        adapter_mode="opencode_local",
        prompt_profile="opencode_executor",
        timeout=240,
        supports_json=True,
        supports_tools=True,
        supports_code=True,
        supports_long_context=True,
        priority=0,
        description="OpenCode → opencode.free (local free router).",
    ))
    # Qwen coder binding: registered but disabled — see Qwen assessment.
    reg.register(ToolModelBinding(
        binding_id="opencode:qwen",
        tool_id="opencode",
        resource_id="qwen.primary",
        roles=("executor",),
        adapter_mode="openai_compatible",
        prompt_profile="opencode_executor",
        timeout=120,
        supports_json=True,
        supports_tools=True,
        supports_code=True,
        supports_long_context=False,
        priority=1,
        enabled=False,
        description="OpenCode → qwen.primary (UNVERIFIED, disabled).",
    ))
    reg.register(ToolModelBinding(
        binding_id="opencode:deepseek",
        tool_id="opencode",
        resource_id="deepseek.shared",
        roles=("executor",),
        adapter_mode="openai_compatible",
        prompt_profile="opencode_executor",
        timeout=120,
        supports_json=True,
        supports_tools=True,
        supports_code=True,
        supports_long_context=False,
        priority=2,
        description="OpenCode → deepseek.shared (UNVERIFIED).",
    ))
    reg.register(ToolModelBinding(
        binding_id="opencode:minimax",
        tool_id="opencode",
        resource_id="minimax.shared",
        roles=("executor",),
        adapter_mode="openai_compatible",
        prompt_profile="opencode_executor",
        timeout=120,
        supports_json=True,
        supports_tools=False,
        supports_code=True,
        supports_long_context=False,
        priority=3,
        description="OpenCode → minimax.shared (UNVERIFIED).",
    ))
    # Hermes ------------------------------------------------------------------
    reg.register(ToolModelBinding(
        binding_id="hermes:minimax",
        tool_id="hermes",
        resource_id="minimax.shared",
        roles=("reviewer",),
        adapter_mode="openai_compatible",
        prompt_profile="hermes_reviewer",
        timeout=120,
        supports_json=True,
        supports_tools=False,
        supports_code=True,
        supports_long_context=True,
        priority=0,
        description="Hermes → minimax.shared (preferred reviewer).",
    ))
    reg.register(ToolModelBinding(
        binding_id="hermes:qwen",
        tool_id="hermes",
        resource_id="qwen.primary",
        roles=("reviewer",),
        adapter_mode="openai_compatible",
        prompt_profile="hermes_reviewer",
        timeout=120,
        supports_json=True,
        supports_tools=False,
        supports_code=True,
        supports_long_context=True,
        priority=1,
        enabled=False,
        description="Hermes → qwen.primary (UNVERIFIED, disabled).",
    ))
    reg.register(ToolModelBinding(
        binding_id="hermes:deepseek",
        tool_id="hermes",
        resource_id="deepseek.shared",
        roles=("reviewer",),
        adapter_mode="openai_compatible",
        prompt_profile="hermes_reviewer",
        timeout=120,
        supports_json=True,
        supports_tools=False,
        supports_code=True,
        supports_long_context=False,
        priority=2,
        description="Hermes → deepseek.shared (UNVERIFIED).",
    ))
    # OpenClaw ----------------------------------------------------------------
    reg.register(ToolModelBinding(
        binding_id="openclaw:minimax",
        tool_id="openclaw",
        resource_id="minimax.shared",
        roles=("planner",),
        adapter_mode="openai_compatible",
        prompt_profile="openclaw_planner",
        timeout=120,
        supports_json=True,
        supports_tools=True,
        supports_code=False,
        supports_long_context=True,
        priority=0,
        description="OpenClaw → minimax.shared (preferred planner).",
    ))
    reg.register(ToolModelBinding(
        binding_id="openclaw:qwen",
        tool_id="openclaw",
        resource_id="qwen.primary",
        roles=("planner",),
        adapter_mode="openai_compatible",
        prompt_profile="openclaw_planner",
        timeout=120,
        supports_json=True,
        supports_tools=True,
        supports_code=False,
        supports_long_context=True,
        priority=1,
        enabled=False,
        description="OpenClaw → qwen.primary (UNVERIFIED, disabled).",
    ))
    reg.register(ToolModelBinding(
        binding_id="openclaw:kimi",
        tool_id="openclaw",
        resource_id="kimi.cold_standby",
        roles=("planner",),
        adapter_mode="openai_compatible",
        prompt_profile="openclaw_planner",
        timeout=120,
        supports_json=True,
        supports_tools=False,
        supports_code=False,
        supports_long_context=True,
        priority=2,
        enabled=False,
        description="OpenClaw → kimi.cold_standby (cold standby).",
    ))
    # Claude ------------------------------------------------------------------
    reg.register(ToolModelBinding(
        binding_id="claude:deepseek",
        tool_id="claude",
        resource_id="deepseek.shared",
        roles=("executor", "reviewer"),
        adapter_mode="anthropic_compatible",
        prompt_profile="claude_specialist",
        timeout=600,
        supports_json=True,
        supports_tools=True,
        supports_code=True,
        supports_long_context=True,
        priority=0,
        description="Claude → deepseek.shared (preferred specialist).",
    ))
    reg.register(ToolModelBinding(
        binding_id="claude:qwen",
        tool_id="claude",
        resource_id="qwen.primary",
        roles=("executor", "reviewer"),
        adapter_mode="openai_compatible",
        prompt_profile="claude_specialist",
        timeout=300,
        supports_json=True,
        supports_tools=True,
        supports_code=True,
        supports_long_context=False,
        priority=1,
        enabled=False,
        description="Claude → qwen.primary (UNVERIFIED, disabled).",
    ))
    reg.register(ToolModelBinding(
        binding_id="claude:minimax",
        tool_id="claude",
        resource_id="minimax.shared",
        roles=("executor", "reviewer"),
        adapter_mode="openai_compatible",
        prompt_profile="claude_specialist",
        timeout=300,
        supports_json=True,
        supports_tools=False,
        supports_code=True,
        supports_long_context=False,
        priority=2,
        description="Claude → minimax.shared (UNVERIFIED).",
    ))
    # Codex -------------------------------------------------------------------
    reg.register(ToolModelBinding(
        binding_id="codex:deepseek",
        tool_id="codex",
        resource_id="deepseek.shared",
        roles=("executor",),
        adapter_mode="openai_compatible",
        prompt_profile="codex_batch",
        timeout=300,
        supports_json=True,
        supports_tools=True,
        supports_code=True,
        supports_long_context=False,
        priority=0,
        description="Codex → deepseek.shared (preferred batch).",
    ))
    reg.register(ToolModelBinding(
        binding_id="codex:qwen",
        tool_id="codex",
        resource_id="qwen.primary",
        roles=("executor",),
        adapter_mode="openai_compatible",
        prompt_profile="codex_batch",
        timeout=300,
        supports_json=True,
        supports_tools=True,
        supports_code=True,
        supports_long_context=False,
        priority=1,
        enabled=False,
        description="Codex → qwen.primary (UNVERIFIED, disabled).",
    ))
    reg.register(ToolModelBinding(
        binding_id="codex:minimax",
        tool_id="codex",
        resource_id="minimax.shared",
        roles=("executor",),
        adapter_mode="openai_compatible",
        prompt_profile="codex_batch",
        timeout=300,
        supports_json=True,
        supports_tools=False,
        supports_code=True,
        supports_long_context=False,
        priority=2,
        description="Codex → minimax.shared (UNVERIFIED).",
    ))
    reg.register(ToolModelBinding(
        binding_id="minimax-official:minimax",
        tool_id="minimax-official",
        resource_id="minimax.shared",
        roles=("executor",),
        adapter_mode="openai_compatible",
        prompt_profile="minimax_official_executor",
        timeout=120,
        supports_json=True,
        supports_tools=False,
        supports_code=True,
        supports_long_context=True,
        priority=0,
        enabled=True,
        description="Task 014 MVP path: real GMI Cloud MiniMax-M3 via aios_model_gateway; managed by client guard (AIOS_MINIMAX_OFFICIAL_ENABLED, 30/15000 limiter).",
    ))
    # --- Local Ollama failure domain (LOCAL-MODEL-GUARD closure 20260725) ---
    # Each tool gets one `*:ollama` binding for registry symmetry, but
    # the binding is **disabled** until an operator records an explicit
    # user-approved timestamp AND sets the policy-allows flag (see
    # ``_shared_ollama_local``).  No automatic probe, no canary, no
    # rescue, no last-resort path.  These bindings are visible to the
    # monitor only; they MUST NOT participate in candidate walks,
    # Acceptance, Monitor count, or production statistics.
    #
    # Per the close-out brief §二 the binding is **always**:
    #   enabled=False, production_eligible=False,
    #   routing_eligible=False, automatic_fallback_allowed=False,
    #   recovery_manager_allowed=False, canary_allowed=False,
    #   manual_task_only=True, max_concurrency=1,
    #   blocked_reason=USER_APPROVAL_REQUIRED
    # (independently of any future operator-side approval).
    _ollama_common = dict(
        resource_id="ollama.local",
        adapter_mode="openai_compatible",
        prompt_profile="openai_compatible",
        timeout=120,
        supports_json=True,
        supports_tools=False,
        supports_code=False,
        supports_long_context=False,
        priority=99,                # numerically high, but enabled=False
                                   # so it never enters the walk; the
                                   # test name now reads "highest
                                   # numeric priority but disabled".
        enabled=False,
        manual_task_only=True,      # local-model guard — no auto-routing
        automatic_fallback_allowed=False,  # per §二
        recovery_manager_allowed=False,   # per §二
        canary_allowed=False,             # per §二
        max_concurrency=1,                 # per §二
        blocked_reason="USER_APPROVAL_REQUIRED",
        description=(
            "Local Ollama binding (USER_APPROVAL_REQUIRED; "
            "disabled by default; never enters candidate walks; "
            "manual_task_only=True; max_concurrency=1)."
        ),
    )
    for spec in (
        ("opencode:ollama", "opencode", ("executor",)),
        ("hermes:ollama",   "hermes",   ("reviewer",)),
        ("openclaw:ollama", "openclaw", ("planner",)),
        ("claude:ollama",   "claude",   ("executor", "reviewer")),
        ("codex:ollama",    "codex",    ("executor",)),
    ):
        binding_id, tool_id, roles = spec
        reg.register(ToolModelBinding(
            binding_id=binding_id,
            tool_id=tool_id,
            roles=roles,
            **{k: v for k, v in _ollama_common.items()
               if k not in ("roles",)},
        ))
    return reg


def build_default_policy_registry() -> ToolModelPolicyRegistry:
    """Build the canonical policy registry.

    Each tool's ``candidate_bindings`` lists every binding in priority
    order. The failover engine never invents ordering. Qwen / Kimi
    candidates are included but their bindings are disabled, so the
    engine simply skips them; this is what makes the registry future-
    ready without enabling untrusted providers in P8A.
    """
    reg = ToolModelPolicyRegistry()
    # The Ollama local-failure-domain is declared in the binding registry
    # (see ``build_default_binding_registry``) but it is NOT a member of
    # the failover engine's candidate walk because that walk honours the
    # P8A invariant ``max_model_attempts=2`` / ``max_model_failovers=1``.
    # The Ollama bindings are made discoverable via ``list_for_tool(...)``
    # so the Recovery Manager and Result Push layer can call them as the
    # *last-resort* rescue path after the engine declares exhaustion.
    # This keeps existing P8A invariants stable (no second router is
    # invented) while preserving the production-eligibility contract.
    reg.register(ToolModelPolicy(
        tool_id="opencode",
        candidate_bindings=("opencode:free", "opencode:qwen",
                             "opencode:deepseek", "opencode:minimax"),
        preferred_binding="opencode:free",
        max_model_attempts=2,
        max_model_failovers=1,
        max_cost=0.10,
        max_tokens=20000,
        strict_model=None,
        allow_model_fallback=True,
        description="OpenCode executor candidates (Ollama is last-resort rescue).",
    ))
    reg.register(ToolModelPolicy(
        tool_id="hermes",
        candidate_bindings=("hermes:minimax", "hermes:qwen",
                             "hermes:deepseek"),
        preferred_binding="hermes:minimax",
        max_model_attempts=2,
        max_model_failovers=1,
        max_cost=0.20,
        max_tokens=20000,
        strict_model=None,
        allow_model_fallback=True,
        description="Hermes reviewer candidates (Ollama is last-resort rescue).",
    ))
    reg.register(ToolModelPolicy(
        tool_id="openclaw",
        candidate_bindings=("openclaw:minimax", "openclaw:qwen",
                             "openclaw:kimi"),
        preferred_binding="openclaw:minimax",
        max_model_attempts=2,
        max_model_failovers=1,
        max_cost=0.20,
        max_tokens=20000,
        strict_model=None,
        allow_model_fallback=True,
        description="OpenClaw planner candidates (Ollama is last-resort rescue).",
    ))
    reg.register(ToolModelPolicy(
        tool_id="claude",
        candidate_bindings=("claude:deepseek", "claude:qwen",
                             "claude:minimax"),
        preferred_binding="claude:deepseek",
        max_model_attempts=2,
        max_model_failovers=1,
        max_cost=0.40,
        max_tokens=40000,
        strict_model=None,
        allow_model_fallback=True,
        description="Claude specialist candidates (Ollama is last-resort rescue).",
    ))
    reg.register(ToolModelPolicy(
        tool_id="codex",
        candidate_bindings=("codex:deepseek", "codex:qwen",
                             "codex:minimax"),
        preferred_binding="codex:deepseek",
        max_model_attempts=2,
        max_model_failovers=1,
        max_cost=0.30,
        max_tokens=30000,
        strict_model=None,
        description="Codex batch executor candidates (Ollama is last-resort rescue).",
    ))
    reg.register(ToolModelPolicy(
        tool_id="minimax-official",
        candidate_bindings=("minimax-official:minimax",),
        preferred_binding="minimax-official:minimax",
        max_model_attempts=1,
        max_model_failovers=0,
        max_cost=0.30,
        max_tokens=8000,
        strict_model=None,
        allow_model_fallback=False,
        description="Task 014 MVP path: real GMI Cloud MiniMax-M3 via aios_model_gateway call_model('minimax', ...); single binding, no model fallback.",
    ))
    return reg


# ---------------------------------------------------------------------------
# Compatibility marker status constants — used by Acceptance to mark
# each (tool, binding) pair with the P8A assessment verdict.
# ---------------------------------------------------------------------------


COMPATIBILITY_SUPPORTED = "SUPPORTED"
COMPATIBILITY_SUPPORTED_WITH_ADAPTER = "SUPPORTED_WITH_ADAPTER"
COMPATIBILITY_UNVERIFIED = "UNVERIFIED"
COMPATIBILITY_UNSAFE = "UNSAFE"
COMPATIBILITY_INCOMPATIBLE = "INCOMPATIBLE"
ALL_COMPATIBILITY = (
    COMPATIBILITY_SUPPORTED,
    COMPATIBILITY_SUPPORTED_WITH_ADAPTER,
    COMPATIBILITY_UNVERIFIED,
    COMPATIBILITY_UNSAFE,
    COMPATIBILITY_INCOMPATIBLE,
)


__all__ = [
    # constants
    "FAILURE_SCOPE_RESOURCE", "FAILURE_SCOPE_BINDING",
    "FAILURE_SCOPE_TOOL_ADAPTER", "FAILURE_SCOPE_LOCAL_RUNTIME",
    "FAILURE_SCOPE_TASK_INPUT", "ALL_FAILURE_SCOPES",
    "COMPATIBILITY_SUPPORTED", "COMPATIBILITY_SUPPORTED_WITH_ADAPTER",
    "COMPATIBILITY_UNVERIFIED", "COMPATIBILITY_UNSAFE",
    "COMPATIBILITY_INCOMPATIBLE", "ALL_COMPATIBILITY",
    # dataclasses
    "SharedModelResource", "SharedModelResourceState",
    "ToolModelBinding", "ToolModelBindingState",
    "ToolModelPolicy",
    # registries
    "SharedModelResourceRegistry", "ToolModelBindingRegistry",
    "ToolModelPolicyRegistry",
    # singletons
    "get_default_resource_registry", "set_default_resource_registry",
    "get_default_binding_registry", "set_default_binding_registry",
    "get_default_policy_registry", "set_default_policy_registry",
    # builders
    "build_default_resource_registry",
    "build_default_binding_registry",
    "build_default_policy_registry",
]