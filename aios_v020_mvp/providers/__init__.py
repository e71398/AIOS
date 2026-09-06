"""Provider adapter subpackage.

Defines a small, deterministic interface for chat completion
providers. Two concrete adapters ship:

    * ``OfflineTestProvider`` - OFFLINE TEST STUB. NOT a real LLM.
                              Returns a fixed JSON envelope for
                              every request. Used only when
                              ``AIOS_MVP_OFFLINE=1`` is set. Do
                              not use for production validation.
    * ``HTTPChatProvider``   - OpenAI-compatible HTTP client used
                              for MiniMax, OpenAI, Anthropic, the
                              bundled ``localai`` profile (which
                              defaults to LocalAI on
                              ``http://127.0.0.1:8080/v1``) and any
                              other server that exposes
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
from .local_provider import OfflineTestProvider, OFFLINE_TEST_PROVIDER_NAME
from .http_provider import HTTPChatProvider, build_http_provider

__all__ = [
    "Provider",
    "ProviderError",
    "ProviderRequest",
    "ProviderResponse",
    "ROLE_SYSTEM",
    "ROLE_USER",
    "ROLE_ASSISTANT",
    "OfflineTestProvider",
    "OFFLINE_TEST_PROVIDER_NAME",
    "HTTPChatProvider",
    "build_http_provider",
]
