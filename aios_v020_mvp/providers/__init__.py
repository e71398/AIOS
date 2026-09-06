"""Provider adapter subpackage.

Defines a small, deterministic interface for chat completion
providers. Two concrete adapters ship:

    * ``LocalProvider``     - deterministic offline generator that
                              satisfies the same response contract
                              as an HTTP provider. This is the
                              MVP default and is used to exercise
                              the full Planner / Executor / Reviewer
                              flow without any external service.
    * ``HTTPChatProvider``  - OpenAI-compatible HTTP client used
                              for MiniMax, OpenAI, Anthropic (via
                              an OpenAI-compat shim) and any other
                              server that exposes
                              ``POST /v1/chat/completions``.

The provider interface intentionally returns plain dicts with
``{text, input_tokens, output_tokens, finish_reason, raw}``
so the orchestrator can record usage and the reviewer can
inspect the response without depending on SDK-specific types.
"""

from .base import (
    Provider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ROLE_SYSTEM,
    ROLE_USER,
    ROLE_ASSISTANT,
)
from .local_provider import LocalProvider
from .http_provider import HTTPChatProvider, build_http_provider

__all__ = [
    "Provider",
    "ProviderError",
    "ProviderRequest",
    "ProviderResponse",
    "ROLE_SYSTEM",
    "ROLE_USER",
    "ROLE_ASSISTANT",
    "LocalProvider",
    "HTTPChatProvider",
    "build_http_provider",
]
