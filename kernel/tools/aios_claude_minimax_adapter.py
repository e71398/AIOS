#!/usr/bin/env python3
"""AIOS P8B-R Claude × MiniMax Independent Binding Adapter.

This module provides a Claude-typed binding that talks to MiniMax
without touching the Claude binary's existing DeepSeek configuration.

Critical invariants:

* ``tool_id`` stays ``"claude"`` for every call. The adapter
  NEVER changes the public identity of the tool; the failover
  engine swaps the *binding*, not the tool.
* No global / shared state is mutated: each instance owns its
  own :class:`MiniMaxClient`; subprocess env overrides are
  per-call copies; no module-level os.environ assignment.
* The Claude DeepSeek configuration is NEVER touched. The
  adapter is a *parallel* binding path; it does not replace or
  rewrite the DeepSeek config.
* Each call returns a normalised :class:`MiniMaxCallResult` plus
  a structured :class:`ClaudeBindingRecord` that the failover
  engine and acceptance / monitor layers can serialise.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from aios_minimax_client import (
    DEFAULT_MODEL,
    MiniMaxCallResult,
    MiniMaxClient,
)


BINDING_ID = "claude:minimax"
RESOURCE_ID = "minimax.shared"
TOOL_ID = "claude"


@dataclass
class ClaudeBindingRecord:
    """Structured record of one Claude × MiniMax call.

    Mirrors :class:`ModelAttemptRecord` for the fields the
    failover engine needs. Tool identity stays ``claude``;
    the binding tracks its own usage / failure state.
    """

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
    parsed_marker: Optional[str] = None
    parsed_tool_id: Optional[str] = None
    parsed_status: Optional[str] = None
    concurrency_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ClaudeBindingUsageSummary:
    """Account-scoped usage aggregated across all Claude
    MiniMax calls in this process lifetime.
    """

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


class ClaudeMiniMaxAdapter:
    """Tool-typed adapter for Claude's MiniMax binding.

    Each instance carries its own :class:`MiniMaxClient`; the
    shared :class:`ClaudeBindingUsageSummary` is updated under a
    re-entrant lock. There is no module-level mutable state.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout: int = 90,
        resource_id: str = RESOURCE_ID,
        binding_id: str = BINDING_ID,
        shared_usage: Optional["ClaudeBindingUsageSummary"] = None,
    ) -> None:
        self.tool_id = TOOL_ID
        self.binding_id = str(binding_id)
        self.resource_id = str(resource_id)
        self.model = str(model)
        self._client = MiniMaxClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            resource_id=resource_id,
            binding_id=binding_id,
            tool_id=TOOL_ID,
        )
        self._usage = shared_usage or ClaudeBindingUsageSummary()
        self._lock = threading.RLock()
        self._records: List[ClaudeBindingRecord] = []

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def get_client(self) -> MiniMaxClient:
        return self._client

    def with_subprocess_env(self, base: Optional[Mapping[str, str]] = None
                             ) -> Dict[str, str]:
        """Per-call subprocess env override (isolation helper)."""
        return self._client.with_subprocess_env(base)

    @property
    def usage(self) -> ClaudeBindingUsageSummary:
        return self._usage

    def records(self) -> List[ClaudeBindingRecord]:
        with self._lock:
            return list(self._records)

    def chat(
        self,
        messages: Any,
        *,
        max_tokens: Optional[int] = None,
        temperature: float = 0.0,
        fake_response: Optional[str] = None,
        fake_usage: Optional[Mapping[str, int]] = None,
        fake_failure: Optional[str] = None,
        concurrency_id: str = "",
        model_override: Optional[str] = None,
    ) -> Tuple[ClaudeBindingRecord, MiniMaxCallResult]:
        """Drive a Claude-typed MiniMax call.

        ``tool_id`` on the returned record is always ``"claude"``;
        the underlying resource_id stays ``minimax.shared``. The
        caller can pass the record to
        :meth:`ModelFailoverEngine.record_model_attempt` to
        update the shared cooldown state.
        """
        t0 = datetime.now(timezone.utc)
        result = self._client.chat(
            messages,
            model_override=model_override,
            temperature=temperature,
            max_tokens=max_tokens,
            fake_response=fake_response,
            fake_usage=fake_usage,
            fake_failure=fake_failure,
        )
        t1 = datetime.now(timezone.utc)
        record = self._build_record(
            started=t0, finished=t1, result=result,
            concurrency_id=concurrency_id,
        )
        self._ingest(record, result)
        return record, result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_record(
        self, *, started: datetime, finished: datetime,
        result: MiniMaxCallResult, concurrency_id: str,
    ) -> ClaudeBindingRecord:
        marker, parsed_tool, parsed_status = _parse_marker(result.content)
        preview = result.content[:160]
        return ClaudeBindingRecord(
            tool_id=self.tool_id,
            binding_id=self.binding_id,
            resource_id=self.resource_id,
            called_at=started.isoformat(),
            duration_seconds=max(0.0, (finished - started).total_seconds()),
            success=bool(result.success),
            failure_kind=result.failure_kind,
            failure_scope=result.failure_scope,
            error_message=result.error_message,
            model_used=result.model_used,
            input_tokens=int(result.usage.input_tokens),
            output_tokens=int(result.usage.output_tokens),
            total_tokens=int(result.usage.total_tokens),
            estimated_cost=float(result.usage.estimated_cost),
            content_preview=preview,
            parsed_marker=marker,
            parsed_tool_id=parsed_tool,
            parsed_status=parsed_status,
            concurrency_id=str(concurrency_id or ""),
        )

    def _ingest(self, record: ClaudeBindingRecord,
                result: MiniMaxCallResult) -> None:
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


def _parse_marker(content: str) -> Tuple[Optional[str], Optional[str],
                                          Optional[str]]:
    """Best-effort marker extraction from a chat response.

    The Claude binding asks the model to emit a strict JSON
    envelope. We only do a lightweight scan; the failover engine
    trusts the record, not the marker, for selection decisions.
    """
    if not content:
        return None, None, None
    text = content.strip()
    if not text:
        return None, None, None
    # Strip leading markdown fences.
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text else text
        text = text.lstrip("json").lstrip()
        text = text.split("```", 1)[0]
    try:
        obj = json.loads(text)
    except Exception:
        return None, None, None
    if not isinstance(obj, dict):
        return None, None, None
    marker = obj.get("marker")
    tool = obj.get("tool_id")
    status = obj.get("status")
    return (
        str(marker) if marker is not None else None,
        str(tool) if tool is not None else None,
        str(status) if status is not None else None,
    )


__all__ = [
    "BINDING_ID", "RESOURCE_ID", "TOOL_ID",
    "ClaudeBindingRecord", "ClaudeBindingUsageSummary",
    "ClaudeMiniMaxAdapter", "parse_marker",
]
# public re-export for tests
parse_marker = _parse_marker