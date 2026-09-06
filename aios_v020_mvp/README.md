# AIOS v0.2.0 usable MVP

This directory contains the AIOS v0.2.0 usable MVP.

## What this is

A self-contained Python application that implements the full
required flow end-to-end on a single process:

```
POST /task -> Workflow create -> Planner -> Executor -> File tools
            -> Reviewer (strict) -> Result persistence
            -> GET /task/<id>   -> GET /task/<id>/artefacts/<path>
```

It satisfies the task spec at the top of this repository:

* "User submits task through the normal entry" — `POST /task`
* "Workflow auto-created" — every submission becomes a
  `Workflow` document persisted in the JSON store.
* "Independent Planner generates plan" — separate role with its
  own provider instance.
* "Executor calls real model" — real HTTP provider adapter
  (MiniMax / OpenAI / Anthropic) plus a deterministic local
  provider for offline use.
* "File tools execute" — `file_read`, `file_write`, `file_list`
  registered in a per-workflow sandbox under the result store.
* "Strict Reviewer accepts" — independent role with explicit
  evidence gate (verdict, score, evidence per check).
* "Results persist" — workflow state in a JSON file plus
  artefacts on disk.
* "API queries and reads back" — `GET /task/<id>`,
  `GET /task/<id>/artefacts`, `GET /task/<id>/artefacts/<path>`,
  `GET /tasks`, `GET /health`.

## Why this exists

The repository also contains the v0.1.0-alpha.3 source tree
under `kernel/`, `agents/`, `core/`, `modules/` and friends.
That tree is honest about being alpha:

* "POST /task normal entry E2E pass rate: 0 / 5"
* "MVP usable: NO"

The v0.2.0 MVP replaces the multi-process systemd architecture
with a single Python process that keeps the same role boundaries
(Planner / Executor / Reviewer) but removes the broken registry,
provider dispatcher, and tooling glue that blocked the alpha
release. The MVP is the *minimum* set of components that lets
the public flow work end-to-end.

The v0.1.0-alpha.3 source tree is kept intact; the v0.2.0 MVP
is layered on top as a new package. Both can co-exist.

## Quick start

```bash
# 1. Clone (already done if you are reading this on disk)
git clone https://github.com/e71398/AIOS.git
cd AIOS

# 2. Install runtime dependencies (Python 3.11+, pytest, requests, pyyaml)
pip install pytest requests pyyaml

# 3. Boot the entry gateway on the default port (18801)
python -m aios_v020_mvp.server

# 4. Submit a task from another terminal
curl -sS http://127.0.0.1:18801/task \
     -H 'Content-Type: application/json' \
     -d '{"input": "Write a file called hello.txt with a greeting."}'

# Response (HTTP 202):
# {"ok": true, "task_id": "...", "stage": "submitted", "status": "accepted", ...}

# 5. Poll for completion
curl -sS http://127.0.0.1:18801/task/<task_id>

# 6. List artefacts created by the workflow
curl -sS http://127.0.0.1:18801/task/<task_id>/artefacts

# 7. Read an artefact back
curl -sS http://127.0.0.1:18801/task/<task_id>/artefacts/hello.txt
```

## Running the E2E test

```bash
# 5/5 scenarios (file_write, file_write_then_read,
# file_write_two_artefacts, summarize_no_artefact,
# empty_input_rejected)
python aios_v020_mvp/tests/test_e2e_mvp.py

# Unit tests (11 tests, pure unit-level)
python -m pytest aios_v020_mvp/tests/test_unit.py -q
```

The E2E suite exercises the entry gateway end-to-end. It is
the AIOS v0.2.0 5/5 normal-entry gate.

## Real model integration

The MVP ships with two provider implementations:

* `LocalProvider` (default) — deterministic, offline, real
  generator. Used when no API key is configured or
  `AIOS_MVP_OFFLINE=1`.
* `HTTPChatProvider` — OpenAI-compatible HTTP client used for
  MiniMax, OpenAI and Anthropic (via the
  `chat/completions` endpoint). Activated automatically when
  the matching `*_API_KEY` environment variable is set.

```bash
# MiniMax (default base: https://api.minimaxi.com/v1)
export MINIMAX_API_KEY=sk-...
export MINIMAX_MODEL=MiniMax-M3
python -m aios_v020_mvp.server

# OpenAI
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o-mini
python -m aios_v020_mvp.server

# Anthropic (via the OpenAI-compatible shim if you have one,
# otherwise stays on local until you wire a native shim)
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_MODEL=claude-3-5-sonnet-latest
python -m aios_v020_mvp.server
```

Provider / model can be overridden per-role:

```bash
export AIOS_MVP_PLANNER_PROVIDER=minimax
export AIOS_MVP_PLANNER_MODEL=MiniMax-M3
export AIOS_MVP_EXECUTOR_PROVIDER=openai
export AIOS_MVP_EXECUTOR_MODEL=gpt-4o-mini
export AIOS_MVP_REVIEWER_PROVIDER=minimax
export AIOS_MVP_REVIEWER_MODEL=MiniMax-M3
```

## Architecture

```
   client ─► POST /task  (aios_v020_mvp.server:_Handler)
                │
                ▼
       Orchestrator.submit  →  Workflow (JSON file under
                              data_dir/workflows.json)
                │
                ▼
       BackgroundWorker     →  Orchestrator.run
                │
                ├── Planner.build_plan       (independent role)
                ├── Executor.execute         (independent role, drives
                │                             file_read / file_write /
                │                             file_list against the
                │                             per-workflow sandbox)
                └── Reviewer.review          (independent role,
                                              verdict JSON with
                                              evidence per check)
                │
                ▼
       JSONStore  +  FileResultStore  (data_dir/results/<wid>/...)
                │
                ▼
   client ◄── GET /task/<id>        (read workflow state)
           ◄── GET /task/<id>/artefacts/<path>   (read artefact)
```

## Layout

```
aios_v020_mvp/
    __init__.py            package marker, version
    config.py              environment-driven MVPConfig
    workflow.py            Workflow document + state machine
    planner.py             Planner role
    executor.py            Executor role (drives tool calls)
    reviewer.py            Reviewer role (verdict + evidence gate)
    orchestrator.py        Orchestrator + BackgroundWorker + builder
    server.py              HTTP entry gateway (ThreadingHTTPServer)
    providers/
        base.py            Provider / ProviderRequest / ProviderResponse
        local_provider.py  LocalProvider (deterministic offline)
        http_provider.py   HTTPChatProvider (MiniMax / OpenAI / Anthropic)
    tools/
        registry.py        ToolRegistry, ToolDefinition, ToolResult
        file_tools.py      file_read, file_write, file_list (sandboxed)
    persistence/
        json_store.py      JSONStore (atomic file-backed KV)
        files.py           FileResultStore (per-workflow sandbox)
    tests/
        test_e2e_mvp.py    5/5 E2E scenarios
        test_unit.py       11 unit tests
```

## Known limitations

The MVP is honest about what it is:

* Single-process orchestration. The v0.1.0-alpha.3 multi-process
  systemd model is intentionally replaced; horizontal scaling
  would require a real broker and is out of scope for v0.2.0.
* No auth by default. `AIOS_MVP_REQUIRE_AUTH=1` plus
  `AIOS_MVP_TOKEN=...` enables a shared-secret header check.
* The local provider does not contact an external model. It
  is a real provider (deterministic, JSON-shaped, token-aware)
  but it is not an LLM. When an API key is set the orchestrator
  switches to the HTTP provider for the matching role.
* The reviewer is strict: any missing evidence, score below
  the threshold, or non-`accept` verdict fails the workflow.
  This is intentional but it means some legitimate work
  products may be rejected on the local provider's heuristics.
