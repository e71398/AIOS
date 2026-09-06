"""OFFLINE TEST ONLY provider for the AIOS v0.2.0 MVP.

=================================================================
WARNING: This provider is an OFFLINE TEST STUB. It is NOT a real
language model. It does NOT perform any inference, planning,
reviewing, summarising, classification, extraction, or execution.
It exists solely so the MVP can be smoke-tested in CI and on a
fresh checkout where no real LLM API key is available.

The class deliberately returns a tiny generic JSON envelope
``{"note": "offline_test_response", "tokens": N}`` for every
request. It is *not* smart enough to fake a real role output.
Any workflow that passes via this provider must be regarded as
**not** validated by a real LLM.

The orchestrator and the config layer are responsible for
ensuring this provider is only selected when the operator has
explicitly set ``AIOS_MVP_OFFLINE=1`` (or
``AIOS_MVP_ALLOW_NO_PROVIDER=1``). Production deployments must
use :class:`HTTPChatProvider` against a real model endpoint.

Module-level constant :data:`OFFLINE_TEST_PROVIDER_NAME`
(``'offline_test'``) is the single source of truth for the
provider identifier used in configuration and health output.
=================================================================
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import (
    Provider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)


OFFLINE_TEST_PROVIDER_NAME: str = "offline_test"


def _last_user_message(messages: List[Dict[str, str]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


class OfflineTestProvider(Provider):
    """OFFLINE TEST STUB provider. Not a real language model.

    Always returns a generic JSON envelope
    ``{"note": "offline_test_response", "tokens": N}``. The
    provider is intentionally trivial so it cannot be confused
    for real role output (planner / executor / reviewer).
    """

    name = OFFLINE_TEST_PROVIDER_NAME

    def __init__(self, model: str = "offline-test") -> None:
        self.model = model

    def chat(self, request: ProviderRequest) -> ProviderResponse:
        prompt = _last_user_message(request.messages)
        if not prompt:
            raise ProviderError("no user message in request")
        joined = "\n".join(str(m.get("content", "")) for m in request.messages)
        out_tokens = _estimate_tokens(prompt)
        payload = {
            "note": "offline_test_response",
            "tokens": out_tokens,
        }
        text = json.dumps(payload, ensure_ascii=False)
        if request.max_tokens and len(text) > request.max_tokens * 4:
            text = text[: request.max_tokens * 4].rstrip()
        return ProviderResponse(
            text=text,
            input_tokens=_estimate_tokens(joined),
            output_tokens=_estimate_tokens(text),
            finish_reason="stop",
            raw={
                "provider": self.name,
                "model": self.model,
                "offline_test": True,
                "prompt_chars": len(prompt),
            },
        )

    def health(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "provider": self.name,
            "provider_type": OFFLINE_TEST_PROVIDER_NAME,
            "model": self.model,
            "inference_ready": False,
            "offline_test": True,
            "warning": (
                "OfflineTestProvider is OFFLINE TEST ONLY and does "
                "not perform real inference."
            ),
        }


__all__ = ["OfflineTestProvider", "OFFLINE_TEST_PROVIDER_NAME"]