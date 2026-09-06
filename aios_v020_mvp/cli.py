"""Tiny CLI client for the AIOS v0.2.0 MVP gateway.

Examples::

    python -m aios_v020_mvp.cli health
    python -m aios_v020_mvp.cli submit "Write a file called hello.txt."
    python -m aios_v020_mvp.cli get <task_id>
    python -m aios_v020_mvp.cli wait <task_id>
    python -m aios_v020_mvp.cli artefacts <task_id>
    python -m aios_v020_mvp.cli cat <task_id> <rel_path>

Environment::

    AIOS_MVP_URL     base URL (default http://127.0.0.1:18801)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple


def _base_url() -> str:
    return os.environ.get("AIOS_MVP_URL", "http://127.0.0.1:18801").rstrip("/")


def _http(method: str, path: str, body: Any = None) -> Tuple[int, Dict[str, Any]]:
    url = f"{_base_url()}{path}"
    headers = {"Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"ok": False, "error": raw}


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_health(_: list) -> int:
    code, body = _http("GET", "/health")
    print(f"HTTP {code}")
    _print(body)
    return 0 if code == 200 else 1


def cmd_submit(args: list) -> int:
    if not args:
        print("usage: cli submit <input text> [--sync]", file=sys.stderr)
        return 2
    sync = False
    if args and args[-1] == "--sync":
        sync = True
        args = args[:-1]
    payload = {"input": " ".join(args).strip()}
    if sync:
        payload["async"] = False
    code, body = _http("POST", "/task", payload)
    print(f"HTTP {code}")
    _print(body)
    if sync:
        return 0 if code == 200 and body.get("ok") else 1
    return 0 if code in (200, 202) and body.get("ok") else 1


def cmd_get(args: list) -> int:
    if not args:
        print("usage: cli get <task_id>", file=sys.stderr)
        return 2
    code, body = _http("GET", f"/task/{args[0]}")
    print(f"HTTP {code}")
    _print(body)
    return 0 if code == 200 else 1


def cmd_wait(args: list) -> int:
    if not args:
        print("usage: cli wait <task_id> [--timeout=30]", file=sys.stderr)
        return 2
    wid = args[0]
    timeout = 30.0
    for arg in args[1:]:
        if arg.startswith("--timeout="):
            try:
                timeout = float(arg.split("=", 1)[1])
            except ValueError:
                pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, body = _http("GET", f"/task/{wid}")
        if code != 200:
            print(f"HTTP {code}", file=sys.stderr)
            _print(body)
            return 1
        stage = body["workflow"]["stage"]
        if stage in ("completed", "failed"):
            _print(body)
            return 0 if stage == "completed" else 2
        time.sleep(0.2)
    print(f"timeout waiting for {wid}", file=sys.stderr)
    return 3


def cmd_artefacts(args: list) -> int:
    if not args:
        print("usage: cli artefacts <task_id>", file=sys.stderr)
        return 2
    code, body = _http("GET", f"/task/{args[0]}/artefacts")
    print(f"HTTP {code}")
    _print(body)
    return 0 if code == 200 else 1


def cmd_cat(args: list) -> int:
    if len(args) < 2:
        print("usage: cli cat <task_id> <rel_path>", file=sys.stderr)
        return 2
    wid, rel = args[0], "/".join(args[1:])
    code, body = _http("GET", f"/task/{wid}/artefacts/{rel}")
    print(f"HTTP {code}")
    if code == 200 and body.get("ok"):
        print(body.get("content", ""))
    else:
        _print(body)
    return 0 if code == 200 else 1


COMMANDS = {
    "health": cmd_health,
    "submit": cmd_submit,
    "get": cmd_get,
    "wait": cmd_wait,
    "artefacts": cmd_artefacts,
    "cat": cmd_cat,
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
