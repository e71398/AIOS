# Architecture

This document describes the **actual** runtime architecture of AIOS
v0.1.0-alpha.1 based on the code in this repository. It is intentionally
honest about what works and what is blocked.

## High-level data flow

```
client
  │
  ▼
aios_entry_gateway           (HTTP, 127.0.0.1:18801 by default)
  │
  │  identity / session
  │
  ▼
Redis state bus              (aios:bus:*)
  │
  ▼
aios_orchestrator (daemon)   reads queue, drives workflow
  │
  ├── planner role
  ├── executor role          → aios_executor_daemon_<tool>
  ├── reviewer role
  ├── verifier role          → aios_verification_gate
  └── result-push role
                 │
                 ▼
            ToolBus
                 │
                 ├── tools (registry, default-deny)
                 │
                 ▼
         Provider Adapter   (off by default)
                 │
                 ▼
        Result store + history (filesystem + Redis hash)
```

A separate `aios_monitor.py` watches the bus and reports health.

## Components

### `aios_entry_gateway` (`kernel/tools/aios_entry_gateway.py`)

HTTP entry. Reads incoming JSON requests, applies identity / session
rules, and writes a task to the Redis queue. Default bind is
`127.0.0.1:18801`.

### `aios_orchestrator` (`kernel/tools/aios_orchestrator.py`)

Long-running daemon. Reads from `aios:bus:queue:*`, picks a role
(planner / executor / reviewer / verifier / result-push) and dispatches
work. **This is where the current core blocker lives:** the in-process
Provider Registry reports inconsistent state and the `minimax-official`
provider is marked `UNVERIFIED`, which short-circuits the normal
planner → executor → reviewer loop.

### `aios_executor_daemon_<tool>` (`kernel/tools/aios_executor_daemon.py`)

Per-tool executor. Spawned once per registered tool. Receives planner
output, calls the tool through the ToolBus, and writes a result.

### `aios_verification_gate` (`kernel/tools/aios_verification_gate.py`)

Independent reviewer. Re-runs a subset of the executor's checks against
the persisted evidence. Emits PASS / FAIL into the bus.

### `aios_result_push` (`kernel/tools/aios_result_push.py`)

Publishes final results back to the client-facing API and the
filesystem history.

### `aios_monitor` (`kernel/tools/aios_monitor.py`)

Health probe / metrics dashboard.

### Centers (`kernel/centers/`)

Long-running support components that operate on the bus asynchronously:

- `autonomy_initiative_center` — initiative scoring / curiosity engine.
- `evolution_center` — feature / prompt evolution with hermes backend.
- `intelligence_growth_center` — knowledge ingest / dedup / open-source
  radar.

Centers are useful reference implementations of "long-running
background process that owns part of the bus".

## Configuration

- `config/features.toml` — feature flags (Provider on/off, etc.).
- `config/module_manifest.json` — what components load at startup.
- `config/tool_adapters.json` — ToolBus adapter definitions.
- `.env` — local secrets and bind addresses (see `.env.example`).

## Failure modes

The system is designed so each component can fail and restart
independently. The current Alpha has at least the following known
inconsistencies — see `docs/CURRENT_LIMITATIONS.md`:

- Orchestrator Provider Registry inconsistent at startup.
- Planner / Reviewer JSON truncation under long plans.
- Some tests have ordering-dependent failures.
- No production-grade auth or multi-tenant isolation.