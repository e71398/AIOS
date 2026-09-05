#!/usr/bin/env python3
"""
Host-evidence prompt injection helpers
=====================================

This module is loaded by :mod:`aios_orchestrator` (executor prompt
construction) and :mod:`aios_verification_gate` (reviewer prompt
construction) to splice the canonical host-evidence block that
:mod:`aios_host_readonly_evidence` persisted on the workflow hash.

It is intentionally a SEPARATE module from
``aios_host_readonly_evidence`` so the orchestrator / verifier do
not pay the host-probe cost when only reading the workflow.  The
data is stored as a JSON string on the workflow hash and decoded
here.

Public surface:

* :func:`load_workflow_host_evidence(parent_id)` — read the JSON
  evidence block off the workflow hash and return the decoded dict.
* :func:`build_host_evidence_executor_section(evidence)` — produce
  the prompt section the Executor consumes.
* :func:`build_host_evidence_reviewer_section(evidence)` — produce
  the prompt section the Reviewer consumes.

The two sections share the same source-of-truth evidence dict but
target different consumers: the executor needs the full facts so it
can answer the user goal; the reviewer needs a compact, deterministic
summary so it can compare the deliverable against the authoritative
baseline.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from aios_orchestrator import get_workflow  # type: ignore


_MAX_SECTION_CHARS: int = 6500
_MAX_ITEMS_IN_SECTION: int = 14


# FIX_ONE closure (2026-08-17): capabilities whose ``body`` field is the
# canonical fact source for the executor.  When the host-evidence budget
# allows, the verbatim body is appended to the executor prompt so the
# executor can answer independent-live queries from real evidence instead
# of fabricating.  Only text-body capabilities are listed; binary / error
# items must remain index-only.
_BODY_CAPABILITIES = frozenset({
    "WEB_DISCOVERY_FETCH",
    "READ_FILE",
    "LIST_DIRECTORY",
    "FILE_METADATA",
    "GIT_LOG",
    "GIT_STATUS",
    "GIT_BRANCH",
    "GIT_HEAD",
    "LOCAL_HTTP_GET",
    "SYSTEMD_USER_STATUS",
    "SYSTEMD_USER_SHOW",
    "JOURNAL_USER_UNIT_RECENT",
})


def load_workflow_host_evidence(parent_id: str) -> Optional[Dict[str, Any]]:
    """Read the workflow hash and return the parsed host evidence dict."""
    if not parent_id:
        return None
    try:
        workflow = get_workflow(parent_id)
    except Exception:
        return None
    if not isinstance(workflow, dict):
        return None
    raw = workflow.get("host_evidence")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def build_host_evidence_executor_section(
    evidence: Dict[str, Any],
    *,
    max_chars: int = _MAX_SECTION_CHARS,
    max_items: int = _MAX_ITEMS_IN_SECTION,
) -> str:
    """Render the prompt section the Executor consumes.

    V1 final-production shape: do NOT dump the full raw bodies
    into the executor prompt (they exceed the 7000-char deliverable
    cap, and the LLM ends up truncating mid-line).  Instead,
    surface a compact summary + the failed_unit body (the
    single most-likely-to-be-missed production concern) so the
    LLM has a usable mental model of host state without exceeding
    the deliverable cap.  The full body is still on the workflow
    hash for the Reviewer to cross-check.

    2026-08-17 FIX_ONE closure: bodies for ``WEB_DISCOVERY_FETCH``
    (and any other text-body capability that fits the budget) are
    appended verbatim after the index so the executor can answer
    independent-live queries from real evidence instead of
    fabricating.  Bodies are truncated to the remaining ``max_chars``
    budget so the overall prompt fits ``hard_limit``.
    """
    if not evidence:
        return ""
    items = list(evidence.get("items") or [])[:max_items]
    summary = evidence.get("summary") or {}
    profile = evidence.get("profile", "")
    generated_at = evidence.get("generated_at", "")

    # Compact per-item index: capability, ok/error, unit, count.
    index = []
    failed_unit = None
    body_fragments: List[str] = []
    remaining_chars = max_chars
    for it in items:
        if not isinstance(it, dict):
            continue
        cap = str(it.get("capability", ""))
        ok = "error" not in it
        err = "" if ok else str(it.get("error", ""))
        index.append({
            "capability": cap,
            "ok": ok,
            "error": err,
            "unit": it.get("unit"),
            "match_count": it.get("match_count"),
            "listening_count": it.get("listening_count"),
            "http_status": it.get("status"),
        })
        if it.get("capability") == "SYSTEMD_USER_FAILED":
            body_text = str(it.get("body") or "")
            if body_text:
                failed_unit = body_text[:600]
        # FIX_ONE: surface text bodies that fit the budget so the
        # executor can quote/use real evidence instead of inventing.
        body_text = str(it.get("body") or "")
        if body_text and cap in _BODY_CAPABILITIES:
            # Reserve ~600 chars for the index header + headers below
            if remaining_chars > 800:
                excerpt = body_text[: max(0, remaining_chars - 800)]
                if excerpt:
                    body_fragments.append(
                        f"\n--- {cap} BODY (verbatim from AIOS host evidence) ---\n"
                        + excerpt
                    )
                    remaining_chars -= len(excerpt) + 80
    payload = {
        "profile": profile,
        "generated_at": generated_at,
        "summary": summary,
        "index": index,
    }
    body = json.dumps(payload, ensure_ascii=False, default=str)
    if len(body) > remaining_chars:
        body = body[: max(0, remaining_chars - 50)]
    out = (
        "\n\nAUTHORITATIVE HOST EVIDENCE (collected by AIOS on submit, "
        "outside the Codex sandbox):\n"
        "Do NOT re-probe systemd / journalctl / localhost / git from "
        "inside the sandbox.  The body below is a compact INDEX of "
        "what AIOS captured.  Every concrete numeric / state value "
        "you cite must be one of the HF-* ids from the "
        "AUTHORITATIVE FACTS instruction above.  Do NOT invent, "
        "recompute, or restate any value (PID, port count, memory, "
        "CPU, restart count, service state, HTTP status, timestamp). "
        "If a capability reported ok=false, you MUST mention the "
        "error in your summary.  For text-body capabilities "
        "(WEB_DISCOVERY_FETCH, READ_FILE, LIST_DIRECTORY, …) the "
        "verbatim body is appended below the index so you can quote "
        "it directly.\n"
        + body
    )
    if body_fragments:
        out += "".join(body_fragments)
    if failed_unit:
        out += (
            "\n\nFAILED-UNIT BODY (verbatim from host evidence — "
            "must be quoted in your summary if SYSTEMD_USER_FAILED "
            "is listed in the index above):\n"
            + failed_unit
        )
    return out


def build_host_evidence_reviewer_section(
    evidence: Dict[str, Any],
    *,
    max_chars: int = 1200,
    max_items: int = 8,
    include_bodies: bool = False,
) -> str:
    """Render the prompt section the Reviewer consumes.

    The Reviewer is the canonical fact-grounder; this function
    embeds the structured ``summary`` block plus the
    ``capability`` / ok / truncated flag of every host-evidence
    item so the Reviewer can correlate the Executor's claims
    against the actual systemd / journal / ports / health /
    task-status payloads collected by AIOS on submit.  When
    ``include_bodies`` is True (use case: codex / fast reviewer
    sub-processes), the body of every item is embedded too —
    capped per-item — so the Reviewer can quote exact PID /
    timestamp values.
    """
    if not evidence:
        return ""
    items = list(evidence.get("items") or [])[:max_items]
    summary = evidence.get("summary") or {}
    full_items = []
    per_item_body_cap = 1500
    for item in items:
        if not isinstance(item, dict):
            continue
        compact = {
            "capability": item.get("capability", ""),
            "ok": "error" not in item,
            "truncated": bool(item.get("truncated")),
            "path": item.get("path"),
            "unit": item.get("unit"),
            "lines_requested": item.get("lines_requested"),
            "match_count": item.get("match_count"),
            "listening_count": item.get("listening_count"),
            "status": item.get("status"),
            "error": item.get("error"),
            "size_bytes": item.get("size_bytes"),
            "entry_count": item.get("entry_count"),
            "stderr": (item.get("stderr") or "")[:256] or None,
        }
        if include_bodies:
            body = item.get("body")
            if isinstance(body, str) and len(body) > per_item_body_cap:
                body = body[:per_item_body_cap] + "...[truncated]"
            if body is not None:
                compact["body"] = body
        full_items.append(compact)
    payload = {
        "profile": evidence.get("profile", ""),
        "generated_at": evidence.get("generated_at", ""),
        "summary": summary,
        "items": full_items,
        "host_evidence_used": bool(
            evidence.get("items") and evidence.get("profile", "").upper()
            in ("OPS", "AUDIT")
        ),
    }
    body = json.dumps(payload, ensure_ascii=False, default=str)
    if len(body) > max_chars:
        body = body[:max_chars]
    return (
        "\n\nAUTHORITATIVE HOST EVIDENCE (collected by AIOS on submit, "
        "outside any sandbox):\n" + body
    )


__all__ = [
    "load_workflow_host_evidence",
    "build_host_evidence_executor_section",
    "build_host_evidence_reviewer_section",
]