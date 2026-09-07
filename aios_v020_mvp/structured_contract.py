"""Structured-output contract for AIOS v0.2.0 MVP roles.

All three roles (Planner, Executor, Reviewer) emit and consume JSON
shaped by ``SCHEMA_VERSION = "aios-v020-1.0"``. The same top-level
envelope is used everywhere:

    {
        "schema_version": "aios-v020-1.0",
        "role": "planner" | "executor" | "reviewer",
        "status": "ok" | "error",
        "data": { ... role-specific ... },
        "notes": "free-form short note (optional, never parsed)"
    }

The contract is intentionally minimal: LLM is asked to emit JSON
only, we strip markdown fences, we accept the envelope both with and
without the wrapper keys, and we never silently accept a non-JSON or
empty response — every parse failure raises ``ContractError`` with a
precise reason code.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


SCHEMA_VERSION = "aios-v020-1.0"


class ContractError(RuntimeError):
    """Raised when a real LLM response cannot be coerced into the
    AIOS v0.2.0 structured contract.

    The ``reason`` attribute is a short machine-readable code so the
    Reviewer / E2E harness can categorise failures.
    """

    REASONS = (
        "empty_response",
        "no_json_object",
        "truncated_json",
        "missing_field",
        "wrong_field_type",
        "unknown_kind",
        "schema_version_mismatch",
        "multiple_conflicting_json",
        "multiple_json_identical",
        "malformed_json",
    )

    def __init__(self, message: str, reason: str = "unknown") -> None:
        super().__init__(message)
        self.reason = reason


_FENCE_RE = re.compile(
    r"^\s*(?:`{3}(?:json|JSON)?\s*\n?)|(?:\n?\s*`{3}\s*)$",
    flags=re.MULTILINE,
)


def strip_markdown_fences(text: str) -> str:
    """Remove ```json / ``` fences the model often wraps its output in.

    Only strips fences at the start / end of the response. Inline
    fences are left alone so we do not accidentally truncate the body.
    """
    s = (text or "").strip()
    if not s:
        return s
    leading = re.match(r"^\s*`{3}(?:json|JSON)?\s*\n", s)
    if leading:
        s = s[leading.end():]
    s = re.sub(r"\n`{3}\s*$", "", s)
    s = re.sub(r"^\s*`{3}\s*\n", "", s)
    return s.strip()


def _find_top_level_json_values(text: str) -> List[Any]:
    """Return every top-level JSON value in ``text``, in order.

    Uses ``json.JSONDecoder.raw_decode`` at every ``{`` / ``[`` offset,
    then discards candidates that are nested inside a larger candidate
    so that a single object containing nested objects is counted as
    ONE value, not N.
    """
    if not text:
        return []
    decoder = json.JSONDecoder()
    cleaned = (text or "").strip()
    spans = []
    for m in re.finditer(r"[\[{]", cleaned):
        try:
            obj, end = decoder.raw_decode(cleaned, m.start())
            spans.append((m.start(), end, obj))
        except json.JSONDecodeError:
            continue
    top = []
    for idx, (st, en, obj) in enumerate(spans):
        contained = any(
            other[0] < st and other[1] >= en
            for j, other in enumerate(spans)
            if j != idx
        )
        if not contained:
            top.append(obj)
    return top


def _looks_truncated(text: str) -> bool:
    """Heuristic: response starts a JSON value but ends before it
    closes (e.g. max_tokens cut mid-object)."""
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    if cleaned.endswith("}"):
        return False
    # If json.loads fails on the whole text and the first non-space
    # char opens a value that is not closed, treat as truncated.
    try:
        json.loads(cleaned)
        return False
    except json.JSONDecodeError:
        pass
    first = cleaned.lstrip()[:1]
    if first not in ("{", "["):
        return False
    # Count braces as a cheap sanity check.
    opens = cleaned.count("{") + cleaned.count("[")
    closes = cleaned.count("}") + cleaned.count("]")
    last_char = cleaned.rstrip()[-1:]
    if opens > closes and last_char not in ("}", "]"):
        return True
    return False


def resolve_single_json(text: str):
    """Resolve exactly one JSON value from a model response.

    Policy:
      * A response that IS exactly one JSON value: accepted.
      * A response that is exactly one markdown-fenced JSON value:
        accepted after the fence is stripped.
      * A response with one JSON value plus surrounding prose:
        accepted.
      * A response containing MULTIPLE top-level JSON values that are
        semantically identical (deep-equal): accepted, but flagged in
        the returned diagnostics as ``{"duplicates": N}``.
      * A response containing MULTIPLE top-level JSON values that are
        semantically CONFLICTING: REJECTED with
        ``ContractError(multiple_conflicting_json)``.
      * Empty / malformed / truncated responses: REJECTED with the
        corresponding reason.

    Returns ``(obj, diagnostics_dict)`` and raises :class:`ContractError`
    otherwise.
    """
    if text is None or not str(text).strip():
        raise ContractError("response is empty", "empty_response")
    cleaned = str(text).strip()

    # Strategy 1: the whole response is JSON.
    for attempt in (cleaned, strip_markdown_fences(cleaned)):
        if not attempt:
            continue
        try:
            return json.loads(attempt), {"parser": "exact"}
        except json.JSONDecodeError:
            continue

    # Strategy 2: enumerate top-level candidates.
    values = _find_top_level_json_values(cleaned)
    if not values:
        if _looks_truncated(cleaned):
            raise ContractError(
                "response was truncated mid-JSON", "truncated_json",
            )
        raise ContractError(
            "no JSON object found in response", "no_json_object",
        )
    if len(values) == 1:
        return values[0], {"parser": "single_candidate"}

    # Multiple candidates: deduplicate by deep equality.
    seen = []
    for v in values:
        if v not in seen:
            seen.append(v)
    if len(seen) == 1:
        # All identical — accept, but record it so the caller can audit.
        return seen[0], {"parser": "multiple_identical", "duplicates": len(values)}
    raise ContractError(
        f"response contains {len(seen)} conflicting JSON objects "
        f"(duplicates={len(values)}); refusing to pick one silently",
        "multiple_conflicting_json",
    )


def extract_first_json(text: str) -> Optional[Any]:
    """Legacy compatibility entry point.

    Returns the single resolved JSON value, or None if the response is
    empty, truncated, malformed, or contains CONFLICTING JSON objects.
    Prefer :func:`resolve_single_json` in new code so the precise
    reason is available.
    """
    try:
        obj, _ = resolve_single_json(text)
        return obj
    except ContractError:
        return None


def validate_envelope(obj: Any, expected_role: str) -> Dict[str, Any]:
    """Validate a parsed JSON object against the AIOS contract.

    Accepts two shapes:

      * Strict envelope: ``{schema_version, role, status, data, notes?}``
      * Bare data: ``{...}`` — we treat the whole object as ``data``
        and stamp ``schema_version`` / ``role`` / ``status`` ourselves.

    Returns the strict envelope. Raises :class:`ContractError` on any
    schema violation.
    """
    if obj is None:
        raise ContractError("response parsed to None", "no_json_object")
    if not isinstance(obj, dict):
        raise ContractError(
            f"response was not a JSON object (got {type(obj).__name__})",
            "no_json_object",
        )
    if "data" in obj and isinstance(obj["data"], dict):
        envelope = obj
    else:
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "role": expected_role,
            "status": "ok",
            "data": obj,
            "notes": "",
        }
    sv = envelope.get("schema_version")
    if sv is not None and sv != SCHEMA_VERSION:
        envelope.setdefault("notes", "")
        envelope["notes"] = (
            (envelope.get("notes") or "")
            + f" [schema_version={sv}!=aios-v020-1.0]"
        ).strip()
        envelope["schema_version"] = SCHEMA_VERSION
    elif sv is None:
        envelope["schema_version"] = SCHEMA_VERSION
    envelope["role"] = expected_role
    envelope.setdefault("status", "ok")
    envelope.setdefault("notes", "")
    if not isinstance(envelope["data"], dict):
        raise ContractError(
            "envelope 'data' must be a JSON object",
            "wrong_field_type",
        )
    return envelope


REPAIR_SYSTEM = (
    "You previously produced output that did not match the AIOS "
    "structured-output contract. The user will now show you the "
    "previous output and the validation error. Respond with ONE "
    "single JSON object that matches the AIOS contract exactly. "
    "Do not include prose, do not wrap in markdown fences, do not "
    "explain. Output ONLY the JSON object."
)


def repair_prompt(role: str, previous_output: str, error: str) -> Dict[str, str]:
    """Build a corrective user message asking the model to repair its
    previous response into the contract shape.
    """
    return {
        "role": "user",
        "content": (
            f"role={role}\n"
            f"validation_error={error}\n"
            f"previous_output:\n{previous_output}\n\n"
            f"Respond with the corrected JSON object now."
        ),
    }


def parse_role_response(text: str, expected_role: str):
    """Resolve AND validate a role response in one step.

    Runs the multi-JSON safety gate (:func:`resolve_single_json`), then
    validates the envelope shape. Any violation raises
    :class:`ContractError` with a precise reason (e.g.
    ``multiple_conflicting_json``, ``truncated_json``).
    """
    obj, diag = resolve_single_json(text)
    env = validate_envelope(obj, expected_role)
    env["_parse_diagnostics"] = diag
    return env

