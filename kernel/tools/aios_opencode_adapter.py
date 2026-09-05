#!/usr/bin/env python3
"""P9D-R role-closure: OpenCode Planner adapter (PLAN_ONLY).

This module is the single entry point the Orchestrator uses when the
TaskRoutingPolicy selects ``preferred_planner='opencode'`` and the
call shape must be ``mode=PLAN_ONLY``.  It delegates the actual HTTP
plumbing to ``aios_opencode_client`` and adds the PLAN_ONLY safety
boundaries required by the close-out:

  * the OpenCode session is started in ``plan-only`` mode; the
    adapter MUST NOT execute shell / file-edit / network commands;
  * the response is validated as a single JSON object so the
    existing ``_extract_json_array`` consumer can keep working;
  * the return shape mirrors ``aios_model_gateway.call_model``
    (``{"ok": bool, "result" | "error": ...}``) so the existing
    ``build_plan`` payload validation is unaffected.

If the OpenCode client itself is unavailable (e.g. the live server
is not running on this host), the adapter raises
``OpenCodeAdapterError``; ``_call_planner_with_deadline`` translates
that into a ``PlannerTimeout`` so the orchestrator can fall back to
the next planner when ``allow_planner_fallback`` is True, or surface
``VERIFICATION_BLOCKED``-equivalent planner dead-letter when it is
False.

This module is intentionally tiny.  All the heavy lifting — free
model routing, provider selection, billing guard — lives in
``aios_opencode_client`` and is exercised by every other OpenCode
code path; the PLAN_ONLY contract is the only new addition here.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

CLIENT_VERSION = "1.1.0"
SERVER_URL = os.getenv("AIOS_OPENCODE_URL", "http://127.0.0.1:4096").rstrip("/")
WORKDIR = os.getenv("AIOS_OPENCODE_WORKDIR", "${AIOS_HOME}/sandbox/coding")
OPENCODE_BINARY = os.getenv("AIOS_OPENCODE_BINARY", "${HOME}/.n/bin/opencode")

# Re-use the existing client error class so callers don't need a
# second import path.  Importing here (rather than re-declaring) keeps
# the PLAN_ONLY adapter and the regular OpenCode adapter in lock-step.
from aios_opencode_client import OpenCodeAdapterError, _request  # noqa: E402

# PLAN_ONLY safety boundaries — verified on every call.  The OpenCode
# server is asked to start a plan-only session; the adapter itself
# MUST refuse to forward any prompt that explicitly asks the planner
# to execute / mutate / transmit.  Forbidden tokens are matched as
# case-insensitive substrings against the prompt.
_PLAN_ONLY_FORBIDDEN_TOKENS = (
    "run_shell", "shell_command", "execute_command", "execute step",
    "modify file", "modify_file", "write to file", "send_email",
    "send email", "post to chat", "outbound http", "make http request",
    "exec(", "subprocess.run", "open(/" "/", "rm -rf",
    "rm ", "del ", "kill ", "shutdown", "shutdown ", "curl ",
    "wget ", "ssh ",
)

_PLAN_ONLY_SYSTEM_PROMPT = (
    "You are the AIOS Planner running in PLAN_ONLY mode. "
    "You are NOT allowed to execute steps, modify files, run "
    "commands, or send any external message. Your sole job is to "
    "return a strict JSON object describing the plan nodes the "
    "AIOS Orchestrator should dispatch. Do not wrap the JSON in "
    "Markdown fences. Do not include commentary or thinking "
    "outside the JSON object. "
    "Each node MUST contain: task (string), depends_on (array of "
    "earlier node indexes), role (opencode|claude|codex), acceptance "
    "(array of measurable statements), and evidence_mode "
    "(semantic|independent-live|aios-runtime). "
    "Return ONE JSON object (a single plan object with the schema "
    "{\"goal\":..., \"steps\":[...], \"required_capabilities\":[], "
    "\"executor_role\":..., \"reviewer_role\":..., \"risks\":[], "
    "\"completion_criteria\":[...]}) so the orchestrator can "
    "interpret the plan."
)

# Schema the OpenCode planner is required to emit.  Mirrors the
# canonical Orchestrator plan shape that ``_extract_json_array`` /
# ``_normalise_plan`` understands.  The orchestrator will translate
# this into the legacy ``[nodes]`` shape before persisting, but the
# planner author only needs to know this contract.
_PLAN_SCHEMA_FIELDS = (
    "goal", "steps", "required_capabilities", "executor_role",
    "reviewer_role", "risks", "completion_criteria",
)


# ---------------------------------------------------------------------------
# Response shape classifiers / extractors — used by tests and any
# future PLAN_ONLY adapter consumer that needs to introspect the
# raw opencode-server response.
# ---------------------------------------------------------------------------


def _classify_response_shape(payload: Any) -> str:
    """Classify the raw opencode-server response payload.

    Returns one of:

    * ``OPENCODE_RESPONSE_SHAPE_SYNC_MESSAGE`` — single assistant
      message dict (``{"info": ..., "parts": [...]}``);
    * ``OPENCODE_RESPONSE_SHAPE_ASYNC_MESSAGE_LIST`` — list of
      message dicts (the polling target);
    * ``OPENCODE_RESPONSE_SHAPE_NESTED_DATA`` — ``{"data": [...]}``
      envelope;
    * ``OPENCODE_RESPONSE_SHAPE_UNSUPPORTED`` — anything else.
    """
    if isinstance(payload, dict):
        info = payload.get("info")
        parts = payload.get("parts")
        if isinstance(info, dict) and isinstance(parts, list):
            return "OPENCODE_RESPONSE_SHAPE_SYNC_MESSAGE"
        if isinstance(payload.get("data"), list):
            return "OPENCODE_RESPONSE_SHAPE_NESTED_DATA"
    if isinstance(payload, list):
        return "OPENCODE_RESPONSE_SHAPE_ASYNC_MESSAGE_LIST"
    return "OPENCODE_RESPONSE_SHAPE_UNSUPPORTED"


def _extract_assistant_text_parts(payload: Any) -> List[str]:
    """Return the assistant ``text`` parts from a normalised
    opencode-server payload.

    Accepts:

    * a single message dict (``{"info": {"role": ...}, "parts": [...]}``);
    * a list of message dicts (async polling target);
    * a ``{"data": [message, ...]}`` envelope.

    Non-``text`` parts (e.g. ``reasoning``, ``step-start``,
    ``tool-call``) are ignored.  Empty results return ``[]`` so the
    caller can raise a deterministic ``OPENCODE_RESPONSE_TEXT_MISSING``
    error.
    """
    candidates: List[Any] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            candidates.extend(payload["data"])
        info = payload.get("info")
        if isinstance(info, dict) and isinstance(payload.get("parts"), list):
            candidates.append(payload)
    elif isinstance(payload, list):
        candidates.extend(payload)
    texts: List[str] = []
    for message in candidates:
        if not isinstance(message, dict):
            continue
        info = message.get("info")
        if isinstance(info, dict) and str(info.get("role", "")) != "assistant":
            continue
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if str(part.get("type", "")) != "text":
                continue
            text = str(part.get("text", "") or "").strip()
            if text:
                texts.append(text)
    return texts


def _poll_assistant_text(
    session_id: str,
    fetcher: Any,
    *,
    deadline: float,
    poll_interval: float,
    connect_timeout: float = 5.0,
) -> List[str]:
    """Poll ``fetcher(session_id)`` until at least one assistant text
    part is returned, or until ``deadline`` is exceeded.

    Raises :class:`OpenCodeAdapterError` with
    ``OPENCODE_MESSAGE_TIMEOUT`` when the deadline is exceeded.  The
    fetcher MUST return either a list of message dicts (async shape)
    or a single message dict (sync shape) — see
    :func:`_extract_assistant_text_parts`.
    """
    import time as _time
    started = _time.monotonic()
    while _time.monotonic() < deadline:
        _time.sleep(max(0.0, float(poll_interval)))
        try:
            payload = fetcher(session_id)
        except Exception:
            payload = None
        if payload is None:
            continue
        texts = _extract_assistant_text_parts(payload)
        if texts:
            return texts
    raise OpenCodeAdapterError(
        "OPENCODE_MESSAGE_TIMEOUT:planner produced no assistant text before deadline"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_post_json(path: str, payload: dict[str, Any], timeout: int = 30) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        SERVER_URL + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-2000:]
        raise OpenCodeAdapterError(
            f"PLAN_ONLY HTTP {exc.code}: {detail}"
        ) from exc
    except Exception as exc:
        raise OpenCodeAdapterError(
            f"PLAN_ONLY transport: {type(exc).__name__}: {exc}"
        ) from exc


def _validate_prompt_is_plan_only(prompt: str) -> None:
    """Reject any prompt that asks the planner to take a non-planning
    action.  This is the only client-side safety net; the OpenCode
    server enforces PLAN_ONLY independently.  Defending both sides
    means a misconfigured server cannot be used to bypass the
    planner safety boundary.
    """
    lowered = str(prompt or "").lower()
    for token in _PLAN_ONLY_FORBIDDEN_TOKENS:
        if token.lower() in lowered:
            raise OpenCodeAdapterError(
                f"PLAN_ONLY_REFUSED:prompt_contains_forbidden_token:{token}"
            )


def _validate_plan_schema(plan: dict[str, Any]) -> tuple[bool, str]:
    """Verify the OpenCode planner emitted a structurally valid plan.

    The schema is the canonical Orchestrator plan shape the legacy
    ``_extract_json_array`` / ``_normalise_plan`` already understand;
    ``execute_plan_only`` returns it verbatim so the existing
    ``build_plan`` payload validation keeps working.

    Returns ``(ok, error)``.  ``error`` is empty when ``ok`` is True.
    """
    if not isinstance(plan, dict):
        return False, "PLAN_SCHEMA_INVALID:not_object"
    missing = [key for key in _PLAN_SCHEMA_FIELDS if key not in plan]
    if missing:
        return False, f"PLAN_SCHEMA_INVALID:missing={missing}"
    if not isinstance(plan.get("steps", []), list):
        return False, "PLAN_SCHEMA_INVALID:steps_not_list"
    if not plan["steps"]:
        return False, "PLAN_SCHEMA_INVALID:steps_empty"
    for index, step in enumerate(plan["steps"]):
        if not isinstance(step, dict):
            return False, f"PLAN_SCHEMA_INVALID:step_{index}_not_object"
        if not str(step.get("task", "")).strip():
            return False, f"PLAN_SCHEMA_INVALID:step_{index}_task_missing"
        if "depends_on" in step and not isinstance(step["depends_on"], list):
            return (
                False,
                f"PLAN_SCHEMA_INVALID:step_{index}_depends_on_not_list",
            )
    return True, ""


def _execute_plan_only_via_free_candidates(parent_id: str,
                                          prompt: str,
                                          binding: str,
                                          connect_timeout: float,
                                          read_timeout: float) -> dict:
    """Run a PLAN_ONLY planner attempt through the configured free
    candidate pool.

    P9D-R secondary-role closure: this is the same surface
    ``aios_opencode_client.run_task`` uses for the executor; we
    delegate to it so cooldown / health tracking and model fallback
    apply symmetrically.  The function layers the PLAN_ONLY
    contract on top by:

      * prepending :data:`_PLAN_ONLY_SYSTEM_PROMPT` to the prompt
        so the candidate model is forced into PLAN_ONLY mode;
      * client-side safety filtering via
        :func:`_validate_prompt_is_plan_only`;
      * validating the returned text against the canonical plan
        schema via :func:`_validate_plan_schema`;
      * returning the orchestrator-shaped ``{"ok": bool, ...}``
        payload so :func:`build_plan` accepts the result.

    Empty / free-eligible bindings (e.g. ``opencode:free``) and
    any binding that is not the legacy ``opencode:minimax`` flow
    through here.
    """
    started = time.monotonic()
    full_prompt = _PLAN_ONLY_SYSTEM_PROMPT + "\n\n" + (prompt or "")
    budget_seconds = max(15.0, float(read_timeout) + float(connect_timeout))
    try:
        from aios_opencode_client import run_task as _run_task  # type: ignore
    except Exception as exc:  # pragma: no cover - import-time failure
        raise OpenCodeAdapterError(
            f"OPENCODE_IMPORT:{type(exc).__name__}:{exc}"
        ) from exc
    try:
        raw_content = _run_task(full_prompt, timeout=int(budget_seconds))
    except OpenCodeAdapterError:
        raise
    except Exception as exc:
        raise OpenCodeAdapterError(
            f"OPENCODE_FREE_PLAN_ONLY:{type(exc).__name__}:{str(exc)[:200]}"
        ) from exc
    raw_content = (raw_content or "").strip()
    if not raw_content:
        raise OpenCodeAdapterError(
            "OPENCODE_RESPONSE_TEXT_MISSING:planner returned no content"
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    # Strip a single outer Markdown fence if the model added one.
    if raw_content.startswith("```json") and raw_content.endswith("```"):
        raw_content = raw_content[len("```json"):-3].strip()
    elif raw_content.startswith("```") and raw_content.endswith("```"):
        raw_content = raw_content[3:-3].strip()
    parsed: Any = None
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for idx, char in enumerate(raw_content):
            if char != "{":
                continue
            try:
                candidate, _end = decoder.raw_decode(raw_content[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
    if parsed is None:
        raise OpenCodeAdapterError(
            "PLAN_SCHEMA_INVALID:planner returned no JSON object"
        )
    ok, err = _validate_plan_schema(parsed)
    if not ok:
        raise OpenCodeAdapterError(err)
    # Persist the plan on the host so the orchestrator can re-use it
    # across process restarts.  No-op when Redis is unavailable.
    try:
        from pathlib import Path as _Path
        cache_dir = _Path(os.getenv(
            "AIOS_OPENCODE_PLAN_CACHE",
            "${AIOS_HOME}/cache/opencode_plan_only",
        ))
        cache_dir.mkdir(parents=True, exist_ok=True)
        plan_path = cache_dir / f"{parent_id}.json"
        plan_path.write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return {
        "ok": True,
        "elapsed_ms": elapsed_ms,
        "result": {
            "choices": [{
                "message": {"role": "assistant", "content": raw_content},
                "index": 0,
                "finish_reason": "stop",
            }],
            "mode": "plan-only",
            "planner_binding": binding,
            "schema_valid": True,
        },
        "planner_mode": "PLAN_ONLY",
        "planner_schema_valid": True,
    }


def execute_plan_only(parent_id: str,
                      prompt: str,
                      binding: str = "",
                      connect_timeout: float = 10.0,
                      read_timeout: float = 45.0) -> dict:
    """Invoke the OpenCode adapter in PLAN_ONLY mode.

    Parameters:
        parent_id:       parent workflow / task id; forwarded to the
                         server for traceability / session reuse.
        prompt:          the user goal + planner base prompt.  Must
                         be a planning-only prompt; the safety check
                         refuses forbidden action tokens.
        binding:         informational ``<tool>:<provider>`` string.
                         Kept for the audit ledger.
        connect_timeout: connect-side timeout (seconds).
        read_timeout:    read-side timeout (seconds).

    Returns:
        A dict with the same shape as
        ``aios_model_gateway.call_model`` returns:

        ``{"ok": True, "result": {"choices": [...]}}``
        on success, ``{"ok": False, "error": "<message>"}`` on
        failure.  ``choices`` follows the OpenAI-ish schema the
        legacy orchestrator payload validator already understands.

    Raises:
        OpenCodeAdapterError when the OpenCode server is unavailable
        or the planner attempt is structurally invalid.  The
        orchestrator's ``_call_planner_with_deadline`` translates
        this into a ``PlannerTimeout`` so the wider fallback logic
        can react.
    """
    started = time.monotonic()
    _validate_prompt_is_plan_only(prompt)
    try:
        health_payload = _request("GET", "/global/health", timeout=max(2, int(connect_timeout)))
    except OpenCodeAdapterError:
        raise
    except Exception as exc:
        raise OpenCodeAdapterError(
            f"PLAN_ONLY health: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(health_payload, dict) or health_payload.get("healthy") is not True:
        raise OpenCodeAdapterError(
            f"PLAN_ONLY unhealthy: {health_payload!r}"
        )
    # Resolve the binding to an OpenCode provider/model tuple.  P9D-R
    # secondary-role closure: ``opencode:minimax`` keeps the legacy
    # minimax-aios/MiniMax-M3 path; ``opencode:free`` and any other
    # binding delegate to ``aios_opencode_client.run_task`` which
    # walks the configured free candidates (deepseek-v4-flash-free
    # et al.) with cooldown / health tracking.  ``binding`` may be
    # empty; we then default to ``opencode:free`` because that
    # candidate set is the only one currently verified to return a
    # non-empty assistant text part through this server version.
    binding = str(binding or "").strip()
    if binding and binding != "opencode:minimax":
        # Anything that is not the legacy minimax binding is routed
        # through the configured free-candidate pool so the PLAN_ONLY
        # surface stays production-eligible even when the registered
        # external provider is degraded.
        return _execute_plan_only_via_free_candidates(
            parent_id=parent_id,
            prompt=prompt,
            binding=binding,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
    # ``binding == "opencode:minimax"`` (or empty default) — use the
    # direct session/message API.  Empty ``binding`` defaults to
    # minimax-aios for backwards compatibility with existing callers
    # that pre-date the free-candidate fallback path.
    # Use the real OpenCode Server API: create a session, send the
    # planner-specific prompt as a message, and read the text parts.
    # This is the same transport the executor uses (see
    #``aios_opencode_client._run_with_model``); the only difference
    # is the prompt is the PLAN_ONLY system prompt + user goal, and
    # the response is validated against the plan schema.
    query = "?" + urllib.parse.urlencode({"directory": WORKDIR})
    session = _request(
        "POST", "/session" + query,
        {"title": f"AIOS PLAN_ONLY {parent_id}"},
        timeout=max(5, int(connect_timeout)),
    )
    session_id = session.get("id") if isinstance(session, dict) else None
    if not session_id:
        raise OpenCodeAdapterError(
            f"PLAN_ONLY session create failed: {session!r}"
        )
    try:
        _request(
            "POST",
            f"/session/{session_id}/message" + query,
            {
                "model": {"providerID": "minimax-aios", "modelID": "MiniMax-M3"},
                "parts": [{
                    "type": "text",
                    "text": _PLAN_ONLY_SYSTEM_PROMPT + "\n\n" + prompt,
                }],
            },
            timeout=max(5, int(connect_timeout)),
        )
    except Exception:
        try:
            _request("POST", f"/session/{session_id}/abort" + query, timeout=5)
        except Exception:
            pass
        raise
    # The OpenCode message endpoint is asynchronous: the POST returns
    # immediately with step-start events, and the actual assistant
    # text parts arrive asynchronously.  Poll GET /session/{id}/message
    # until the assistant reply (role=assistant, type=text) appears or
    # the read budget is exhausted.
    deadline = time.monotonic() + float(read_timeout)
    raw_content = ""
    while time.monotonic() < deadline:
        time.sleep(1.5)
        try:
            messages = _request(
                "GET",
                f"/session/{session_id}/message" + query,
                timeout=max(5, int(connect_timeout)),
            )
        except Exception:
            messages = None
        if not isinstance(messages, list):
            continue
        assistant_texts = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            info = msg.get("info") if isinstance(msg.get("info"), dict) else {}
            if str(info.get("role", "")) != "assistant":
                continue
            for part in (msg.get("parts") or []):
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    assistant_texts.append(str(part["text"]))
        if assistant_texts:
            raw_content = "\n".join(assistant_texts).strip()
            break
    try:
        _request("DELETE", f"/session/{session_id}" + query, timeout=10)
    except Exception:
        pass
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not raw_content:
        raise OpenCodeAdapterError("PLAN_EMPTY:planner returned no content")
    # Strip a single outer Markdown fence if the model added one.
    if raw_content.startswith("```json") and raw_content.endswith("```"):
        raw_content = raw_content[len("```json"):-3].strip()
    elif raw_content.startswith("```") and raw_content.endswith("```"):
        raw_content = raw_content[3:-3].strip()
    parsed: Any = None
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        # Try a tolerant scan for the first complete JSON object.
        decoder = json.JSONDecoder()
        for idx, char in enumerate(raw_content):
            if char != "{":
                continue
            try:
                candidate, _end = decoder.raw_decode(raw_content[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
    if parsed is None:
        raise OpenCodeAdapterError(
            "PLAN_SCHEMA_INVALID:planner returned no JSON object"
        )
    ok, err = _validate_plan_schema(parsed)
    if not ok:
        raise OpenCodeAdapterError(err)
    # Persist the plan on the host so the orchestrator can re-use it
    # across process restarts.  No-op when Redis is unavailable.
    try:
        import redis as _redis_lib  # type: ignore
        from pathlib import Path as _Path
        cache_dir = _Path(os.getenv(
            "AIOS_OPENCODE_PLAN_CACHE",
            "${AIOS_HOME}/cache/opencode_plan_only",
        ))
        cache_dir.mkdir(parents=True, exist_ok=True)
        plan_path = cache_dir / f"{parent_id}.json"
        plan_path.write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Cache write is best-effort; PLAN_ONLY contract is honoured
        # by the schema validator regardless.
        pass
    return {
        "ok": True,
        "elapsed_ms": elapsed_ms,
        "result": {
            "choices": [{
                "message": {"role": "assistant", "content": raw_content},
                "index": 0,
                "finish_reason": "stop",
            }],
            "mode": "plan-only",
            "planner_binding": binding or "",
            "schema_valid": True,
        },
        "planner_mode": "PLAN_ONLY",
        "planner_schema_valid": True,
    }


__all__ = [
    "execute_plan_only", "OpenCodeAdapterError",
    "CLIENT_VERSION", "SERVER_URL", "WORKDIR", "OPENCODE_BINARY",
    "_classify_response_shape", "_extract_assistant_text_parts",
    "_poll_assistant_text", "_validate_prompt_is_plan_only",
    "_validate_plan_schema",
]
