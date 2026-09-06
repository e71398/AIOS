# AIOS Changelog

All notable changes to AIOS are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-09-07

### Added

- **`aios_v020_mvp/`** - a self-contained, runnable MVP that
  delivers the full user-facing flow on a single Python
  process: `POST /task` -> Workflow -> Planner -> Executor ->
  File tools -> Reviewer -> Result persistence ->
  `GET /task/<id>` and `GET /task/<id>/artefacts/<path>`.
- New HTTP entry gateway in `aios_v020_mvp.server` (port
  `18801` by default, env `AIOS_MVP_PORT`).
- New provider adapters: a deterministic offline
  `LocalProvider` (the MVP default) and an OpenAI-compatible
  `HTTPChatProvider` that talks to MiniMax / OpenAI /
  Anthropic when the matching `*_API_KEY` is set.
- New file tools (`file_read`, `file_write`, `file_list`)
  with a per-workflow sandbox under
  `data_dir/results/<workflow_id>/`.
- New role modules: `aios_v020_mvp.planner`,
  `aios_v020_mvp.executor`, `aios_v020_mvp.reviewer`.
- New persistence layer: atomic JSON store + filesystem
  result store (`aios_v020_mvp.persistence`).
- New `BackgroundWorker` running submitted workflows on a
  background thread so `POST /task` returns immediately.
- New `scripts/run_mvp.py` launcher (`start`, `status`,
  `e2e`, `unit`).
- New `aios_v020_mvp.cli` client (`submit`, `get`, `wait`,
  `artefacts`, `cat`, `health`).
- New `aios_v020_mvp/tests/test_e2e_mvp.py` - 5/5 normal-entry
  E2E scenarios.
- New `aios_v020_mvp/tests/test_unit.py` - 11 unit tests
  covering the JSON store, file store, tool registry,
  planner, executor, reviewer, reviewer-evidence gate and
  the HTTP provider against an in-process mock OpenAI
  endpoint.

### Status

- 5/5 normal-entry E2E scenarios pass.
- MVP usable: **YES** for the user-facing flow defined in
  this release.

### Migration notes

- The v0.1.0-alpha.3 source tree (under `kernel/`, `agents/`,
  `core/`, `modules/`) is kept intact and is still tagged
  `v0.1.0-alpha.3`.
- The v0.2.0 MVP is layered on top as a new package and does
  not modify any of the alpha files. The MVP does not use
  the alpha multi-process systemd model; it runs in a single
  Python process.

## [0.1.0-alpha.3] - 2026-08-13

### Added

- Public source tree for the AIOS multi-process alpha.
- Entry gateway skeleton, orchestrator daemon, planner /
  executor / reviewer role boundaries.
- Redis-backed state bus (`aios:bus:*`) and filesystem
  result store.
- Provider adapter interface (OpenAI-compatible HTTP
  adapters), shipped **disabled by default**.
- Demo provider, demo tool, minimal workflow under
  `examples/`.
- Native canary and full regression test suites.

### Status

- `POST /task` normal entry E2E pass rate: 0 / 5.
- MVP usable: **NO** - documented honestly in `README.md`
  under "Known limitations".
