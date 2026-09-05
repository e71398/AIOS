#!/usr/bin/env python3
"""Acceptance checks for the two-layer AIOS evolution boundary."""
import json, subprocess, sys, tempfile
from pathlib import Path

HOME = Path("${AIOS_HOME}"); TOOLS = HOME / "kernel/tools"
EVOLUTION = HOME / "kernel/centers/evolution_center"
sys.path[:0] = [str(TOOLS), str(EVOLUTION)]
import evolution_controller as controller
from aios_tool_evolution import TOOL_NAMES, health_all

checks = []
def check(name, ok, evidence=""): checks.append({"name": name, "ok": bool(ok), "evidence": str(evidence)[:500]})

state = controller.status()
check("core:auto-deploy-disabled", state.get("auto_deploy") is False, state)
check("core:two-owner-gates", state.get("owner_approval_required") and
      state.get("deployment_confirmation_required"), state)

with tempfile.TemporaryDirectory(prefix="aios-evolution-test-") as temp:
    root = Path(temp); old = (controller.PROPOSALS, controller.AUDIT)
    controller.PROPOSALS = root / "proposals"; controller.AUDIT = root / "audit"
    first, created = controller.create_proposal("test", "contract boundary test", "no production write")
    second, duplicate = controller.create_proposal("test", "contract boundary test", "no production write")
    check("proposal:atomic-and-deduplicated", created and not duplicate and
          first["proposal_id"] == second["proposal_id"], first["proposal_id"])
    rejected = controller.reject(first["proposal_id"], "acceptance test", "test-owner")
    check("proposal:owner-reject", rejected["status"] == "rejected", rejected["status"])
    try:
        controller.deploy(first["proposal_id"], "wrong-confirmation", "test-owner")
        guarded = False
    except PermissionError: guarded = True
    check("deployment:explicit-confirmation", guarded, "wrong confirmation denied")
    controller.PROPOSALS, controller.AUDIT = old

tools = health_all()
check("tools:independent-health", all(x.get("ok") for x in tools.values()), tools)
check("tools:five-chips", set(tools) == set(TOOL_NAMES), sorted(tools))

event_source = (TOOLS / "aios_event_daemon.py").read_text(encoding="utf-8")
check("events:intelligence-to-tool-learning", "ingest_intelligence" in event_source,
      "intel events route to tool profiles")
check("events:core-relevance-gate", "core_terms" in event_source,
      "only core-contract intelligence creates proposals")

web = (HOME / "kernel/centers/intelligence_growth_center/web_server.py").read_text(encoding="utf-8")
check("ui:owner-controls", all(path in web for path in ("/approve", "/reject", "/deploy")),
      "8848 approve/reject/deploy endpoints")

timer = subprocess.run(["systemctl", "--user", "is-enabled", "aios-evolution-review.timer"],
                       capture_output=True, text=True)
active = subprocess.run(["systemctl", "--user", "is-active", "aios-evolution-review.timer"],
                        capture_output=True, text=True)
check("timer:evolution-review", timer.stdout.strip() == "enabled" and active.stdout.strip() == "active",
      timer.stdout + active.stdout)

models = subprocess.run(["systemctl", "--user", "is-active", "ollama.service", "llama-api.service"],
                        capture_output=True, text=True)
check("local-model:inactive", models.stdout.splitlines() == ["inactive", "inactive"], models.stdout)

report = {"schema": "aios-evolution-acceptance/1.0", "passed": sum(c["ok"] for c in checks),
          "failed": sum(not c["ok"] for c in checks), "checks": checks}
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["failed"] == 0 else 1)
