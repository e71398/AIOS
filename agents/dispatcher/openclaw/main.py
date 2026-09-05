#!/usr/bin/env python3
"""Legacy OpenClaw channel adapter.

AIOS 5.2.1 keeps this import surface for compatibility only. Planning,
dispatch, repair, verification and aggregation are owned by aios-orchestrator.
"""
import json
import sys
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_dispatcher import (
    aggregate_results as _aggregate_results,
    decompose_task,
    detect_logic_depth,
    detect_modality,
    detect_priority,
    dispatch as _dispatch,
    get_sender_id,
    show_status,
)


def dispatch(user_input: str, source: str = "feishu", sender_id: str = "local"):
    result = _dispatch(user_input, source=source, sender_id=sender_id)
    if not isinstance(result, dict):
        return []
    task_ids = result.get("task_ids")
    if isinstance(task_ids, list):
        return task_ids
    parent_id = str(result.get("parent_id", "") or "")
    return [parent_id] if parent_id else []


def aggregate_results(task_ids, timeout_seconds: int = 60):
    return _aggregate_results(task_ids, timeout_seconds=timeout_seconds)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "--status":
        show_status()
    else:
        print(json.dumps(dispatch(" ".join(sys.argv[1:])), ensure_ascii=False))
