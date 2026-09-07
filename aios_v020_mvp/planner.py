"""Planner role for the v0.2.0 MVP.

The Planner is an independent role: it does not see tool results and
does not modify artefacts. Its only job is to turn the user task into
a JSON plan with steps the Executor can interpret.

The Planner emits the AIOS v0.2.0 structured-output envelope (see
:mod:`aios_v020_mvp.structured_contract`). The ``data`` field looks
like::

    {
      "goal": "one-sentence goal statement",
      "steps": [
        {
          "id": "s1",
          "action": "think" | "file_read" | "file_write" | "file_list" | "respond",
          "tool":   "file_read" | "file_write" | "file_list" | null,
          "arguments": { ... }
        }
      ],
      "expected_output": "...",
      "acceptance_criteria": ["criterion 1", ...]
    }

If the provider returns invalid JSON the Planner applies a single
repair pass before giving up.
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
from .workflow import Workflow, WorkflowStage


PLANNER_SYSTEM = (
    "You are the AIOS Planner. Convert the user's task into a strict JSON "
    "plan. Output EXACTLY one JSON object, no prose, no markdown fences.\n\n"
    "Top-level keys (order does not matter):\n"
    "  schema_version : must be the string \"aios-v020-1.0\"\n"
    "  role           : must be the string \"planner\"\n"
    "  status         : \"ok\" (or \"error\" only if the task itself is broken)\n"
    "  data           : an object with the plan fields below\n"
    "  notes          : optional short note (never parsed)\n\n"
    "Inside data use EXACTLY these keys:\n"
    "  goal                  : one sentence describing the overall goal\n"
    "  steps                 : array of step objects (see below)\n"
    "  expected_output       : one sentence describing what should be produced\n"
    "  acceptance_criteria   : array of 1-3 short criterion strings\n\n"
    "Each step object has these keys:\n"
    "  id          : string like \"s1\", \"s2\", ...\n"
    "  action      : one of \"think\", \"file_read\", \"file_write\", "
                  "\"file_list\", \"respond\"\n"
    "  tool        : for tool actions the tool name; null for think/respond\n"
    "  arguments   : for tool actions the arguments object; null otherwise\n\n"
    "Allowed tool arguments:\n"
    "  file_read  : {\"path\": \"...\"}\n"
    "  file_write : {\"path\": \"...\", \"content\": \"...\"}\n"
    "  file_list  : {\"path\": \"\" or a subdirectory prefix}\n"
    "All paths must be relative (the executor sandboxes them).\n\n"
    "Respond with the JSON only. No prose. No fences."
)


_ALLOWED_ACTIONS = {"think", "file_read", "file_write", "file_list", "respond"}
_TOOL_FOR_ACTION = {
    "file_read": "file_read",
    "file_write": "file_write",
    "file_list": "file_list",
}


@dataclass
class Planner:
    provider: Provider
    model: str
    max_steps: int = 6

    def build_plan(self, workflow: Workflow) -> Dict[str, Any]:
        prompt = json.dumps(
            {"task": workflow.task, "max_steps": self.max_steps},
            ensure_ascii=False,
        )
        envelope, raw = self._call_with_repair(
            [
                {"role": ROLE_SYSTEM, "content": PLANNER_SYSTEM},
                {"role": ROLE_USER, "content": prompt},
            ],
            expected_role="planner",
        )
        plan = self._coerce_plan(envelope)
        workflow.plan = plan
        workflow.plan_raw = raw
        workflow.transition(WorkflowStage.PLANNED, note="planner emitted plan")
        return plan

    def _call_with_repair(
        self, messages: List[Dict[str, str]], expected_role: str,
    ):
        response = self.provider.chat(
            ProviderRequest(
                messages=messages, model=self.model,
                max_tokens=1400, temperature=0.1,
                metadata={"role": expected_role},
            )
        )
        envelope, err = self._try_envelope(response.text, expected_role)
        if envelope is not None:
            return envelope, response.text
        repair_messages = [
            {"role": ROLE_SYSTEM, "content": REPAIR_SYSTEM},
            *messages,
            {"role": ROLE_ASSISTANT, "content": response.text},
            repair_prompt(expected_role, response.text, str(err)),
        ]
        repair_response = self.provider.chat(
            ProviderRequest(
                messages=repair_messages, model=self.model,
                max_tokens=1400, temperature=0.0,
                metadata={"role": expected_role, "phase": "repair"},
            )
        )
        envelope, err2 = self._try_envelope(repair_response.text, expected_role)
        if envelope is None:
            raise ProviderError(
                f"planner contract violated after repair: {err2}; "
                f"raw={repair_response.text[:200]!r}"
            )
        return envelope, repair_response.text

    @staticmethod
    def _try_envelope(text: str, expected_role: str):
        obj = extract_first_json(text)
        if obj is None:
            return None, ContractError(
                "no JSON object in response", reason="no_json_object",
            )
        try:
            return validate_envelope(obj, expected_role), None
        except ContractError as exc:
            return None, exc

    @staticmethod
    def _coerce_plan(envelope: Dict[str, Any]) -> Dict[str, Any]:
        """Normalise the envelope's ``data`` into the AIOS plan shape.

        Tolerates real-LLM quirks: missing schema_version, mixed
        action/kind naming, tool_call envelope, missing acceptance
        list. Always returns a usable plan if ANY tool action can be
        extracted; if the LLM genuinely emitted nothing usable we
        raise ProviderError so the orchestrator can fail the
        workflow with a clear reason.
        """
        data = envelope.get("data") or {}
        if not isinstance(data, dict):
            raise ProviderError("planner 'data' must be a JSON object")
        goal = str(
            data.get("goal") or data.get("summary") or ""
        ).strip() or "Execute the user task with file tools."
        # Step list may live under several keys.
        steps_raw = data.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            steps_raw = data.get("actions")
        if not isinstance(steps_raw, list) or not steps_raw:
            steps_raw = data.get("plan")
        if not isinstance(steps_raw, list) or not steps_raw:
            # No step list under any accepted key. This is a hard
            # contract failure: synthesising a fake step here would
            # let the OfflineTestProvider (or any empty response)
            # masquerade as a real plan, which the 029 boundary
            # rules explicitly forbid.
            raise ProviderError(
                "planner output contained no step list "
                "(looked for data.steps / data.actions / data.plan)"
            )
        steps: List[Dict[str, Any]] = []
        for i, raw in enumerate(steps_raw[:6]):
            if not isinstance(raw, dict):
                continue
            action = str(
                raw.get("action") or raw.get("kind") or ""
            ).strip().lower()
            if not action:
                continue
            if action not in _ALLOWED_ACTIONS:
                if action == "tool_call":
                    tool_name = str(raw.get("tool") or "").strip()
                    action = (
                        tool_name
                        if tool_name in _TOOL_FOR_ACTION
                        else "respond"
                    )
                else:
                    # Unknown action — skip silently rather than
                    # failing the whole plan.
                    continue
            tool = raw.get("tool")
            args = raw.get("arguments")
            if args is None:
                args = raw.get("args")
            if not isinstance(args, dict):
                args = {}
            steps.append({
                "id": str(raw.get("id") or f"s{i+1}"),
                "action": action,
                "tool": (
                    tool
                    if tool in ("file_read", "file_write", "file_list")
                    else _TOOL_FOR_ACTION.get(action)
                ),
                "arguments": args,
                "description": str(raw.get("description") or ""),
            })
        if not steps:
            # Every candidate step was unusable (unknown action names,
            # non-dict entries, ...). Fail loudly rather than passing
            # an empty plan to the Executor.
            raise ProviderError("planner produced no usable steps")
        expected_output = str(
            data.get("expected_output") or data.get("expected") or goal
        ).strip() or "The work product addresses the user task."
        acceptance = data.get("acceptance_criteria")
        if not isinstance(acceptance, list) or not acceptance:
            acceptance = data.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            acceptance = ["The work product addresses the user task."]
        return {
            "schema_version": envelope.get("schema_version"),
            "role": envelope.get("role"),
            "status": envelope.get("status"),
            "notes": envelope.get("notes", ""),
            "goal": goal,
            "steps": steps,
            "expected_output": expected_output,
            "acceptance_criteria": [
                str(c).strip() for c in acceptance if str(c).strip()
            ],
        }


