# Contributor Quickstart

This page tells a new contributor the fastest path from `git clone` to a working change.

## 1. Local setup

AIOS targets Python 3.11+ and is published as `aios` on the public index. For local source-tree work, clone the repo and use a virtualenv:

```bash
git clone https://github.com/e71398/AIOS.git
cd AIOS
python -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

The public Git Tag and the Python distribution version both derive from `pyproject.toml` `[project].version`. The current public tag is `v0.1.0-alpha.3` and the wheel/sdist version is `0.1.0a3`.

## 2. Tests you can run offline (no Provider, no Redis, no Docker)

```bash
# Compile every .py file in the tree
python -m compileall -q kernel agents modules core

# Parallel import smoke (mirrors CI)
python .github/workflows/scripts/import_smoke.py

# Stable contract tests (no Provider, no external deps)
pytest kernel/tools/tests/test_stable_v1_cli_contract.py -q
pytest kernel/tools/tests/test_registry_path_boundary.py -q
```

These tests are what the CI runs in the `Core unit tests` job. They do not call any Provider and do not require Redis or any other runtime. If they pass locally, your change is safe to push.

## 3. Workflow

1. Fork the repo.
2. Create a branch off `main`: `git checkout -b fix/<short-name>`.
3. Make your change. Keep the commit history readable; one logical change per commit.
4. Run the offline tests above.
5. Open a Pull Request against `main`.
6. CI runs all six jobs. The `Syntax and import checks` job uses `.github/workflows/scripts/import_smoke.py` and is bounded to ~1 minute wall time on the runner.
7. A maintainer (currently the repo owner) will squash-merge after the required checks pass and the conversation is resolved.

## 4. Style

- Python 3.11+. Type hints where the existing module already uses them.
- No new top-level dependencies without an Issue discussion first.
- Provider keys, real user data, real logs and real databases do not belong in this tree, ever. The CI `secret-pattern-scan` and `Registry and path-boundary` jobs enforce this.

## 5. What NOT to do

- Do not push to `main` directly. Branch protection enforces this.
- Do not force-push. Branch protection enforces this.
- Do not edit the alpha.1 or alpha.2 Git tags. They are historical.
- Do not commit `.env`, `opencode-aios.json`, real Provider keys, real database dumps or real user logs.
- Do not widen the `secret-pattern-scan` regex to silence a failure. Fix the fixture or the source.
- Do not write `continue-on-error: true` to make CI green. Fix the underlying issue.

## 6. Where to ask

- Bug or feature: open an Issue.
- Design or "how should this work": open a Discussion (Announcements / General).
- Security issue: do not file a public Issue. Follow `SECURITY.md`.
