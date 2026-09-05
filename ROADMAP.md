# Roadmap

AIOS is in Alpha. The items below are tracked publicly and will appear as
GitHub issues on the public tracker. They are ordered roughly by
priority, not by date.

## 1. Stabilize the core loop (blocking public 5/5 E2E)

- [ ] Fix Canonical Registry and remove duplicate module loads
      (`kernel/tools/aios_orchestrator.py`).
- [ ] Open `POST /task → Orchestrator` normal entry path.
- [ ] Fix Planner structured-output truncation (token budget).
- [ ] Fix Reviewer structured-output truncation (token budget).
- [ ] Pass the 5/5 normal-entry E2E suite.
- [ ] Repair the native Canary suite.
- [ ] Repair test ordering dependencies.

## 2. Tools and safety

- [ ] Add a first-class read-only AIOS state tool for Executor.
- [ ] Add a sandboxed write-file tool for Executor.
- [ ] Document the permission matrix in `docs/TOOL_SECURITY.md`.
- [ ] Audit every Tool default to confirm `TOOL_PERMISSION_DEFAULT=deny`.

## 3. Providers

- [ ] Ship a local model Provider (e.g. Ollama, llama.cpp HTTP).
- [ ] Polish the Provider Adapter interface (see `docs/PROVIDER_ADAPTER.md`).
- [ ] Add Provider rate-limit / cost-budget telemetry that does **not**
      leak usage data.

## 4. Auth and isolation

- [ ] Implement identity-bound sessions end-to-end.
- [ ] Add workspace isolation per identity.
- [ ] Default-deny non-loopback binds.

## 5. New contributor experience

- [ ] Add a `make demo` workflow that runs the Demo Provider, Demo Tool
      and a tiny planner flow end-to-end with **zero** external calls.
- [ ] Add a Provider Adapter starter template.
- [ ] Add a Tool starter template.

## 6. Out of scope (Alpha)

- Production SLAs.
- High-availability multi-region deployment.
- Federated multi-tenant billing.

Issues will be created at release time. Labels used: `bug`,
`enhancement`, `good first issue`, `help wanted`, `provider`, `tools`,
`security`, `testing`, `architecture`, `documentation`.

## v0.1.0-alpha.2 — CI repair release

- Fixes `pyproject.toml` build-backend (was invalid; now `setuptools.build_meta`).
- Fixes secret pattern scan false positives in test fixtures (six lines).
- No new features.
