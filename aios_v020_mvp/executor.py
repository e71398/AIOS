"""Executor role for the v0.2.0 MVP.

The Executor is the only role that touches the file tools. It
receives the plan emitted by the Planner, asks the configured
provider for an action envelope, and then:

    1. executes any file_* tool calls against the per-workflow sandbox;
    2. accumulates the tool results into the work product;
    3. records a summary that the Reviewer will validate.

The Executor emits the AIOS v0.2.0 structured-output envelope (see
:mod:`aios_v020_mvp.structured_contract`). The ``data`` field looks
like::

    {
      "actions": [
        {"kind": "tool_call", "tool": "file_write",
         "args": {"path": "...", "content": "..."}},
        {"kind": "respond", "text": "..."}
      ],
      "summary": "...",
      "finish_reason": "stop"
    }

Internally we normalise three envelope shapes that real LLMs tend to
emit (kind=tool_call / kind=file_write / kind=file_read) into a
single canonical shape before invoking the tool registry.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

from .providers import (
    Provider,
    ProviderError,
    ProviderRequest,
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_USER,
)
from .structured_contract import (
    ContractError,
    REPAIR_SYSTEM,
    extract_first_json,
    repair_prompt,
    validate_envelope,
)
from .tools import ToolInvocation, ToolRegistry, ToolResult
from .workflow import Workflow, WorkflowStage


EXECUTOR_SYSTEM = (
    "You are the AIOS Executor. Given a plan, emit concrete actions. "
    "Output EXACTLY one JSON object, no prose, no markdown fences.\n\n"
    "Top-level keys:\n"
    "  schema_version : must be \"aios-v020-1.0\"\n"
    "  role           : must be \"executor\"\n"
    "  status         : \"ok\" (or \"error\" if the plan is impossible)\n"
    "  data           : object with actions / summary / finish_reason\n"
    "  notes          : optional short note\n\n"
    "Inside data use EXACTLY:\n"
    "  actions        : array of action objects (see below)\n"
    "  summary        : one-sentence description of what was done\n"
    "  finish_reason  : always the string \"stop\"\n\n"
    "Action objects:\n"
    "  {\"kind\":\"tool_call\",\"tool\":\"file_read\",\"args\":{\"path\":\"...\"}}\n"
    "  {\"kind\":\"tool_call\",\"tool\":\"file_write\",\"args\":{\"path\":\"...\",\"content\":\"...\"}}\n"
    "  {\"kind\":\"tool_call\",\"tool\":\"file_list\",\"args\":{\"path\":\"\"}}\n"
    "  {\"kind\":\"respond\",\"text\":\"final user-facing text\"}\n\n"
    "If you write a file, include BOTH path and content. Paths must be "
    "relative (the sandbox takes care of the rest). "
    "IMPORTANT: the user message includes an `input_files` object with "
    "the real content of files available in the sandbox. When the task "
    "asks you to summarise, review or report on a file, derive your "
    "content from that real `input_files` material — never write "
    "placeholder text such as 'will be written here'. Respond with the "
    "JSON only."
)


_TOOL_KINDS = {"file_read", "file_write", "file_list"}


@dataclass
class Executor:
    provider: Provider
    model: str
    tools: ToolRegistry
    file_store: Any
    max_actions: int = 10

    def execute(self, workflow: Workflow) -> Dict[str, Any]:
        if workflow.plan is None:
            raise ValueError("workflow has no plan; run Planner first")
        # Give the model the ACTUAL content of the files already in the
        # workflow sandbox (seeded via POST /task "files"). Without this
        # the model can only parrot whatever placeholder text the plan
        # happened to contain; with it the model can produce real
        # summaries / reviews derived from real file contents.
        input_files: Dict[str, str] = {}
        try:
            for art in self.file_store.list(workflow.id)[:10]:
                try:
                    input_files[art.rel_path] = self.file_store.read(
                        workflow.id, art.rel_path,
                    )[:8000]
                except Exception:
                    continue
        except Exception:
            input_files = {}
        prompt = json.dumps(
            {
                "task": workflow.task,
                "plan": workflow.plan,
                "input_files": input_files,
            },
            ensure_ascii=False,
        )
        envelope, raw, in_tok, out_tok = self._call_with_repair(
            [
                {"role": ROLE_SYSTEM, "content": EXECUTOR_SYSTEM},
                {"role": ROLE_USER, "content": prompt},
            ],
            expected_role="executor",
        )
        actions = envelope["data"].get("actions", [])
        # Backward compat: also accept ``steps`` (a planner-style envelope).
        if not actions:
            actions = envelope["data"].get("steps", [])
        normalised: List[Dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = str(action.get("kind") or "").strip().lower()
            if kind == "tool_call":
                tool = str(action.get("tool") or "").strip().lower()
                if tool not in _TOOL_KINDS:
                    continue
                args = action.get("args")
                if not isinstance(args, dict):
                    args = {}
                normalised.append({"kind": "tool_call", "tool": tool, "args": args})
            elif kind in _TOOL_KINDS:
                args = action.get("args")
                if not isinstance(args, dict):
                    args = {}
                normalised.append({"kind": "tool_call", "tool": kind, "args": args})
            elif kind == "respond":
                text = str(action.get("text") or "").strip()
                if text:
                    normalised.append({"kind": "respond", "text": text})
        tool_results: List[ToolResult] = []
        for action in normalised:
            if action["kind"] != "tool_call":
                continue
            invocation = ToolInvocation(tool=action["tool"], args=action["args"])
            result = self.tools.invoke(invocation, workflow.id, self.file_store)
            tool_results.append(result)
        work_product = {
            "schema_version": envelope.get("schema_version"),
            "role": envelope.get("role"),
            "status": envelope.get("status"),
            "notes": envelope.get("notes", ""),
            "summary": envelope["data"].get("summary", ""),
            "actions": normalised,
            "tool_results": [r.to_dict() for r in tool_results],
            "artefacts": self._collect_artefacts(tool_results),
            "provider": {
                "name": self.provider.name,
                "model": self.model,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            },
            "raw_executor_response": raw,
        }
        workflow.execution = work_product
        workflow.transition(
            WorkflowStage.EXECUTING, note="executor produced work product",
        )
        return work_product

    def _call_with_repair(
        self, messages: List[Dict[str, str]], expected_role: str,
    ):
        response = self.provider.chat(
            ProviderRequest(
                messages=messages, model=self.model,
                max_tokens=1800, temperature=0.2,
                metadata={"role": expected_role},
            )
        )
        envelope, err = self._try_envelope(response.text, expected_role)
        if envelope is not None:
            return envelope, response.text, response.input_tokens, response.output_tokens
        repair_messages = [
            {"role": ROLE_SYSTEM, "content": REPAIR_SYSTEM},
            *messages,
            {"role": ROLE_ASSISTANT, "content": response.text},
            repair_prompt(expected_role, response.text, str(err)),
        ]
        repair_response = self.provider.chat(
            ProviderRequest(
                messages=repair_messages, model=self.model,
                max_tokens=1800, temperature=0.0,
                metadata={"role": expected_role, "phase": "repair"},
            )
        )
        envelope, err2 = self._try_envelope(repair_response.text, expected_role)
        if envelope is None:
            raise ProviderError(
                f"executor contract violated after repair: {err2}; "
                f"raw={repair_response.text[:200]!r}"
            )
        return (
            envelope,
            repair_response.text,
            repair_response.input_tokens,
            repair_response.output_tokens,
        )

    @staticmethod
    def _try_envelope(text: str, expected_role: str):
        obj = extract_first_json(text)
        if obj is None:
            return None, ContractError(
                "no JSON object in response", reason="no_json_object",
            )
        try:
            env = validate_envelope(obj, expected_role)
        except ContractError as exc:
            return None, exc
        data = env["data"]
        # Accept either ``actions`` or ``steps`` (planner-style) as the
        # source list. The Executor normalises both internally.
        if "actions" in data and isinstance(data["actions"], list):
            return env, None
        if "steps" in data and isinstance(data["steps"], list):
            data["actions"] = list(data["steps"])
            return env, None
        return None, ContractError(
            "executor 'data.actions' (or 'steps') missing or not a list",
            reason="missing_field",
        )

    @staticmethod
    def _collect_artefacts(tool_results: List[ToolResult]) -> List[Dict[str, Any]]:
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


