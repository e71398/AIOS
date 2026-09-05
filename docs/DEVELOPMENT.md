# Development

This document explains how to set up an offline-friendly development
environment for AIOS and how to add a Provider, a Tool, or a new
long-running component.

## Setup

```bash
git clone https://github.com/<owner>/AIOS.git
cd AIOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
AIOS_OFFLINE=1 ./start.sh start
curl -sS http://127.0.0.1:18801/health
```

Expected: HTTP 200 with `{"status":"ok","offline":true}`.

## Code layout

```
kernel/
  centers/                 long-running support components
  config/                  on-disk defaults loaded before any .env
  core/                    protocol-level types
  orchestration/           pipeline registry / state machine
  prompts/                 prompt registry (Provider-bound)
  protocols/               bus / capability / safety contracts
  state/                   ephemeral state shared by components
  tools/                   the bulk of the runtime: daemons, services,
                           tests, scripts, providers, executors
agents/                    process-level dispatchers / pluggable tools
modules/                   third-party plugin packages
systemd/                   local-development service templates
docs/                      architecture, contracts, limitations
examples/                  Demo Provider, Demo Tool, minimal workflow
.github/workflows/         CI and full regression
```

## Adding a Provider

1. Implement the adapter contract under `kernel/tools/providers/`.
2. Register the provider in `config/module_manifest.json`.
3. Default it to **off** in `config/features.toml`.
4. Add a stub HTTP server test under `kernel/tools/tests/` — **no**
   real Provider call.
5. Add a row to `THIRD_PARTY_NOTICES.md`.

See `docs/PROVIDER_ADAPTER.md` for the full contract.

## Adding a Tool

1. Implement the tool under `kernel/tools/tools/` (or your own package)
   with the standard `Tool` interface (input schema, output schema,
   permission class).
2. Register the tool in `config/module_manifest.json`.
3. Default the permission to `deny`.
4. Add a unit test under `kernel/tools/tests/`.
5. Document any destructive capability in `docs/TOOL_SECURITY.md`.

## Adding a Center

1. Create a sub-package under `kernel/centers/<your_center>/`.
2. Implement an `__init__.py` exporting `start()`, `stop()`, `health()`.
3. Subscribe to the bus keys your center owns.
4. Add a smoke test under `kernel/tools/tests/`.

## Running tests

```bash
# offline smoke
pytest kernel/tools/tests/test_stable_v1_cli_contract.py -q
pytest kernel/tools/tests/test_registry_path_boundary.py -q

# full regression
./scripts/run_full_regression.sh
```

`KNOWN_FAILURES` will be printed honestly. Do not delete or skip these
tests silently — file an issue.

## Code style

- Python 3.10+ syntax.
- No implicit relative imports.
- No print-based logging in `kernel/tools/`; use the structured logger
  in `kernel/tools/aios_bus.py`.
- No new direct subprocess calls in `kernel/tools/*.py` — extend the
  ToolBus instead.