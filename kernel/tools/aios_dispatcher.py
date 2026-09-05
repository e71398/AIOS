#!/usr/bin/env python3
"""Legacy dispatcher compatibility adapter.

AIOS 5.2 has exactly one workflow owner: aios_orchestrator.  This module keeps
old imports and pins working but performs no planning, routing, retry, safety
or verification decisions of its own.
"""
import json
import sys
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import get_queue_status, register_pin
from aios_orchestrator import submit, wait_workflow


def dispatch(text: str, source: str = "cli", sender_id: str = "legacy",
             session_key: str = "", preferred_executor: str = "",
             user_priority=None, user_logic_depth: str = "",
             verification_criteria: list = None, **_ignored):
    """Forward an old dispatcher call to the canonical Orchestrator."""
    return submit(
        text, source=source, sender_id=sender_id, session_key=session_key,
        preferred_executor=preferred_executor, user_priority=user_priority,
        user_logic_depth=user_logic_depth,
        verification_criteria=verification_criteria or [],
    )


def aggregate_results(task_ids, timeout_seconds: int = 60, parent_id: str = ""):
    """Forward legacy aggregation to the parent workflow result."""
    parent = str(parent_id or (task_ids[0] if task_ids else ""))
    if not parent:
        return {"status": "failed", "error": "missing_parent_id"}
    return wait_workflow(parent, timeout_seconds=timeout_seconds)


def decompose_task(text: str):
    """Compatibility only: preserve one user goal; Orchestrator plans it."""
    return [str(text)] if str(text).strip() else []


def detect_priority(_text: str) -> int:
    return 3


def detect_logic_depth(_text: str) -> str:
    return "low"


def detect_modality(_text: str) -> str:
    return "text"


def get_sender_id(sender_id: str = "legacy", **_kwargs) -> str:
    return str(sender_id or "legacy")


def show_status():
    status = get_queue_status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return status


register_pin("openclaw.dispatch", dispatch,
             "Compatibility pin forwarded to orchestrator.submit")
register_pin("openclaw.aggregate", aggregate_results,
             "Compatibility pin forwarded to orchestrator.wait")


if __name__ == "__main__":
    if "--status" in sys.argv:
        show_status()
    elif len(sys.argv) > 1:
        print(json.dumps(dispatch(" ".join(sys.argv[1:])), ensure_ascii=False, indent=2))
    else:
        print("AIOS dispatcher compatibility adapter -> aios-orchestrator")
