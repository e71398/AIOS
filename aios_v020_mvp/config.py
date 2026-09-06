"""AIOS v0.2.0 MVP configuration.

All configuration is read from environment variables with safe
defaults so the MVP can boot on a clean Python install with no
external services running.

Environment variables:

    AIOS_MVP_HOST             - bind address for the entry gateway
                                 (default: 127.0.0.1)
    AIOS_MVP_PORT             - bind port (default: 18801)
    AIOS_MVP_DATA_DIR         - directory for workflow state and
                                 artefacts (default: <repo>/.aios_mvp_data)
    AIOS_MVP_PROVIDER         - provider name to use
                                 (default: ``local`` if no API key,
                                 otherwise the first available real
                                 provider from MINIMAX_API_KEY,
                                 OPENAI_API_KEY, ANTHROPIC_API_KEY).
    AIOS_MVP_PLANNER_PROVIDER - provider for planner (default = provider)
    AIOS_MVP_PLANNER_MODEL    - model name for planner
                                 (default: depends on provider)
    AIOS_MVP_EXECUTOR_PROVIDER- provider for executor (default = provider)
    AIOS_MVP_EXECUTOR_MODEL   - model name for executor
                                 (default: depends on provider)
    AIOS_MVP_REVIEWER_PROVIDER- provider for reviewer (default = provider)
    AIOS_MVP_REVIEWER_MODEL   - model name for reviewer
                                 (default: depends on provider)
    AIOS_MVP_OFFLINE          - if "1", never make external HTTP calls
    MINIMAX_API_KEY           - API key for MiniMax provider
    MINIMAX_API_BASE          - MiniMax endpoint override
    MINIMAX_MODEL             - MiniMax model name
    OPENAI_API_KEY            - OpenAI provider
    OPENAI_MODEL              - OpenAI model name
    ANTHROPIC_API_KEY         - Anthropic provider
    ANTHROPIC_MODEL           - Anthropic model name
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _repo_root() -> Path:
    """Best-effort repo root for default data dir."""
    here = Path(__file__).resolve().parent
    return here.parent


@dataclass
class ProviderSpec:
    """Concrete provider/model binding for a single role."""

    provider: str
    model: str


@dataclass
class MVPConfig:
    """Resolved MVP configuration."""

    host: str = "127.0.0.1"
    port: int = 18801
    data_dir: Path = field(default_factory=lambda: _repo_root() / ".aios_mvp_data")
    offline: bool = False

    planner: ProviderSpec = field(default_factory=lambda: ProviderSpec("local", "mvp-local"))
    executor: ProviderSpec = field(default_factory=lambda: ProviderSpec("local", "mvp-local"))
    reviewer: ProviderSpec = field(default_factory=lambda: ProviderSpec("local", "mvp-local"))

    request_timeout_s: float = 60.0

    auth_token: Optional[str] = None
    require_auth: bool = False


def _detect_provider() -> str:
    """Pick the first available real provider, else ``local``."""
    if os.environ.get("AIOS_MVP_OFFLINE", "0") == "1":
        return "local"
    if os.environ.get("MINIMAX_API_KEY"):
        return "minimax"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "local"


def _resolve_spec(env_provider: str, env_model: str, default_provider: str) -> ProviderSpec:
    provider = os.environ.get(env_provider, default_provider).strip().lower()
    default_model = "mvp-local" if provider == "local" else "unknown-model"
    model = os.environ.get(env_model, default_model).strip()
    return ProviderSpec(provider=provider, model=model)


def load_config() -> MVPConfig:
    """Build an :class:`MVPConfig` from the current environment."""
    cfg = MVPConfig()

    cfg.host = os.environ.get("AIOS_MVP_HOST", cfg.host)
    cfg.port = int(os.environ.get("AIOS_MVP_PORT", str(cfg.port)))
    cfg.data_dir = Path(os.environ.get("AIOS_MVP_DATA_DIR", str(cfg.data_dir))).resolve()
    cfg.offline = os.environ.get("AIOS_MVP_OFFLINE", "0") == "1"

    default_provider = _detect_provider()
    cfg.planner = _resolve_spec(
        "AIOS_MVP_PLANNER_PROVIDER", "AIOS_MVP_PLANNER_MODEL", default_provider
    )
    cfg.executor = _resolve_spec(
        "AIOS_MVP_EXECUTOR_PROVIDER", "AIOS_MVP_EXECUTOR_MODEL", default_provider
    )
    cfg.reviewer = _resolve_spec(
        "AIOS_MVP_REVIEWER_PROVIDER", "AIOS_MVP_REVIEWER_MODEL", default_provider
    )

    cfg.request_timeout_s = float(os.environ.get("AIOS_MVP_TIMEOUT", str(cfg.request_timeout_s)))
    cfg.auth_token = os.environ.get("AIOS_MVP_TOKEN")
    cfg.require_auth = os.environ.get("AIOS_MVP_REQUIRE_AUTH", "0") == "1"

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
