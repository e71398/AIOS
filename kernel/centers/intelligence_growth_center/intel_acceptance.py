#!/usr/bin/env python3
"""Focused acceptance checks for AIOS Intelligence & Growth Center."""
import json, sqlite3, subprocess, sys, urllib.error, urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "intel.db"
checks = []

def check(name, ok, evidence=""):
    checks.append({"name": name, "ok": bool(ok), "evidence": str(evidence)[:500]})

def http(path, method="GET"):
    request = urllib.request.Request(f"http://127.0.0.1:8848{path}", method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())

try:
    status, health = http("/health")
    check("http:health", status == 200 and health.get("ok") is True, health)
except Exception as exc:
    check("http:health", False, exc)

try:
    status, stats = http("/api/stats")
    modules = stats.get("by_module", {})
    check("data:three-modules", status == 200 and all(modules.get(m, 0) > 0 for m in
          ("open_source", "global_intel", "opportunity")), modules)
except Exception as exc:
    check("data:three-modules", False, exc)

try:
    urllib.request.urlopen("http://127.0.0.1:8848/api/scan", timeout=5)
    check("api:scan-requires-post", False, "GET unexpectedly accepted")
except urllib.error.HTTPError as exc:
    check("api:scan-requires-post", exc.code == 405, exc.code)
except Exception as exc:
    check("api:scan-requires-post", False, exc)

conn = sqlite3.connect(DB)
fetch_rows = conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
check("audit:fetch-log-populated", fetch_rows > 0, fetch_rows)
latest = dict(conn.execute("""SELECT source,status FROM fetch_log f WHERE id IN
    (SELECT MAX(id) FROM fetch_log GROUP BY source)""").fetchall())
active_sources = {"github_trending", "github_trending_weekly", "hackernews",
                  "techcrunch_rss", "arxiv_ai", "lobsters_rss",
                  "devto_startups", "producthunt"}
check("sources:latest-success", active_sources.issubset(latest) and
      all(latest.get(source) == "ok" for source in active_sources), latest)
conn.close()

cron = subprocess.run([sys.executable, str(BASE / "intel_cron.py"), "invalid"],
                      capture_output=True, text=True, timeout=10)
check("cron:command-validation", cron.returncode == 2, cron.stderr)

hermes_env = Path.home() / ".hermes/.env"
env_keys = set()
if hermes_env.is_file():
    for line in hermes_env.read_text(errors="ignore").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            env_keys.add(line.split("=", 1)[0].strip())
needed = {"FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_HOME_CHANNEL"}
check("notify:hermes-config", needed.issubset(env_keys), sorted(needed & env_keys))

opportunity_source = (BASE / "opportunity_radar.py").read_text()
check("notify:openclaw-isolated", ".openclaw" not in opportunity_source.lower() and
      "OPENCLAW_CLI" not in opportunity_source, "Hermes-only notification adapter")

models = subprocess.run(["systemctl", "--user", "is-active",
                         "ollama.service", "llama-api.service"],
                        capture_output=True, text=True)
states = models.stdout.splitlines()
check("local-model:inactive", states == ["inactive", "inactive"], states)

service = subprocess.run(["systemctl", "--user", "is-active", "aios-intel.service"],
                         capture_output=True, text=True)
check("service:active", service.stdout.strip() == "active", service.stdout)

report = {"schema": "aios-intel-acceptance/1.0",
          "passed": sum(c["ok"] for c in checks),
          "failed": sum(not c["ok"] for c in checks), "checks": checks}
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["failed"] == 0 else 1)
