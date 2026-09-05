#!/usr/bin/env python3
"""Prove a future AI chip is discovered by configuration only."""
import json, os, shutil, subprocess, tempfile
from pathlib import Path

LIVE = Path("${AIOS_HOME}")
checks = []
def check(name, ok, evidence=""):
    checks.append({"name":name,"ok":bool(ok),"evidence":str(evidence)[:500]})

with tempfile.TemporaryDirectory(prefix="aios-future-tool-") as raw:
    home = Path(raw); (home / "config").mkdir(); (home / "cache").mkdir(); (home / "sandbox").mkdir()
    adapters = json.loads((LIVE / "config/tool_adapters.json").read_text(encoding="utf-8"))
    lifecycle = json.loads((LIVE / "config/tool_lifecycle.json").read_text(encoding="utf-8"))
    adapters["tools"]["future_ai"] = {
        "enabled": True, "label": "Future AI", "model": "test-model",
        "provider": "test-provider", "executable": "/bin/echo",
        "version_args": ["future-ai 1.0"], "inference_required": True,
        "probe_args": ["{prompt}"], "probe_success_marker": "AIOS_OK",
        "probe_timeout_seconds": 5,
    }
    package = home / "future-package"; package.mkdir()
    lifecycle["tools"]["future_ai"] = {
        "package":"future-ai", "package_path":str(package), "service":"",
        "upgrade_command":["/bin/true"], "latest_command":["/bin/true"],
        "auto_binary_upgrade":False, "learning":True,
        "learning_keywords":["future"],
    }
    (home / "config/tool_adapters.json").write_text(json.dumps(adapters), encoding="utf-8")
    (home / "config/tool_lifecycle.json").write_text(json.dumps(lifecycle), encoding="utf-8")
    env = dict(os.environ); env["AIOS_HOME"] = str(home)
    adapter = LIVE / "kernel/tools/aios_tool_adapter.py"
    probe = subprocess.run(["python3",str(adapter),"probe","future_ai","--force"],env=env,text=True,capture_output=True)
    health = subprocess.run(["python3",str(adapter),"health"],env=env,text=True,capture_output=True)
    probe_data = json.loads(probe.stdout)["future_ai"]
    health_data = json.loads(health.stdout)
    check("future:probe-real", probe.returncode == 0 and probe_data.get("model_available"), probe_data)
    check("future:auto-discovered-by-adapter", "future_ai" in health_data and len(health_data)==len(adapters["tools"]), health_data.keys())
    code = ("import sys;sys.path.insert(0,'${AIOS_HOME}/kernel/tools');"
            "from aios_tool_evolution import tool_names;print(','.join(tool_names()))")
    evolution = subprocess.run(["python3","-c",code],env=env,text=True,capture_output=True)
    check("future:auto-discovered-by-lifecycle", "future_ai" in evolution.stdout.strip().split(","), evolution.stdout)

report={"schema":"aios-dynamic-tool-acceptance/1.0",
        "passed":sum(x["ok"] for x in checks),"failed":sum(not x["ok"] for x in checks),"checks":checks}
print(json.dumps(report,ensure_ascii=False,indent=2))
raise SystemExit(0 if report["failed"]==0 else 1)
