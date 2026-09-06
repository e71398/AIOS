"""Executor role for the v0.2.0 MVP.

The Executor is the only role that touches the file tools.
It receives the plan emitted by the Planner, asks the
configured provider for an action envelope, and then:

    1. executes any ``file_*`` tool calls against the
       per-workflow sandbox;
    2. accumulates the tool results into the work product;
    3. records a summary that the Reviewer will validate.

The Executor contract (the JSON the Executor emits) is:

    {
      "actions": [
        { "kind": "tool_call", "tool": "file_write",
          "args": { "path": "...", "content": "..." } },
        ...
        { "kind": "respond", "text": "..." }
      ],
      "summary": "...",
      "finish_reason": "stop"
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .providers import (
    Provider,
    ProviderError,
    ProviderRequest,
    ROLE_SYSTEM,
    ROLE_USER,
)
from .tools import (
    ToolInvocation,
    ToolRegistry,
    ToolResult,
)
from .workflow import Workflow, WorkflowStage


EXECUTOR_SYSTEM = (
    "You are an AIOS Executor. Convert the plan into concrete actions.\n"
    "Output a single JSON object with keys: actions, summary, finish_reason.\n"
    "Each action is either {\"kind\":\"tool_call\",\"tool\":\"file_read|file_write|file_list\","
    "\"args\":{...}} or {\"kind\":\"respond\",\"text\":\"...\"}.\n"
    "Do not include any text outside the JSON object."
)


@dataclass
class Executor:
    provider: Provider
    model: str
    tools: ToolRegistry
    file_store: Any  # FileResultStore, kept loosely typed to avoid a cycle
    max_actions: int = 8

    def execute(self, workflow: Workflow) -> Dict[str, Any]:
        if workflow.plan is None:
            raise ValueError("workflow has no plan; run Planner first")
        prompt = json.dumps(
            {"task": workflow.task, "plan": workflow.plan},
            ensure_ascii=False,
        )
        response = self.provider.chat(
            ProviderRequest(
                messages=[
                    {"role": ROLE_SYSTEM, "content": EXECUTOR_SYSTEM},
                    {"role": ROLE_USER, "content": prompt},
                ],
                model=self.model,
                max_tokens=1600,
                temperature=0.2,
            )
        )
        envelope = self._parse_envelope(response.text)
        actions: List[Dict[str, Any]] = envelope["actions"][: self.max_actions]
        tool_results: List[ToolResult] = []
        for action in actions:
            if action.get("kind") == "tool_call":
                tool = str(action.get("tool", ""))
                args = action.get("args") or {}
                if not isinstance(args, dict):
                    args = {}
                invocation = ToolInvocation(tool=tool, args=args)
                result = self.tools.invoke(invocation, workflow.id, self.file_store)
                tool_results.append(result)
        work_product = {
            "summary": envelope.get("summary", ""),
            "actions": actions,
            "tool_results": [r.to_dict() for r in tool_results],
            "artefacts": self._collect_artefacts(tool_results),
            "provider": {
                "name": self.provider.name,
                "model": self.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            },
            "raw_executor_response": response.text,
        }
        workflow.execution = work_product
        workflow.transition(WorkflowStage.EXECUTING, note="executor produced work product")
        return work_product

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_envelope(self, text: str) -> Dict[str, Any]:
        import re

        candidate = (text or "").strip()
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            obj = None
            for match in re.finditer(r"[\[{]", candidate):
                try:
                    parsed, _ = decoder.raw_decode(candidate[match.start():])
                    obj = parsed
                    break
                except json.JSONDecodeError:
                    continue
        if not isinstance(obj, dict):
            raise ProviderError("executor output was not a JSON object")
        actions = obj.get("actions", [])
        if not isinstance(actions, list):
            raise ProviderError("executor output 'actions' must be an array")
        return {
            "actions": actions,
            "summary": str(obj.get("summary", "")).strip(),
            "finish_reason": str(obj.get("finish_reason", "stop")).strip() or "stop",
        }

    def _collect_artefacts(self, tool_results: List[ToolResult]) -> List[Dict[str, Any]]:
        artefacts: List[Dict[str, Any]] = []
        for r in tool_results:
            if r.status != "ok":
                continue
            res = r.result or {}
            if r.tool == "file_write":
                artefacts.append({
                    "tool": "file_write",
                    "path": res.get("path"),
                    "size_bytes": int(res.get("size_bytes", 0)),
                    "abs_path": res.get("abs_path"),
                })
            elif r.tool == "file_read":
                artefacts.append({
                    "tool": "file_read",
                    "path": res.get("path"),
                    "size_bytes": int(res.get("size_bytes", 0)),
                })
        return artefacts
