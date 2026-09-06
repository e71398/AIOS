"""AIOS v0.2.0 MVP provider base classes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


class ProviderError(RuntimeError):
    """Raised when a provider cannot satisfy a request."""


@dataclass
class ProviderRequest:
    messages: List[Dict[str, str]]
    model: str
    max_tokens: int = 1024
    temperature: float = 0.2
    timeout_s: float = 60.0
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
