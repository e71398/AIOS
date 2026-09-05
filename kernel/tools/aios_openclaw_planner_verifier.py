#!/usr/bin/env python3
"""AIOS P8B-R OpenClaw × MiniMax pure planner verifier.

OpenClaw's ``openclaw:minimax`` binding already exists. The
verifier confirms the binding without sending 飞书,
opening sessions, or executing plans:

* no subprocess writes to the OpenClaw binary;
* no agent session is opened on the OpenClaw side;
* the verifier asks MiniMax for a *plan sketch* only and
  inspects the response locally; it NEVER forwards the plan
  to OpenClaw for execution.

The OpenClaw binary, the OpenClaw config, and any agent
session state stay untouched. The only mutable state is the
per-instance :class:`OpenClawPlannerUsageSummary`.
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


BINDING_ID = "openclaw:minimax"
RESOURCE_ID = "minimax.shared"
TOOL_ID = "openclaw"
PLAN_MARKER = "OPENCLAW_P8B_PLAN_SKETCH"
FORBIDDEN_EXECUTION_TOKENS = (
    "EXECUTE",
    "send_feishu",
    "send_telegram",
    "publish_plan",
    "trigger_orchestrator",
)


@dataclass
class OpenClawPlannerRecord:
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
    plan_text: str = ""
    plan_steps: int = 0
    execution_keywords_present: int = 0
    concurrency_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OpenClawPlannerUsageSummary:
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


class OpenClawPlannerVerifier:
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout: int = 90,
        shared_usage: Optional["OpenClawPlannerUsageSummary"] = None,
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
        self._usage = shared_usage or OpenClawPlannerUsageSummary()
        self._lock = threading.RLock()
        self._records: List[OpenClawPlannerRecord] = []

    def get_client(self) -> MiniMaxClient:
        return self._client

    @property
    def usage(self) -> OpenClawPlannerUsageSummary:
        return self._usage

    def records(self) -> List[OpenClawPlannerRecord]:
        with self._lock:
            return list(self._records)

    def with_subprocess_env(self, base: Optional[Mapping[str, str]] = None
                             ) -> Dict[str, str]:
        return self._client.with_subprocess_env(base)

    def plan(
        self,
        task: str,
        *,
        fake_response: Optional[str] = None,
        fake_failure: Optional[str] = None,
        concurrency_id: str = "",
        model_override: Optional[str] = None,
    ) -> Tuple[OpenClawPlannerRecord, MiniMaxCallResult, str]:
        """Ask MiniMax for a *plan sketch*; never execute it.

        ``task`` is the user-facing task description; the
        verifier constructs an OpenClaw planner-style prompt
        that explicitly forbids sending 飞书 / Telegram /
        publishing a plan. The output is returned as a local
        string and never forwarded to OpenClaw.
        """
        messages = [
            {"role": "system",
             "content": ("You are the OpenClaw planner running on the "
                         "minimax.shared resource. Produce only a "
                         "structured plan sketch as JSON. DO NOT "
                         "execute, send, or publish anything. "
                         f"Always include the marker '{PLAN_MARKER}' "
                         "verbatim in the output.")},
            {"role": "user",
             "content": f"task: {task}"},
        ]
        t0 = datetime.now(timezone.utc)
        result = self._client.chat(
            messages,
            model_override=model_override,
            temperature=0.0,
            max_tokens=200,
            fake_response=fake_response,
            fake_failure=fake_failure,
        )
        t1 = datetime.now(timezone.utc)
        plan_text, steps, exec_kw = _inspect_plan(result.content)
        record = OpenClawPlannerRecord(
            tool_id=self.tool_id,
            binding_id=self.binding_id,
            resource_id=self.resource_id,
            called_at=t0.isoformat(),
            duration_seconds=max(0.0, (t1 - t0).total_seconds()),
            success=bool(result.success and PLAN_MARKER in (result.content or "")),
            failure_kind=result.failure_kind if not result.success else None,
            failure_scope=result.failure_scope if not result.success else None,
            error_message=result.error_message if not result.success else None,
            model_used=result.model_used,
            input_tokens=int(result.usage.input_tokens),
            output_tokens=int(result.usage.output_tokens),
            total_tokens=int(result.usage.total_tokens),
            estimated_cost=float(result.usage.estimated_cost),
            plan_text=plan_text,
            plan_steps=steps,
            execution_keywords_present=exec_kw,
            concurrency_id=str(concurrency_id or ""),
        )
        self._ingest(record)
        return record, result, plan_text

    def _ingest(self, record: OpenClawPlannerRecord) -> None:
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


def _inspect_plan(content: str) -> Tuple[str, int, int]:
    """Extract plan text and count steps / forbidden keywords.

    A real OpenClaw call would forward the plan to an agent
    session; the verifier MUST NOT do that. We only count
    forbidden keywords so tests can assert the response is
    truly plan-only.
    """
    if not content:
        return "", 0, 0
    txt = content.strip()
    body = txt
    if txt.startswith("```"):
        body = txt.split("```", 2)[1] if "```" in txt else txt
        body = body.lstrip("json").lstrip()
        body = body.split("```", 1)[0]
    # Try JSON parse to count steps array; fall back to line count.
    steps = 0
    plan_text = body
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            arr = obj.get("steps") or obj.get("plan") or []
            if isinstance(arr, list):
                steps = len(arr)
            elif isinstance(arr, str):
                steps = len([ln for ln in arr.splitlines() if ln.strip()])
            plan_text = json.dumps(obj, ensure_ascii=False)[:400]
        elif isinstance(obj, list):
            steps = len(obj)
            plan_text = json.dumps(obj, ensure_ascii=False)[:400]
    except Exception:
        # Fallback: count non-empty lines starting with digits / dashes.
        steps = sum(1 for ln in body.splitlines()
                    if ln.strip().startswith(("-", "*", "1", "2", "3",
                                              "4", "5", "6", "7", "8", "9")))
        plan_text = body[:400]
    lower = body.lower()
    exec_kw = sum(1 for kw in FORBIDDEN_EXECUTION_TOKENS if kw.lower() in lower)
    return plan_text, steps, exec_kw


__all__ = [
    "BINDING_ID", "RESOURCE_ID", "TOOL_ID", "PLAN_MARKER",
    "FORBIDDEN_EXECUTION_TOKENS",
    "OpenClawPlannerRecord", "OpenClawPlannerUsageSummary",
    "OpenClawPlannerVerifier",
]