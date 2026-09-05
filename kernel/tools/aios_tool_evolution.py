#!/usr/bin/env python3
"""Independent learning and rolling upgrades for replaceable AI tool chips."""
import argparse, json, os, re, shutil, subprocess, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.getenv("AIOS_HOME", "${AIOS_HOME}")).resolve()
TOOLS = HOME / "kernel/tools"; sys.path.insert(0, str(TOOLS))
CONFIG = HOME / "config/tool_lifecycle.json"
PROFILES = HOME / "knowledge/tool_learning"
OUTCOMES = HOME / "logs/tool_learning"
MAINTENANCE = HOME / "cache/tool_maintenance"
BACKUPS = HOME / "checkpoint/tool_upgrades"
HISTORY = HOME / "logs/tool_upgrades"
INTELLIGENCE = HOME / "knowledge/tool_learning/intelligence"

def now(): return datetime.now(timezone.utc).isoformat()
def load_config():
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    if str(data.get("contract_version")) not in ("1.0", "1.1"): raise ValueError("unsupported adapter contract")
    return data

def tool_names(): return tuple(load_config().get("tools", {}).keys())

# Compatibility export. All runtime loops call tool_names(), so newly registered
# tools are discovered without editing this module.
TOOL_NAMES = tool_names()

def _tool(name):
    tools = load_config()["tools"]
    if name not in tools: raise KeyError(name)
    return tools[name]

def is_maintenance(name: str) -> bool: return (MAINTENANCE / name).exists()
def _set_maintenance(name: str, enabled: bool, reason: str = ""):
    MAINTENANCE.mkdir(parents=True, exist_ok=True); marker = MAINTENANCE / name
    if enabled: marker.write_text(json.dumps({"ts": now(), "reason": reason}), encoding="utf-8")
    elif marker.exists(): marker.unlink()
    try:
        from aios_bus import _is_available, _redis_client
        if _is_available():
            key = f"aios:tool:maintenance:{name}"
            if enabled: _redis_client.set(key, reason or "upgrade", ex=3600)
            else: _redis_client.delete(key)
    except Exception: pass

def _env():
    env = dict(os.environ); env["PATH"] = "${HOME}/.n/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    return env

def _run(command, timeout=300, cwd=None):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                          cwd=cwd, env=_env(), shell=False)

def _service_active(unit: str) -> bool:
    if not unit: return True
    result = _run(["systemctl", "--user", "is-active", unit], 20)
    return result.stdout.strip() == "active"

def health(name: str) -> dict:
    from aios_tool_adapter import get_adapter
    adapter = get_adapter(name); result = adapter.health()
    unit = _tool(name).get("service", "")
    result.update({"service": unit, "service_active": _service_active(unit),
                   "maintenance": is_maintenance(name)})
    result["infrastructure_ok"] = bool(result.get("contract_ok") and result["service_active"] and not result["maintenance"])
    result["fully_operational"] = bool(result["infrastructure_ok"] and result.get("model_available"))
    result["ok"] = result["fully_operational"]
    result["state"] = "operational" if result["fully_operational"] else ("degraded" if result["infrastructure_ok"] else "offline")
    return result

def health_all(): return {name: health(name) for name in tool_names()}

def record_outcome(tool: str, status: str, task_name: str = "", summary: str = "", task_id: str = ""):
    if tool not in tool_names() or status not in ("verified", "rejected"):
        return False
    OUTCOMES.mkdir(parents=True, exist_ok=True)
    record = {"ts": now(), "tool": tool, "status": status, "task_id": task_id,
              "task_name": task_name[:300], "summary": summary[:500]}
    with (OUTCOMES / f"{tool}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    learn(tool)
    return True

def _read_outcomes(tool: str, limit=200):
    path = OUTCOMES / f"{tool}.jsonl"
    if not path.is_file(): return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]:
        try: rows.append(json.loads(line))
        except Exception: pass
    return rows

def ingest_intelligence(event: dict) -> dict:
    """Route external intelligence into tool-local learning, never into core files."""
    payload = event.get("payload", event)
    text = f"{payload.get('title','')} {payload.get('summary','')}".lower()
    defaults = {
        "openclaw": ("orchestrat", "workflow", "gateway", "feishu", "channel", "dispatch"),
        "hermes": ("learn", "calibrat", "knowledge", "memory", "evaluation"),
        "opencode": ("cli", "script", "plugin", "tool", "automation"),
        "claude": ("architect", "audit", "security", "reasoning", "design"),
        "codex": ("batch", "parallel", "migration", "repository", "codegen"),
    }
    routing = {name: tuple(cfg.get("learning_keywords", defaults.get(name, ())))
               for name, cfg in load_config()["tools"].items()}
    targets = [name for name, words in routing.items() if any(word in text for word in words)]
    if not targets and float(payload.get("score", 0) or 0) >= 4.5: targets = ["opencode", "claude"]
    INTELLIGENCE.mkdir(parents=True, exist_ok=True)
    record = {"ts": now(), "title": payload.get("title", "")[:200],
              "summary": payload.get("summary", "")[:500], "url": payload.get("url", ""),
              "score": payload.get("score", 0), "source": event.get("source", "intel")}
    for name in targets:
        with (INTELLIGENCE / f"{name}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        learn(name)
    return {"accepted": bool(targets), "tools": targets}

def _historic_states(limit=1000):
    rows = []
    try:
        from aios_bus import _is_available, _redis_client
        if not _is_available(): return rows
        for key in _redis_client.scan_iter(match="aios:bus:state:*", count=200):
            raw = _redis_client.hgetall(key)
            row = {(k.decode() if isinstance(k, bytes) else str(k)):
                   (v.decode(errors="replace") if isinstance(v, bytes) else str(v))
                   for k, v in raw.items()}
            if row: rows.append(row)
            if len(rows) >= limit: break
    except Exception: pass
    return rows

def learn(tool: str) -> dict:
    cfg = _tool(tool)
    rows = [row for row in _read_outcomes(tool)
            if row.get("status") in ("verified", "rejected")]
    if not rows:
        recent = _historic_states()
        if tool == "openclaw": rows = recent  # learns routing outcomes across all chips
        elif tool == "hermes": rows = [r for r in recent if r.get("status") == "failed"]
        else: rows = [r for r in recent if r.get("executor") == tool]
    profile_dir = PROFILES / tool; profile_dir.mkdir(parents=True, exist_ok=True)
    successes = [r for r in rows if r.get("status") in ("completed", "verified")]
    failures = [r for r in rows if r.get("status") == "failed"]
    words = Counter()
    for row in failures:
        failure_text = (row.get("summary") or row.get("result_summary") or "").lower()
        for word in re.findall(r"[a-zA-Z_]{4,}|[\u4e00-\u9fff]{2,}", failure_text):
            if word not in {"error", "failed", "任务", "失败"}: words[word] += 1
    reusable = []
    for row in reversed(successes):
        text = (row.get("summary") or row.get("result_summary") or "").strip()
        if text and text not in reusable: reusable.append(text[:180])
        if len(reusable) >= 5: break
    profile = {"schema": "aios-tool-learning/1.0", "tool": tool, "updated_at": now(),
        "learning_enabled": bool(cfg.get("learning")), "samples": len(rows),
        "successes": len(successes), "failures": len(failures),
        "avoid_patterns": [w for w, _ in words.most_common(8)],
        "reusable_patterns": reusable, "adapter_contract": "1.0"}
    intel_file = INTELLIGENCE / f"{tool}.jsonl"
    candidates = []
    if intel_file.is_file():
        for line in intel_file.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:]:
            try:
                item = json.loads(line)
                if item.get("title") and item["title"] not in [x.get("title") for x in candidates]:
                    candidates.append(item)
            except Exception: pass
    profile["research_candidates"] = candidates[-10:]
    if tool == "openclaw":
        routes = Counter(r.get("executor", r.get("system", "unknown")) for r in successes)
        profile["successful_routes"] = dict(routes.most_common())
    if tool == "hermes":
        reports = sorted((HOME / "knowledge/calibration_reports").glob("hermes_learn_*.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        profile["latest_calibration_report"] = str(reports[0]) if reports else ""
    temp = profile_dir / "profile.tmp"; target = profile_dir / "profile.json"
    temp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"); os.replace(temp, target)
    return profile

def learn_all(): return {name: learn(name) for name in tool_names() if _tool(name).get("learning")}

def guidance(tool: str) -> str:
    path = PROFILES / tool / "profile.json"
    if not path.is_file(): return ""
    try: profile = json.loads(path.read_text(encoding="utf-8"))
    except Exception: return ""
    lines = []
    if profile.get("avoid_patterns"): lines.append("避免已知失败模式: " + ", ".join(profile["avoid_patterns"][:5]))
    if profile.get("reusable_patterns"): lines.append("可参考本模块近期成功经验: " + profile["reusable_patterns"][0][:200])
    if profile.get("research_candidates"):
        lines.append("可选的新能力线索: " + profile["research_candidates"][-1].get("title", "")[:160])
    return "\n".join(lines)[:500]

def apply_guidance(tool: str, task_text: str) -> str:
    hint = guidance(tool)
    return task_text if not hint else f"[本工具独立学习记忆]\n{hint}\n[当前任务]\n{task_text}"

def _snapshot(name: str) -> Path:
    source = Path(_tool(name)["package_path"]).resolve()
    if not source.is_dir(): raise FileNotFoundError(source)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = BACKUPS / name / stamp / "package"
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = _run(["cp", "-a", "--reflink=auto", str(source), str(destination)], 900)
    if result.returncode: raise RuntimeError("snapshot failed: " + result.stderr[-500:])
    retention = int(load_config()["policy"].get("backup_retention", 2))
    old = sorted((BACKUPS / name).glob("*/package"), key=lambda p: p.stat().st_mtime, reverse=True)[retention:]
    for path in old: shutil.rmtree(path.parent)
    return destination

def _restore(name: str, snapshot: Path):
    target = Path(_tool(name)["package_path"]).resolve()
    if not snapshot.is_dir() or name not in snapshot.parts: raise ValueError("invalid tool snapshot")
    if target.exists(): shutil.rmtree(target)
    result = _run(["cp", "-a", "--reflink=auto", str(snapshot), str(target)], 900)
    if result.returncode: raise RuntimeError("restore failed: " + result.stderr[-500:])

def _write_history(name: str, record: dict):
    HISTORY.mkdir(parents=True, exist_ok=True)
    path = HISTORY / f"{name}.jsonl"
    with path.open("a", encoding="utf-8") as handle: handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        from aios_bus import publish_event
        publish_event("tool.upgraded", record, "tool-evolution")
    except Exception: pass

def _notify(text: str):
    try:
        sys.path.insert(0, str(HOME / "kernel/centers/intelligence_growth_center"))
        from opportunity_radar import _send_hermes_feishu
        return _send_hermes_feishu([], text)
    except Exception as exc: return {"sent": False, "reason": str(exc)[:200]}

def upgrade(name: str, automatic=False, force=False) -> dict:
    cfg = _tool(name)
    if automatic and not cfg.get("auto_binary_upgrade"):
        return {"ok": True, "tool": name, "state": "deferred",
                "reason": cfg.get("auto_disabled_reason", "automatic upgrade disabled")}
    before = health(name)
    if not before.get("infrastructure_ok") and not force: raise RuntimeError(f"pre-upgrade infrastructure failed: {before}")
    snapshot = None; unit = cfg.get("service", ""); _set_maintenance(name, True, "isolated-upgrade")
    try:
        snapshot = _snapshot(name)
        if unit: _run(["systemctl", "--user", "stop", unit], 60)
        command = cfg["upgrade_command"]
        result = _run(command, 1800, str(Path(cfg["package_path"]).parent))
        if unit: _run(["systemctl", "--user", "start", unit], 60)
        _set_maintenance(name, False)
        after = health(name)
        ok = result.returncode == 0 and after.get("infrastructure_ok")
        rolled_back = False
        if not ok and load_config()["policy"].get("rollback_on_failure", True):
            if unit: _run(["systemctl", "--user", "stop", unit], 60)
            _restore(name, snapshot)
            if unit: _run(["systemctl", "--user", "start", unit], 60)
            rolled_back = True; after = health(name)
        record = {"ts": now(), "tool": name, "automatic": automatic,
            "before_version": before.get("version"), "after_version": after.get("version"),
            "command_rc": result.returncode, "contract_ok": after.get("infrastructure_ok"),
            "rolled_back": rolled_back, "ok": bool(ok), "service": unit,
            "output_tail": (result.stdout + result.stderr)[-800:]}
        _write_history(name, record)
        _notify(f"🧩 AI 工具 `{name}` 独立升级：{'成功' if ok else '失败并回滚' if rolled_back else '失败'}\n"
                f"{record['before_version']} → {record['after_version']}\nAIOS 核心未停机。")
        return record
    finally:
        _set_maintenance(name, False)
        if unit and not _service_active(unit): _run(["systemctl", "--user", "start", unit], 60)

def check_update(name: str) -> dict:
    cfg = _tool(name); result = _run(cfg["latest_command"], 120, str(Path(cfg["package_path"]).parent))
    current = health(name).get("version", "")
    latest = (result.stdout or result.stderr).strip().splitlines()[-1] if (result.stdout or result.stderr).strip() else ""
    if name != "hermes": current_number = re.search(r"\d+(?:\.\d+)+", current or "")
    else: current_number = None
    available = ("behind" in latest.lower()) if name == "hermes" else bool(current_number and latest and current_number.group(0) != latest)
    return {"tool": name, "ok": result.returncode == 0, "current": current,
            "latest": latest[:300], "update_available": available,
            "auto_binary_upgrade": bool(cfg.get("auto_binary_upgrade"))}

def auto_upgrade() -> dict:
    policy = load_config()["policy"]
    if not policy.get("auto_upgrade_enabled"): return {"ok": True, "state": "disabled"}
    checked = []; upgraded = []
    for name in tool_names():
        info = check_update(name); checked.append(info)
        if info["update_available"] and info["auto_binary_upgrade"]:
            upgraded.append(upgrade(name, automatic=True))
            if len(upgraded) >= int(policy.get("max_tools_per_run", 1)): break
    return {"ok": all(r.get("ok") for r in upgraded), "checked": checked, "upgraded": upgraded}

def main():
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health"); sub.add_parser("learn-all"); sub.add_parser("auto-upgrade")
    for command in ("learn", "check", "upgrade"):
        p = sub.add_parser(command); p.add_argument("tool", choices=tool_names())
    args = parser.parse_args()
    if args.cmd == "health": result = health_all()
    elif args.cmd == "learn-all": result = learn_all()
    elif args.cmd == "auto-upgrade": result = auto_upgrade()
    elif args.cmd == "learn": result = learn(args.tool)
    elif args.cmd == "check": result = check_update(args.tool)
    else: result = upgrade(args.tool)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
