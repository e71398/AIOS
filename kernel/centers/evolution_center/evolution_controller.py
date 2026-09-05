#!/usr/bin/env python3
"""AIOS controlled evolution state machine.

External intelligence and execution experience may create proposals, but only the
owner can approve sandbox implementation and separately authorize deployment.
"""
import argparse, hashlib, json, os, shutil, sqlite3, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.getenv("AIOS_HOME", "${AIOS_HOME}")).resolve()
TOOLS = HOME / "kernel/tools"
EVOLUTION = HOME / "kernel/centers/evolution_center"
INTEL = HOME / "kernel/centers/intelligence_growth_center"
PROPOSALS = HOME / "knowledge/pending_approval/evolution"
WORKSPACES = HOME / "sandbox/coding/evolution"
AUDIT = HOME / "logs/evolution"
UPGRADES = HOME / "checkpoint/upgrades"
for path in (TOOLS, EVOLUTION, INTEL):
    sys.path.insert(0, str(path))

ALLOWED_TARGET_ROOTS = [HOME / p for p in ("kernel", "config", "docs", "agents", "core")]
ALLOWED_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".md", ".sh", ".txt"}
BLOCKED_PARTS = {".env", "secret", "token", "auth", "credential", "intel.db",
                 "checkpoint", "logs", "cache"}
RELEVANCE = {"ai", "agent", "llm", "rag", "workflow", "orchestrat", "security",
             "monitor", "backup", "plugin", "tool", "automation", "sandbox"}
CORE_RELEVANCE = ("aios core", "aios核心", "event bus", "总线协议",
                  "adapter contract", "适配器合同", "control plane", "控制面",
                  "core protocol", "核心协议")

def now(): return datetime.now(timezone.utc).isoformat()

def _audit(action: str, proposal_id: str = "", detail=None):
    AUDIT.mkdir(parents=True, exist_ok=True)
    record = {"ts": now(), "action": action, "proposal_id": proposal_id,
              "detail": detail or {}}
    with (AUDIT / "evolution.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

def _proposal_path(proposal_id: str) -> Path:
    if not proposal_id.startswith("EV-") or "/" in proposal_id or "\\" in proposal_id:
        raise ValueError("invalid proposal id")
    return PROPOSALS / f"{proposal_id}.json"

def _write(proposal: dict):
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    target = _proposal_path(proposal["proposal_id"])
    proposal["updated_at"] = now()
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)

def get_proposal(proposal_id: str) -> dict:
    path = _proposal_path(proposal_id)
    if not path.is_file(): raise FileNotFoundError(proposal_id)
    return json.loads(path.read_text(encoding="utf-8"))

def list_proposals(status: str = "") -> list:
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    proposals = []
    for path in PROPOSALS.glob("EV-*.json"):
        try: proposal = json.loads(path.read_text(encoding="utf-8"))
        except Exception: continue
        if not status or proposal.get("status") == status: proposals.append(proposal)
    return sorted(proposals, key=lambda p: p.get("created_at", ""), reverse=True)

def create_proposal(kind: str, title: str, summary: str, evidence=None,
                    source: str = "system", risk: str = "L4") -> tuple[dict, bool]:
    fingerprint = hashlib.sha256(
        f"{kind}|{title.strip().lower()}|{summary.strip().lower()}".encode()).hexdigest()[:12]
    for proposal in list_proposals():
        if proposal.get("fingerprint") == fingerprint:
            return proposal, False
    proposal_id = f"EV-{datetime.now().strftime('%Y%m%d')}-{fingerprint[:8]}"
    proposal = {"schema": "aios-evolution-proposal/1.0", "proposal_id": proposal_id,
        "fingerprint": fingerprint, "kind": kind, "title": title[:160],
        "summary": summary[:2000], "evidence": evidence or {}, "source": source,
        "risk": risk, "status": "pending_approval", "created_at": now(),
        "updated_at": now(), "implementation_task_id": "", "history": [
            {"ts": now(), "state": "pending_approval", "actor": "evolution-controller"}]}
    _write(proposal); _audit("proposal_created", proposal_id, {"kind": kind, "source": source})
    return proposal, True

def _notify(text: str) -> dict:
    try:
        from opportunity_radar import _send_hermes_feishu
        return _send_hermes_feishu([], text)
    except Exception as exc:
        return {"sent": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}

def notify_pending(proposals: list):
    if not proposals: return {"sent": False, "reason": "nothing_new"}
    lines = ["🧬 **AIOS 进化提案待审批**"]
    for proposal in proposals[:5]:
        lines.append(f"• `{proposal['proposal_id']}` {proposal['title']}")
    lines.append("请在 8848 进化提案页审阅。批准只允许在 sandbox 制作候选，部署仍需二次确认。")
    result = _notify("\n".join(lines)); _audit("pending_notified", detail=result)
    return result

def scan_intelligence(hours: int = 24) -> list:
    db = INTEL / "intel.db"
    if not db.is_file(): return []
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute("""SELECT source,title,url,summary,module,score,created_at
        FROM intel_items WHERE created_at>=? AND score>=4.0
        ORDER BY score DESC,created_at DESC LIMIT 30""", (cutoff,)).fetchall()
    conn.close(); created = []
    for row in rows:
        text = f"{row['title']} {row['summary']}".lower()
        if not any(keyword in text for keyword in RELEVANCE): continue
        try:
            from aios_tool_evolution import ingest_intelligence
            ingest_intelligence({"type": "intel.discovered", "source": f"intel:{row['source']}",
                "payload": {"title": row["title"], "summary": row["summary"],
                            "url": row["url"], "score": row["score"]}})
        except Exception: pass
        if not any(term in text for term in CORE_RELEVANCE):
            continue  # ordinary capability intelligence belongs to tool-local learning
        proposal, is_new = create_proposal(
            "external_capability", f"评估并吸收外部能力：{row['title']}",
            "研究该外部能力是否能以可替换芯片方式补全 AIOS；只生成候选设计，不自动安装。",
            {"url": row["url"], "score": row["score"], "module": row["module"]},
            f"intel:{row['source']}")
        if is_new: created.append(proposal)
        if len(created) >= 5: break
    return created

def ingest_hermes_analysis(analysis: dict, notify: bool = True) -> list:
    created = []
    try:
        from aios_hermes_learn import _detect_knowledge_gaps
        gaps = _detect_knowledge_gaps(analysis.get("failure_analysis", {}), analysis.get("skills", []))
    except Exception:
        gaps = []
    for gap in gaps[:5]:
        try:
            from aios_tool_evolution import ingest_intelligence
            ingest_intelligence({"source": "hermes-learning", "payload": {
                "title": "Hermes execution gap", "summary": gap, "score": 5.0}})
        except Exception: pass
        if not any(term.lower() in gap.lower() for term in CORE_RELEVANCE):
            continue
        proposal, is_new = create_proposal("execution_gap", f"补全执行能力：{gap[:100]}", gap,
            {"hermes_analysis": True}, "hermes-learning")
        if is_new: created.append(proposal)
    for hint in analysis.get("calibration", {}).get("router_hints", [])[:3]:
        detail = hint.get("detail", "")
        if not detail: continue
        if not any(term.lower() in detail.lower() for term in CORE_RELEVANCE):
            continue
        proposal, is_new = create_proposal("calibration", f"校准建议：{detail[:100]}", detail,
            hint, "hermes-calibration")
        if is_new: created.append(proposal)
    if notify: notify_pending(created)
    return created

def ingest_event(event: dict) -> dict:
    event_type = event.get("type", ""); payload = event.get("payload", {})
    if event_type not in ("intel.discovered", "opportunity.discovered"):
        return {"created": 0, "reason": "unsupported_event"}
    title = payload.get("title", "")
    summary = payload.get("summary", "")
    score = float(payload.get("score", 0) or 0)
    text = f"{title} {summary}".lower()
    if score < 4.0 or not any(keyword in text for keyword in RELEVANCE):
        return {"created": 0, "reason": "below_relevance_gate"}
    proposal, is_new = create_proposal("intelligence_event", f"情报能力候选：{title}",
        "由情报事件触发。需先研究适配价值与模块边界。", payload,
        event.get("source", "intel-event"))
    if is_new: notify_pending([proposal])
    return {"created": int(is_new), "proposal_id": proposal["proposal_id"]}

def approve(proposal_id: str, actor: str = "owner") -> dict:
    from aios_bus import enqueue_task
    proposal = get_proposal(proposal_id)
    if proposal["status"] != "pending_approval":
        raise ValueError(f"proposal is {proposal['status']}, not pending_approval")
    workspace = (WORKSPACES / proposal_id).resolve(); workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "proposal.json").write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    task_name = (f"在 {workspace} 中研究提案 {proposal_id}，生成 candidate_manifest.json 和候选文件；"
                 "只允许写该目录，不得安装、部署或写其他目录。")
    context = (proposal["summary"] + "\nmanifest必须包含proposal_id、target、candidate、service和tests；"
               "target是建议位置，candidate必须位于本提案目录。")
    task_id = enqueue_task(task_name, system="claude", priority=2, logic_depth="high",
        source="evolution_owner_approved", context=context,
        verification_criteria=["所有产物仅在提案sandbox", "候选文件语法有效", "包含candidate_manifest.json"])
    if not task_id: raise RuntimeError("failed to enqueue implementation task")
    proposal.update({"status": "dispatched", "implementation_task_id": task_id,
                     "approved_by": actor, "approved_at": now(), "workspace": str(workspace)})
    proposal["history"].append({"ts": now(), "state": "dispatched", "actor": actor,
                                "task_id": task_id})
    _write(proposal); _audit("implementation_approved", proposal_id, {"task_id": task_id, "actor": actor})
    return proposal

def reject(proposal_id: str, reason: str = "owner rejected", actor: str = "owner") -> dict:
    proposal = get_proposal(proposal_id)
    if proposal["status"] not in ("pending_approval", "implementation_incomplete"):
        raise ValueError(f"cannot reject from {proposal['status']}")
    proposal.update({"status": "rejected", "rejected_by": actor, "rejection_reason": reason})
    proposal["history"].append({"ts": now(), "state": "rejected", "actor": actor, "reason": reason})
    _write(proposal); _audit("proposal_rejected", proposal_id, {"reason": reason})
    return proposal

def _validate_manifest(proposal: dict) -> tuple[bool, str, dict]:
    workspace = Path(proposal["workspace"]).resolve(); manifest_path = workspace / "candidate_manifest.json"
    if not manifest_path.is_file(): return False, "candidate_manifest.json missing", {}
    try: manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc: return False, f"invalid manifest: {exc}", {}
    if manifest.get("proposal_id") != proposal["proposal_id"]: return False, "proposal_id mismatch", manifest
    candidate = Path(manifest.get("candidate", "")).resolve()
    target = Path(manifest.get("target", "")).resolve()
    if workspace not in candidate.parents or not candidate.is_file():
        return False, "candidate must be a file inside proposal workspace", manifest
    if not any(root == target.parent or root in target.parents for root in ALLOWED_TARGET_ROOTS):
        return False, "target outside approved AIOS module roots", manifest
    lower = str(target).lower()
    if target.suffix.lower() not in ALLOWED_SUFFIXES or any(part in lower for part in BLOCKED_PARTS):
        return False, "target type/path is blocked", manifest
    from aios_safe_upgrade import validate
    validation = validate(candidate)
    if validation.returncode: return False, f"candidate validation failed: {validation.stderr}", manifest
    from aios_enforcer import pre_exec_safety_check
    wm_ok, wm_reason = pre_exec_safety_check({"task_id": proposal["proposal_id"],
        "task_name": f"deploy candidate to {target}", "logic_depth": "high"})
    if not wm_ok or wm_reason != "wm_approved":
        return False, f"World Model approval required: {wm_reason}", manifest
    return True, "candidate and World Model approved", manifest

def reconcile() -> list:
    from aios_bus import get_task_state
    from aios_verification_gate import verify_specific
    changed = []
    for proposal in list_proposals("dispatched"):
        state = get_task_state(proposal.get("implementation_task_id", ""))
        status = state.get("status", "unknown")
        if status in ("failed", "cancelled"):
            proposal["status"] = "implementation_failed"; proposal["result"] = state
        elif status in ("completed", "verified"):
            verified = verify_specific(proposal["implementation_task_id"])
            if not verified.get("passed"):
                proposal["status"] = "validation_failed"; proposal["validation"] = verified
            else:
                ok, reason, manifest = _validate_manifest(proposal)
                proposal["validation"] = {"passed": ok, "reason": reason}
                proposal["candidate_manifest"] = manifest
                proposal["status"] = "awaiting_deploy_approval" if ok else "implementation_incomplete"
        else: continue
        proposal["history"].append({"ts": now(), "state": proposal["status"], "actor": "reconciler"})
        _write(proposal); _audit("proposal_reconciled", proposal["proposal_id"], {"status": proposal["status"]})
        changed.append(proposal)
        _notify(f"🧬 AIOS 进化提案 `{proposal['proposal_id']}` 状态：**{proposal['status']}**")
    return changed

def deploy(proposal_id: str, confirmation: str, actor: str = "owner") -> dict:
    if confirmation != f"DEPLOY:{proposal_id}": raise PermissionError("explicit deployment confirmation required")
    proposal = get_proposal(proposal_id)
    if proposal["status"] != "awaiting_deploy_approval":
        raise ValueError(f"proposal is {proposal['status']}, not awaiting_deploy_approval")
    ok, reason, manifest = _validate_manifest(proposal)
    if not ok: raise RuntimeError(reason)
    target = Path(manifest["target"]).resolve(); candidate = Path(manifest["candidate"]).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists(); upgrade_record = None
    if existed:
        from aios_safe_upgrade import upgrade
        applied = upgrade(target, candidate, manifest.get("service"))
        records = sorted(UPGRADES.glob(f"{target.name}.*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        upgrade_record = records[0] if records else None
    else:
        staged = target.with_name(target.name + ".staged")
        shutil.copy2(candidate, staged); os.replace(staged, target); applied = True
    regression = subprocess.run(["python3", str(TOOLS / "aios_acceptance.py")],
                                capture_output=True, text=True, timeout=180)
    rolled_back = False
    if not (applied and regression.returncode == 0):
        try:
            if existed and upgrade_record:
                from aios_safe_upgrade import rollback
                rollback(upgrade_record)
            elif not existed and target.exists():
                target.unlink()
            rolled_back = True
        except Exception: pass
    proposal["deployment"] = {"target": str(target), "actor": actor, "ts": now(),
        "applied": bool(applied), "regression_rc": regression.returncode,
        "rolled_back": rolled_back, "regression_tail": (regression.stdout + regression.stderr)[-1000:]}
    proposal["status"] = "deployed" if applied and regression.returncode == 0 else (
        "deployment_rolled_back" if rolled_back else "deployment_failed")
    proposal["history"].append({"ts": now(), "state": proposal["status"], "actor": actor})
    _write(proposal); _audit("deployment_finished", proposal_id, proposal["deployment"])
    _notify(f"🧬 AIOS 进化提案 `{proposal_id}` 部署结果：**{proposal['status']}**")
    return proposal

def review() -> dict:
    created = scan_intelligence(); reconciled = reconcile(); notification = notify_pending(created)
    return {"created": len(created), "reconciled": len(reconciled), "notification": notification,
            "pending": len(list_proposals("pending_approval")),
            "awaiting_deploy": len(list_proposals("awaiting_deploy_approval"))}

def status() -> dict:
    counts = {}
    for proposal in list_proposals(): counts[proposal["status"]] = counts.get(proposal["status"], 0) + 1
    return {"ok": True, "counts": counts, "proposal_dir": str(PROPOSALS),
            "workspace_dir": str(WORKSPACES), "auto_deploy": False,
            "owner_approval_required": True, "deployment_confirmation_required": True}

def main():
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("review"); sub.add_parser("status"); sub.add_parser("list")
    a = sub.add_parser("approve"); a.add_argument("proposal_id"); a.add_argument("--actor", default="owner")
    r = sub.add_parser("reject"); r.add_argument("proposal_id"); r.add_argument("--reason", default="owner rejected")
    d = sub.add_parser("deploy"); d.add_argument("proposal_id"); d.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.cmd == "review": result = review()
    elif args.cmd == "status": result = status()
    elif args.cmd == "list": result = list_proposals()
    elif args.cmd == "approve": result = approve(args.proposal_id, args.actor)
    elif args.cmd == "reject": result = reject(args.proposal_id, args.reason)
    else: result = deploy(args.proposal_id, args.confirm)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
