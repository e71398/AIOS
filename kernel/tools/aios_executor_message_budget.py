#!/usr/bin/env python3
"""
AIOS Executor Message Budget
============================

Single, authoritative budget helper for the executor (``Codex`` /
``OpenCode`` / …) prompt that ``aios_orchestrator._execution_text``
hands to the protocol layer.  The protocol layer rejects any payload
above ``HARD_LIMIT`` chars with
``Protocol blocked: 消息过长 (<len> > 4096)``; this helper guarantees
that no path can ever produce such a payload.

Contract (enforced end-to-end):

* :data:`HARD_LIMIT`  — 4096  (absolute protocol cap)
* :data:`TARGET_LIMIT` — 3800  (soft target; leaves headroom for
  transport- and encoding-driven size drift on Chinese / multi-byte
  prompts)

Priority order — pieces are appended in this order, each clipped to
the remaining budget:

    P1  base            (system / user task / acceptance / output format)
    P2  hf_instruction  (AUTHORITATIVE FACTS / HF-* lock)
    P3  host_evidence   (compact index + selected anchors)
    P4  failed_unit     (verbatim SYSTEMD_USER_FAILED body excerpt)
    P5  repair context  (previous_result correction block)

Behaviour rules:

* Profile-scoped caps are baked in: GENERAL gets a tighter host-evidence
  cap than OPS / AUDIT so the 9-item JSON block that originally tripped
  the 4096 ceiling is shrunk to essentials.
* Host evidence is rendered via compact JSON INDEX only — named fields,
  no raw body dump.  Full bodies stay on the workflow hash for the
  Reviewer.
* When the base (P1) alone exceeds the budget, the helper preserves
  the mandatory ``Original user goal (authoritative):`` anchor, clips
  the rest to budget, and records a clip — it never silently expands
  ``HARD_LIMIT``.
* Post-construction ``len(final) > HARD_LIMIT`` triggers an
  ``ExecutorMessageBudgetOverflow`` flag on the report; the production
  caller (``_execution_text``) applies a final hard clip so the
  executor call still ships while the violation is logged.

This module is the SINGLE source of truth — no other code may apply
``[:4096]``-style clamps to an executor prompt.  Add new pieces by
extending :class:`BudgetPiece` with a new priority; do not duplicate
the assembly loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

log = logging.getLogger("aios.executor_message_budget")

# Hard protocol cap; any larger payload is rejected by the upstream
# executor-protocol layer with ``Protocol blocked: 消息过长``.
HARD_LIMIT: int = 4096

# Soft target — leaves ~300 chars headroom for shell-escape /
# content-encoding inflation caused by Chinese / multi-byte content.
TARGET_LIMIT: int = 3800


class ExecutorMessageBudgetOverflow(RuntimeError):
    """P0 invariant: a final prompt exceeded HARD_LIMIT."""


@dataclass(frozen=True)
class BudgetPiece:
    tag: str
    body: str


_PROFILE_SECTION_CAPS = {
    # 2026-08-17 FIX_ONE closure: GENERAL-section raised from 900 → 5500
    # so the executor prompt can carry the full WEB_DISCOVERY_FETCH body
    # (≈1.5 KB) plus its index header.  Without the body the executor has
    # no usable fact source and either fabricates weather/astronomy data
    # or refuses to answer — both rejected by the grounding gate.
    "GENERAL": 5500,
    "OPS": 1600,
    "AUDIT": 1300,
    "CODE": 950,
}
_PROFILE_ITEMS_CAPS = {
    "GENERAL": 6,
    "OPS": 10,
    "AUDIT": 8,
    "CODE": 6,
}
_PROFILE_FAILED_CAPS = {
    "GENERAL": 300,
    "OPS": 600,
    "AUDIT": 500,
    "CODE": 350,
}


def profile_caps(profile: str) -> Tuple[int, int, int]:
    """Return ``(section_cap, items_cap, failed_cap)`` for a profile."""
    p = (profile or "GENERAL").upper()
    if p not in _PROFILE_SECTION_CAPS:
        log.warning(
            "unknown_host_evidence_profile profile=%s using_GENERAL_fallback", p,
        )
        p = "GENERAL"
    return (
        _PROFILE_SECTION_CAPS[p],
        _PROFILE_ITEMS_CAPS[p],
        _PROFILE_FAILED_CAPS[p],
    )


PRIORITY_ORDER: Tuple[str, ...] = (
    "base",
    "hf_instruction",
    "host_evidence",
    "failed_unit",
    "repair_context",
)


_HOST_TRIM_MARKER = "\n\n[host-evidence trimmed to fit 4096-char protocol cap]"
_FAILED_TRIM_MARKER = "\n\n[failed-unit trimmed to fit 4096-char protocol cap]"
_REPAIR_TRIM_MARKER = "\n\n[repair-context trimmed to fit 4096-char protocol cap]"


def render_compact_host_evidence_index(
    host_evidence: dict,
    section_cap: int,
    items_cap: int,
) -> str:
    """Compact JSON index with named fields only (no raw bodies)."""
    import json as _json
    if not host_evidence:
        return ""
    items = []
    for it in list(host_evidence.get("items") or [])[:items_cap]:
        if not isinstance(it, dict):
            continue
        flat = {
            "capability": str(it.get("capability", "")),
            "ok": "error" not in it,
            "unit": it.get("unit"),
            "status": it.get("status"),
            "match_count": it.get("match_count"),
            "listening_count": it.get("listening_count"),
            "error": (it.get("error") or None),
        }
        items.append({k: v for k, v in flat.items() if v is not None})
    payload = {
        "profile": host_evidence.get("profile", ""),
        "generated_at": host_evidence.get("generated_at", ""),
        "summary": host_evidence.get("summary") or {},
        "items": items,
    }
    body = _json.dumps(payload, ensure_ascii=False, default=str)
    if len(body) > section_cap:
        body = body[:section_cap]
    return (
        "\n\nAUTHORITATIVE HOST EVIDENCE (compact index; full bodies on "
        "workflow hash):\n" + body
    )


def _render_host_evidence(host_evidence: dict, profile: str) -> str:
    """Render a compact host-evidence section via the canonical builder."""
    if not host_evidence:
        return ""
    section_cap, items_cap, _ = profile_caps(profile)
    try:
        from aios_orchestrator_host_evidence_injection import (
            build_host_evidence_executor_section,
        )
        return build_host_evidence_executor_section(
            host_evidence,
            max_chars=section_cap,
            max_items=items_cap,
        )
    except Exception:  # pragma: no cover
        return render_compact_host_evidence_index(
            host_evidence, section_cap, items_cap,
        )


def render_failed_unit(host_evidence: dict, profile: str) -> str:
    """Extract the ``SYSTEMD_USER_FAILED`` body excerpt (profile-scoped)."""
    if not host_evidence:
        return ""
    _, _, failed_cap = profile_caps(profile)
    for it in host_evidence.get("items") or []:
        if not isinstance(it, dict):
            continue
        if it.get("capability") == "SYSTEMD_USER_FAILED":
            body = str(it.get("body") or "")[:failed_cap]
            if body:
                return (
                    "\n\nFAILED-UNIT BODY (verbatim excerpt; cite only "
                    "if SYSTEMD_USER_FAILED is in the index above):\n"
                    + body
                )
    return ""


def render_hf_instruction(host_evidence: dict) -> str:
    """Append the HF AUTHORITATIVE FACTS instruction when anchors exist."""
    if not host_evidence:
        return ""
    try:
        from aios_host_evidence_lock import (
            extract_anchor_facts,
            HF_PROMPT_INSTRUCTION,
        )
        anchors = extract_anchor_facts(host_evidence)
        if anchors:
            return HF_PROMPT_INSTRUCTION
    except Exception:  # pragma: no cover
        return ""
    return ""


@dataclass
class AssemblyReport:
    """Diagnostic record emitted by :func:`assemble_executor_message`."""

    final_message: str = ""
    final_length: int = 0
    target_limit: int = TARGET_LIMIT
    hard_limit: int = HARD_LIMIT
    pieces: List[Tuple[str, int]] = field(default_factory=list)
    clips: List[Tuple[str, int]] = field(default_factory=list)
    overflow: bool = False

    @property
    def passed(self) -> bool:
        return (not self.overflow) and self.final_length <= self.hard_limit


def _clip_body(
    tag: str,
    body: str,
    remaining: int,
    out_parts: List[str],
    used: List[int],
    report: AssemblyReport,
) -> int:
    """Append a clipped piece to ``out_parts`` under the given ``remaining``."""
    if remaining <= 0:
        return used[0]
    if len(body) <= remaining:
        out_parts.append(body)
        used[0] += len(body)
        report.pieces.append((tag, len(body)))
        return used[0]
    if tag == "host_evidence":
        cut = body[: max(0, remaining - len(_HOST_TRIM_MARKER))]
        out_parts.append(cut + _HOST_TRIM_MARKER)
        report.clips.append((tag, len(body) - len(cut)))
        used[0] += len(cut) + len(_HOST_TRIM_MARKER)
        return used[0]
    if tag == "failed_unit":
        cut = body[: max(0, remaining - len(_FAILED_TRIM_MARKER))]
        out_parts.append(cut + _FAILED_TRIM_MARKER)
        report.clips.append((tag, len(body) - len(cut)))
        used[0] += len(cut) + len(_FAILED_TRIM_MARKER)
        return used[0]
    if tag == "repair_context":
        cut = body[: max(0, remaining - len(_REPAIR_TRIM_MARKER))]
        out_parts.append(cut + _REPAIR_TRIM_MARKER)
        report.clips.append((tag, len(body) - len(cut)))
        used[0] += len(cut) + len(_REPAIR_TRIM_MARKER)
        return used[0]
    if tag == "base":
        anchor = "Original user goal (authoritative):\n"
        idx = body.find(anchor)
        if idx >= 0 and remaining > idx + len(anchor) + 200:
            head = body[: idx + len(anchor)]
            tail = body[idx + len(anchor):][: max(0, remaining - len(head))]
            out_parts.append(head + tail)
            report.clips.append((tag, len(body) - (len(head) + len(tail))))
            used[0] += len(head) + len(tail)
        else:
            clipped = body[:remaining]
            out_parts.append(clipped)
            report.clips.append((tag, len(body) - len(clipped)))
            used[0] += len(clipped)
        return used[0]
    # Unknown tag — hard-clip with marker.
    clipped = body[: max(0, remaining - 40)]
    out_parts.append(clipped + "\n\n[trimmed to fit 4096-char protocol cap]")
    report.clips.append((tag, len(body) - len(clipped)))
    used[0] = remaining
    return used[0]


def assemble_executor_message(
    base: str = "",
    *,
    host_evidence: Optional[dict] = None,
    repair_context: str = "",
    profile: Optional[str] = None,
) -> AssemblyReport:
    """Assemble the final executor message under the budget.

    See module docstring for the full contract.
    """
    report = AssemblyReport()
    pieces: List[BudgetPiece] = []
    if base:
        pieces.append(BudgetPiece("base", base))
    if host_evidence:
        resolved_profile = (
            profile or host_evidence.get("profile") or "GENERAL"
        )
        hf = render_hf_instruction(host_evidence)
        if hf:
            pieces.append(BudgetPiece("hf_instruction", hf))
        he = _render_host_evidence(host_evidence, resolved_profile)
        if he:
            pieces.append(BudgetPiece("host_evidence", he))
        fu = render_failed_unit(host_evidence, resolved_profile)
        if fu:
            pieces.append(BudgetPiece("failed_unit", fu))
    if repair_context:
        pieces.append(BudgetPiece("repair_context", repair_context))

    out_parts: List[str] = []
    used = [0]
    for tag in PRIORITY_ORDER:
        matching = [p for p in pieces if p.tag == tag]
        if not matching:
            continue
        body = matching[0].body
        remaining = TARGET_LIMIT - used[0]
        if remaining <= 0:
            break
        _clip_body(tag, body, remaining, out_parts, used, report)
        if used[0] >= TARGET_LIMIT:
            break
    final_message = "".join(out_parts)
    report.final_message = final_message
    report.final_length = len(final_message)
    if report.final_length > HARD_LIMIT:
        report.overflow = True
        log.error(
            "executor_message_budget_overflow len=%d hard_limit=%d",
            report.final_length, HARD_LIMIT,
        )
    return report


def hard_clip_for_protocol(message: str) -> str:
    """Last-mile hard clip applied by the production caller."""
    if len(message) > HARD_LIMIT:
        return message[:HARD_LIMIT]
    return message


__all__ = [
    "HARD_LIMIT",
    "TARGET_LIMIT",
    "ExecutorMessageBudgetOverflow",
    "BudgetPiece",
    "AssemblyReport",
    "PRIORITY_ORDER",
    "profile_caps",
    "render_compact_host_evidence_index",
    "render_failed_unit",
    "render_hf_instruction",
    "assemble_executor_message",
    "hard_clip_for_protocol",
]  # noqa: E501
