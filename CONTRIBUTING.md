# Contributing to AIOS

Thank you for your interest in contributing to AIOS. This project is in
**Alpha**. We welcome bug reports, documentation fixes, new providers, new
tools, and refactors — but please align with the conventions below before
opening a pull request.

## Ground rules

- Be respectful. See `CODE_OF_CONDUCT.md`.
- Do not commit secrets. See `SECURITY.md` for how to report any you discover.
- Do not enable paid providers or add paid API calls in CI.
- Do not silently skip or rewrite failing tests; mark them `KNOWN_FAILURES`
  in the test runner and file an issue.

## Branching

- Branch off `main`.
- Use the prefixes:
    - `fix/<short-name>` for bug fixes.
    - `feat/<short-name>` for new features.
    - `docs/<short-name>` for documentation only.
    - `chore/<short-name>` for refactors / housekeeping.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/) form:

```
<type>(<scope>): <short summary>

<optional body>
```

Examples:

```
fix(orchestrator): reconcile Provider Registry on startup
feat(provider): add Demo provider with no external calls
docs(readme): correct Provider Adapter default state
```

## Development setup

```bash
git clone https://github.com/<owner>/AIOS.git
cd AIOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
AIOS_OFFLINE=1 ./start.sh start
```

Tests:

```bash
pytest kernel/tools/tests -q
./scripts/run_full_regression.sh
```

## Adding a new Provider

1. Implement the adapter contract in `kernel/tools/providers/`.
2. Register it in `config/module_manifest.json` under a new key.
3. Default it to **off** in `config/features.toml`.
4. Add a unit test under `kernel/tools/tests/` using a stub HTTP server, **not**
   a real provider.
5. Add a row to `THIRD_PARTY_NOTICES.md`.

See `docs/PROVIDER_ADAPTER.md` for the full contract.

## Adding a new Tool

1. Implement the tool in `kernel/tools/tools/` (or your own package) with the
   standard `Tool` interface (input schema, output schema, permission class).
2. Register the tool in `config/module_manifest.json`.
3. Default the permission to `deny`.
4. Add a unit test under `kernel/tools/tests/`.
5. Document any destructive or write capability in `docs/TOOL_SECURITY.md`.

See `docs/TOOL_SECURITY.md`.

## Pull request checklist

- [ ] CI is green (offline mode).
- [ ] No new model calls added to CI.
- [ ] No secrets, no private paths, no provider defaults flipped to on.
- [ ] Tests added or updated.
- [ ] `docs/` updated if behavior or contract changed.
- [ ] `CHANGELOG.md` updated under the next unreleased version.
- [ ] Issue referenced.

## Review process

- Two approvals are required before merge to `main`.
- At least one approval must come from a maintainer with `area/*` ownership
  matching the touched area (e.g. orchestrator, provider, tool, security).
- Security-sensitive changes (auth, network bind, default flips) require a
  dedicated security review.

## Reporting security issues

Please **do not** open a public issue for security bugs. Follow
`SECURITY.md`.