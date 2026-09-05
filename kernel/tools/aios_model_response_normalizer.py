#!/usr/bin/env python3
"""AIOS P8C-U Universal Model Response Normalizer.

This module is the single point where any AI Provider's raw text
response is parsed into a structured payload that the rest of the
system can reason about. It is intentionally *Provider-agnostic*:

* It does not special-case ``minimax``, ``deepseek`` or ``qwen``.
* It strips provider-specific thinking blocks (``<think>…``)
  and Markdown fences.
* It extracts the first balanced JSON object.
* It validates the object against one of three well-known schemas:
  planner, reviewer verdict, code result.
* It captures diagnostic metadata: ``raw_response_hash``,
  ``normalization_steps``, ``truncation_summary``, ``response_size``,
  ``parsed_object_size``, ``schema_match``.

Classification rules:

* If the model's response does not satisfy the required schema after
  best-effort extraction, the result is marked
  ``external_contract_failure`` and the *caller* (model failover
  engine) MAY switch to the next model within the same tool.
* If the parser itself raises (e.g. response is bytes, not text),
  the result is ``internal_parser_exception`` and the *caller* MUST
  NOT switch models — this is a tool / adapter bug, not a model bug.
* The normalizer NEVER inspects secret material, NEVER probes a
  Provider, NEVER writes to disk. It is pure.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Schema catalogue
# ---------------------------------------------------------------------------

# A schema is a required-key set; we do not require structural types
# here.  The downstream tool adapter is responsible for deeper
# validation; the normalizer just guarantees *the required keys are
# present* and the object is a JSON object (not an array / scalar).

PLANNER_SCHEMA: Tuple[str, ...] = ("steps",)
REVIEWER_SCHEMA: Tuple[str, ...] = ("verdict",)
CODE_RESULT_SCHEMA: Tuple[str, ...] = ("result",)

ALL_SCHEMAS: Mapping[str, Tuple[str, ...]] = {
    "planner": PLANNER_SCHEMA,
    "reviewer": REVIEWER_SCHEMA,
    "code_result": CODE_RESULT_SCHEMA,
    "raw": (),  # no schema; just structure
}


# Failure categories emitted by the normalizer.

NORMALIZER_OK = "ok"
NORMALIZER_EXTERNAL_CONTRACT_FAILURE = "external_contract_failure"
NORMALIZER_INTERNAL_PARSER_EXCEPTION = "internal_parser_exception"
NORMALIZER_EMPTY_RESPONSE = "empty_response"


# ---------------------------------------------------------------------------
# Step catalog (recorded for traceability)
# ---------------------------------------------------------------------------


_NORMALIZATION_STEPS: Tuple[str, ...] = (
    "received",
    "type_check",
    "decode_text",
    "strip_think",
    "strip_markdown_fence",
    "extract_json_object",
    "schema_validate",
    "classify",
    "truncate",
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class NormalizedResponse:
    """Structured output of :func:`normalize_model_response`.

    ``category`` is one of:

    * ``ok``                                — schema satisfied
    * ``external_contract_failure``         — model output unusable
      (after best-effort extraction). The *model* is at fault; the
      caller may try another model within the same tool.
    * ``internal_parser_exception``         — the parser itself failed
      (e.g. ``bytes`` passed in, or the input object was the wrong
      type entirely). The *adapter* is at fault; the caller MUST NOT
      switch models.
    * ``empty_response``                    — no usable content.

    ``normalization_steps`` is the ordered list of transformations
    actually applied (subset of ``_NORMALIZATION_STEPS``); useful for
    debugging and acceptance.
    """

    category: str
    parsed: Optional[Any] = None
    raw_text: str = ""
    raw_response_hash: str = ""
    response_size: int = 0
    parsed_object_size: int = 0
    schema: str = "raw"
    schema_match: bool = False
    missing_keys: Tuple[str, ...] = ()
    normalization_steps: Tuple[str, ...] = ()
    truncation_summary: str = ""
    error_message: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["missing_keys"] = list(self.missing_keys)
        data["normalization_steps"] = list(self.normalization_steps)
        return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Hard cap on response size to keep diagnostic payload bounded.  Any
# response longer than this is truncated; the truncation_summary field
# records the cut.
MAX_NORMALIZED_RESPONSE_SIZE = 200 * 1024  # 200 KB raw text

# Hard cap on parsed-object size after normalisation.  Prevents
# oversized JSON from blowing up monitor / acceptance payloads.
MAX_PARSED_OBJECT_SIZE = 32 * 1024  # 32 KB


def normalize_model_response(
    raw: Any,
    *,
    schema: str = "raw",
    max_response_size: int = MAX_NORMALIZED_RESPONSE_SIZE,
) -> NormalizedResponse:
    """Normalize a raw model response into structured payload.

    :param raw:        The raw response from a Provider / adapter.
                       Accepts ``str``, ``bytes``, ``dict`` (already
                       parsed), ``list`` or ``None``. Anything else is
                       treated as a parser exception.
    :param schema:     One of ``planner``, ``reviewer``, ``code_result``
                       or ``raw``. Controls required-key validation.
    :param max_response_size: Hard cap on the text body that the
                       normalizer will read; larger payloads are
                       truncated and flagged.
    :returns:          :class:`NormalizedResponse`. Never raises.
    """
    steps: List[str] = []
    finished = _iso_now()
    schema_keys = ALL_SCHEMAS.get(schema, ())
    error_message = ""

    # 0. Type check — if the caller gave us something exotic, treat
    #    it as an *internal parser exception*. We never raise.
    if raw is None:
        return _empty("empty_response", schema, steps, finished,
                      error_message="raw is None")
    steps.append("received")

    # 1. Decode to text
    try:
        text = _coerce_to_text(raw)
    except Exception as exc:
        return NormalizedResponse(
            category=NORMALIZER_INTERNAL_PARSER_EXCEPTION,
            schema=schema,
            normalization_steps=tuple(steps + ["type_check", "decode_text"]),
            finished_at=finished,
            error_message=f"decode failed: {exc!r}",
        )
    steps.append("type_check")
    steps.append("decode_text")

    raw_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    if not text.strip():
        return NormalizedResponse(
            category=NORMALIZER_EMPTY_RESPONSE,
            schema=schema,
            raw_text="",
            raw_response_hash=raw_hash,
            response_size=len(text.encode("utf-8", errors="replace")),
            normalization_steps=tuple(steps),
            finished_at=finished,
        )

    # 2. Truncate the working text — keep the first N characters.
    truncation_summary = ""
    if len(text) > max_response_size:
        text = text[:max_response_size]
        truncation_summary = f"truncated at {max_response_size} chars"

    # 3. Strip <think>… blocks.  Repeated removes.
    stripped = _strip_think_blocks(text)
    if stripped != text:
        steps.append("strip_think")
        text = stripped

    # 4. Strip markdown code fences if present.
    stripped = _strip_markdown_fence(text)
    if stripped != text:
        steps.append("strip_markdown_fence")
        text = stripped

    # 5. JSON object extraction.
    parsed: Any = None
    extraction_error = ""
    if isinstance(raw, (dict, list)):
        # Caller already gave us a parsed object.
        parsed = raw
    else:
        try:
            parsed = _extract_first_json_object(text)
        except Exception as exc:
            extraction_error = repr(exc)
    if parsed is not None or extraction_error:
        steps.append("extract_json_object")

    # 6. Schema validation.
    schema_match = False
    missing_keys: List[str] = []
    if isinstance(parsed, dict):
        for key in schema_keys:
            if key not in parsed:
                missing_keys.append(key)
        schema_match = not missing_keys
    elif parsed is None:
        schema_match = False
        if not schema_keys:
            # ``raw`` schema accepts None — call it ok.
            schema_match = True
    steps.append("schema_validate")

    # 7. Classification.
    if isinstance(parsed, dict) and schema_match:
        category = NORMALIZER_OK
    elif parsed is None and not schema_keys:
        # ``raw`` schema, no JSON found — still ok; the caller will
        # treat it as plain text.
        category = NORMALIZER_OK
    elif extraction_error and parsed is None:
        category = NORMALIZER_INTERNAL_PARSER_EXCEPTION
    elif parsed is None:
        category = NORMALIZER_EXTERNAL_CONTRACT_FAILURE
    else:
        category = NORMALIZER_EXTERNAL_CONTRACT_FAILURE
    steps.append("classify")

    # 8. Size cap on the parsed object.
    parsed_size = 0
    if parsed is not None:
        try:
            parsed_size = len(json.dumps(parsed, ensure_ascii=False).encode("utf-8"))
        except Exception:
            parsed_size = 0
    if parsed_size > MAX_PARSED_OBJECT_SIZE:
        truncation_summary = (
            f"{truncation_summary}; parsed object truncated at "
            f"{MAX_PARSED_OBJECT_SIZE} bytes"
        )
        parsed = _truncate_parsed(parsed, MAX_PARSED_OBJECT_SIZE)

    steps.append("truncate")

    return NormalizedResponse(
        category=category,
        parsed=parsed,
        raw_text=text if len(text) <= max_response_size else text[:max_response_size],
        raw_response_hash=raw_hash,
        response_size=len(text.encode("utf-8", errors="replace")),
        parsed_object_size=parsed_size,
        schema=schema,
        schema_match=schema_match,
        missing_keys=tuple(missing_keys),
        normalization_steps=tuple(steps),
        truncation_summary=truncation_summary,
        error_message=extraction_error,
        finished_at=finished,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _coerce_to_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, ensure_ascii=False)
    raise TypeError(f"unsupported raw response type: {type(raw).__name__}")


_THINK_RE = re.compile(r"<think>.*?", re.DOTALL | re.IGNORECASE)


def _strip_think_blocks(text: str) -> str:
    """Repeatedly strip ``<think>…`` blocks.

    Some models emit them nested or in series; we apply the
    substitution until the text is stable so the post-condition holds.
    """
    prev = None
    out = text
    while prev != out:
        prev = out
        out = _THINK_RE.sub("", out)
    return out


_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL,
)


def _strip_markdown_fence(text: str) -> str:
    """If the *entire* response is a single fenced block, unwrap it."""
    match = _FENCE_RE.match(text.strip())
    if match:
        return match.group(1)
    return text


def _extract_first_json_object(text: str) -> Optional[Any]:
    """Find the first balanced JSON object in ``text``.

    Returns ``None`` if no JSON object is found.  Raises on parse
    errors so the caller can decide between external contract failure
    and internal parser exception.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:idx + 1]
                return json.loads(candidate)
    return None


def _truncate_parsed(parsed: Any, cap: int) -> Any:
    """Best-effort truncation of a parsed object.

    Strategy: serialise the object; if the serialisation exceeds
    ``cap`` bytes, replace the deepest list with ``[…truncated…]``
    until it fits.  Never raises.
    """
    try:
        encoded = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    except Exception:
        return parsed
    if len(encoded) <= cap:
        return parsed
    if isinstance(parsed, dict):
        out = {}
        running = 2  # braces
        for k, v in parsed.items():
            try:
                v_encoded = json.dumps(v, ensure_ascii=False).encode("utf-8")
            except Exception:
                v_encoded = b"<unencodable>"
            if running + len(v_encoded) + len(k) + 4 > cap:
                out[k] = "...truncated..."
                break
            out[k] = v
            running += len(v_encoded) + len(k) + 4
        return out
    if isinstance(parsed, list):
        return [..., "...truncated..."] if parsed else []
    return parsed


def _empty(category: str, schema: str, steps: Sequence[str],
           finished: str, error_message: str = "") -> NormalizedResponse:
    return NormalizedResponse(
        category=category,
        schema=schema,
        normalization_steps=tuple(steps),
        finished_at=finished,
        error_message=error_message,
    )


# ---------------------------------------------------------------------------
# Mapping helpers (for callers that need a structured mapping)
# ---------------------------------------------------------------------------


def category_to_failure_kind(category: str) -> str:
    """Map a normalizer category to a ``FAILURE_KIND`` string used by
    the model failover engine.
    """
    if category == NORMALIZER_OK:
        return ""
    if category == NORMALIZER_EXTERNAL_CONTRACT_FAILURE:
        return "external_contract_failure"
    if category == NORMALIZER_INTERNAL_PARSER_EXCEPTION:
        return "malformed_response_local"
    if category == NORMALIZER_EMPTY_RESPONSE:
        return "external_contract_failure"
    return "external_contract_failure"


__all__ = [
    "PLANNER_SCHEMA", "REVIEWER_SCHEMA", "CODE_RESULT_SCHEMA",
    "ALL_SCHEMAS",
    "NORMALIZER_OK",
    "NORMALIZER_EXTERNAL_CONTRACT_FAILURE",
    "NORMALIZER_INTERNAL_PARSER_EXCEPTION",
    "NORMALIZER_EMPTY_RESPONSE",
    "NormalizedResponse",
    "normalize_model_response",
    "category_to_failure_kind",
    "MAX_NORMALIZED_RESPONSE_SIZE",
    "MAX_PARSED_OBJECT_SIZE",
]