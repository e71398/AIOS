# Changelog

All notable changes to AIOS will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
*for the public release channel*. Internal development lineage uses its own
version numbers (currently 5.2.8).

## [v0.1.0-alpha.1] - 2026-09-04

### Added

- Initial public source tree of AIOS.
- Entry gateway, orchestrator, planner, executor, reviewer and verifier
  components.
- Redis-backed state bus and filesystem result store.
- Provider Adapter interface with OpenAI-compatible HTTP adapters
  (disabled by default).
- Tool registry with deny-by-default permissions.
- systemd unit templates for local development.
- Demo Provider, Demo Tool, and minimal workflow under `examples/`.
- GitHub Actions CI for offline syntax, import, registry, path-boundary
  and secret-pattern checks.
- GitHub Actions full-regression workflow (manual + scheduled).

### Known issues (do not block Alpha publication 鈥?they are honest and
documented)

- `POST /task 鈫?Orchestrator` normal entry path does not pass 5/5 E2E.
- Provider Registry state is inconsistent in the systemd Orchestrator
  (`minimax-official` reports `UNVERIFIED`).
- Planner and Reviewer JSON output may be truncated.
- Executor lacks a first-class read-only system-state tool and a
  sandboxed write-file tool.
- Native Canary suite has known failures.
- Some tests have ordering-dependent failures; they are flagged
  `KNOWN_FAILURES` in CI rather than skipped silently.

### Security defaults

- `HTTP_BIND=127.0.0.1` by default.
- `EXTERNAL_PROVIDERS_ENABLED=0` by default.
- `AIOS_MINIMAX_OFFICIAL_ENABLED=0` by default.
- `TOOL_PERMISSION_DEFAULT=deny` by default.
- `AUTH_REQUIRED_FOR_NON_LOOPBACK=1` by default.
- systemd units use `NoNewPrivileges=true`, `PrivateTmp=true`, run as
  the unprivileged `aios` service user.

### Notes

- This Alpha is the first public release. It is not production-ready.
  See `README.md`, `docs/CURRENT_LIMITATIONS.md` and `ROADMAP.md`.