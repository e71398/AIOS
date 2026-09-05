#!/usr/bin/env python3
"""AIOS P8B-R Hermes × MiniMax binding verifier.

The Hermes binding already exists (``hermes:minimax`` → ``minimax.shared``).
This module provides a *non-mutating* verifier that:

* confirms the binding identity (``tool_id=hermes``,
  ``resource_id=minimax.shared``);
* runs a single minimax-side ``HERMES_P8B_MINIMAX_OK`` review
  verdict without disturbing the Hermes binary's review state;
* aggregates account-scoped usage into the existing
  ``minimax.shared`` ledger without sharing any private state
  with the Codex / Claude adapters.

No subprocess env mutation; the instance owns a per-binding
``MiniMaxClient``; the only writable shared state is the
account-scoped :class:`HermesBindingUsageSummary` summary.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from aios_minimax_client import (
    DEFAULT_MODEL,
    MiniMaxCallResult,
    MiniMaxClient,
)


BINDING_ID = "hermes:minimax"
RESOURCE_ID = "minimax.shared"
TOOL_ID = "hermes"
EXPECTED_VERDICT = "HERMES_P8B_MINIMAX_OK"


@dataclass
class HermesBindingRecord:
    tool_id: str = TOOL_ID
    binding_id: str = BINDING_ID
    resource_id: str = RESOURCE_ID
    called_at: str = ""
    duration_seconds: float = 0.0
    success: bool = False
    failure_kind: Optional[str] = None
    failure_scope: Optional[str] = None
    error_message: Optional[str] = None
    model_used: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    content_preview: str = ""
    verdict: Optional[str] = None
    concurrency_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HermesBindingUsageSummary:
    calls: int = 0
    successes: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    last_call_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HermesMiniMaxVerifier:
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout: int = 90,
        shared_usage: Optional["HermesBindingUsageSummary"] = None,
    ) -> None:
        self.tool_id = TOOL_ID
        self.binding_id = BINDING_ID
        self.resource_id = RESOURCE_ID
        self.model = str(model)
        self._client = MiniMaxClient(
            base_url=base_url, api_key=api_key,
            model=model, timeout=timeout,
            resource_id=RESOURCE_ID, binding_id=BINDING_ID,
            tool_id=TOOL_ID,
        )
        self._usage = shared_usage or HermesBindingUsageSummary()
        self._lock = threading.RLock()
        self._records: List[HermesBindingRecord] = []

    def get_client(self) -> MiniMaxClient:
        return self._client

    @property
    def usage(self) -> HermesBindingUsageSummary:
        return self._usage

    def records(self) -> List[HermesBindingRecord]:
        with self._lock:
            return list(self._records)

    def with_subprocess_env(self, base: Optional[Mapping[str, str]] = None
                             ) -> Dict[str, str]:
        return self._client.with_subprocess_env(base)

    def review(
        self,
        subject: str,
        *,
        fake_response: Optional[str] = None,
        fake_failure: Optional[str] = None,
        concurrency_id: str = "",
        model_override: Optional[str] = None,
    ) -> Tuple[HermesBindingRecord, MiniMaxCallResult, str]:
        """Run a no-side-effect minimax-side reviewer verdict.

        ``subject`` is the proposal / artifact to review; the
        verifier asks MiniMax for the literal token
        ``HERMES_P8B_MINIMAX_OK`` (or any deterministic
        verdict string) and never posts, sends, or alters
        state outside this instance.
        """
        messages = [
            {"role": "system",
             "content": ("You are the Hermes reviewer running on the "
                         "minimax.shared resource. Reply with only "
                         "the exact string: HERMES_P8B_MINIMAX_OK")},
            {"role": "user",
             "content": f"review subject: {subject}"},
        ]
        t0 = datetime.now(timezone.utc)
        result = self._client.chat(
            messages,
            model_override=model_override,
            temperature=0.0,
            max_tokens=32,
            fake_response=fake_response,
            fake_failure=fake_failure,
        )
        t1 = datetime.now(timezone.utc)
        verdict = _extract_verdict(result.content)
        record = HermesBindingRecord(
            tool_id=self.tool_id,
            binding_id=self.binding_id,
            resource_id=self.resource_id,
            called_at=t0.isoformat(),
            duration_seconds=max(0.0, (t1 - t0).total_seconds()),
            success=bool(result.success and verdict == EXPECTED_VERDICT),
            failure_kind=result.failure_kind if not result.success else None,
            failure_scope=result.failure_scope if not result.success else None,
            error_message=result.error_message if not result.success else None,
            model_used=result.model_used,
            input_tokens=int(result.usage.input_tokens),
            output_tokens=int(result.usage.output_tokens),
            total_tokens=int(result.usage.total_tokens),
            estimated_cost=float(result.usage.estimated_cost),
            content_preview=result.content[:160],
            verdict=verdict,
            concurrency_id=str(concurrency_id or ""),
        )
        self._ingest(record)
        return record, result, verdict

    def _ingest(self, record: HermesBindingRecord) -> None:
        with self._lock:
            self._records.append(record)
            self._usage.calls += 1
            if record.success:
                self._usage.successes += 1
            else:
                self._usage.failures += 1
            self._usage.input_tokens += record.input_tokens
            self._usage.output_tokens += record.output_tokens
            self._usage.total_tokens += record.total_tokens
            self._usage.estimated_cost += record.estimated_cost
            self._usage.last_call_at = record.called_at


def _extract_verdict(content: str) -> str:
    if not content:
        return ""
    txt = content.strip()
    if txt.startswith("```"):
        txt = txt.split("```", 2)[1] if "```" in txt else txt
        txt = txt.lstrip("json").lstrip()
        txt = txt.split("```", 1)[0]
    # Direct string match wins.
    if EXPECTED_VERDICT in txt:
        return EXPECTED_VERDICT
    # JSON envelope fallback.
    try:
        obj = json.loads(txt)
    except Exception:
        return ""
    if isinstance(obj, dict) and obj.get("verdict"):
        return str(obj["verdict"])
    return ""


__all__ = [
    "BINDING_ID", "RESOURCE_ID", "TOOL_ID", "EXPECTED_VERDICT",
    "HermesBindingRecord", "HermesBindingUsageSummary",
    "HermesMiniMaxVerifier",
]