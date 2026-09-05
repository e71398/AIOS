#!/usr/bin/env python3
"""AIOS Capability Version Engine — declared-vs-running reconciliation.

P2-VERS-001 closure (2026-08-17): every ``chips[*].contract`` declared
in ``config/module_manifest.json`` is now reconciled against the
*running* version of the underlying binary / package.  Drift is
surfaced through one of three well-defined channels:

  * CLI:   ``python3 aios_capability_versions.py`` lists every chip
           with declared vs running vs drift column.
  * JSON:  ``python3 aios_capability_versions.py --json`` for
           machine consumption.
  * Lib:   ``reconcile()`` returns a list of
           ``CapabilityVersionReport`` dicts the orchestrator /
           acceptance scripts can import.

Resolution rules:
  * ``kernel/tools/<name>.py``            — ``__VERSION__`` module
    constant (defaulting to ``1.0.0`` when absent).
  * ``${HOME}/.n/bin/<executable>``   — ``<executable> --version``
    one-shot subprocess, parsed as semver.
  * ``pip show <package>``                — declared version of a
    Python package; mapped from the chip ``runtime_dep`` field.
  * ``npm list -g --depth=0 <package>``   — declared version of a
    Node package; mapped from the chip ``npm_dep`` field.

Drift classification:
  * ``match``    — declared semver == running semver
  * ``older``    — running older than declared
  * ``newer``    — running newer than declared (forward-compat
                   drift, recorded but does not fail)
  * ``unknown``  — running version not parseable
  * ``missing``  — binary / package not on the host

The exit code is 0 when every chip is ``match`` / ``newer`` / ``unknown``
(no forced upgrade required) and 1 when at least one chip is
``missing`` or ``older`` (forced upgrade recommended).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AIOS_HOME = Path(os.environ.get("AIOS_HOME", "${AIOS_HOME}"))
MANIFEST_PATH = AIOS_HOME / "config" / "module_manifest.json"
TOOLS = AIOS_HOME / "kernel" / "tools"
sys.path.insert(0, str(TOOLS))

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass
class CapabilityVersionReport:
    chip_id: str
    kind: str
    role: str
    declared: str = ""
    running: str = ""
    drift: str = "unknown"
    source: str = ""
    note: str = ""
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _parse_semver(text: str) -> Optional[Tuple[int, int, int]]:
    """Parse a ``MAJOR.MINOR.PATCH`` triple from arbitrary text."""
    if not text:
        return None
    m = _SEMVER_RE.search(text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _classify_drift(declared: str, running: str) -> str:
    """Compare two semver strings; return drift label."""
    d = _parse_semver(declared)
    r = _parse_semver(running)
    if d is None or r is None:
        return "unknown"
    if d == r:
        return "match"
    if r < d:
        return "older"
    return "newer"


def _read_python_module_version(name: str) -> Optional[str]:
    """Return the ``__VERSION__`` attribute of ``kernel.tools.<name>`` if any."""
    try:
        mod = __import__(f"aios_{name}" if not name.startswith("aios_") else name,
                        fromlist=["__VERSION__"])
        return str(getattr(mod, "__VERSION__", "") or "") or None
    except Exception:
        return None


def _read_executable_version(executable: str, args: List[str], timeout: int = 8) -> Optional[str]:
    """Return the version of an executable via ``<exe> <args>``."""
    path = Path(executable).expanduser()
    if not path.is_absolute():
        path = (AIOS_HOME / executable).resolve()
    if not path.is_file():
        return None
    cmd = [str(path), *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, shell=False,
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        sem = _parse_semver(out)
        if sem:
            return f"{sem[0]}.{sem[1]}.{sem[2]}"
        first = (out.strip().splitlines() or [""])[0]
        return first[:80] if first else None
    except Exception:
        return None


def _read_pip_version(package: str, timeout: int = 8) -> Optional[str]:
    """Return the installed pip version of a Python package."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "show", package],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
        return None
    except Exception:
        return None


def _read_npm_version(package: str, timeout: int = 8) -> Optional[str]:
    """Return the installed npm version of a global Node package."""
    try:
        proc = subprocess.run(
            ["npm", "list", "-g", "--depth=0", package],
            capture_output=True, text=True, timeout=timeout,
        )
        m = re.search(rf"{re.escape(package)}@(\S+)", proc.stdout or "")
        if m:
            return m.group(1)
        return None
    except Exception:
        return None


def reconcile(manifest: Optional[Dict[str, Any]] = None) -> List[CapabilityVersionReport]:
    """Return a per-chip CapabilityVersionReport list."""
    if manifest is None:
        if not MANIFEST_PATH.is_file():
            return []
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    reports: List[CapabilityVersionReport] = []
    for chip in manifest.get("chips", []):
        cid = str(chip.get("id", "?"))
        kind = str(chip.get("kind", "?"))
        role = str(chip.get("role", "?"))
        declared = str(chip.get("contract", "")).strip()
        report = CapabilityVersionReport(
            chip_id=cid, kind=kind, role=role, declared=declared,
        )
        # Determine the running version by chip id.
        # 1) Python module under kernel/tools/ — by chip id mapping
        py_module = {
            "aios-orchestrator": "aios_orchestrator",
            "aios-verification-gate": "aios_verification_gate",
            "aios-acceptance": "aios_acceptance",
            "aios-monitor": "aios_monitor",
            "aios-backup": "aios_backup",
            "aios-enforcer": "aios_enforcer",
            "aios-tool-adapter": "aios_tool_adapter",
        }.get(cid)
        if py_module:
            v = _read_python_module_version(py_module)
            if v:
                report.running = v
                report.source = f"python:{py_module}"
            else:
                report.running = "missing"
                report.source = f"python:{py_module}"
                report.drift = "missing"
                report.note = "module import failed"
        else:
            # 2) Map by chip id to executable + args.
            exec_map = {
                "openclaw": ("${HOME}/.n/bin/openclaw", ["--version"]),
                "codex": ("${HOME}/.n/bin/codex", ["--version"]),
                "opencode": ("${HOME}/.n/bin/opencode", ["--version"]),
                "claude": ("${HOME}/.local/bin/claude", ["--version"]),
                "hermes": ("${HOME}/.hermes/hermes-agent/venv/bin/hermes",
                            ["--version"]),
            }
            m = exec_map.get(cid)
            if m:
                v = _read_executable_version(m[0], m[1])
                if v:
                    report.running = v
                    report.source = m[0]
                else:
                    report.running = "missing"
                    report.source = m[0]
                    report.drift = "missing"
                    report.note = "executable not found"
            else:
                # 3) Optional pip / npm dependency mapping.
                pip_dep = chip.get("runtime_dep")
                if pip_dep:
                    v = _read_pip_version(pip_dep)
                    if v:
                        report.running = v
                        report.source = f"pip:{pip_dep}"
                    else:
                        report.running = "missing"
                        report.source = f"pip:{pip_dep}"
                        report.drift = "missing"
                npm_dep = chip.get("npm_dep")
                if not pip_dep and npm_dep:
                    v = _read_npm_version(npm_dep)
                    if v:
                        report.running = v
                        report.source = f"npm:{npm_dep}"
                    else:
                        report.running = "missing"
                        report.source = f"npm:{npm_dep}"
                        report.drift = "missing"
        if report.drift != "missing":
            report.drift = _classify_drift(report.declared, report.running)
        if not report.declared:
            report.drift = "unknown"
            report.note = (report.note + ";no declared contract").strip(";")
        reports.append(report)
    return reports


def _print_table(rows: List[CapabilityVersionReport]) -> None:
    headers = ("CHIP", "DECLARED", "RUNNING", "DRIFT")
    widths = (
        max(len(headers[0]), max((len(r.chip_id) for r in rows), default=0)),
        max(len(headers[1]), max((len(r.declared) for r in rows), default=0)),
        max(len(headers[2]), max((len(r.running) for r in rows), default=0)),
        max(len(headers[3]), max((len(r.drift) for r in rows), default=0)),
    )
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join((r.chip_id.ljust(widths[0]),
                         r.declared.ljust(widths[1]),
                         r.running.ljust(widths[2]),
                         r.drift.ljust(widths[3]))))


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    parser.add_argument("--manifest", default=str(MANIFEST_PATH),
                        help="override manifest path")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    rows = reconcile(manifest)
    if args.json:
        print(json.dumps([r.to_dict() for r in rows], indent=2, ensure_ascii=False))
    else:
        _print_table(rows)
    drift_failures = sum(1 for r in rows if r.drift in ("older", "missing"))
    return 1 if drift_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CapabilityVersionReport",
    "reconcile",
    "main",
]
