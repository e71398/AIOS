"""Demo Tool (read-only).

This is a *Demo* tool. It is intentionally simple and read-only. It
exists as a template for new Tool authors and as a working example
for the offline test suite.

DO NOT mistake this for a real Tool with side effects. It only
inspects `$AIOS_DATA_DIR` and returns small structured metadata.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ToolInput:
    path: str


@dataclass
class ToolOutput:
    status: str
    exists: bool
    size_bytes: int = 0
    error: Optional[str] = None


class DemoTool:
    """Demo Tool. Permission: read. No external calls."""

    name = "demo_tool"
    permission = "read"

    def invoke(self, request: ToolInput) -> ToolOutput:
        data_root = Path(os.environ.get("AIOS_DATA_DIR", os.getcwd()))
        target = (data_root / request.path).resolve()
        # Containment check.
        try:
            target.relative_to(data_root.resolve())
        except ValueError:
            return ToolOutput(status="error", exists=False, error="path_outside_data_dir")
        if not target.exists():
            return ToolOutput(status="ok", exists=False)
        if not target.is_file():
            return ToolOutput(status="error", exists=True, error="not_a_file")
        return ToolOutput(
            status="ok",
            exists=True,
            size_bytes=target.stat().st_size,
        )

    def health(self) -> dict:
        return {"status": "ok", "tool": self.name, "permission": self.permission}

    def shutdown(self) -> None:
        return None


if __name__ == "__main__":
    t = DemoTool()
    print(t.invoke(ToolInput(path="CHANGELOG.md")))