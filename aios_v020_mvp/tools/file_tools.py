"""Built-in file tools: read, write, list.

The sandbox is the per-workflow directory inside the result
store. ``context`` for these tools is a
:class:`~aios_v020_mvp.persistence.FileResultStore` instance.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from .registry import (
    ToolDefinition,
    ToolError,
    ToolInvocation,
    ToolResult,
    safe_relpath,
    safe_serialise,
)


def _store(context: Any):
    if context is None:
        raise ToolError("file tools require a FileResultStore context")
    return context


def _file_read(invocation: ToolInvocation, workflow_id: str, context: Any) -> ToolResult:
    store = _store(context)
    rel = safe_relpath(invocation.args.get("path", ""))
    if not store.exists(workflow_id, rel):
        return ToolResult(
            tool="file_read",
            status="error",
            error=f"file does not exist: {rel}",
        )
    try:
        content = store.read(workflow_id, rel)
    except FileNotFoundError as exc:
        return ToolResult(tool="file_read", status="error", error=str(exc))
    return ToolResult(
        tool="file_read",
        status="ok",
        result={
            "path": rel,
            "size_bytes": len(content.encode("utf-8")),
            "content": content,
        },
    )


def _file_write(invocation: ToolInvocation, workflow_id: str, context: Any) -> ToolResult:
    store = _store(context)
    rel = safe_relpath(invocation.args.get("path", ""))
    content = invocation.args.get("content", "")
    if not isinstance(content, str):
        raise ToolError("file_write: content must be a string")
    if len(content) > 1_000_000:
        raise ToolError("file_write: content exceeds 1MB cap")
    artefact = store.write(workflow_id, rel, content)
    return ToolResult(
        tool="file_write",
        status="ok",
        result={
            "path": artefact.rel_path,
            "size_bytes": artefact.size_bytes,
            "abs_path": str(artefact.abs_path),
        },
    )


def _file_list(invocation: ToolInvocation, workflow_id: str, context: Any) -> ToolResult:
    store = _store(context)
    rel = invocation.args.get("path", "")
    if rel:
        rel = safe_relpath(rel)
    artefacts = store.list(workflow_id)
    if rel:
        artefacts = [a for a in artefacts if a.rel_path.startswith(rel.rstrip("/") + "/") or a.rel_path == rel]
    return ToolResult(
        tool="file_list",
        status="ok",
        result={
            "path": rel or "",
            "files": safe_serialise([a.to_dict() for a in artefacts]),
        },
    )


def register_default_file_tools(registry) -> None:
    """Register the default file tools on ``registry``."""
    registry.register(
        ToolDefinition(
            name="file_read",
            description="Read a file from the workflow sandbox and return its content.",
            permission="read",
            handler=_file_read,
        )
    )
    registry.register(
        ToolDefinition(
            name="file_write",
            description="Write a file to the workflow sandbox (overwrites if present).",
            permission="write",
            handler=_file_write,
        )
    )
    registry.register(
        ToolDefinition(
            name="file_list",
            description="List files in the workflow sandbox (optionally under a prefix).",
            permission="read",
            handler=_file_list,
        )
    )
