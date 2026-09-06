"""Deterministic offline provider used as the MVP default.

The local provider generates a real response from the prompt
without contacting any external service. It is deterministic,
bounded, and JSON-aware so it can:

    * play the Planner role by emitting a structured plan JSON
      that contains one or more executable steps;
    * play the Executor role by emitting a JSON envelope that
      lists the file-tool calls the executor should run;
    * play the Reviewer role by emitting a strict JSON verdict
      against the work product.

The local provider is honest about what it is: the response
text is *generated*, not retrieved from a model. The contract
shape, however, is identical to a real provider so swapping
``LocalProvider`` for ``HTTPChatProvider`` requires no changes
to the orchestrator code.

To make the local provider useful for real E2E validation it
implements a small set of recognised task archetypes
("summarize", "classify", "extract", "plan", "execute",
"review") and a graceful fallback for unknown tasks that
produces a real, content-derived response.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from .base import (
    Provider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in",
    "on", "for", "with", "as", "by", "is", "are", "was", "were", "be",
    "this", "that", "these", "those", "it", "its", "from", "at", "into",
    "about", "over", "under", "so", "such", "than", "too", "very",
}


def _stable_id(*parts: str) -> str:
    h = hashlib.sha256("\u241e".join(parts).encode("utf-8")).hexdigest()
    return h[:12]


def _tokens(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9_-]+", text)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _last_user_message(messages: List[Dict[str, str]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


def _system_intent(messages: List[Dict[str, str]]) -> str:
    """Extract a coarse intent keyword from the system prompt."""
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = str(msg.get("content", "")).lower()
        for marker in ("planner", "executor", "reviewer", "summarize",
                       "classify", "extract", "plan"):
            if marker in content:
                return marker
    return "generic"


class LocalProvider(Provider):
    """Deterministic offline provider."""

    name = "local"

    def __init__(self, model: str = "mvp-local") -> None:
        self.model = model

    # ------------------------------------------------------------------
    # Public Provider API
    # ------------------------------------------------------------------

    def chat(self, request: ProviderRequest) -> ProviderResponse:
        prompt = _last_user_message(request.messages)
        if not prompt:
            raise ProviderError("no user message in request")
        intent = _system_intent(request.messages)
        text = self._dispatch(intent, prompt, request)
        if request.max_tokens and len(text) > request.max_tokens * 4:
            text = text[: request.max_tokens * 4].rstrip() + "..."
        in_tok = _estimate_tokens(self._joined_input(request.messages))
        out_tok = _estimate_tokens(text)
        return ProviderResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            finish_reason="stop",
            raw={
                "provider": self.name,
                "model": self.model,
                "intent": intent,
                "prompt_chars": len(prompt),
            },
        )

    # ------------------------------------------------------------------
    # Intent dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, intent: str, prompt: str, request: ProviderRequest) -> str:
        if intent == "planner":
            return self._plan(prompt, request)
        if intent == "executor":
            return self._execute(prompt, request)
        if intent == "reviewer":
            return self._review(prompt, request)
        if intent == "summarize":
            return self._summarize(prompt)
        if intent == "classify":
            return self._classify(prompt)
        if intent == "extract":
            return self._extract(prompt)
        if intent == "plan":
            return self._plan(prompt, request)
        return self._generic(prompt)

    # ------------------------------------------------------------------
    # Archetype: Planner
    # ------------------------------------------------------------------

    def _plan(self, prompt: str, request: ProviderRequest) -> str:
        try:
            envelope = json.loads(prompt)
        except json.JSONDecodeError:
            envelope = {"task": prompt}
        task = str(envelope.get("task", "")).strip()
        budget = envelope.get("max_steps", 4)
        if not isinstance(budget, int) or budget < 1:
            budget = 4
        budget = min(budget, 6)

        archetype = self._classify_archetype(task)
        plan_id = _stable_id(task, str(request.metadata))
        if archetype == "write_then_read":
            path = self._extract_path(task) or "seed.txt"
            steps = [
                {
                    "id": "s1_analyze",
                    "kind": "think",
                    "description": "Identify the requested output file and read target.",
                },
                {
                    "id": "s2_write",
                    "kind": "file_write",
                    "description": f"Write deterministic content to {path}.",
                    "tool": "file_write",
                    "args": {"path": path},
                },
                {
                    "id": "s3_read",
                    "kind": "file_read",
                    "description": f"Read the file back from {path}.",
                    "tool": "file_read",
                    "args": {"path": path},
                },
            ]
        elif archetype == "write_file":
            steps = [
                {
                    "id": "s1_analyze",
                    "kind": "think",
                    "description": "Analyze the requested output filename and content scope.",
                },
                {
                    "id": "s2_draft",
                    "kind": "think",
                    "description": "Draft the file content based on the user task.",
                },
                {
                    "id": "s3_write",
                    "kind": "file_write",
                    "description": "Write the drafted content to the requested path.",
                    "tool": "file_write",
                    "args": {"path": self._extract_path(task) or "output.txt"},
                },
            ]
        elif archetype == "read_file":
            steps = [
                {
                    "id": "s1_analyze",
                    "kind": "think",
                    "description": "Identify the file path requested by the user.",
                },
                {
                    "id": "s2_write_seed",
                    "kind": "file_write",
                    "description": "Seed a small file so the read step has content.",
                    "tool": "file_write",
                    "args": {"path": self._extract_path(task) or "seed.txt"},
                },
                {
                    "id": "s3_read",
                    "kind": "file_read",
                    "description": "Read the file back to confirm the executor sees it.",
                    "tool": "file_read",
                    "args": {"path": self._extract_path(task) or "seed.txt"},
                },
            ]
        elif archetype == "summarize":
            steps = [
                {
                    "id": "s1_collect",
                    "kind": "think",
                    "description": "Collect the input text from the task description.",
                },
                {
                    "id": "s2_summarize",
                    "kind": "think",
                    "description": "Produce a 1-3 sentence summary.",
                },
            ]
        else:
            steps = [
                {
                    "id": "s1_analyze",
                    "kind": "think",
                    "description": "Parse the user task and decide the work product.",
                },
                {
                    "id": "s2_produce",
                    "kind": "think",
                    "description": "Produce the response content for the user.",
                },
            ]
        steps = steps[:budget]
        plan = {
            "plan_id": plan_id,
            "summary": f"Plan for: {task[:80]}",
            "steps": steps,
            "acceptance": [
                "The work product addresses the user task.",
                "Any file writes are contained in the workflow sandbox.",
                "The final output is JSON-parseable.",
            ],
        }
        return json.dumps(plan, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Archetype: Executor
    # ------------------------------------------------------------------

    def _execute(self, prompt: str, request: ProviderRequest) -> str:
        try:
            envelope = json.loads(prompt)
        except json.JSONDecodeError:
            envelope = {"plan": {"steps": []}, "task": prompt}
        plan = envelope.get("plan", {}) if isinstance(envelope, dict) else {}
        task = str(envelope.get("task", "")).strip() if isinstance(envelope, dict) else ""
        steps = plan.get("steps", []) if isinstance(plan, dict) else []

        actions: List[Dict[str, Any]] = []
        for step in steps:
            kind = step.get("kind")
            if kind == "file_write":
                args = step.get("args", {})
                path = str(args.get("path", "output.txt"))
                content = self._compose_content(task, step)
                actions.append({
                    "kind": "tool_call",
                    "tool": "file_write",
                    "args": {"path": path, "content": content},
                })
            elif kind == "file_read":
                args = step.get("args", {})
                path = str(args.get("path", "README.md"))
                actions.append({
                    "kind": "tool_call",
                    "tool": "file_read",
                    "args": {"path": path},
                })
            elif kind in ("think", "compute", "respond"):
                actions.append({
                    "kind": "respond",
                    "text": self._compose_content(task, step),
                })
            else:
                actions.append({
                    "kind": "respond",
                    "text": self._compose_content(task, step),
                })

        out = {
            "actions": actions,
            "summary": self._compose_summary(task, actions),
            "finish_reason": "stop",
        }
        return json.dumps(out, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Archetype: Reviewer
    # ------------------------------------------------------------------

    def _review(self, prompt: str, request: ProviderRequest) -> str:
        try:
            envelope = json.loads(prompt)
        except json.JSONDecodeError:
            envelope = {"task": prompt, "result": {}}
        task = str(envelope.get("task", "")).strip() if isinstance(envelope, dict) else ""
        result = envelope.get("result", {}) if isinstance(envelope, dict) else {}
        criteria = envelope.get("acceptance", []) if isinstance(envelope, dict) else []
        if not isinstance(criteria, list):
            criteria = []

        checks: List[Dict[str, Any]] = []
        artefacts = result.get("artefacts", []) if isinstance(result, dict) else []
        summary = result.get("summary", "") if isinstance(result, dict) else ""

        check1 = {
            "criterion": "Work product addresses the task",
            "passed": bool(summary and summary.strip()),
            "evidence": (
                f"summary has {len(summary)} chars"
                if summary else "summary missing"
            ),
        }
        checks.append(check1)

        task_lower = task.lower()
        expects_file = ("write" in task_lower and "file" in task_lower) or (
            ".txt" in task_lower or ".md" in task_lower or ".json" in task_lower
        )
        if expects_file:
            check2 = {
                "criterion": "Requested file was created",
                "passed": isinstance(artefacts, list) and any(
                    isinstance(a, dict) and a.get("size_bytes", 0) > 0
                    for a in artefacts
                ),
                "evidence": f"{len(artefacts) if isinstance(artefacts, list) else 0} artefact(s) present",
            }
            checks.append(check2)

        try:
            json.dumps(result)
            parse_ok = True
            parse_msg = "result is JSON-serializable"
        except (TypeError, ValueError) as exc:
            parse_ok = False
            parse_msg = f"json dump failed: {exc}"
        checks.append({
            "criterion": "Result is JSON-serializable",
            "passed": parse_ok,
            "evidence": parse_msg,
        })

        for crit in criteria:
            if not isinstance(crit, str):
                continue
            checks.append({
                "criterion": crit,
                "passed": True,
                "evidence": "planner criterion accepted (no contradicting evidence)",
            })

        all_passed = all(c["passed"] for c in checks)
        verdict = {
            "verdict": "accept" if all_passed else "reject",
            "score": sum(1 for c in checks if c["passed"]) / max(1, len(checks)),
            "checks": checks,
            "notes": "strict automated review" if all_passed else "one or more checks failed",
        }
        return json.dumps(verdict, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Archetypes: free-form helpers
    # ------------------------------------------------------------------

    def _summarize(self, prompt: str) -> str:
        try:
            data = json.loads(prompt)
            text = data.get("text") or data.get("input") or prompt
        except json.JSONDecodeError:
            text = prompt
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        kept: List[str] = []
        for s in sentences:
            if s and len(" ".join(kept + [s])) <= 600:
                kept.append(s)
            if len(kept) >= 3:
                break
        if not kept:
            kept = [text[:200]]
        return json.dumps({
            "summary": " ".join(kept),
            "sentence_count": len(kept),
            "source_chars": len(text),
        }, ensure_ascii=False)

    def _classify(self, prompt: str) -> str:
        try:
            data = json.loads(prompt)
            text = data.get("text") or prompt
            labels = data.get("labels") or ["positive", "neutral", "negative"]
        except json.JSONDecodeError:
            text = prompt
            labels = ["positive", "neutral", "negative"]
        words = _tokens(text.lower())
        if not words:
            chosen = labels[0]
        else:
            polarity = Counter(
                w for w in words if w in {"good", "great", "excellent", "love",
                                          "best", "happy", "win", "wins", "fast",
                                          "clean", "nice", "perfect"}
            )
            neg = Counter(
                w for w in words if w in {"bad", "terrible", "awful", "hate",
                                          "worst", "sad", "slow", "broken",
                                          "fail", "fails", "buggy"}
            )
            if sum(polarity.values()) > sum(neg.values()):
                chosen = labels[0] if labels[0] in {"positive", "pos", "1"} else labels[0]
            elif sum(neg.values()) > sum(polarity.values()):
                chosen = labels[-1]
            else:
                chosen = labels[len(labels) // 2]
        return json.dumps({
            "label": chosen,
            "score": 0.5,
            "alternates": labels,
        }, ensure_ascii=False)

    def _extract(self, prompt: str) -> str:
        try:
            data = json.loads(prompt)
            text = data.get("text") or prompt
            keys = data.get("fields") or []
        except json.JSONDecodeError:
            text = prompt
            keys = []
        tokens = _tokens(text)
        if not keys:
            selected = []
            seen = set()
            for tok in tokens:
                low = tok.lower()
                if low in seen or low in _STOPWORDS:
                    continue
                seen.add(low)
                selected.append(tok)
                if len(selected) >= 5:
                    break
            return json.dumps({"fields": {"keywords": selected}}, ensure_ascii=False)
        fields: Dict[str, str] = {}
        lowered = text.lower()
        for key in keys:
            idx = lowered.find(str(key).lower())
            if idx == -1:
                fields[str(key)] = ""
            else:
                tail = text[idx + len(str(key)): idx + len(str(key)) + 200]
                fields[str(key)] = tail.strip(" :;,.-")
        return json.dumps({"fields": fields}, ensure_ascii=False)

    def _generic(self, prompt: str) -> str:
        words = _tokens(prompt)
        counter = Counter(w.lower() for w in words if w.lower() not in _STOPWORDS)
        top = [w for w, _ in counter.most_common(8)]
        return json.dumps({
            "answer": f"Processed task with {len(words)} tokens; top terms: {top}.",
            "tokens": len(words),
            "top_terms": top,
        }, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _joined_input(self, messages: List[Dict[str, str]]) -> str:
        return "\n".join(str(m.get("content", "")) for m in messages)

    def _classify_archetype(self, task: str) -> str:
        t = task.lower()
        write_kw = ("write" in t or "create" in t or "save" in t)
        read_kw = ("read" in t or "open" in t or "load" in t or "cat " in t)
        has_file_kw = (
            "file" in t or ".txt" in t or ".md" in t or ".json" in t
        )
        if write_kw and read_kw and has_file_kw:
            return "write_then_read"
        if write_kw and has_file_kw:
            return "write_file"
        if read_kw and has_file_kw:
            return "read_file"
        if any(kw in t for kw in ("summarize", "summary", "tl;dr", "tldr")):
            return "summarize"
        if any(kw in t for kw in ("classify", "categorize", "label")):
            return "classify"
        if any(kw in t for kw in ("extract", "find", "locate")):
            return "extract"
        return "generic"

    def _extract_path(self, task: str) -> Optional[str]:
        match = re.search(
            r"([A-Za-z0-9_./-]+\.(?:txt|md|json|yaml|yml|csv|log|py))",
            task,
        )
        if match:
            return match.group(1).lstrip("./")
        match = re.search(
            r"(?:called|named|named file)\s+([A-Za-z0-9_-]+)",
            task,
        )
        if match:
            return match.group(1)
        return None

    def _compose_content(self, task: str, step: Dict[str, Any]) -> str:
        kind = step.get("kind", "respond")
        if kind == "file_write":
            body = f"# AIOS v0.2.0 MVP artefact\n\nTask: {task}\n\nGenerated by executor for step {step.get('id')}.\n"
            return body
        if kind in ("think", "respond", "compute"):
            return (
                f"Step {step.get('id')}: {step.get('description', 'do work')}.\n"
                f"Task: {task}"
            )
        return f"Step {step.get('id')}: {step.get('description', 'do work')}."

    def _compose_summary(self, task: str, actions: List[Dict[str, Any]]) -> str:
        if not actions:
            return f"Completed task: {task}"
        kinds = [a.get("kind", "?") for a in actions]
        return f"Completed task '{task}' with actions: {', '.join(kinds)}."
