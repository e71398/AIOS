"""Offline test for the Demo Tool. No external calls."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from tool import DemoTool, ToolInput


def test_demo_tool_inside_data_dir():
    t = DemoTool()
    # Point at the candidate root so the test sees real files.
    os.environ["AIOS_DATA_DIR"] = HERE
    out = t.invoke(ToolInput(path="tool.py"))
    assert out.status == "ok"
    assert out.exists is True
    assert out.size_bytes > 0


def test_demo_tool_outside_data_dir_rejected():
    t = DemoTool()
    os.environ["AIOS_DATA_DIR"] = HERE
    out = t.invoke(ToolInput(path="/etc/passwd"))
    assert out.status == "error"
    assert "outside" in (out.error or "")


def test_demo_tool_missing_file():
    t = DemoTool()
    os.environ["AIOS_DATA_DIR"] = HERE
    out = t.invoke(ToolInput(path="does-not-exist.txt"))
    assert out.status == "ok"
    assert out.exists is False


def test_demo_tool_health():
    t = DemoTool()
    h = t.health()
    assert h["status"] == "ok"
    assert h["tool"] == "demo_tool"
    assert h["permission"] == "read"


if __name__ == "__main__":
    test_demo_tool_inside_data_dir()
    test_demo_tool_outside_data_dir_rejected()
    test_demo_tool_missing_file()
    test_demo_tool_health()
    print("demo_tool tests: 4 passed")