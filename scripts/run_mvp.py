#!/usr/bin/env python3
"""Convenience launcher for the AIOS v0.2.0 MVP.

Usage::

    python scripts/run_mvp.py start    # start the gateway (foreground)
    python scripts/run_mvp.py status   # hit /health
    python scripts/run_mvp.py e2e      # run the canonical 5/5 E2E
    python scripts/run_mvp.py unit     # run the unit tests
"""

from __future__ import annotations

import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def cmd_start(_: list) -> int:
    from aios_v020_mvp.server import main as server_main
    return server_main()


def cmd_status(_: list) -> int:
    import json
    import urllib.error
    import urllib.request

    base = os.environ.get("AIOS_MVP_URL", "http://127.0.0.1:18801")
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=5) as resp:
            print(json.dumps(json.loads(resp.read().decode("utf-8")), indent=2))
            return 0
    except urllib.error.URLError as exc:
        print(f"unreachable: {exc}", file=sys.stderr)
        return 1


def cmd_e2e(_: list) -> int:
    from aios_v020_mvp.tests import test_e2e_mvp
    return test_e2e_mvp.main()


def cmd_unit(_: list) -> int:
    import subprocess
    return subprocess.call(
        [sys.executable, "-m", "pytest",
         os.path.join(ROOT, "aios_v020_mvp", "tests", "test_unit.py"),
         "-q"]
    )


COMMANDS = {
    "start": cmd_start,
    "status": cmd_status,
    "e2e": cmd_e2e,
    "unit": cmd_unit,
}


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        print("commands:", ", ".join(COMMANDS))
        return 0
    cmd = argv[1]
    handler = COMMANDS.get(cmd)
    if handler is None:
        print(f"unknown command: {cmd}", file=sys.stderr)
        print("commands:", ", ".join(COMMANDS), file=sys.stderr)
        return 2
    return handler(argv[2:])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
