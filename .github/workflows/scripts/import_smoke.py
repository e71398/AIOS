#!/usr/bin/env python3
"""Parallel import smoke test for CI.

Validates that every .py file under kernel/agents/modules/core can be imported,
running imports in parallel with per-file and overall timeouts to fit
within a 15-minute CI budget. Failures and per-file timeouts are reported
but do not silently skip files.

History:
- alpha.1 / alpha.2 used a sequential importlib.import_module loop; with ~230
  .py files this exceeded the 15-minute CI timeout.
- This parallel version preserves the same intent (every file is imported,
  failures cause non-zero exit) while keeping wall time bounded.
"""
import sys
import os
import pathlib
import time
import concurrent.futures as cf

PER_FILE_TIMEOUT = int(os.environ.get("IMPORT_SMOKE_PER_FILE_TIMEOUT", "3"))
OVERALL_DEADLINE = int(os.environ.get("IMPORT_SMOKE_OVERALL_DEADLINE", str(12 * 60)))
MAX_WORKERS = int(os.environ.get("IMPORT_SMOKE_MAX_WORKERS", "8"))

REPO_ROOT = pathlib.Path(os.environ.get(
    "GITHUB_WORKSPACE",
    str(pathlib.Path(__file__).resolve().parents[3])
))


def to_module_name(repo_root: pathlib.Path, file_path: pathlib.Path) -> str:
    rel = file_path.relative_to(repo_root)
    parts = list(rel.parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def try_import(file_path: pathlib.Path):
    import importlib
    t0 = time.monotonic()
    try:
        mod = to_module_name(REPO_ROOT, file_path)
        importlib.import_module(mod)
        return (str(file_path), None, time.monotonic() - t0)
    except Exception as e:
        return (str(file_path), f"{type(e).__name__}: {e}", time.monotonic() - t0)


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

    print(f"import_smoke: {len(files)} files to process", file=sys.stderr)
    t_start = time.monotonic()

    errs = 0
    timed_out = 0
    err_details = []

    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(try_import, f): f for f in files}
        try:
            for fut in cf.as_completed(futures, timeout=OVERALL_DEADLINE):
                path = None
                err = None
                try:
                    path, err, _dt = fut.result(timeout=PER_FILE_TIMEOUT)
                except cf.TimeoutError:
                    path = str(futures[fut])
                    err = f"TIMEOUT (>={PER_FILE_TIMEOUT}s per-file)"
                    timed_out += 1
                except Exception as e:
                    path = str(futures[fut])
                    err = f"FUTURE_ERROR: {type(e).__name__}: {e}"
                if err:
                    err_details.append((path, err))
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

    for path, err in err_details[:30]:
        rel = path
        try:
            rel = str(pathlib.Path(path).relative_to(REPO_ROOT))
        except ValueError:
            pass
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
        f"import_smoke: total={len(files)} errs={errs} timeouts={timed_out} elapsed={elapsed:.1f}s",
        file=sys.stderr,
    )
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
