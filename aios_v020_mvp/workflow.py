"""Workflow state machine for the v0.2.0 MVP.

A workflow moves through the following stages:

    SUBMITTED -> PLANNED -> EXECUTING -> REVIEWED -> COMPLETED
                              |
                              +-> FAILED (terminal on review reject
                                           or unrecoverable executor
                                           error)

Each transition is recorded in the workflow document so the
GET /task/<id> endpoint can replay the full history and the
reviewer has a tamper-evident audit trail.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class WorkflowStage(str, Enum):
    """Discrete workflow stages."""

    SUBMITTED = "submitted"
    PLANNED = "planned"
    EXECUTING = "executing"
    REVIEWED = "reviewed"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class WorkflowEvent:
    """One transition in a workflow's history."""

    stage: str
    at: float
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"stage": self.stage, "at": self.at, "note": self.note}


# Allowed transitions. The orchestrator only ever moves forward
# along this graph; any other transition is a programmer error.
TRANSITIONS = {
    WorkflowStage.SUBMITTED: {WorkflowStage.PLANNED, WorkflowStage.FAILED},
    WorkflowStage.PLANNED: {WorkflowStage.EXECUTING, WorkflowStage.FAILED},
    WorkflowStage.EXECUTING: {WorkflowStage.REVIEWED, WorkflowStage.FAILED},
    WorkflowStage.REVIEWED: {WorkflowStage.COMPLETED, WorkflowStage.FAILED},
    WorkflowStage.COMPLETED: set(),
    WorkflowStage.FAILED: set(),
}


@dataclass
class Workflow:
    """Single task workflow document."""

    id: str
    task: str
    source: str = "mvp"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    stage: str = WorkflowStage.SUBMITTED.value
    plan: Optional[Dict[str, Any]] = None
    plan_raw: Optional[str] = None
    execution: Optional[Dict[str, Any]] = None
    review: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def transition(self, next_stage: WorkflowStage, note: Optional[str] = None) -> None:
        current = WorkflowStage(self.stage)
        allowed = TRANSITIONS[current]
        if next_stage not in allowed:
            raise ValueError(
                f"illegal transition: {current.value} -> {next_stage.value}"
            )
        now = time.time()
        self.stage = next_stage.value
        self.updated_at = now
        self.events.append(WorkflowEvent(stage=next_stage.value, at=now, note=note).to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "source": self.source,
            "stage": self.stage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "plan": self.plan,
            "plan_raw": self.plan_raw,
            "execution": self.execution,
            "review": self.review,
            "events": list(self.events),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workflow":
        wf = cls(
            id=str(data["id"]),
            task=str(data.get("task", "")),
            source=str(data.get("source", "mvp")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            stage=str(data.get("stage", WorkflowStage.SUBMITTED.value)),
            plan=data.get("plan"),
            plan_raw=data.get("plan_raw"),
            execution=data.get("execution"),
            review=data.get("review"),
            events=list(data.get("events", []) or []),
            error=data.get("error"),
        )
        return wf


def is_terminal(stage: str) -> bool:
    return stage in (WorkflowStage.COMPLETED.value, WorkflowStage.FAILED.value)


def is_success(stage: str) -> bool:
    return stage == WorkflowStage.COMPLETED.value
