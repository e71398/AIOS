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
import threading
import time
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


# ----------------------------------------------------------------------
# JSONL sink shared across processes
# ----------------------------------------------------------------------
#
# The closeout 5/5 E2E harness spawns the AIOS gateway in a subprocess.
# The harness wants every real provider call written to a JSONL file
# for post-run analysis. Setting ``HTTPChatProvider._call_sink`` on the
# harness process does NOT reach the server subprocess because they
# import the class separately.
#
# The fix is to honor an environment variable, ``AIOS_PROVIDER_SINK``,
# pointing at a writable file path. The sink appends one JSON object
# per HTTP call. Writes are guarded by a process-wide lock so that
# threads inside the gateway never interleave a JSON line.

_SINK_PATH = os.environ.get("AIOS_PROVIDER_SINK", "").strip()
_SINK_LOCK = threading.Lock()


def _env_sink(record: Dict[str, Any]) -> None:
    if not _SINK_PATH:
        return
    try:
        line = json.dumps(record, ensure_ascii=False)
    except Exception:
        return
    with _SINK_LOCK:
        try:
            with open(_SINK_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # never let sink failures break the provider
            pass


class HTTPChatProvider(Provider):
    """OpenAI-compatible chat completion HTTP client."""

    # Optional per-class sink used by the closeout test harness to
    # record every real HTTP call. Set externally to a callable
    # ``sink(record: dict) -> None``; left None in production.
    # If unset, the provider falls back to the env-driven
    # ``_env_sink`` so subprocess-based harnesses still capture
    # every call.
    _call_sink = None

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
        sink = HTTPChatProvider._call_sink or _env_sink
        t0 = time.time()
        http_status = None
        body_excerpt = None
        try:
            with urllib.request.urlopen(req, timeout=request.timeout_s) as resp:
                http_status = resp.status
                raw_bytes = resp.read()
                raw = json.loads(raw_bytes.decode("utf-8"))
                body_excerpt = raw_bytes[:200].decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            if sink is not None:
                try:
                    sink({
                        "ts": time.time(),
                        "role": _infer_role(request.messages, request.metadata),
                        "model": request.model or self.model,
                        "provider": self.name,
                        "url": url,
                        "http_status": exc.code,
                        "latency_ms": int((time.time() - t0) * 1000),
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "body_excerpt": detail,
                        "metadata": dict(request.metadata or {}),
                    })
                except Exception:
                    pass
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
        latency_ms = int((time.time() - t0) * 1000)
        role = _infer_role(request.messages, request.metadata)
        if sink is not None:
            try:
                sink({
                    "ts": time.time(),
                    "role": role,
                    "model": request.model or self.model,
                    "provider": self.name,
                    "url": url,
                    "http_status": http_status,
                    "latency_ms": latency_ms,
                    "prompt_tokens": in_tok,
                    "completion_tokens": out_tok,
                    "body_excerpt": body_excerpt,
                    "metadata": dict(request.metadata or {}),
                })
            except Exception:
                pass
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


def _infer_role(messages, metadata=None) -> str:
    """Determine which AIOS role issued this provider call.

    ``metadata["role"]`` is authoritative: all three roles now stamp
    it explicitly on every :class:`ProviderRequest`. Content sniffing
    is only a legacy fallback and is unreliable because a role's
    prompt embeds the *previous* role's JSON output (which contains
    the literal word "planner" / "executor" / "reviewer").
    """
    if isinstance(metadata, dict):
        explicit = str(metadata.get("role") or "").strip().lower()
        if explicit in ("planner", "executor", "reviewer"):
            phase = str(metadata.get("phase") or "").strip().lower()
            return f"{explicit}_repair" if phase == "repair" else explicit
    role = "unknown"
    for msg in messages:
        content = str(msg.get("content", "")).lower()
        if "aios planner" in content:
            role = "planner"
        elif "aios executor" in content:
            role = "executor"
        elif "aios reviewer" in content:
            role = "reviewer"
    return role