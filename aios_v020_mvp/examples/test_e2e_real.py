"""End-to-end real-LLM test harness for AIOS v0.2.0 MVP closeout 029.

Drives LocalAI on http://127.0.0.1:8080/v1 with the
qwen2.5-coder-7b-instruct-q5_K_M model through five required
scenarios plus a restart-readback check. Records every real HTTP
call into _closeout_029/E2E_REAL_CALLS.jsonl.
"""
from __future__ import annotations

import io
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from aios_v020_mvp.providers import HTTPChatProvider  # noqa: E402


REAL_MODEL = "qwen2.5-coder-7b-instruct-q5_K_M"
LOCALAI_BASE = os.environ.get("LOCALAI_API_BASE", "http://127.0.0.1:8080/v1")
ARTIFACTS_DIR = os.path.abspath(
    os.path.join(ROOT, "_closeout_029", "e2e_real_seed")
)
E2E_LOG_PATH = os.path.abspath(
    os.path.join(ROOT, "_closeout_029", "E2E_REAL_CALLS.jsonl")
)
LOG: List[str] = []


def _log(line: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {line}"
    LOG.append(line)
    print(line, flush=True)


def _http_json(method, url, body=None, timeout=30.0):
    headers = {"Content-Type": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn_server(port, data_dir, env_extra):
    env = os.environ.copy()
    env["AIOS_MVP_OFFLINE"] = env_extra.get("AIOS_MVP_OFFLINE", "0")
    env["AIOS_MVP_HOST"] = "127.0.0.1"
    env["AIOS_MVP_PORT"] = str(port)
    env["AIOS_MVP_DATA_DIR"] = data_dir
    env["LOCALAI_API_BASE"] = LOCALAI_BASE
    env["LOCALAI_MODEL"] = REAL_MODEL
    env["AIOS_PROVIDER_TIMEOUT_S"] = "900"
    env["AIOS_PROVIDER_SINK"] = E2E_LOG_PATH
    env.pop("AIOS_MVP_ALLOW_NO_PROVIDER", None)
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("MINIMAX_API_KEY", None)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    for k, v in env_extra.items():
        env[k] = v
    # Use a temp .py file rather than `python -c` because `python -c`
    # parses the whole script as a single logical line 鈥?putting a
    # compound statement (while/for/if) after a `;` is a SyntaxError.
    # Write the launcher to a file we control so we can quote ROOT
    # safely and get readable tracebacks if the server crashes.
    import tempfile as _tempfile
    launcher_fd, launcher_path = _tempfile.mkstemp(prefix="aios_mvp_server_", suffix=".py")
    os.close(launcher_fd)
    with io.open(launcher_path, "w", encoding="utf-8") as _lf:
        _lf.write(
            "import sys, time\n"
            "sys.path.insert(0, %r)\n" % ROOT +
            "from aios_v020_mvp.config import load_config\n"
            "from aios_v020_mvp.server import build_gateway\n"
            "cfg = load_config()\n"
            "gw = build_gateway(cfg)\n"
            "gw.start()\n"
            "try:\n"
            "    while True:\n"
            "        time.sleep(1)\n"
            "except KeyboardInterrupt:\n"
            "    gw.shutdown()\n"
        )
    cmd = [sys.executable, launcher_path]
    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Stash the launcher path so the caller can clean it up.
    proc._launcher_path = launcher_path  # type: ignore[attr-defined]
    return proc


def _wait_for_health(base_url, timeout_s=30.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            code, body = _http_json("GET", f"{base_url}/health", timeout=2.0)
            if code == 200 and isinstance(body, dict) and body.get("ok"):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def _submit(base_url, payload):
    code, body = _http_json("POST", f"{base_url}/task", payload, timeout=20.0)
    assert code == 202, f"submit failed: {code} {body!r}"
    return str(body["task_id"])


def _wait_terminal(base_url, wid, timeout_s=900.0):
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        code, body = _http_json("GET", f"{base_url}/task/{wid}", timeout=10.0)
        assert code == 200, f"GET /task/{wid} failed: {code} {body!r}"
        last = body["workflow"]
        if last.get("stage") in ("completed", "failed"):
            return last
        time.sleep(0.5)
    raise TimeoutError(
        f"workflow {wid} did not finish in {timeout_s}s; last stage={last.get('stage')}"
    )

# ---------- Seed file builders ----------

def _seed_random_nonce_file(workdir):
    nonce = "nonce-" + "".join(random.choices("0123456789abcdef", k=16))
    path = os.path.join(workdir, "nonce_seed.txt")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(f"# seed: {nonce}\n")
    return path, nonce


def _seed_random_text_file(workdir, words=80):
    pool = [
        "ecosystem", "neural", "kernel", "service", "agent", "workflow",
        "module", "planner", "executor", "reviewer", "tool", "context",
        "token", "prompt", "router", "queue", "checkpoint", "manifest",
        "schema", "validator", "policy", "registry", "scheduler",
        "dispatcher", "telemetry", "audit", "trace",
    ]
    rnd = random.Random(os.urandom(16))
    body = " ".join(rnd.choice(pool) for _ in range(words))
    path = os.path.join(workdir, "text_seed.txt")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(f"# source paragraph (random seed)\n{body}\n")
    return path, body


def _seed_random_python_file(workdir):
    rnd = random.Random(os.urandom(16))
    var1 = "v_" + "".join(rnd.choices("abcdef", k=4))
    var2 = "v_" + "".join(rnd.choices("abcdef", k=4))
    fn = "fn_" + "".join(rnd.choices("abcdef", k=4))
    src = textwrap.dedent(
        f"""\
        # AIOS random python file
        def {fn}(x):
            {var1} = x
            {var2} = {var1} / 0
            return {var2}
        print({fn}(5))
        """
    )
    path = os.path.join(workdir, "buggy.py")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(src)
    return path, src


def _seed_random_config_file(workdir):
    rnd = random.Random(os.urandom(16))
    cfg = {
        "name": "aios-" + "".join(rnd.choices("0123456789", k=5)),
        "replicas": rnd.randint(0, 5),
        "image": "aios/aios:latest",
        "env": {
            "LOG_LEVEL": "INFO",
            "TIMEOUT_MS": str(rnd.choice([100, 200, 500, 1000])),
        },
        "ports": [18801],
    }
    path = os.path.join(workdir, "config.json")
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return path, json.dumps(cfg)


def _seed_random_topic_file(workdir):
    rnd = random.Random(os.urandom(16))
    topics = [
        "agent runtime", "kernel scheduler", "planner heuristics",
        "executor sandbox", "reviewer evidence gate",
        "tool registry", "telemetry pipeline",
    ]
    topic = rnd.choice(topics)
    path = os.path.join(workdir, "topic.txt")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(f"Topic: {topic}\n")
    return path, topic


# ---------- Scenario runners ----------

def _scenario_random_nonce(base_url, workdir):
    path, nonce = _seed_random_nonce_file(workdir)
    task = (
        f"Read the file at {path} and write a new file at "
        f"{workdir}/nonce_out.txt whose entire content is the literal "
        f"string after 'seed:' (a single token). Then read "
        f"{workdir}/nonce_out.txt and assert the content equals the seed."
    )
    wid = _submit(base_url, {"input": task})
    final = _wait_terminal(base_url, wid)
    return {"name": "random_nonce", "wid": wid, "final": final,
            "path": f"{workdir}/nonce_out.txt"}


def _scenario_summarize_random(base_url, workdir):
    path, body = _seed_random_text_file(workdir)
    task = (
        f"Read the file at {path} and write a one-sentence summary to "
        f"{workdir}/summary.txt using the file_write tool."
    )
    wid = _submit(base_url, {"input": task})
    final = _wait_terminal(base_url, wid)
    return {"name": "summarize_random", "wid": wid, "final": final,
            "path": f"{workdir}/summary.txt"}


def _scenario_python_errors(base_url, workdir):
    path, src = _seed_random_python_file(workdir)
    task = (
        f"Read the file at {path} and identify any syntax or logic "
        f"errors. Write a short report to {workdir}/python_review.txt "
        f"describing the error(s)."
    )
    wid = _submit(base_url, {"input": task})
    final = _wait_terminal(base_url, wid)
    return {"name": "python_errors", "wid": wid, "final": final,
            "path": f"{workdir}/python_review.txt"}


def _scenario_config_review(base_url, workdir):
    path, body = _seed_random_config_file(workdir)
    task = (
        f"Read the JSON config at {path} and write a reviewer-style "
        f"verdict to {workdir}/config_review.txt with bullet points "
        f"covering name, replicas, ports, and any risks you see."
    )
    wid = _submit(base_url, {"input": task})
    final = _wait_terminal(base_url, wid)
    return {"name": "config_review", "wid": wid, "final": final,
            "path": f"{workdir}/config_review.txt"}


def _scenario_markdown_report(base_url, workdir):
    path, topic = _seed_random_topic_file(workdir)
    task = (
        f"Read the file at {path} and produce a Markdown report (3-5 "
        f"bulleted sections) saved to {workdir}/report.md using the "
        f"file_write tool. The report must cover the topic."
    )
    wid = _submit(base_url, {"input": task})
    final = _wait_terminal(base_url, wid)
    return {"name": "markdown_report", "wid": wid, "final": final,
            "path": f"{workdir}/report.md"}


SCENARIOS = [
    _scenario_random_nonce,
    _scenario_summarize_random,
    _scenario_python_errors,
    _scenario_config_review,
    _scenario_markdown_report,
]


# ---------- Sink for HTTPChatProvider ----------

def _make_sink(jsonl_path):
    f = io.open(jsonl_path, "a", encoding="utf-8")

    def sink(rec):
        rec2 = dict(rec)
        f.write(json.dumps(rec2, ensure_ascii=False) + "\n")
        f.flush()

    return sink, f

def main():
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    io.open(E2E_LOG_PATH, "w", encoding="utf-8").close()

    # LocalAI probe
    try:
        code, body = _http_json("GET", f"{LOCALAI_BASE}/models", timeout=10.0)
        _log(f"LocalAI /v1/models -> {code}; models: {body}")
        assert code == 200, f"LocalAI returned {code}"
        models = body.get("data", body.get("models", []))
        if isinstance(models, dict):
            models = list(models.values())
        assert any(REAL_MODEL in (m.get("id") or m.get("name") or m.get("model") or "")
                   for m in models), f"{REAL_MODEL} not found on LocalAI"
    except Exception as exc:
        _log(f"FATAL: LocalAI probe failed: {exc!r}")
        return 2

    sink, sink_file = _make_sink(E2E_LOG_PATH)
    HTTPChatProvider._call_sink = sink

    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="aios_mvp_e2e_real_")
    _log(f"starting server on port {port}; data_dir={data_dir}")
    proc = _spawn_server(port, data_dir, {})
    base_url = f"http://127.0.0.1:{port}"
    try:
        ok = _wait_for_health(base_url, timeout_s=30.0)
        if not ok:
            _log("FATAL: server health probe timed out")
            return 3
        code, hbody = _http_json("GET", f"{base_url}/health", timeout=5.0)
        providers = hbody.get("providers", {})
        for role in ("planner", "executor", "reviewer"):
            assert providers[role]["provider"] == "localai", (
                f"{role} provider={providers[role]['provider']}, expected localai"
            )
            assert providers[role]["model"] == REAL_MODEL, (
                f"{role} model={providers[role]['model']}, expected {REAL_MODEL}"
            )
        _log(f"server ready; providers={providers}")

        results = []
        for scenario in SCENARIOS:
            name = scenario.__name__
            t0 = time.time()
            try:
                res = scenario(base_url, ARTIFACTS_DIR)
                elapsed = time.time() - t0
                final = res["final"]
                stage = final.get("stage")
                review = final.get("review") or {}
                verdict = review.get("verdict")
                _log(
                    f"{name}: stage={stage} verdict={verdict} "
                    f"elapsed={elapsed:.1f}s"
                )
                assert stage == "completed", f"{name} stage={stage} != completed"
                assert verdict == "accept", (
                    f"{name} review.verdict={verdict} != accept"
                )
                results.append({
                    "name": name, "status": "PASS",
                    "elapsed_s": round(elapsed, 2),
                    "stage": stage, "verdict": verdict,
                    "wid": res["wid"],
                    "input_tokens": (
                        final.get("execution", {})
                              .get("provider", {})
                              .get("input_tokens")),
                    "output_tokens": (
                        final.get("execution", {})
                              .get("provider", {})
                              .get("output_tokens")),
                    "artefacts": len(final.get("execution", {})
                                      .get("artefacts", []))})
            except Exception as exc:
                tb = traceback.format_exc()
                _log(f"{name}: FAIL: {exc}\n{tb}")
                results.append({"name": name, "status": "FAIL",
                                "error": str(exc), "trace": tb})

        # Restart and re-read
        _log("=== RESTART_READBACK scenario ===")
        last_wid = next(
            (r["wid"] for r in reversed(results) if "wid" in r), None
        )
        _log(f"killing server pid={proc.pid}")
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(2.0)

        port2 = _free_port()
        proc2 = _spawn_server(port2, data_dir, {})
        base_url2 = f"http://127.0.0.1:{port2}"
        ok = _wait_for_health(base_url2, timeout_s=30.0)
        assert ok, "restarted server failed to come up"
        if last_wid:
            code, body = _http_json("GET", f"{base_url2}/task/{last_wid}",
                                    timeout=10.0)
            assert code == 200, f"post-restart GET failed: {code} {body!r}"
            doc = body["workflow"]
            _log(
                f"post-restart GET /task/{last_wid}: "
                f"stage={doc['stage']} verdict={doc.get('review', {}).get('verdict')}"
            )
            assert doc["stage"] == "completed"
            assert doc["review"]["verdict"] == "accept"
        results.append({"name": "restart_readback", "status": "PASS"})
        proc2.kill()
        try:
            proc2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc2.kill()
    finally:
        HTTPChatProvider._call_sink = None
        sink_file.close()
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass

    pass_count = sum(1 for r in results if r["status"] == "PASS")
    total = len(results)
    _log(f"=== REAL E2E SUMMARY: {pass_count}/{total} scenarios passed ===")
    for r in results:
        line = f"  [{r['status']}] {r['name']}"
        if "elapsed_s" in r:
            line += f" ({r['elapsed_s']}s)"
        if "error" in r:
            line += f" -- {r['error']}"
        _log(line)
    log_path = os.path.abspath(
        os.path.join(ROOT, "_closeout_029", "REAL_E2E_5of5.log")
    )
    with io.open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")
    return 0 if pass_count == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
