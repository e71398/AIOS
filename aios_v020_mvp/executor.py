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
    "You are the AIOS Executor. Given a plan, emit concrete tool "
    "actions. Output EXACTLY one JSON object, no prose, no markdown "
    "fences.\n\n"
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
    "RULES:\n"
    "  1. The user message lists WORKSPACE FILES as metadata only "
    "(path, name, size, allowed tools). It NEVER contains file content.\n"
    "  2. To read a file's content you MUST emit a file_read tool_call. "
    "After the host executes your actions the tool results (with the "
    "real content, read by the HOST) are returned to you in a follow-up "
    "message; derive your response from those results.\n"
    "  3. If you write a file, include BOTH path and content, and make "
    "sure the content is a real deliverable derived from the tool "
    "results — never placeholder text such as 'will be written here'.\n"
    "  4. Paths must be relative (the sandbox takes care of the rest).\n"
    "Respond with the JSON only."
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
        # Sandbox METADATA only — never file content. The model must
        # emit file_read to obtain content, which the HOST executes and
        # returns in a follow-up turn. PROMPT_PRELOADED_FILE_CONTENT=0
        # is an auditable invariant enforced by this module.
        sandbox_files = self._sandbox_metadata(workflow.id)
        prompt = json.dumps(
            {
                "task": workflow.task,
                "plan": workflow.plan,
                "workspace": {
                    "files": sandbox_files,
                    "allowed_tools": ["file_read", "file_write", "file_list"],
                },
            },
            ensure_ascii=False,
        )
        messages = [
            {"role": ROLE_SYSTEM, "content": EXECUTOR_SYSTEM},
            {"role": ROLE_USER, "content": prompt},
        ]
        # Turn 1: model emits read / introspection actions.
        envelope, raw, in_tok, out_tok = self._call_with_repair(
            messages, expected_role="executor",
        )
        turn1_actions = self._normalise_actions(envelope)
        tool_results: List[ToolResult] = []
        for action in turn1_actions:
            if action["kind"] != "tool_call":
                continue
            tool_results.append(self._run_tool(action, workflow.id))

        read_results = [
            r for r in tool_results
            if r.tool in ("file_read", "file_list") and r.status == "ok"
        ]
        actions_record = list(turn1_actions)
        if read_results:
            # Turn 2: feed back the REAL host tool results and let the
            # model produce the final deliverable (e.g. file_write).
            feedback = json.dumps(
                {
                    "tool_results": self._tool_results_for_feedback(read_results),
                    "instruction": (
                        "These are the REAL results of the file tools "
                        "executed by the HOST. If the task requires "
                        "producing a file, now emit the file_write "
                        "tool_call with the real deliverable content. "
                        "Respond with the usual JSON envelope only."
                    ),
                },
                ensure_ascii=False,
            )
            turn2_messages = [
                {"role": ROLE_SYSTEM, "content": REPAIR_SYSTEM},
                {"role": ROLE_USER, "content": prompt},
                {"role": ROLE_ASSISTANT, "content": raw},
                {"role": ROLE_SYSTEM, "content": (
                    "You have just read the file content above. You MUST "
                    "now output a file_write tool_call with the real "
                    "deliverable. Do NOT use a respond action. Output "
                    "ONLY the JSON envelope with a file_write action."
                )},
            ]
            envelope, raw, in_tok, out_tok = self._call_with_repair(
                turn2_messages, expected_role="executor",
            )
            turn2_actions = self._normalise_actions(envelope)
            for action in turn2_actions:
                if action["kind"] != "tool_call":
                    continue
                tool_results.append(self._run_tool(action, workflow.id))
            actions_record = actions_record + [
                a for a in turn2_actions if a["kind"] == "tool_call"
            ]

        work_product = {
            "schema_version": envelope.get("schema_version"),
            "role": envelope.get("role"),
            "status": envelope.get("status"),
            "notes": envelope.get("notes", ""),
            "summary": envelope["data"].get("summary", ""),
            "actions": actions_record,
            "tool_results": [r.to_dict() for r in tool_results],
            "artefacts": self._collect_artefacts(tool_results),
            "provider": {
                "name": self.provider.name,
                "model": self.model,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            },
            "prompt_preloaded_file_content": False,
            "raw_executor_response": raw,
        }
        workflow.execution = work_product
        workflow.transition(
            WorkflowStage.EXECUTING, note="executor produced work product",
        )
        return work_product

    def _sandbox_metadata(self, workflow_id: str) -> List[Dict[str, Any]]:
        """Return path/name/size metadata for files already in the
        workflow sandbox. NEVER returns file content."""
        meta: List[Dict[str, Any]] = []
        try:
            for art in self.file_store.list(workflow_id)[:20]:
                meta.append({
                    "path": art.rel_path,
                    "name": art.rel_path.rsplit("/", 1)[-1],
                    "size_bytes": art.size_bytes,
                })
        except Exception:
            return meta
        return meta

    def _normalise_actions(self, envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
        actions = envelope["data"].get("actions", [])
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
        return normalised

    def _run_tool(self, action: Dict[str, Any], workflow_id: str) -> ToolResult:
        invocation = ToolInvocation(tool=action["tool"], args=action["args"])
        return self.tools.invoke(invocation, workflow_id, self.file_store)

    @staticmethod
    def _tool_results_for_feedback(
        results: List[ToolResult], max_chars: int = 6000,
    ) -> List[Dict[str, Any]]:
        out = []
        for r in results:
            body = (r.result or {}).get("content")
            if isinstance(body, str) and len(body) > max_chars:
                body = body[:max_chars] + "\n...[truncated by host]"
            out.append({
                "tool": r.tool,
                "status": r.status,
                "path": (r.result or {}).get("path"),
                "size_bytes": (r.result or {}).get("size_bytes"),
                "content": body,
                "error": r.error,
            })
        return out

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
        from .structured_contract import parse_role_response

        try:
            env = parse_role_response(text, expected_role)
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


