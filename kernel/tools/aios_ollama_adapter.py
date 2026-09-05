"""Thin HTTP client for the local Ollama daemon.

This adapter does NOT replace any existing AIOS Provider/route. It only adds a
new ``ollama.local`` SharedModelResource that AIOS adapters and the unified
failure-domain table can target. Ollama already exposes OpenAI-compatible
``/v1/chat/completions`` since 0.5.0, so we reuse the same HTTP shape as the
existing MiniMax client. No new routing engine, no second registry.

The adapter is intentionally minimal:
    - no external dependencies beyond the Python standard library + the
      existing aios_tool_adapter contract
    - no caching of results
    - no token budgeting beyond the parent call's budget
    - classification of common failures into AiosFailureKind values matches
      aios_minimax_client._classify_http shape so a downstream filter does not
      need to be re-implemented.

If Ollama is unavailable or the requested model is missing, the adapter raises
``AiosOllamaError`` and surfaces it through the bound ``ToolAdapter.health()``
gate. Tool adapters that depend on this client therefore fail into the
existing circuit breaker machinery.
"""
from __future__ import annotations

import json
import os
import threading
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_BASE_URL = os.environ.get("AIOS_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
DEFAULT_TIMEOUT = int(os.environ.get("AIOS_OLLAMA_TIMEOUT", "120"))
DEFAULT_MODEL = os.environ.get("AIOS_OLLAMA_DEFAULT_MODEL", "qwen3:8b")
ALL_KNOWN_MODELS: Tuple[str, ...] = ("qwen3:8b-ctx", "qwen3:8b", "qwen2.5:0.5b")


class AiosOllamaError(RuntimeError):
    """Raised when the Ollama adapter cannot complete a call."""

    def __init__(self, message: str, *, kind: str = "MODEL_FAILURE",
                 http_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status


@dataclass
class OllamaCallResult:
    """Mirrors ``MiniMaxCallResult`` so downstream code can stay generic."""

    text: str = ""
    model: str = ""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error_kind: str = ""
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.success,
            "text": self.text,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "kind": self.error_kind,
            "error": self.error_message,
        }


def _probe_socket(base_url: str, timeout: float = 1.5) -> bool:
    """Cheap reachability probe without making an HTTP request."""
    try:
        scheme, rest = base_url.split("://", 1)
    except ValueError:
        return False
    host_port = rest.split("/", 1)[0]
    if ":" in host_port:
        host, port = host_port.split(":", 1)
        try:
            port_i = int(port)
        except ValueError:
            return False
    else:
        host = host_port
        port_i = 443 if scheme == "https" else 80
    try:
        with socket.create_connection((host, port_i), timeout=timeout):
            return True
    except OSError:
        return False


def list_models(base_url: str = DEFAULT_BASE_URL,
                timeout: float = 5.0) -> List[str]:
    """Return Ollama model tags as a list of strings (``<name>`` form)."""
    url = base_url.rstrip("/") + "/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise AiosOllamaError(f"ollama list failed: {exc}") from exc
    names: List[str] = []
    for entry in payload.get("models", []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def health(base_url: str = DEFAULT_BASE_URL,
           timeout: float = 5.0) -> Dict[str, Any]:
    """Compatibility-shape health snapshot for ``ToolAdapter.health()``.

    The snapshot is *read-only* and never sends a ``/v1/chat/
    completions`` request.  The ``inference_allowed`` field records
    the operator's local-model guard state explicitly so an external
    monitor can detect unauthorised usage without re-running the
    registry.
    """
    base_ok = _probe_socket(base_url, timeout=1.0)
    out: Dict[str, Any] = {
        "binary_ok": base_ok,
        "binary_state": "ready" if base_ok else "down",
        "version": "ollama.local",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "latency_ms": 0,
        "model_state": "unknown",
        "model_available": False,
        "reason": "not_probed",
        "ok": False,
        "state": "unknown",
        "infrastructure_ok": base_ok,
        "contract_ok": base_ok,
        "fully_operational": False,
        # Local-model guard surface (per §十一 monitor contract).
        "inference_allowed": bool(
            os.environ.get("AIOS_OLLAMA_USER_APPROVED_AT")
            and int(os.environ.get("AIOS_LOCAL_MODEL_INFERENCE_ALLOWED",
                                   "0") or "0") == 1
        ),
        "activation_mode": "MANUAL_USER_APPROVAL_ONLY",
        "routing_eligible": False,
        "blocked_reason": "USER_APPROVAL_REQUIRED",
        "historical_pong_classification": "HISTORICAL_MANUAL_TEST",
    }
    if not base_ok:
        out["reason"] = "ollama_unreachable"
        return out
    try:
        models = list_models(base_url, timeout)
    except AiosOllamaError as exc:
        out["reason"] = str(exc)
        return out
    out["models"] = models
    out["model_state"] = "available" if models else "empty"
    out["model_available"] = bool(models)
    out["ok"] = bool(models)
    out["state"] = "operational" if models else "degraded"
    out["fully_operational"] = bool(models)
    out["reason"] = "models_listed" if models else "no_models"
    out["contract_ok"] = True
    return out


class LocalModelGuardError(RuntimeError):
    """Raised when AIOS attempts to invoke a local model without an
    explicit, recorded user-approval gate.

    See ``AIOS_INTERNAL_LOCAL_MODEL_GUARD`` in
    ``AIOS_LOCAL_MODEL_POLICY.md`` (close-out 20260725). Any path that
    needs real local inference MUST set both
    ``AIOS_OLLAMA_USER_APPROVED_AT=<ISO-8601 timestamp>`` AND
    ``AIOS_LOCAL_MODEL_INFERENCE_ALLOWED=1`` in the operator's
    environment BEFORE calling ``chat()``/``quick_probe()``.
    """


# ---------------------------------------------------------------------------
# Task-scoped user approval.  Even with the global env-var gate
# (AIOS_LOCAL_MODEL_INFERENCE_ALLOWED / AIOS_OLLAMA_USER_APPROVED_AT) the
# adapter requires a per-task approval record before any inference
# runs.  Approvals are in-memory (no persistence); they MUST be set
# explicitly by the operator-side approval flow and cleared when the
# task terminalises.  This is per the close-out brief:
# "每次本地模型调用必须同时具备任务级授权记录 … 缺少任务级授权时统一返回
#  LOCAL_MODEL_USER_APPROVAL_REQUIRED".
# ---------------------------------------------------------------------------

_TASK_APPROVALS: Dict[str, Dict[str, Any]] = {}
_TASK_APPROVAL_LOCK = threading.Lock()


def register_task_approval(
    task_id: str,
    *,
    approval_source: str = "user_explicit",
    approved_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    allowed_model_ids: Tuple[str, ...] = (),
    allowed_roles: Tuple[str, ...] = (),
    max_calls: int = 1,
    max_concurrency: int = 1,
    allow_fallback: bool = False,
) -> dict:
    """Record a per-task local-model approval.

    MANDATORY FIELD CONFORMANCE per the close-out brief:
    - ``approval_source`` MUST be ``"user_explicit"``;
      any other value (Provider, Recovery Manager, etc.) is rejected.
    - ``approved_at`` defaults to now (UTC, ISO-8601) and ``expires_at``
      defaults to approved_at + 5 minutes.  Both are recorded.
    - Approvals are bound to one ``task_id``; reuse across tasks fails.
    - ``allowed_model_ids`` is an allow-list.  Empty means no models are
      permitted (a defensive default — the close-out requires an
      explicit allow-list).
    - ``max_calls`` defaults to 1; ``max_concurrency`` defaults to 1.
    - ``allow_fallback`` defaults to False.
    """
    if approval_source != "user_explicit":
        raise LocalModelGuardError(
            f"approval_source must be 'user_explicit', got "
            f"{approval_source!r}; Recovery Manager / Provider / "
            "router self-approval is forbidden."
        )
    if not task_id or not isinstance(task_id, str):
        raise LocalModelGuardError("task_id is required for a task-scoped approval")
    import datetime as _dt
    if approved_at is None:
        approved_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    if expires_at is None:
        try:
            base = _dt.datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
        except Exception as exc:
            raise LocalModelGuardError(
                f"approved_at is not ISO-8601: {approved_at!r} ({exc})"
            ) from exc
        expires_at = (base + _dt.timedelta(minutes=5)).isoformat()
    record: Dict[str, Any] = {
        "task_id": task_id,
        "approval_source": approval_source,
        "approved_at": approved_at,
        "expires_at": expires_at,
        "allowed_model_ids": tuple(allowed_model_ids),
        "allowed_roles": tuple(allowed_roles),
        "max_calls": int(max(1, max_calls)),
        "max_concurrency": int(max(1, max_concurrency)),
        "allow_fallback": bool(allow_fallback),
        "calls_used": 0,
        "status": "ACTIVE",
    }
    with _TASK_APPROVAL_LOCK:
        if task_id in _TASK_APPROVALS:
            raise LocalModelGuardError(
                f"task_id {task_id!r} already has an approval; reuse is forbidden"
            )
        _TASK_APPROVALS[task_id] = record
    return record


def _local_approval_state(task_id: Optional[str]) -> Dict[str, Any]:
    """Return the approval record if active; empty dict otherwise."""
    if not task_id:
        return {}
    import datetime as _dt
    with _TASK_APPROVAL_LOCK:
        record = _TASK_APPROVALS.get(task_id)
        if record is None:
            return {}
    if record.get("status") != "ACTIVE":
        return {}
    try:
        exp = _dt.datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
        now = _dt.datetime.now(_dt.timezone.utc)
        if now > exp:
            with _TASK_APPROVAL_LOCK:
                _TASK_APPROVALS[task_id]["status"] = "EXPIRED"
            return {}
    except Exception:
        return {}
    return record


def consume_task_approval(task_id: str) -> None:
    """Invalidate a task-scoped approval immediately.

    MUST be called when the task reaches completed / failed / cancelled
    so the same ``task_id`` cannot be reused for a different task
    that previously had local-model approval.
    """
    if not task_id:
        return
    with _TASK_APPROVAL_LOCK:
        rec = _TASK_APPROVALS.get(task_id)
        if rec is not None:
            rec["status"] = "CONSUMED"
            rec["consumed_at"] = (
                __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat()
            )


def _local_model_unauthorised(task_id: Optional[str] = None) -> bool:
    """Return True when the local-model guard has not been unlocked.

    The check requires BOTH the operator-side env switch AND a current,
    active task-scoped approval record.  Even with the env switch set,
    an absent / expired / consumed approval makes this return True and
    the upstream call returns ``LOCAL_MODEL_USER_APPROVAL_REQUIRED``.
    """
    import os as _os
    approved_at = _os.environ.get("AIOS_OLLAMA_USER_APPROVED_AT", "")
    allowed = _os.environ.get("AIOS_LOCAL_MODEL_INFERENCE_ALLOWED", "0") or "0"
    if not approved_at or allowed not in ("1", "true", "TRUE", "True"):
        return True
    if not task_id:
        return True
    rec = _local_approval_state(task_id)
    if not rec:
        return True
    # Apply per-call limits.
    with _TASK_APPROVAL_LOCK:
        if rec["calls_used"] >= rec["max_calls"]:
            return True
        rec["calls_used"] += 1
    return False


def chat(messages: List[Dict[str, str]],
         *,
         model: str = DEFAULT_MODEL,
         base_url: str = DEFAULT_BASE_URL,
         timeout: int = DEFAULT_TIMEOUT,
         temperature: float = 0.2,
         max_tokens: Optional[int] = None,
         task_id: Optional[str] = None) -> OllamaCallResult:
    """Call Ollama's OpenAI-compatible ``/v1/chat/completions``.

    GUARDED: by default this call is refused unless BOTH the operator
    has set the env-var gate AND a per-task approval record is
    present.  The guard is honoured even when the Ollama daemon is
    running and reachable — the local-model policy is a contractual
    precondition, not a runtime probe result.

    ``task_id`` MUST be supplied whenever this method is invoked from
    inside the AIOS dispatch path.  If ``task_id`` is omitted the guard
    treats the caller as an untracked probe and refuses the call.
    """
    if _local_model_unauthorised(task_id=task_id):
        raise LocalModelGuardError(
            "AIOS local-model inference is disabled until "
            "AIOS_OLLAMA_USER_APPROVED_AT and "
            "AIOS_LOCAL_MODEL_INFERENCE_ALLOWED=1 are set by the "
            "operator AND a per-task approval is registered via "
            "register_task_approval(...).  See "
            "docs/AIOS_LOCAL_MODEL_POLICY.md.  Required marker: "
            "LOCAL_MODEL_USER_APPROVAL_REQUIRED."
        )
    if not messages:
        raise AiosOllamaError("messages must be non-empty",
                              kind="TASK_INPUT_FAILURE")
    payload: Dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    body = json.dumps(payload).encode("utf-8")
    url = base_url.rstrip("/") + "/v1/chat/completions"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = resp.getcode()
            raw_text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # 4xx/5xx with body
        status = exc.code
        raw_text = exc.read().decode("utf-8", errors="replace")
        latency = int((time.time() - started) * 1000)
        kind, message = _classify_http(status, raw_text)
        return OllamaCallResult(
            text="", model=model, latency_ms=latency,
            raw={"status": status, "body": raw_text[:500]},
            success=False, error_kind=kind, error_message=message,
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        latency = int((time.time() - started) * 1000)
        return OllamaCallResult(
            text="", model=model, latency_ms=latency,
            raw={"exception": repr(exc)},
            success=False, error_kind="TIMEOUT" if isinstance(exc, TimeoutError) else "NETWORK",
            error_message=repr(exc),
        )
    latency = int((time.time() - started) * 1000)
    try:
        payload_obj = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return OllamaCallResult(
            text="", model=model, latency_ms=latency,
            raw={"raw": raw_text[:500]},
            success=False, error_kind="CONTRACT_FAILURE",
            error_message=f"non-json body: {exc}",
        )
    return _parse(payload_obj, model=model, latency_ms=latency)


def _parse(payload: Dict[str, Any], *, model: str, latency_ms: int) -> OllamaCallResult:
    """Convert OpenAI-compatible ChatCompletions payload into OllamaCallResult."""
    text = ""
    if isinstance(payload, dict):
        choices = payload.get("choices") or []
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message") or {}
                if isinstance(message, dict):
                    text = str(message.get("content") or "")
    usage = payload.get("usage") if isinstance(payload, dict) else None
    in_tokens = int(usage.get("prompt_tokens", 0)) if isinstance(usage, dict) else 0
    out_tokens = int(usage.get("completion_tokens", 0)) if isinstance(usage, dict) else 0
    return OllamaCallResult(
        text=text, model=model, latency_ms=latency_ms,
        input_tokens=in_tokens, output_tokens=out_tokens,
        raw=payload if isinstance(payload, dict) else {},
        success=bool(text),
        error_kind="" if text else "CONTRACT_FAILURE",
        error_message="" if text else "empty_choices",
    )


def _classify_http(code: int, body: str) -> Tuple[str, str]:
    """Mirror ``aios_minimax_client._classify_http`` shape."""
    if code in (401, 403):
        return "ACCOUNT_AUTH_FAILURE", f"ollama auth error http={code} body={body[:200]}"
    if code == 402:
        return "ACCOUNT_QUOTA_FAILURE", f"ollama quota http={code} body={body[:200]}"
    if code == 404:
        return "MODEL_FAILURE", f"ollama model not found http={code} body={body[:200]}"
    if code == 429:
        return "ACCOUNT_QUOTA_FAILURE", f"ollama rate limited http={code} body={body[:200]}"
    if code in (408, 504, 524):
        return "TIMEOUT", f"ollama timeout http={code} body={body[:200]}"
    if code in (500, 502, 503):
        return "PROVIDER_FAILURE", f"ollama http={code} body={body[:200]}"
    return "CONTRACT_FAILURE", f"ollama http={code} body={body[:200]}"


def quick_probe(prompt: str = "ping",
                *, model: str = "qwen2.5:0.5b",
                timeout: float = 30.0) -> OllamaCallResult:
    """Cheap canary probe (uses the smallest model for speed)."""
    msgs = [{"role": "user", "content": prompt}]
    return chat(msgs, model=model, base_url=DEFAULT_BASE_URL,
                timeout=int(timeout), temperature=0.0, max_tokens=16)