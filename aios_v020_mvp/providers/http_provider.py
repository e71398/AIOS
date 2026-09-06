"""Real OpenAI-compatible HTTP chat completion provider.

Used to drive the Executor (and optionally Planner/Reviewer)
against a real model API. Supports:

    * MiniMax       - https://api.minimaxi.com/v1
    * OpenAI        - https://api.openai.com/v1
    * Anthropic     - https://api.anthropic.com/v1 (via OpenAI
                       compat shim if available, or native call
                       adapter)
    * Any other      - base URL + api key + model name

The provider is intentionally tiny: it serializes the request
to JSON, posts it, parses the response, and returns the standard
``ProviderResponse`` envelope. No streaming, no tools, no vision.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .base import (
    Provider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)


_PROFILES: Dict[str, Dict[str, str]] = {
    "minimax": {
        "base": os.environ.get(
            "MINIMAX_API_BASE", "https://api.minimaxi.com/v1"
        ),
        "model_default": os.environ.get("MINIMAX_MODEL", "MiniMax-M3"),
        "key_env": "MINIMAX_API_KEY",
    },
    "openai": {
        "base": os.environ.get(
            "OPENAI_API_BASE", "https://api.openai.com/v1"
        ),
        "model_default": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "key_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "base": os.environ.get(
            "ANTHROPIC_API_BASE", "https://api.anthropic.com/v1"
        ),
        "model_default": os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
        "key_env": "ANTHROPIC_API_KEY",
    },
    "localai": {
        "base": os.environ.get(
            "LOCALAI_API_BASE", "http://127.0.0.1:8080/v1"
        ),
        "model_default": os.environ.get(
            "LOCALAI_MODEL", "qwen2.5-coder-7b-instruct-q5_K_M"
        ),
        "key_env": "LOCALAI_API_KEY",
    },
}


def build_http_provider(provider: str, model: str) -> "HTTPChatProvider":
    """Construct an :class:`HTTPChatProvider` for a known provider name.

    Raises :class:`ProviderError` if the required API key is missing.
    """
    name = provider.strip().lower()
    if name not in _PROFILES:
        raise ProviderError(f"unknown http provider: {provider!r}")
    profile = _PROFILES[name]
    api_key = os.environ.get(profile["key_env"], "")
    if not api_key and name != "localai":
        raise ProviderError(
            f"{profile['key_env']} not set; cannot use {name} provider"
        )
    if not api_key:
        # LocalAI commonly allows unauthenticated access. Use a benign
        # placeholder so the Authorization header stays well-formed.
        api_key = "not-needed"
    return HTTPChatProvider(
        name=name,
        base_url=profile["base"],
        api_key=api_key,
        model=model or profile["model_default"],
    )


class HTTPChatProvider(Provider):
    """OpenAI-compatible chat completion HTTP client."""

    def __init__(self, name: str, base_url: str, api_key: str, model: str) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    # ------------------------------------------------------------------
    # Provider API
    # ------------------------------------------------------------------

    def chat(self, request: ProviderRequest) -> ProviderResponse:
        url = f"{self.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": request.model or self.model,
            "messages": list(request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=request.timeout_s) as resp:
                raw_bytes = resp.read()
                raw = json.loads(raw_bytes.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise ProviderError(
                f"{self.name} http {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"{self.name} connection error: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{self.name} bad json: {exc}") from exc

        try:
            choice = raw["choices"][0]
            text = choice["message"]["content"]
            finish = choice.get("finish_reason", "stop")
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"{self.name} unexpected response shape: {exc}"
            ) from exc
        usage = raw.get("usage", {})
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        return ProviderResponse(
            text=text or "",
            input_tokens=in_tok,
            output_tokens=out_tok,
            finish_reason=finish or "stop",
            raw={"provider": self.name, "model": self.model, "raw": raw},
        )

    def health(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "provider": self.name,
            "provider_type": "real_llm",
            "model": self.model,
            "base_url": self.base_url,
            "has_key": bool(self.api_key),
            "inference_ready": bool(self.api_key),
        }
