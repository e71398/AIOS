"""Offline test for the minimal workflow. No external calls."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from workflow import run


def test_run_returns_expected_shape():
    result = run("test-1")
    assert isinstance(result, dict)
    assert result["task_id"] == "test-1"
    assert result["provider"]["status"] == "ok"
    assert result["provider"]["text"]
    assert result["tool"]["status"] == "ok"
    assert result["tool"]["exists"] is True


if __name__ == "__main__":
    test_run_returns_expected_shape()
    print("workflow test: 1 passed")