"""Tool registry for the v0.2.0 MVP.

The MVP keeps tool wiring minimal: each tool is a function
taking a dict of arguments and returning a JSON-serialisable
result. The registry enforces a per-workflow sandbox through
the :class:`~aios_v020_mvp.persistence.FileResultStore`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# Environment-driven tool-execution sink. Set AIOS_TOOL_SINK to a
# writable path to append one JSON line per REAL host-side tool
# execution (file_read / file_write / file_list). This gives the
# closeout harness an auditable record that tools were actually
# executed by the host and lets the E2E gate count per-tool calls.
_TOOL_SINK_PATH = os.environ.get("AIOS_TOOL_SINK", "").strip()
_TOOL_SINK_LOCK = threading.Lock()


def _env_tool_sink(record: Dict[str, Any]) -> None:
    if not _TOOL_SINK_PATH:
        return
    try:
        line = json.dumps(record, ensure_ascii=False)
    except Exception:
        return
    with _TOOL_SINK_LOCK:
        try:
            with open(_TOOL_SINK_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_./-]")



class ToolError(RuntimeError):
    """Raised when a tool rejects an invocation."""


@dataclass
class ToolInvocation:
    """Single tool call from the executor."""

    tool: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """Result of a tool invocation."""

    tool: str
    status: str  # "ok" | "error"
    result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class ToolDefinition:
    """Description of a registered tool."""

    name: str
    description: str
    permission: str  # "read" | "write"
    handler: Callable[["ToolInvocation", str, Any], ToolResult]


class ToolRegistry:
    """A tiny in-process tool registry with sandbox enforcement."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ToolError(f"tool already registered: {definition.name}")
        self._tools[definition.name] = definition

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> List[str]:
        return sorted(self._tools)

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "permission": t.permission,
            }
            for t in self._tools.values()
        ]

    def invoke(
        self,
        invocation: ToolInvocation,
        workflow_id: str,
        context: Any,
    ) -> ToolResult:
        definition = self._tools.get(invocation.tool)
        if definition is None:
            return ToolResult(
                tool=invocation.tool,
                status="error",
                error=f"unknown tool: {invocation.tool}",
            )
        try:
            result = definition.handler(invocation, workflow_id, context)
        except ToolError as exc:
            result = ToolResult(tool=invocation.tool, status="error", error=str(exc))
        except Exception as exc:  # last-resort guard so a tool bug doesn't kill the workflow
            result = ToolResult(
                tool=invocation.tool,
                status="error",
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
        _env_tool_sink({
            "ts": _time.time(),
            "tool": invocation.tool,
            "workflow_id": workflow_id,
            "status": result.status,
            "path": (result.result or {}).get("path"),
            "size_bytes": (result.result or {}).get("size_bytes"),
            "error": result.error,
            "host_executed": True,
        })
        return result

    def invoke_many(
        self,
        invocations: List[ToolInvocation],
        workflow_id: str,
        context: Any,
    ) -> List[ToolResult]:
        out: List[ToolResult] = []
        for inv in invocations:
            out.append(self.invoke(inv, workflow_id, context))
        return out


def safe_relpath(raw: str) -> str:
    """Normalise and validate a relative path used inside the sandbox."""
    if not isinstance(raw, str) or not raw:
        raise ToolError("path must be a non-empty string")
    cleaned = raw.replace("\\", "/").lstrip("/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ToolError("path may not contain '..' segments")
    if not parts:
        raise ToolError("path resolves to empty")
    joined = "/".join(parts)
    if not _SAFE_NAME.match(joined) and not all(
        re.match(r"^[A-Za-z0-9_.-]+$", p) for p in parts
    ):
        raise ToolError(f"unsafe path: {raw!r}")
    return joined


def safe_serialise(value: Any) -> Any:
    """JSON-safe coercion for tool results."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
