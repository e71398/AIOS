"""Reviewer role for the v0.2.0 MVP.

The Reviewer is intentionally strict: it runs an independent
provider call against the work product, parses the verifier
JSON, and applies an explicit evidence gate:

    * Provider returned a valid verdict JSON
    * Verdict is ``accept``
    * Every check has an ``evidence`` field
    * The score is at least ``min_score``

If any gate fails the workflow is failed with a clear reason
so the caller knows what to fix.  The Reviewer never silently
"accepts" a broken work product.
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


REVIEWER_SYSTEM = (
    "You are an AIOS Reviewer. Evaluate the work product against the "
    "acceptance criteria and output strict JSON.\n"
    "Required keys: verdict (\"accept\"|\"reject\"), score (0..1), checks (array), notes.\n"
    "Each check has: criterion, passed (bool), evidence.\n"
    "Do not include any text outside the JSON object."
)


@dataclass
class Reviewer:
    provider: Provider
    model: str
    min_score: float = 0.6
    require_evidence: bool = True

    def review(self, workflow: Workflow) -> Dict[str, Any]:
        if workflow.execution is None:
            raise ValueError("workflow has no execution; run Executor first")
        prompt = json.dumps(
            {
                "task": workflow.task,
                "plan": workflow.plan,
                "result": {
                    "summary": workflow.execution.get("summary", ""),
                    "actions": workflow.execution.get("actions", []),
                    "tool_results": workflow.execution.get("tool_results", []),
                    "artefacts": workflow.execution.get("artefacts", []),
                },
                "acceptance": (workflow.plan or {}).get("acceptance", []),
            },
            ensure_ascii=False,
        )
        response = self.provider.chat(
            ProviderRequest(
                messages=[
                    {"role": ROLE_SYSTEM, "content": REVIEWER_SYSTEM},
                    {"role": ROLE_USER, "content": prompt},
                ],
                model=self.model,
                max_tokens=900,
                temperature=0.0,
            )
        )
        verdict = self._parse_verdict(response.text)
        evidence_ok = self._validate_evidence(verdict)
        accepted = (
            verdict.get("verdict") == "accept"
            and float(verdict.get("score", 0.0)) >= self.min_score
            and (evidence_ok or not self.require_evidence)
        )
        verdict["accepted"] = bool(accepted)
        if not accepted:
            verdict["verdict"] = "reject"
            verdict.setdefault(
                "rejection_reason",
                self._derive_rejection_reason(verdict, evidence_ok),
            )
        workflow.review = verdict
        if accepted:
            workflow.transition(WorkflowStage.REVIEWED, note="reviewer accepted")
            workflow.transition(WorkflowStage.COMPLETED, note="workflow completed")
        else:
            reason = verdict.get("rejection_reason") or verdict.get("notes") or "rejected"
            workflow.transition(
                WorkflowStage.REVIEWED, note=f"reviewer rejected: {reason}"
            )
            workflow.transition(WorkflowStage.FAILED, note=f"rejected: {reason}")
        return verdict

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_verdict(self, text: str) -> Dict[str, Any]:
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
            raise ProviderError("reviewer output was not a JSON object")
        return obj

    def _validate_evidence(self, verdict: Dict[str, Any]) -> bool:
        checks = verdict.get("checks", [])
        if not isinstance(checks, list) or not checks:
            return False
        for check in checks:
            if not isinstance(check, dict):
                return False
            if "passed" not in check:
                return False
            if "evidence" not in check or not str(check["evidence"]).strip():
                return False
        return True

    def _derive_rejection_reason(self, verdict: Dict[str, Any], evidence_ok: bool) -> str:
        if not evidence_ok:
            return "missing or empty evidence in verifier checks"
        if float(verdict.get("score", 0.0)) < self.min_score:
            return f"score {verdict.get('score')} below threshold {self.min_score}"
        if verdict.get("verdict") != "accept":
            return f"verdict was {verdict.get('verdict')!r}, not 'accept'"
        return "reviewer did not accept"
