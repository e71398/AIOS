#!/usr/bin/env python3
"""AIOS P8B-R MiniMax HTTP Client.

A small, dependency-free MiniMax client used by the Claude /
Codex cross-tool minimax bindings. Each ``MiniMaxClient`` instance
owns its own ``base_url``, ``api_key``, ``model`` and ``timeout``;
the instance is the *only* object that knows about the secret, so
binding a different resource means creating a new client — not
mutating a shared one.

Architectural guarantees:

* **No shared mutable state.** ``self._session_token`` is created
  per-instance; no class-level globals store credentials or URLs.
* **Subprocess-safe env override.** :func:`with_subprocess_env`
  returns a copy of ``os.environ`` with the per-instance URL /
  API key injected under well-known ``AIOS_*`` variables; the
  caller can pass this to ``subprocess.run(..., env=...)`` without
  ever mutating the parent's environment.
* **Per-call model override.** :func:`chat` accepts
  ``model_override`` so callers can route one task through a
  different model without touching the client instance.
* **Cost / token accounting.** Every successful call returns
  ``usage`` (input/output/total tokens) populated from the
  response. The caller propagates these to the failover engine
  via :class:`ModelAttemptRecord`.
* **Failure classification.** Errors are mapped to the P8A
  ``FAILURE_SCOPE_*`` taxonomy; ``adapter_response_present=False``
  is the default for transport / DNS / TLS / timeout errors.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from aios_model_resources import (
    FAILURE_SCOPE_BINDING,
    FAILURE_SCOPE_RESOURCE,
    FAILURE_SCOPE_TOOL_ADAPTER,
)

# Default MiniMax env var names. Per-instance overrides are still
# possible via ``with_subprocess_env()``.
DEFAULT_BASE_URL_ENV = "AIOS_MINIMAX_BASE_URL"
DEFAULT_API_KEY_ENV = "AIOS_MINIMAX_API_KEY"
# Existing P8A convention uses MINIMAX_CN_BASE_URL for the public
# endpoint; both are accepted.
FALLBACK_BASE_URL_ENV = "MINIMAX_CN_BASE_URL"
FALLBACK_API_KEY_ENV = "MINIMAX_API_KEY"

DEFAULT_MODEL = "MiniMax-M3"
DEFAULT_TIMEOUT = 90


@dataclass
class MiniMaxUsage:
    """Account-level usage returned by MiniMax.

    The same fields map to :class:`ModelAttemptRecord` in the
    failover engine. ``input_tokens`` / ``output_tokens`` are the
    canonical names; ``total_tokens`` is the convenience sum.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "total_tokens": int(self.total_tokens),
            "estimated_cost": float(self.estimated_cost),
        }


@dataclass
class MiniMaxCallResult:
    """Result of one chat call.

    ``success`` mirrors :class:`ModelAttemptRecord.success`; on
    failure ``failure_kind`` / ``failure_scope`` are populated so
    the caller can forward them straight to the failover engine
    without re-classifying.
    """

    success: bool
    content: str = ""
    usage: MiniMaxUsage = field(default_factory=MiniMaxUsage)
    failure_kind: Optional[str] = None
    failure_scope: Optional[str] = None
    error_message: Optional[str] = None
    adapter_response_present: bool = True
    model_used: Optional[str] = None
    duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": bool(self.success),
            "content": str(self.content),
            "usage": self.usage.to_dict(),
            "failure_kind": self.failure_kind,
            "failure_scope": self.failure_scope,
            "error_message": self.error_message,
            "adapter_response_present": bool(self.adapter_response_present),
            "model_used": self.model_used,
            "duration_seconds": float(self.duration_seconds),
        }


class MiniMaxClient:
    """A self-contained MiniMax client.

    The instance owns exactly one ``base_url`` / ``api_key`` /
    ``model`` tuple. Multiple tools sharing the same
    ``minimax.shared`` resource each get their own instance, but
    the underlying account state is held externally in the
    failover engine — not here.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
        per_1k_input_cost: float = 0.002,
        per_1k_output_cost: float = 0.002,
        resource_id: str = "minimax.shared",
        binding_id: str = "anonymous",
        tool_id: str = "anonymous",
    ) -> None:
        if model and not isinstance(model, str):
            raise TypeError("model must be a string")
        self.base_url = (
            base_url
            or os.environ.get(DEFAULT_BASE_URL_ENV)
            or os.environ.get(FALLBACK_BASE_URL_ENV)
            or ""
        )
        self.api_key = (
            api_key
            or os.environ.get(DEFAULT_API_KEY_ENV)
            or os.environ.get(FALLBACK_API_KEY_ENV)
            or ""
        )
        self.model = str(model or DEFAULT_MODEL)
        self.timeout = int(timeout)
        self.per_1k_input_cost = float(per_1k_input_cost)
        self.per_1k_output_cost = float(per_1k_output_cost)
        self.resource_id = str(resource_id)
        self.binding_id = str(binding_id)
        self.tool_id = str(tool_id)

    # ------------------------------------------------------------------
    # Env override (subprocess isolation)
    # ------------------------------------------------------------------

    def with_subprocess_env(self, base: Optional[Mapping[str, str]] = None
                             ) -> Dict[str, str]:
        """Return a copy of ``base`` (or ``os.environ``) with this
        client's URL and key injected under the canonical
        ``AIOS_*`` names.

        The returned dict is freshly allocated; mutating it does
        not affect the parent process environment. This is the
        *only* way external adapters should pick up a binding's
        credentials — they MUST NOT call ``os.environ[...] = ...``
        at module scope.
        """
        env = dict(base if base is not None else os.environ)
        if self.base_url:
            env[DEFAULT_BASE_URL_ENV] = str(self.base_url)
            # Mirror to the legacy / fallback env name so adapters
            # that read either variable pick up the override.
            env[FALLBACK_BASE_URL_ENV] = str(self.base_url)
        if self.api_key:
            env[DEFAULT_API_KEY_ENV] = str(self.api_key)
            env[FALLBACK_API_KEY_ENV] = str(self.api_key)
        return env

    # ------------------------------------------------------------------
    # HTTP call
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: Any,
        *,
        model_override: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
        fake_response: Optional[str] = None,
        fake_usage: Optional[Mapping[str, int]] = None,
        fake_failure: Optional[str] = None,
    ) -> MiniMaxCallResult:
        """Send a chat completion request.

        ``fake_response`` / ``fake_usage`` / ``fake_failure`` are
        honoured when set so tests can drive the binding without
        hitting the network. A real call is only attempted when
        all three are ``None``.
        """
        if fake_failure is not None:
            return MiniMaxCallResult(
                success=False,
                failure_kind=str(fake_failure),
                failure_scope=_classify_failure(str(fake_failure)),
                error_message=f"forced failure: {fake_failure}",
                adapter_response_present=True,
                model_used=model_override or self.model,
            )
        if fake_response is not None:
            in_tok = int((fake_usage or {}).get("input_tokens", 0))
            out_tok = int((fake_usage or {}).get("output_tokens", 0))
            total = int((fake_usage or {}).get("total_tokens", 0))
            if total == 0 and (in_tok or out_tok):
                total = in_tok + out_tok
            usage = MiniMaxUsage(
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=total,
            )
            return MiniMaxCallResult(
                success=True,
                content=str(fake_response),
                usage=usage,
                model_used=model_override or self.model,
                duration_seconds=0.0,
            )
        if not self.base_url or not self.api_key:
            return MiniMaxCallResult(
                success=False,
                failure_kind="invalid_local_configuration",
                failure_scope=FAILURE_SCOPE_TOOL_ADAPTER,
                error_message="MiniMax base_url or api_key missing",
                adapter_response_present=False,
                model_used=model_override or self.model,
            )
        return self._real_chat(
            messages,
            model_override=model_override,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    def _real_chat(
        self,
        messages: Any,
        *,
        model_override: Optional[str],
        temperature: float,
        max_tokens: Optional[int],
        timeout: Optional[int],
    ) -> MiniMaxCallResult:
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        body: Dict[str, Any] = {
            "model": model_override or self.model,
            "messages": messages,
            "temperature": float(temperature),
        }
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req,
                                        timeout=timeout or self.timeout) as resp:
                payload = resp.read().decode("utf-8")
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            kind, scope = _classify_http(e.code, body_txt)
            return MiniMaxCallResult(
                success=False,
                failure_kind=kind,
                failure_scope=scope,
                error_message=f"HTTP {e.code}: {body_txt[:200]}",
                adapter_response_present=True,
                model_used=model_override or self.model,
                duration_seconds=time.time() - t0,
            )
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as e:
            return MiniMaxCallResult(
                success=False,
                failure_kind="external_timeout" if isinstance(e, TimeoutError) else "external_network_error",
                failure_scope=FAILURE_SCOPE_BINDING,
                error_message=str(e),
                adapter_response_present=False,
                model_used=model_override or self.model,
                duration_seconds=time.time() - t0,
            )
        except Exception as e:  # noqa: BLE001
            return MiniMaxCallResult(
                success=False,
                failure_kind="local_adapter_exception",
                failure_scope=FAILURE_SCOPE_TOOL_ADAPTER,
                error_message=str(e),
                adapter_response_present=False,
                model_used=model_override or self.model,
                duration_seconds=time.time() - t0,
            )

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as e:
            return MiniMaxCallResult(
                success=False,
                failure_kind="malformed_response_local",
                failure_scope=FAILURE_SCOPE_TOOL_ADAPTER,
                error_message=f"json decode error: {e}; payload[:200]={payload[:200]!r}",
                adapter_response_present=True,
                model_used=model_override or self.model,
                duration_seconds=time.time() - t0,
            )
        return _parse_payload(parsed, model_override or self.model,
                              self.per_1k_input_cost, self.per_1k_output_cost,
                              time.time() - t0)


def _parse_payload(payload: Dict[str, Any], model: str,
                    in_cost: float, out_cost: float,
                    duration: float) -> MiniMaxCallResult:
    """Parse an OpenAI-compatible chat response into a MiniMaxCallResult."""
    if not isinstance(payload, dict):
        return MiniMaxCallResult(
            success=False,
            failure_kind="malformed_response_local",
            failure_scope=FAILURE_SCOPE_TOOL_ADAPTER,
            error_message=f"unexpected payload type {type(payload).__name__}",
            adapter_response_present=True,
            model_used=model,
            duration_seconds=duration,
        )
    if "error" in payload and isinstance(payload["error"], dict):
        err = payload["error"]
        msg = str(err.get("message", "")) or "unknown error"
        kind, scope = _classify_message(msg)
        return MiniMaxCallResult(
            success=False,
            failure_kind=kind,
            failure_scope=scope,
            error_message=msg[:300],
            adapter_response_present=True,
            model_used=model,
            duration_seconds=duration,
        )
    choices = payload.get("choices") or []
    if not choices:
        return MiniMaxCallResult(
            success=False,
            failure_kind="malformed_response_local",
            failure_scope=FAILURE_SCOPE_TOOL_ADAPTER,
            error_message="no choices in response",
            adapter_response_present=True,
            model_used=model,
            duration_seconds=duration,
        )
    content = ""
    try:
        content = str(choices[0].get("message", {}).get("content", "") or "")
    except Exception:
        content = ""
    usage_raw = payload.get("usage") or {}
    in_tok = int(usage_raw.get("prompt_tokens", 0) or 0)
    out_tok = int(usage_raw.get("completion_tokens", 0) or 0)
    total = int(usage_raw.get("total_tokens", in_tok + out_tok) or (in_tok + out_tok))
    est_cost = (in_tok * in_cost + out_tok * out_cost) / 1000.0
    return MiniMaxCallResult(
        success=True,
        content=content,
        usage=MiniMaxUsage(
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=total,
            estimated_cost=est_cost,
        ),
        model_used=model,
        duration_seconds=duration,
    )


def _classify_http(code: int, body: str) -> Tuple[str, str]:
    txt = (body or "").lower()
    if code in (401, 403):
        return "invalid_local_configuration", FAILURE_SCOPE_TOOL_ADAPTER
    if code == 402 or "insufficient" in txt or "quota" in txt or "balance" in txt:
        return "quota_exhausted", FAILURE_SCOPE_RESOURCE
    if code == 429 or "rate limit" in txt or "too many requests" in txt:
        return "rate_limited", FAILURE_SCOPE_RESOURCE
    if code in (500, 502, 503, 504) or "unavailable" in txt:
        return "provider_unavailable", FAILURE_SCOPE_RESOURCE
    return "external_contract_failure", FAILURE_SCOPE_RESOURCE


def _classify_message(msg: str) -> Tuple[str, str]:
    txt = (msg or "").lower()
    if "tool" in txt and "not supported" in txt:
        return "token_plan", FAILURE_SCOPE_BINDING
    if "quota" in txt or "insufficient" in txt or "balance" in txt:
        return "quota_exhausted", FAILURE_SCOPE_RESOURCE
    if "rate limit" in txt:
        return "rate_limited", FAILURE_SCOPE_RESOURCE
    if "unavailable" in txt or "cooldown" in txt:
        return "external_service_cooldown", FAILURE_SCOPE_RESOURCE
    if "timeout" in txt:
        return "external_timeout", FAILURE_SCOPE_BINDING
    return "external_contract_failure", FAILURE_SCOPE_RESOURCE


def _classify_failure(kind: str) -> str:
    return {
        "quota_exhausted": FAILURE_SCOPE_RESOURCE,
        "insufficient_balance": FAILURE_SCOPE_RESOURCE,
        "rate_limited": FAILURE_SCOPE_RESOURCE,
        "token_plan": FAILURE_SCOPE_BINDING,
        "provider_unavailable": FAILURE_SCOPE_RESOURCE,
        "external_timeout": FAILURE_SCOPE_BINDING,
        "external_network_error": FAILURE_SCOPE_BINDING,
        "external_service_cooldown": FAILURE_SCOPE_RESOURCE,
        "external_contract_failure": FAILURE_SCOPE_RESOURCE,
    }.get(kind, FAILURE_SCOPE_TOOL_ADAPTER)


__all__ = [
    "MiniMaxClient", "MiniMaxUsage", "MiniMaxCallResult",
    "DEFAULT_BASE_URL_ENV", "DEFAULT_API_KEY_ENV",
    "FALLBACK_BASE_URL_ENV", "FALLBACK_API_KEY_ENV",
    "DEFAULT_MODEL", "DEFAULT_TIMEOUT",
]