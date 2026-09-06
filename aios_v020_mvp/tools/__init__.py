"""Tool subpackage for the v0.2.0 MVP.

Provides a tiny registry of file tools (read/write/list) that
the Executor can invoke. Tools are sandboxed to a per-workflow
directory under the result store so the reviewer can verify
no escape outside the agreed boundary.
"""

from .registry import ToolRegistry, ToolDefinition, ToolError, ToolInvocation, ToolResult
from .file_tools import register_default_file_tools

__all__ = [
    "ToolRegistry",
    "ToolDefinition",
    "ToolError",
    "ToolInvocation",
    "ToolResult",
    "register_default_file_tools",
]
