# AIOS

> AIOS is an experimental AI Agent Operating System.
> Status: Alpha — not production-ready.

AIOS is a multi-component runtime that orchestrates AI agents, tools, workflows and provider adapters.
This repository publishes the v0.1.0-alpha.1 source tree for external contributors who want to study,
audit, extend or harden the system together.

- **Public version:** v0.1.0-alpha.2
- **Internal development lineage:** 5.2.8
- **License:** Apache License 2.0
- **Maturity:** Alpha — not production-ready

## What AIOS Is

AIOS is a long-running service that accepts user requests through an HTTP entry gateway,
breaks them into planner steps, dispatches work to executor daemons (one per upstream AI tool),
gathers tool results, passes them through a reviewer, and persists the outcome to a Redis state
bus and a filesystem result store. It is designed so each component — entry, planner, executor,
reviewer, verifier, monitor, web — is a small stand-alone process that can be restarted and
audited independently.

## Current Status (Honest)

| Item | Value |
| --- | --- |
| Development stage | Alpha |
| Product readiness | NOT_READY |
| `POST /task` normal entry E2E pass rate | 0 / 5 |
| MVP usable | NO |
| Public release | v0.1.0-alpha.1 — clearly Alpha |
| Default provider | OFF (MiniMax and all external providers are off by default) |

The system is honest about what it can and cannot do. The list below is based on the actual code
that ships in this repository.

### Confirmed capabilities

- Entry gateway HTTP service that accepts task submissions and identity-bound session requests.
- Orchestrator that scans a Redis queue and dispatches work to planner / executor / reviewer roles.
- Planner, executor, reviewer process skeletons with contract-level role boundaries.
- Redis-backed state bus (`aios:bus:*`) and filesystem result store.
- Provider adapter interface (see `docs/PROVIDER_ADAPTER.md`) with one shipping adapter family
  (OpenAI-compatible HTTP adapters) that is **disabled by default**.
- Tool registry with permission defaults (deny-by-default).
- systemd unit templates for local development.
- Demo provider, demo tool, and minimal workflow under `examples/` — these are clearly labelled
  Demo and produce **no real model calls**.

### Known limitations (read this before contributing)

- The systemd Orchestrator currently reports an inconsistent Provider Registry; `minimax-official`
  is reported as `UNVERIFIED` and the normal `POST /task → Orchestrator` loop is **not** end-to-end
  passing. This blocks a public 5/5 E2E gate and is tracked as an open issue.
- Planner and Reviewer emit structured JSON whose token budget is tight; long plans may be
  truncated. Fixes are tracked as open issues.
- Executor lacks a first-class read-only system-state tool and a sandboxed write tool. Filesystem
  writes from executor currently go through restricted ToolBus paths only.
- Canary and full-regression test suites have known ordering-dependent failures. They are kept in
  CI and labelled `KNOWN_FAILURES` — they are not silently skipped.
- There is no production-grade authentication or multi-tenant isolation in this Alpha. Bind
  addresses are loopback-only and the API will refuse non-loopback connections without
  `AUTH_REQUIRED_FOR_NON_LOOPBACK=1` plus a valid token.
- MiniMax and other external providers must be marked **off** in `features.toml` for any
  offline / CI run.

## Real Architecture (high level)

```
                 ┌──────────────────────────────┐
   client  ─►    │  aios_entry_gateway (HTTP)   │  127.0.0.1:18801
                 └──────────────┬───────────────┘
                                │
                                ▼
                       Redis state bus  (aios:bus:*)
                                │
                                ▼
              ┌─────────────────────────────────────┐
              │  aios_orchestrator (daemon)         │
              │  scans queue → planner / executor / │
              │  reviewer / verifier / result-push │
              └─────────────────────────────────────┘
                  │           │           │
                  ▼           ▼           ▼
              planner     executor     reviewer
              (role)      (role)       (role)
                              │
                              ▼
                       ToolBus → tools
                              │
                              ▼
                  Provider Adapter (off by default)
                              │
                              ▼
                       Result Store + History
```

A current core blocker: `systemd Orchestrator → Registry state inconsistent → minimax-official
UNVERIFIED → normal task loop blocked`. See `docs/CURRENT_LIMITATIONS.md` and `ROADMAP.md`.

## Installation (offline-friendly)

Prerequisites:

- Linux (Ubuntu 22.04+ recommended)
- Python 3.10+ with `pip`
- `redis-cli` / Redis 6+ for the state bus (a local-only Redis is fine)
- No API keys required to boot in offline mode

```bash
git clone https://github.com/<owner>/AIOS.git
cd AIOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # generated from pyproject.toml; lockfiles live in requirements/
cp .env.example .env              # all credentials are placeholders
./install.sh                      # creates dirs, links nothing external
./start.sh status                 # systemd --user target; see docs/DEVELOPMENT.md
```

The default `features.toml` has every external provider and every paid capability set to `off`.
Do not enable `minimax-official` without reading `docs/PROVIDER_ADAPTER.md` and
`docs/TOOL_SECURITY.md`.

## Offline startup (no API keys, no model calls)

```bash
AIOS_OFFLINE=1 ./start.sh start
curl -sS http://127.0.0.1:18801/health
```

Expected: HTTP 200 with `{"status":"ok","offline":true}`.

## Provider configuration

Providers are configured in `config/features.toml`. The default has them all disabled. To enable
a provider you must:

1. Set `EXTERNAL_PROVIDERS_ENABLED=1` in `.env`.
2. Provide an API key in `.env` (e.g. `MINIMAX_API_KEY=YOUR_API_KEY_HERE`).
3. Set the provider's own flag in `features.toml`, e.g.
   `ai_provider_minimax_official = on` (currently shipped as **off** in this repository).
4. Restart the relevant services.

MiniMax is treated as a paid provider for cost reasons; even when enabled it is gated behind a
per-task budget. See `docs/PROVIDER_ADAPTER.md`.

## Security defaults

- `HTTP_BIND` defaults to `127.0.0.1`. Public bind addresses require explicit opt-in.
- `EXTERNAL_PROVIDERS_ENABLED=0` (default).
- `AIOS_MINIMAX_OFFICIAL_ENABLED=0` (default).
- `TOOL_PERMISSION_DEFAULT=deny` (default).
- `AUTH_REQUIRED_FOR_NON_LOOPBACK=1` (default).
- systemd units ship as templates — no machine-specific paths or credentials.

## Tests

```bash
# offline / CI-only smoke
pytest kernel/tools/tests/test_stable_v1_cli_contract.py -q
pytest kernel/tools/tests/test_registry_path_boundary.py -q

# full regression (will report KNOWN_FAILURES — these are documented, not hidden)
./scripts/run_full_regression.sh
```

CI runs offline-only by design; no provider is contacted from CI.

- `docs/CURRENT_LIMITATIONS.md` — what is broken right now
- `docs/ARCHITECTURE.md` — data flow and component boundaries
- `docs/DEVELOPMENT.md` — how to add a Provider / Tool / Center
- `docs/PROVIDER_ADAPTER.md` — provider contract, rate limits, budget gates
- `docs/TOOL_SECURITY.md` — ToolBus permissions, sandbox write policy

## Roadmap

The first public issues are listed in `ROADMAP.md` and will be created on the public tracker after
release. They include:

1. Fix Canonical Registry and remove duplicate module loads.
2. Open `POST /task → Orchestrator` normal entry path.
3. Add a read-only AIOS state tool for Executor.
4. Add a sandboxed write-file tool for Executor.
5. Fix Planner JSON truncation.
6. Fix Reviewer JSON truncation.
7. Pass the 5/5 normal-entry E2E suite.
8. Repair the native Canary suite.
9. Repair test ordering dependencies.
10. Ship a local model provider.
11. Polish the Provider Adapter interface.
12. Finish auth and workspace isolation.

## Contributing

We welcome issues and pull requests. Read `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and
`SECURITY.md` before opening anything.

- `CONTRIBUTING.md` — branch conventions, CI expectations, review process.
- `CODE_OF_CONDUCT.md` — community standards.
- `SECURITY.md` — how to report vulnerabilities (do **not** file a public issue).
- `SUPPORT.md` — where to ask questions and what is in / out of scope.

## License

Apache License 2.0. See `LICENSE`. Third-party attributions are listed in
`THIRD_PARTY_NOTICES.md`. The NOTICE file lists the project-level notices required by the Apache
2.0 license.

## 中文文档

中文说明见 `README.zh-CN.md`。架构、限制、贡献方式与英文版保持一致。