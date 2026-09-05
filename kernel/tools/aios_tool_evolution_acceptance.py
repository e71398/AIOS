#!/usr/bin/env python3
"""Acceptance checks for independently learning/upgradable tool chips."""
import json, subprocess, sys
from pathlib import Path

HOME = Path("${AIOS_HOME}"); TOOLS = HOME / "kernel/tools"; sys.path.insert(0, str(TOOLS))
from aios_tool_evolution import health_all, is_maintenance, learn_all, load_config, tool_names

checks = []
def check(name, ok, evidence=""): checks.append({"name": name, "ok": bool(ok), "evidence": str(evidence)[:500]})

cfg = load_config()
check("contract:stable-1.0", cfg.get("contract_version") == "1.0", cfg.get("contract_version"))
names = tool_names()
check("tools:registry-dynamic", set(cfg.get("tools", {})) == set(names), sorted(cfg.get("tools", {})))
services = [cfg["tools"][name].get("service") for name in names]
check("isolation:unique-services", len(services) == len(set(services)), services)

health = health_all()
check("health:all-tool-infrastructure", all(item.get("infrastructure_ok") for item in health.values()), health)
check("health:truthful-operational-fields", all("fully_operational" in item and "model_state" in item for item in health.values()), health)
check("maintenance:none", not any(is_maintenance(name) for name in names), names)

profiles = learn_all()
check("learning:independent-profiles", set(profiles) == set(names) and
      all((HOME / "knowledge/tool_learning" / name / "profile.json").is_file() for name in names),
      {name: profile.get("samples") for name, profile in profiles.items()})

executor = (TOOLS / "aios_executor_daemon.py").read_text(encoding="utf-8")
check("routing:maintenance-bypass", "is_maintenance(executor)" in executor,
      "executor skips only the upgrading chip")
check("learning:guidance-injected", "apply_guidance(executor, task_name)" in executor,
      "per-tool profile is injected after core boundary")

for timer in ("aios-tool-learning.timer", "aios-tool-upgrade.timer", "aios-tool-health-probe.timer"):
    enabled = subprocess.run(["systemctl", "--user", "is-enabled", timer], capture_output=True, text=True)
    active = subprocess.run(["systemctl", "--user", "is-active", timer], capture_output=True, text=True)
    check(f"timer:{timer}", enabled.stdout.strip() == "enabled" and active.stdout.strip() == "active",
          enabled.stdout + active.stdout)

models = subprocess.run(["systemctl", "--user", "is-active", "ollama.service", "llama-api.service"],
                        capture_output=True, text=True)
check("local-model:inactive", models.stdout.splitlines() == ["inactive", "inactive"], models.stdout)

report = {"schema": "aios-tool-evolution-acceptance/1.0",
          "passed": sum(c["ok"] for c in checks), "failed": sum(not c["ok"] for c in checks),
          "checks": checks}
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["failed"] == 0 else 1)
