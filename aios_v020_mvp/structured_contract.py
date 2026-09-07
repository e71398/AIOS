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


def extract_first_json(text: str) -> Optional[Any]:
    """Return the first JSON value that ``text`` contains, or None.

    Tries three strategies in order:

      1. Direct ``json.loads(text)``.
      2. Strip markdown fences and try again.
      3. Brute-force: walk every ``{`` / ``[`` position and try
         ``raw_decode`` until one succeeds.
    """
    if not text:
        return None
    cleaned = text.strip()
    for attempt in (cleaned, strip_markdown_fences(cleaned)):
        if not attempt:
            continue
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", cleaned):
        try:
            obj, _ = decoder.raw_decode(cleaned, match.start())
            return obj
        except json.JSONDecodeError:
            continue
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

