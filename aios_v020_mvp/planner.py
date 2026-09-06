"""Planner role for the v0.2.0 MVP.

The Planner is an independent role: it does not see tool
results and does not modify artefacts. Its only job is to
turn the user task into a JSON plan with steps the Executor
can interpret.

The plan contract (the JSON the Planner emits) is:

    {
      "plan_id": "...",
      "summary": "...",
      "steps": [
        {
          "id": "s1_...",
          "kind": "think" | "file_read" | "file_write" | "respond",
          "description": "...",
          "tool": "file_read" | "file_write" | ...,   # only for tool steps
          "args": { ... }                              # only for tool steps
        },
        ...
      ],
      "acceptance": [ "criterion 1", "criterion 2", ... ]
    }

The Planner always runs the request through the configured
provider; if the provider returns invalid JSON the planner
applies a single repair pass (find a JSON object in the text)
before giving up.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .providers import (
    Provider,
    ProviderError,
    ProviderRequest,
    ROLE_SYSTEM,
    ROLE_USER,
)
from .workflow import Workflow, WorkflowStage


PLANNER_SYSTEM = (
    "You are an AIOS Planner. Convert the user's task into a strict JSON plan.\n"
    "Output a single JSON object with keys: plan_id, summary, steps, acceptance.\n"
    "Each step has: id, kind, description, and optional tool/args for tool steps.\n"
    "Allowed kinds: think, file_read, file_write, respond.\n"
    "Do not include any text outside the JSON object."
)


@dataclass
class Planner:
    provider: Provider
    model: str
    max_steps: int = 5

    def build_plan(self, workflow: Workflow) -> Dict[str, Any]:
        prompt = json.dumps(
            {"task": workflow.task, "max_steps": self.max_steps},
            ensure_ascii=False,
        )
        response = self.provider.chat(
            ProviderRequest(
                messages=[
                    {"role": ROLE_SYSTEM, "content": PLANNER_SYSTEM},
                    {"role": ROLE_USER, "content": prompt},
                ],
                model=self.model,
                max_tokens=1200,
                temperature=0.1,
            )
        )
        plan = self._parse_plan(response.text)
        workflow.plan = plan
        workflow.plan_raw = response.text
        workflow.transition(WorkflowStage.PLANNED, note="planner emitted plan")
        return plan

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_plan(self, text: str) -> Dict[str, Any]:
        parsed = self._extract_json(text)
        if not isinstance(parsed, dict):
            raise ProviderError("planner output was not a JSON object")
        if "steps" not in parsed or not isinstance(parsed["steps"], list):
            raise ProviderError("planner output missing 'steps' array")
        parsed["steps"] = parsed["steps"][: self.max_steps]
        if "summary" not in parsed or not parsed["summary"]:
            parsed["summary"] = "Auto-generated plan."
        if "acceptance" not in parsed or not isinstance(parsed["acceptance"], list):
            parsed["acceptance"] = ["The work product addresses the user task."]
        return parsed

    @staticmethod
    def _extract_json(text: str) -> Optional[Any]:
        text = (text or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Find the first balanced JSON object in the response.
        decoder = json.JSONDecoder()
        for match in re.finditer(r"[\[{]", text):
            try:
                obj, _ = decoder.raw_decode(text[match.start():])
                return obj
            except json.JSONDecodeError:
                continue
        return None
