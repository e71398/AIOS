#!/usr/bin/env python3
"""AIOS P8B-R Codex × MiniMax Independent Binding Adapter.

A Codex-typed binding that talks to MiniMax without disturbing
the Codex binary's existing Codex Relay / DeepSeek configuration.

Invariants (mirrored from :mod:`aios_claude_minimax_adapter`):

* ``tool_id`` stays ``"codex"`` for every call.
* No global / shared state is mutated; per-instance client;
  per-call subprocess env override.
* Codex Relay / DeepSeek configuration is never touched.
* Concurrent calls share the *account-scoped* usage summary
  through a re-entrant lock, not through class-level globals.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from aios_minimax_client import (
    DEFAULT_MODEL,
    MiniMaxCallResult,
    MiniMaxClient,
)


BINDING_ID = "codex:minimax"
RESOURCE_ID = "minimax.shared"
TOOL_ID = "codex"


@dataclass
class CodexBindingRecord:
    """Structured record of one Codex × MiniMax call."""

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
    extracted_function_name: Optional[str] = None
    extracted_function_body_preview: str = ""
    concurrency_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CodexBindingUsageSummary:
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


class CodexMiniMaxAdapter:
    """Tool-typed adapter for Codex's MiniMax binding."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout: int = 90,
        resource_id: str = RESOURCE_ID,
        binding_id: str = BINDING_ID,
        shared_usage: Optional["CodexBindingUsageSummary"] = None,
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
        self._usage = shared_usage or CodexBindingUsageSummary()
        self._lock = threading.RLock()
        self._records: List[CodexBindingRecord] = []

    def get_client(self) -> MiniMaxClient:
        return self._client

    def with_subprocess_env(self, base: Optional[Mapping[str, str]] = None
                             ) -> Dict[str, str]:
        return self._client.with_subprocess_env(base)

    @property
    def usage(self) -> CodexBindingUsageSummary:
        return self._usage

    def records(self) -> List[CodexBindingRecord]:
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
    ) -> Tuple[CodexBindingRecord, MiniMaxCallResult]:
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
        self._ingest(record)
        return record, result

    def _build_record(
        self, *, started: datetime, finished: datetime,
        result: MiniMaxCallResult, concurrency_id: str,
    ) -> CodexBindingRecord:
        marker, tool_id, status = _parse_marker(result.content)
        func_name, func_body = _extract_function(result.content)
        preview = result.content[:160]
        return CodexBindingRecord(
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
            parsed_tool_id=tool_id,
            parsed_status=status,
            extracted_function_name=func_name,
            extracted_function_body_preview=func_body,
            concurrency_id=str(concurrency_id or ""),
        )

    def _ingest(self, record: CodexBindingRecord) -> None:
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
    if not content:
        return None, None, None
    text = content.strip()
    if not text:
        return None, None, None
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
    return (
        str(obj.get("marker")) if obj.get("marker") is not None else None,
        str(obj.get("tool_id")) if obj.get("tool_id") is not None else None,
        str(obj.get("status")) if obj.get("status") is not None else None,
    )


_FUNCTION_NAME_RE = re.compile(r"def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _extract_function(content: str) -> Tuple[Optional[str], str]:
    if not content:
        return None, ""
    m = _FUNCTION_NAME_RE.search(content)
    name = m.group(1) if m else None
    body = ""
    if name:
        start = content.find(name)
        if start != -1:
            body = content[start:start + 240]
    return name, body


__all__ = [
    "BINDING_ID", "RESOURCE_ID", "TOOL_ID",
    "CodexBindingRecord", "CodexBindingUsageSummary",
    "CodexMiniMaxAdapter",
]