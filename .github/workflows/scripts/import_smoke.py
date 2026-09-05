#!/usr/bin/env python3
"""Parallel import smoke test for CI (subprocess variant).

Validates that every .py file under kernel/agents/modules/core can be
imported, running each import in a SEPARATE Python subprocess with a hard
timeout. This is the only way to guarantee per-file timeouts when an
import blocks on I/O (Redis socket, network DNS, slow os.fstat, etc.) since
the GIL prevents ThreadPoolExecutor from killing a stuck import.

Why subprocess instead of threads:
- alpha.1/alpha.2 used sequential importlib.import_module; cumulative
  wall time exceeded 15-minute CI timeout.
- First 027 attempt used ThreadPoolExecutor; ThreadPoolExecutor cannot
  interrupt a stuck import (GIL). The 15-minute CI timeout still fires.

Each import subprocess is wrapped by `subprocess.run(..., timeout=PER_FILE_TIMEOUT)`.
When timeout expires, subprocess.run raises TimeoutExpired, we mark the file
as TIMEOUT, and the parent continues.

History:
- v1: sequential importlib.import_module loop (alpha.1 baseline).
- v2: ThreadPoolExecutor + future.result(timeout) (first 027 attempt).
- v3 (this file): subprocess.run(timeout) per file, parallelized by N workers.

Each subprocess imports a single module via `python -c "import MOD"`. Stdlib
imports succeed in milliseconds; missing-dependency imports fail in
milliseconds with ModuleNotFoundError; pathological imports that block
on I/O are killed at PER_FILE_TIMEOUT seconds.

Same intent preserved: every file is attempted, failures cause non-zero
exit, no file is silently skipped.
"""
import sys
import os
import pathlib
import time
import subprocess
import concurrent.futures as cf

PER_FILE_TIMEOUT = int(os.environ.get("IMPORT_SMOKE_PER_FILE_TIMEOUT", "8"))
OVERALL_DEADLINE = int(os.environ.get("IMPORT_SMOKE_OVERALL_DEADLINE", str(12 * 60)))
MAX_WORKERS = int(os.environ.get("IMPORT_SMOKE_MAX_WORKERS", "6"))

REPO_ROOT = pathlib.Path(os.environ.get(
    "GITHUB_WORKSPACE",
    str(pathlib.Path(__file__).resolve().parents[3])
)).resolve()

PYTHON_BIN = sys.executable


def to_module_name(repo_root: pathlib.Path, file_path: pathlib.Path) -> str:
    rel = file_path.relative_to(repo_root)
    parts = list(rel.parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def run_import_subprocess(module: str) -> tuple:
    """Run `import module` in a fresh subprocess with a hard timeout.

    Returns (module, error_or_None, elapsed).
    """
    t0 = time.monotonic()
    code = "import sys; sys.path.insert(0, %r); import %s" % (str(REPO_ROOT), module)
    try:
        r = subprocess.run(
            [PYTHON_BIN, "-c", code],
            capture_output=True,
            timeout=PER_FILE_TIMEOUT,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        elapsed = time.monotonic() - t0
        if r.returncode == 0:
            return (module, None, elapsed)
        # Extract the last line of stderr for the report
        err_out = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        last_line = err_out.splitlines()[-1] if err_out else f"exit {r.returncode}"
        return (module, last_line[:200], elapsed)
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        return (module, f"TIMEOUT (>={PER_FILE_TIMEOUT}s per-file)", elapsed)
    except Exception as e:
        elapsed = time.monotonic() - t0
        return (module, f"FUTURE_ERROR: {type(e).__name__}: {e}", elapsed)


def main():
    roots = ["kernel", "agents", "modules", "core"]
    files = []
    for r in roots:
        rdir = REPO_ROOT / r
        if not rdir.is_dir():
            continue
        for p in rdir.rglob("*.py"):
            sp = str(p)
            if "__pycache__" in sp:
                continue
            if p.name == "__init__.py":
                continue
            files.append(p)

    # Pre-compute module names
    tasks = [(to_module_name(REPO_ROOT, f), f) for f in files]

    print(f"import_smoke: {len(tasks)} files to process (workers={MAX_WORKERS}, per-file timeout={PER_FILE_TIMEOUT}s)", file=sys.stderr)
    t_start = time.monotonic()

    errs = 0
    timed_out = 0
    err_details = []

    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(run_import_subprocess, mod): (mod, f) for mod, f in tasks}
        try:
            for fut in cf.as_completed(futures, timeout=OVERALL_DEADLINE):
                mod, path = futures[fut]
                try:
                    _mod, err, _dt = fut.result()
                except Exception as e:
                    err = f"FUTURE_ERROR: {type(e).__name__}: {e}"
                if err:
                    rel = path
                    try:
                        rel = str(pathlib.Path(path).relative_to(REPO_ROOT))
                    except ValueError:
                        pass
                    if err.startswith("TIMEOUT"):
                        timed_out += 1
                    err_details.append((rel, err))
                    errs += 1
        except cf.TimeoutError:
            remaining = [f for f in futures if not f.done()]
            for r in remaining:
                r.cancel()
            print(
                f"import_smoke: overall deadline hit; {len(remaining)} files abandoned",
                file=sys.stderr,
            )
            errs += len(remaining)

    for rel, err in err_details[:30]:
        print(f"IMPORT_FAIL {rel}: {err}", file=sys.stderr)
    if len(err_details) > 30:
        print(f"import_smoke: ... and {len(err_details) - 30} more failures", file=sys.stderr)
    if timed_out:
        print(
            f"import_smoke: {timed_out} files exceeded per-file timeout of {PER_FILE_TIMEOUT}s",
            file=sys.stderr,
        )

    elapsed = time.monotonic() - t_start
    print(
        f"import_smoke: total={len(tasks)} errs={errs} timeouts={timed_out} elapsed={elapsed:.1f}s",
        file=sys.stderr,
    )
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
