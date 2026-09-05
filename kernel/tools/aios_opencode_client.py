#!/usr/bin/env python3
"""AIOS adapter for OpenCode's official Server API with free-model failover."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CLIENT_VERSION = "1.1.0"
SERVER_URL = os.getenv("AIOS_OPENCODE_URL", "http://127.0.0.1:4096").rstrip("/")
WORKDIR = os.getenv("AIOS_OPENCODE_WORKDIR", "${AIOS_HOME}/sandbox/coding")
MODEL_CONFIG = Path(os.getenv(
    "AIOS_OPENCODE_MODEL_CONFIG",
    "${AIOS_HOME}/config/opencode_models.json",
))
ROUTER_STATE = Path(os.getenv(
    "AIOS_OPENCODE_ROUTER_STATE",
    "${AIOS_HOME}/cache/opencode_model_router.json",
))
OPENCODE_BINARY = os.getenv("AIOS_OPENCODE_BINARY", "${HOME}/.n/bin/opencode")


class OpenCodeAdapterError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request(method: str, path: str, payload: dict[str, Any] | None = None,
             timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        SERVER_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-2000:]
        raise OpenCodeAdapterError(f"OpenCode HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise OpenCodeAdapterError(
            f"OpenCode server unavailable: {type(exc).__name__}: {exc}"
        ) from exc


def _directory_query() -> str:
    return "?" + urllib.parse.urlencode({"directory": WORKDIR})


def health() -> dict[str, Any]:
    result = _request("GET", "/global/health", timeout=5)
    if not isinstance(result, dict) or result.get("healthy") is not True:
        raise OpenCodeAdapterError(f"OpenCode unhealthy: {result!r}")
    return result


# Valid policies:
#  - "free_only"           legacy behaviour: only models whose live
#                          /provider JSON shows cost.input==0 and
#                          cost.output==0 are candidates. Backward
#                          compatible with the previous close-outs
#                          (P9 P10 P11).
#  - "ai_managed_any"      AIOS may dispatch non-free providers that
#                          the operator has explicitly registered in
#                          the same `config/opencode-aios.json` file
#                          under the `provider` block (e.g.
#                          `provider.minimax`). Cooldown/health logic
#                          applies symmetrically to non-free candidates
#                          without changing the OpenCode server side.
_VALID_OPENCODE_POLICIES = ("free_only", "ai_managed_any")


def _load_policy() -> dict[str, Any]:
    try:
        policy = json.loads(MODEL_CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OpenCodeAdapterError(f"model policy unavailable: {type(exc).__name__}: {exc}") from exc
    declared = policy.get("policy")
    if declared not in _VALID_OPENCODE_POLICIES:
        raise OpenCodeAdapterError(
            f"model policy must be one of {_VALID_OPENCODE_POLICIES}, got {declared!r}"
        )
    candidates = policy.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise OpenCodeAdapterError(f"{declared} model candidate list is empty")
    return policy


def _resolve_route(policy: dict[str, Any], binding_id: str) -> tuple[str, str]:
    """Map an AIOS ``ToolModelBinding.binding_id`` like
    ``opencode:minimax`` to the OpenCode ``provider/model`` pair
    that should serve it.

    Returns a 2-tuple ``(provider_id, model_id)`` suitable for
    ``/session/{id}/message``.
    """
    routes = policy.get("routing_map", {})
    if not isinstance(routes, dict):
        raise OpenCodeAdapterError(
            f"routing_map missing or invalid for policy {policy.get('policy')!r}"
        )
    target = routes.get(binding_id)
    if not isinstance(target, dict) or "providerID" not in target or "modelID" not in target:
        raise OpenCodeAdapterError(
            f"no routing entry for binding {binding_id!r}"
        )
    return str(target["providerID"]), str(target["modelID"])


def _candidate_models(policy: dict[str, Any]) -> list[str]:
    override = os.getenv("AIOS_OPENCODE_MODELS", "").strip()
    if override:
        raw = [item.strip() for item in override.split(",") if item.strip()]
    else:
        raw = []
        for item in policy.get("candidates", []):
            if isinstance(item, str):
                raw.append(item)
            elif isinstance(item, dict) and item.get("enabled", True):
                raw.append(str(item.get("model", "")).strip())
    models: list[str] = []
    for model in raw:
        if model and "/" in model and model not in models:
            models.append(model)
    if not models:
        raise OpenCodeAdapterError("no valid free model candidates")
    return models


def _verified_free_models() -> set[str]:
    """Trust live OpenCode metadata, not a name containing the word 'free'."""
    response = _request("GET", "/provider", timeout=15)
    providers = response.get("all", []) if isinstance(response, dict) else []
    free: set[str] = set()
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id", ""))
        models = provider.get("models", {})
        if not isinstance(models, dict):
            continue
        for model_id, metadata in models.items():
            if not isinstance(metadata, dict) or metadata.get("status", "active") != "active":
                continue
            cost = metadata.get("cost", {})
            if (isinstance(cost, dict) and cost.get("input") == 0 and
                    cost.get("output") == 0):
                free.add(f"{provider_id}/{model_id}")
    return free


def _load_state() -> dict[str, Any]:
    try:
        state = json.loads(ROUTER_STATE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _write_state(state: dict[str, Any]) -> None:
    ROUTER_STATE.parent.mkdir(parents=True, exist_ok=True)
    state["schema"] = "aios-opencode-model-router-state/1.0"
    state["updated_at"] = _now()
    temp = ROUTER_STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, ROUTER_STATE)


def _parse_time(value: str) -> float:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (TypeError, ValueError):
        return 0.0


def _failure_kind(detail: str) -> tuple[str, bool]:
    lower = detail.lower()
    if any(item in lower for item in (
        "http 402", "insufficient balance", "quota", "usage limit",
        "token plan", "余额不足", "用量上限",
    )):
        return "quota_exhausted", False
    if any(item in lower for item in ("http 429", "rate limit", "too many requests")):
        return "rate_limited", False
    if any(item in lower for item in (
        "timed out", "timeout", "connection", "network", "dns",
        "streaming response failed", "http 500", "http 502", "http 503", "http 504",
    )):
        return "transient_provider_error", False
    if any(item in lower for item in (
        "http 401", "unauthorized", "invalid api key", "authentication failed",
    )):
        return "auth_failed", True
    return "model_error", False


def _cooldown_seconds(policy: dict[str, Any], kind: str) -> int:
    configured = policy.get("cooldown_seconds", {})
    defaults = {
        "quota_exhausted": 3600,
        "rate_limited": 300,
        "transient_provider_error": 120,
        "auth_failed": 3600,
        "model_error": 120,
    }
    try:
        return max(0, int(configured.get(kind, defaults[kind])))
    except (AttributeError, TypeError, ValueError, KeyError):
        return defaults.get(kind, 120)


def _record_failure(state: dict[str, Any], policy: dict[str, Any], model: str,
                    kind: str, detail: str) -> None:
    models = state.setdefault("models", {})
    current = models.setdefault(model, {})
    current.update({
        "state": kind,
        "failure_count": int(current.get("failure_count", 0)) + 1,
        "last_failure_at": _now(),
        "last_error": detail[-500:],
        "cooldown_until": datetime.fromtimestamp(
            time.time() + _cooldown_seconds(policy, kind), timezone.utc,
        ).isoformat(),
    })


def _record_success(state: dict[str, Any], model: str, attempts: list[dict[str, Any]]) -> None:
    models = state.setdefault("models", {})
    current = models.setdefault(model, {})
    current.update({
        "state": "available",
        "failure_count": 0,
        "last_success_at": _now(),
        "last_error": "",
        "cooldown_until": "",
    })
    state.update({
        "policy": "free_only",
        "last_selected_model": model,
        "last_attempts": attempts,
        "last_result": "success",
    })


def _run_with_model(task: str, model: str, timeout: int) -> str:
    provider_id, model_id = model.split("/", 1)
    query = _directory_query()
    session = _request("POST", "/session" + query, {"title": "AIOS executor task"}, timeout=10)
    session_id = session.get("id") if isinstance(session, dict) else None
    if not session_id:
        raise OpenCodeAdapterError(f"OpenCode did not create a session: {session!r}")
    try:
        response = _request(
            "POST",
            f"/session/{session_id}/message" + query,
            {
                "model": {"providerID": provider_id, "modelID": model_id},
                "parts": [{"type": "text", "text": task}],
            },
            timeout=timeout,
        )
        if not isinstance(response, dict):
            raise OpenCodeAdapterError("OpenCode returned no message")
        info = response.get("info") if isinstance(response.get("info"), dict) else {}
        if info.get("error"):
            raise OpenCodeAdapterError(
                "OpenCode inference failed: " +
                json.dumps(info["error"], ensure_ascii=False)[:2000]
            )
        parts = response.get("parts") if isinstance(response.get("parts"), list) else []
        texts = [
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ]
        result = "\n".join(texts).strip()
        if not result:
            raise OpenCodeAdapterError("OpenCode returned an empty result")
        return result
    except Exception:
        try:
            _request("POST", f"/session/{session_id}/abort" + query, timeout=5)
        except Exception:
            pass
        raise
    finally:
        try:
            _request("DELETE", f"/session/{session_id}" + query, timeout=10)
        except Exception:
            pass


def invoke_with_model(provider_id: str, model_id: str, task: str,
                      timeout: int = 240) -> str:
    """Dispatch a task through a *specific* OpenCode provider/model.

    This is the AIOS-side entry point for the ``ai_managed_any``
    policy path: bindings like ``opencode:minimax`` route here so
    the OpenCode adapter actually reaches ``minimax/MiniMax-M3``
    (or any other operator-registered provider) instead of the
    free-router-only candidates.

    Cooldown/health tracking still applies via ``cache/opencode_model_router.json``
    keyed by the canonical ``provider/model`` string.
    """
    task = (task or "").strip()
    if not task:
        raise OpenCodeAdapterError("empty task")
    if not provider_id or not model_id or "/" in model_id:
        # ``model_id`` itself may not contain ``/``; the combined
        # ``provider/model`` form does.  Reject only ``provider_id``
        # or ``model_id`` that contain an embedded slash.
        raise OpenCodeAdapterError(
            f"invoke_with_model: bad provider/model ({provider_id!r}/"
            f"{model_id!r})"
        )
    policy = _load_policy()
    if policy.get("policy") not in _VALID_OPENCODE_POLICIES:
        raise OpenCodeAdapterError(
            f"policy {policy.get('policy')!r} does not permit dispatch"
        )
    model = f"{provider_id}/{model_id}"
    # Sanity: ensure the live opencode server has the provider
    # configured. This raises OpenCodeAdapterError if not present,
    # which records cooldown and bubbles up the dispatcher.
    health()
    state = _load_state()
    model_state = state.get("models", {}).get(model, {})
    ignore_cooldown = os.getenv("AIOS_OPENCODE_IGNORE_COOLDOWN", "") == "1"
    if (not ignore_cooldown and
            _parse_time(model_state.get("cooldown_until", "")) > time.time()):
        raise OpenCodeAdapterError(
            f"{model} in cooldown until {model_state.get('cooldown_until')!r}"
        )
    per_model_timeout = max(10, int(policy.get("per_model_timeout_seconds", 75)))
    attempts: list[dict[str, Any]] = []
    try:
        result = _run_with_model(task, model, min(per_model_timeout, timeout))
        attempts.append({"model": model, "provider": provider_id,
                         "model_id": model_id, "status": "success"})
        _record_success(state, model, attempts)
        state.update({"last_selected_model": model, "last_result": "success"})
        _write_state(state)
        return result
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        kind, fatal = _failure_kind(detail)
        attempts.append({"model": model, "provider": provider_id,
                         "model_id": model_id, "status": "failed",
                         "reason": kind, "detail": detail[-300:]})
        _record_failure(state, policy, model, kind, detail)
        state.update({"last_selected_model": model, "last_result": kind})
        _write_state(state)
        if fatal:
            raise OpenCodeAdapterError(
                f"fatal provider error: {model}: {detail[:1200]}"
            ) from exc
        raise OpenCodeAdapterError(
            f"{model} failed ({kind}): {detail[:1200]}"
        ) from exc


def run_task(task: str, timeout: int = 240) -> str:
    task = (task or "").strip()
    if not task:
        raise OpenCodeAdapterError("empty task")
    policy = _load_policy()
    candidates = _candidate_models(policy)
    health()
    verified_free = _verified_free_models()
    state = _load_state()
    attempts: list[dict[str, Any]] = []
    deadline = time.monotonic() + max(10, timeout)
    ignore_cooldown = os.getenv("AIOS_OPENCODE_IGNORE_COOLDOWN", "") == "1"
    per_model_timeout = max(10, int(policy.get("per_model_timeout_seconds", 75)))
    fatal_error = ""

    for model in candidates:
        if model not in verified_free:
            attempts.append({"model": model, "status": "skipped", "reason": "not_verified_free"})
            continue
        model_state = state.get("models", {}).get(model, {})
        if (not ignore_cooldown and
                _parse_time(model_state.get("cooldown_until", "")) > time.time()):
            attempts.append({
                "model": model,
                "status": "skipped",
                "reason": "cooldown",
                "cooldown_until": model_state.get("cooldown_until", ""),
            })
            continue
        remaining = int(deadline - time.monotonic())
        if remaining < 5:
            attempts.append({"model": model, "status": "skipped", "reason": "total_timeout"})
            break
        try:
            result = _run_with_model(task, model, min(per_model_timeout, remaining))
            attempts.append({"model": model, "status": "success"})
            _record_success(state, model, attempts)
            _write_state(state)
            return result
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            kind, fatal = _failure_kind(detail)
            attempts.append({"model": model, "status": "failed", "reason": kind,
                             "detail": detail[-300:]})
            _record_failure(state, policy, model, kind, detail)
            _write_state(state)
            if fatal:
                fatal_error = detail
                break

    state.update({
        "policy": "free_only",
        "last_selected_model": "",
        "last_attempts": attempts,
        "last_result": "failed",
    })
    _write_state(state)
    detail = fatal_error or json.dumps(attempts, ensure_ascii=False)
    raise OpenCodeAdapterError(f"all verified free models unavailable: {detail[:1800]}")


def version_text() -> str:
    binary = subprocess.run(
        [OPENCODE_BINARY, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    raw_version = (binary.stdout or binary.stderr).strip().splitlines()
    if binary.returncode != 0 or not raw_version:
        raise OpenCodeAdapterError("OpenCode binary version check failed")
    server = health()
    policy = _load_policy()
    candidates = _candidate_models(policy)
    state = _load_state()
    selected = state.get("last_selected_model") or candidates[0]
    return (
        f"aios-opencode-client {CLIENT_VERSION}; "
        f"opencode {raw_version[0]}; server {server.get('version', 'unknown')}; "
        f"policy free_only; candidates {len(candidates)}; selected {selected}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--version", action="store_true")
    group.add_argument("--probe", metavar="PROMPT")
    group.add_argument("--task", metavar="TASK")
    group.add_argument("--route-state", action="store_true")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()

    try:
        if args.version:
            print(version_text())
        elif args.route_state:
            print(json.dumps(_load_state(), ensure_ascii=False, indent=2))
        else:
            print(run_task(args.probe if args.probe is not None else args.task, args.timeout))
        return 0
    except Exception as exc:
        print(f"OpenCodeAdapterError: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
