"""Example client demonstrating the AIOS v0.2.0 MVP HTTP flow.

Boots the gateway in a background thread, submits three
representative tasks, polls for completion, lists artefacts
and prints the artefacts back.  Useful as a smoke check
without depending on curl.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(ROOT))


def _http(method: str, url: str, body: Any = None, timeout: float = 15.0) -> Tuple[int, Dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"ok": False, "error": raw}


def _wait(base: str, wid: str, timeout_s: float = 30.0) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        code, body = _http("GET", f"{base}/task/{wid}")
        if code != 200:
            raise RuntimeError(f"GET /task/{wid} failed: {code} {body}")
        last = body["workflow"]
        if last["stage"] in ("completed", "failed"):
            return last
        time.sleep(0.2)
    raise TimeoutError(f"workflow {wid} did not finish in {timeout_s}s")


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="aios_mvp_example_")
    port = int(os.environ.get("AIOS_MVP_EXAMPLE_PORT", "18996"))
    os.environ["AIOS_MVP_DATA_DIR"] = data_dir
    os.environ["AIOS_MVP_OFFLINE"] = "1"
    os.environ["AIOS_MVP_HOST"] = "127.0.0.1"
    os.environ["AIOS_MVP_PORT"] = str(port)

    from aios_v020_mvp.config import load_config
    from aios_v020_mvp.server import build_gateway

    cfg = load_config()
    gw = build_gateway(cfg)
    gw.start()
    base = f"http://127.0.0.1:{port}"

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.1)

    tasks = [
        "Write a file called intro.md with a one-paragraph intro to AIOS v0.2.0.",
        "Summarize AIOS in one sentence.",
        "Write a file called plan.md with a 3-step release plan.",
    ]

    try:
        for t in tasks:
            print(f"\n>>> POST /task  input={t!r}")
            code, body = _http("POST", f"{base}/task", {"input": t})
            print(f"<<< HTTP {code} {body}")
            wid = body["task_id"]
            final = _wait(base, wid)
            print(f"    stage={final['stage']} verdict={final['review']['verdict']}")
            code, lst = _http("GET", f"{base}/task/{wid}/artefacts")
            print(f"    artefacts: {lst.get('count', 0)}")
            for art in lst.get("items", []):
                rel = art["path"]
                code, content = _http("GET", f"{base}/task/{wid}/artefacts/{rel}")
                if code == 200:
                    preview = (content.get("content") or "").splitlines()[0]
                    print(f"      - {rel} ({art['size_bytes']} bytes): {preview[:80]!r}")
        print("\nEXAMPLE OK")
        return 0
    finally:
        gw.shutdown()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
