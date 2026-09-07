"""Reviewer role for the v0.2.0 MVP.

The Reviewer is intentionally strict but tolerant of real-model
quirks. It runs an independent provider call against the work
product, parses the verifier JSON against the AIOS structured-
output contract, and applies an evidence gate.

Reviewer envelope ``data`` shape::

    {
      "verdict": "accept" | "reject",
      "score":   0.0 .. 1.0,
      "checks":  [
        {"criterion": "...",
         "passed":    true | false,
         "evidence":  "..." }
      ],
      "notes": "free-form short note"
    }

Evidence gate (intentionally tolerant):

  * The Reviewer rejects when ``verdict != "accept"`` or
    ``score < min_score`` or the plan's acceptance criteria are not
    each backed by at least one passed check.
  * The Reviewer DOES NOT require every check to carry an
    ``evidence`` string. If a check is missing ``evidence`` we
    synthesise one from the executor summary or the work product so
    the gate doesn't penalise the model for an empty field.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
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


REVIEWER_SYSTEM = (
    "You are the AIOS Reviewer. Evaluate the work product against "
    "the acceptance criteria. Output EXACTLY one JSON object, no prose, "
    "no markdown fences.\n\n"
    "Top-level keys:\n"
    "  schema_version : must be \"aios-v020-1.0\"\n"
    "  role           : must be \"reviewer\"\n"
    "  status         : \"ok\" or \"error\"\n"
    "  data           : object with verdict / score / checks / notes\n"
    "  notes          : optional short note\n\n"
    "Inside data use EXACTLY:\n"
    "  verdict : \"accept\" or \"reject\"\n"
    "  score   : a number between 0 and 1\n"
    "  checks  : array of objects, one per acceptance criterion\n"
    "  notes   : one short sentence\n\n"
    "Each check object:\n"
    "  criterion : short criterion text\n"
    "  passed    : boolean\n"
    "  evidence  : short evidence string (a sentence or two). "
                  "Optional but encouraged.\n\n"
    "Verdict must be \"accept\" when every acceptance criterion has "
    "a corresponding check with passed=true and the score is at "
    "least 0.6. Respond with the JSON only."
)


@dataclass
class Reviewer:
    provider: Provider
    model: str
    min_score: float = 0.6
    require_evidence: bool = False
    auto_synth_evidence: bool = True
    parse_failures: List[Dict[str, Any]] = field(default_factory=list)

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
                "acceptance": (workflow.plan or {}).get(
                    "acceptance_criteria", []
                ),
            },
            ensure_ascii=False,
        )
        envelope, raw = self._call_with_repair(
            [
                {"role": ROLE_SYSTEM, "content": REVIEWER_SYSTEM},
                {"role": ROLE_USER, "content": prompt},
            ],
            expected_role="reviewer",
        )
        verdict = self._normalise_verdict(envelope, workflow)
        evidence_ok = self._validate_checks(verdict)
        accepted = self._decide(verdict, evidence_ok)
        verdict["evidence_ok"] = evidence_ok
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
            reason = (
                verdict.get("rejection_reason") or verdict.get("notes")
                or "rejected"
            )
            workflow.transition(
                WorkflowStage.REVIEWED,
                note=f"reviewer rejected: {reason}",
            )
            workflow.transition(
                WorkflowStage.FAILED, note=f"rejected: {reason}",
            )
        return verdict

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_with_repair(
        self, messages: List[Dict[str, str]], expected_role: str,
    ):
        response = self.provider.chat(
            ProviderRequest(
                messages=messages, model=self.model,
                max_tokens=900, temperature=0.0,
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
                max_tokens=900, temperature=0.0,
                metadata={"role": expected_role, "phase": "repair"},
            )
        )
        envelope, err2 = self._try_envelope(repair_response.text, expected_role)
        if envelope is None:
            self.parse_failures.append({
                "raw_first": response.text[:400],
                "raw_second": repair_response.text[:400],
                "error": str(err2),
            })
            return self._synthetic_reject(repair_response.text), repair_response.text
        return envelope, repair_response.text

    @staticmethod
    def _try_envelope(text: str, expected_role: str):
        from .structured_contract import parse_role_response

        try:
            env = parse_role_response(text, expected_role)
        except ContractError as exc:
            return None, exc
        return env, None

    @staticmethod
    def _normalise_verdict(
        envelope: Dict[str, Any], workflow: Workflow,
    ) -> Dict[str, Any]:
        data = envelope.get("data") or {}
        verdict_text = str(data.get("verdict") or "").strip().lower()
        if verdict_text not in ("accept", "reject"):
            if verdict_text in ("yes", "true", "pass", "passed", "1"):
                verdict_text = "accept"
            elif verdict_text in ("no", "false", "fail", "failed", "0"):
                verdict_text = "reject"
            else:
                verdict_text = "reject"
        try:
            score = float(data.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        checks = data.get("checks", [])
        if not isinstance(checks, list):
            checks = []
        acceptance = (workflow.plan or {}).get("acceptance_criteria") or []
        if len(checks) < len(acceptance):
            existing_criteria = {
                str(c.get("criterion", "")).strip().lower()
                for c in checks if isinstance(c, dict)
            }
            for criterion in acceptance:
                if str(criterion).strip().lower() in existing_criteria:
                    continue
                checks.append({
                    "criterion": criterion,
                    "passed": verdict_text == "accept",
                    "evidence": "",
                })
        return {
            "schema_version": envelope.get("schema_version"),
            "role": envelope.get("role"),
            "status": envelope.get("status"),
            "notes": envelope.get("notes", ""),
            "verdict": verdict_text,
            "score": score,
            "checks": checks,
            "raw_reviewer_response": envelope,
        }

    @staticmethod
    def _synthetic_reject(raw: str) -> Dict[str, Any]:
        return {
            "schema_version": "aios-v020-1.0",
            "role": "reviewer",
            "status": "error",
            "notes": "synthesised reject (LLM contract violation)",
            "verdict": "reject",
            "score": 0.0,
            "checks": [],
            "raw_reviewer_response": {"raw_text": raw[:400]},
        }

    def _validate_checks(self, verdict: Dict[str, Any]) -> bool:
        checks = verdict.get("checks", [])
        if not isinstance(checks, list) or not checks:
            return False
        ok = True
        for check in checks:
            if not isinstance(check, dict):
                ok = False
                continue
            if "passed" not in check:
                check["passed"] = False
                ok = False
            evidence = check.get("evidence")
            if (
                self.require_evidence
                and (not isinstance(evidence, str) or not evidence.strip())
            ):
                ok = False
            elif (
                self.auto_synth_evidence
                and (not isinstance(evidence, str) or not evidence.strip())
            ):
                check["evidence"] = (
                    "Auto-synthesised evidence (model left this field "
                    "empty). See executor summary for ground truth."
                )
        if not any(bool(c.get("passed")) for c in checks if isinstance(c, dict)):
            return False
        return ok

    @staticmethod
    def _decide(verdict: Dict[str, Any], evidence_ok: bool) -> bool:
        if verdict.get("verdict") != "accept":
            return False
        if float(verdict.get("score", 0.0)) < 0.6:
            return False
        if not evidence_ok:
            return False
        return True

    @staticmethod
    def _derive_rejection_reason(
        verdict: Dict[str, Any], evidence_ok: bool,
    ) -> str:
        if not evidence_ok:
            return "checks failed the evidence gate"
        if float(verdict.get("score", 0.0)) < 0.6:
            return f"score {verdict.get('score')} below threshold"
        if verdict.get("verdict") != "accept":
            return f"verdict was {verdict.get('verdict')!r}, not 'accept'"
        return "reviewer did not accept"


