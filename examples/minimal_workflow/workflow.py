"""Minimal Workflow (Demo Provider + Demo Tool).

This workflow demonstrates the role boundaries of AIOS in a few
lines of code, using only Demo components.

DO NOT mistake this for a real workflow. It does not contact any
external service and does not modify the system.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "demo_provider"))
sys.path.insert(0, str(HERE.parent / "demo_tool"))

from provider import AdapterRequest, DemoProvider  # noqa: E402
from tool import DemoTool, ToolInput  # noqa: E402


def run(ai_task_id: str = "demo-task") -> dict:
    provider = DemoProvider()
    provider.enabled = True
    tool = DemoTool()
    os.environ["AIOS_DATA_DIR"] = str(HERE)
    pr = provider.invoke(AdapterRequest(prompt=f"task={ai_task_id}"))
    tr = tool.invoke(ToolInput(path="README.md"))
    return {
        "task_id": ai_task_id,
        "provider": {
            "status": pr.status,
            "text": pr.text,
            "input_tokens": pr.input_tokens,
            "output_tokens": pr.output_tokens,
        },
        "tool": {
            "status": tr.status,
            "exists": tr.exists,
            "size_bytes": tr.size_bytes,
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))