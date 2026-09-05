# Tool Security

AIOS Tools are the only code path that lets an Executor affect the
world outside of the AIOS process. This document describes the
permissions model, the sandbox write policy and what is **not** yet
implemented.

## Permissions

Each Tool is registered with a permission class:

| Permission | Allows |
| --- | --- |
| `read` | read files inside `$AIOS_DATA_DIR` and `$AIOS_HOME` |
| `write` | write files inside `$AIOS_DATA_DIR/sandbox/<task_id>/` |
| `network` | make outbound HTTP to allow-listed hosts |
| `subprocess` | spawn a child process on the allow-list |
| `elevated` | none — requires explicit opt-in by operator |

The default for every Tool is **`deny`** (no permission). A Tool may
opt into a permission only when the use case demands it. There is no
implicit upgrade path.

## Sandbox write policy

Executor-initiated writes go to `$AIOS_DATA_DIR/sandbox/<task_id>/`.
The directory is created per task and removed after the task is finalized
(unless the task explicitly requests retention).

Sandbox writes are **not** yet a first-class Executor tool in this
Alpha; they currently go through restricted ToolBus paths. The contract
in this document is the target.

## What is **not** yet implemented

- A formal read-only AIOS state tool for Executor (planned).
- A formal sandboxed write tool for Executor (planned).
- An enforced subprocess allow-list (currently operator policy).

These are tracked under the `tools` and `security` labels on the
public issue tracker.

## Reporting a vulnerability

See `SECURITY.md`.