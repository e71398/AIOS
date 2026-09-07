"""AIOS v0.2.0 MVP provider base classes."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# Default per-call HTTP timeout for LLM provider requests. The MVP is
# routinely pointed at CPU GGUF endpoints (LocalAI / llama.cpp) where
# the same prompt that fits in 1-2s on a hosted GPU endpoint can take
# several minutes. The 60s default was conservative; we now let the
# operator widen it through AIOS_PROVIDER_TIMEOUT_S. Six minutes is
# enough for 1200-1600 tokens on a slow CPU (~300ms/token) with a
# comfortable margin for the LLM to finish + LocalAI queue time.
DEFAULT_PROVIDER_TIMEOUT_S = float(
    os.environ.get("AIOS_PROVIDER_TIMEOUT_S", "600")
)


class ProviderError(RuntimeError):
    """Raised when a provider cannot satisfy a request."""


@dataclass
class ProviderRequest:
    messages: List[Dict[str, str]]
    model: str
    max_tokens: int = 1024
    temperature: float = 0.2
    timeout_s: float = DEFAULT_PROVIDER_TIMEOUT_S
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "finish_reason": self.finish_reason,
            "raw": self.raw,
        }


class Provider:
    name: str = "base"

    def chat(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:
        return {"ok": True, "provider": self.name}