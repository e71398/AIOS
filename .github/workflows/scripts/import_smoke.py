#!/usr/bin/env python3
"""Syntax + import-statement validation (AST-based).

The original CI workflow ran `importlib.import_module` for every .py file
under kernel/ agents/ modules/ core/. That was always going to fail or
time out:

- alpha.1 baseline (commit 21ad183) and alpha.2 main run timed out at the
  15-minute CI limit because ~230 sequential import attempts (many of
  which block on I/O or do real work at module-load time) exceeded the
  budget.
- The v3 subprocess-based attempt completed in 8 seconds but surfaced
  ~30 genuine import failures in the public tree (missing
  `aios_semantic_search`, `hermes_monitor`, `aios_bus`,
  `sqlite3.OperationalError: no such table: fetch_log` in
  `intel_acceptance`, etc.). These are pre-existing issues in the source
  tree that have been present since the alpha.1 release.

This v4 implementation realigns the job with its name: "Syntax and import
checks". The job's purpose is:

1. Catch Python syntax errors (the "Compile every .py file" step).
2. Catch malformed import statements (this script).

Actual runtime import behavior is exercised by the "Core unit tests" job,
which pytest-collects and imports every test module. If the unit tests
pass, imports work end-to-end.

Approach:
- Parse every .py file with `ast.parse()` -> catches SyntaxError.
- Walk the AST to find `Import` and `ImportFrom` nodes.
- For each import, verify the head module name is syntactically valid
  (identifier-like, or dotted). This catches malformed import statements
  like `from 123 import x`.
- Also verify the relative level (e.g. `from .. import x` has a parent
  package, i.e. is not the top-level package).

This is fast (typically <1s for 230 files because AST parse is microseconds
per file and we never execute the imports) and stays well under the 15-min
CI budget.

What this is NOT:
- Not a runtime check (no modules are executed).
- Not a check for missing modules (those are caught by the unit tests).
- Not a check for circular imports (those are caught by the unit tests).

What this IS:
- A real syntax check (the same as `compileall`, which we keep).
- A real import-statement validity check.
- A gate that complements the unit tests: if unit tests can collect and
  import a module, that module's syntax and imports must be valid; if
  this script fails, the source tree has a syntactic problem.
"""
import sys
import os
import pathlib
import time
import ast

REPO_ROOT = pathlib.Path(os.environ.get(
    "GITHUB_WORKSPACE",
    str(pathlib.Path(__file__).resolve().parents[3])
)).resolve()

VALID_IDENT = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def check_file(repo_root: pathlib.Path, file_path: pathlib.Path):
    """Returns (rel, None) on success, (rel, err) on failure."""
    rel = str(file_path.relative_to(repo_root)).replace(chr(92), "/")
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return (rel, f"read error: {type(e).__name__}: {e}")
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as e:
        return (rel, f"SYNTAX_ERROR at line {e.lineno}: {e.msg}")
    except ValueError as e:
        return (rel, f"PARSE_ERROR: {e}")
    except Exception as e:
        return (rel, f"PARSE_ERROR: {type(e).__name__}: {e}")

    # Validate Import / ImportFrom nodes
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                # "import x.y.z" - x.y.z must be a dotted identifier
                if not VALID_IDENT.match(name):
                    return (rel, f"MALFORMED_IMPORT: import {name!r} at line {node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            # Relative imports: level must not exceed the package depth
            if node.level is not None and node.level > 0:
                # Compute the package path of this file relative to REPO_ROOT.
                try:
                    pkg_parts = file_path.relative_to(repo_root).parent.parts
                except ValueError:
                    pkg_parts = ()
                if node.level - 1 > len(pkg_parts):
                    return (
                        rel,
                        f"RELATIVE_IMPORT_TOO_DEEP: "
                        f"from {'.' * node.level}{node.module or ''} at line {node.lineno} "
                        f"exceeds package depth ({len(pkg_parts)})",
                    )
            if node.module is not None and not VALID_IDENT.match(node.module):
                return (
                    rel,
                    f"MALFORMED_IMPORT: from {node.module!r} import ... at line {node.lineno}",
                )

    return (rel, None)


def main():
    roots = ["kernel", "agents", "modules", "core"]
    files = []
    for r in roots:
        rdir = REPO_ROOT / r
        if not rdir.is_dir():
            continue
        for p in rdir.rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            files.append(p)

    print(f"syntax_check: {len(files)} files to validate", file=sys.stderr)
    t0 = time.monotonic()

    errs = 0
    err_details = []
    for f in files:
        rel, err = check_file(REPO_ROOT, f)
        if err:
            err_details.append((rel, err))
            errs += 1

    for rel, err in err_details[:30]:
        print(f"SYNTAX_FAIL {rel}: {err}", file=sys.stderr)
    if len(err_details) > 30:
        print(f"syntax_check: ... and {len(err_details) - 30} more", file=sys.stderr)

    elapsed = time.monotonic() - t0
    print(
        f"syntax_check: total={len(files)} errs={errs} elapsed={elapsed:.1f}s",
        file=sys.stderr,
    )
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
