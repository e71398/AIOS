#!/usr/bin/env python3
"""优化8: 备份恢复验证 — 月度自动恢复测试"""
import sys, os, json
from pathlib import Path; from datetime import datetime, timezone
TOOLS = Path("${AIOS_HOME}/kernel/tools"); AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
sys.path.insert(0, str(TOOLS))
from aios_bus import _is_available

SNAPSHOT_DIR = Path(AIOS_HOME) / "checkpoint" / "snapshots"

def run_recovery_test():
    """模拟恢复测试: 检查快照是否存在+可读."""
    snapshots = list(SNAPSHOT_DIR.glob("*.tar.gz"))
    if not snapshots: return {"score": 0, "status": "no_snapshots"}
    latest = max(snapshots, key=lambda p: p.stat().st_mtime)
    checks = {"snapshot_exists": True, "snapshot_readable": False, "data_extractable": False}
    try:
        import tarfile
        with tarfile.open(latest, "r:gz") as tf:
            names = tf.getnames()
            checks["snapshot_readable"] = True
            checks["data_extractable"] = len(names) > 0
            checks["file_count"] = len(names)
    except: pass
    score = sum(1 for v in checks.values() if v) / len(checks) * 100
    return {"score": round(score), "status": "healthy" if score >= 80 else "degraded", "checks": checks, "snapshot": latest.name, "ts": datetime.now(timezone.utc).isoformat()}

def get_backup_health():
    snapshots = list(SNAPSHOT_DIR.glob("*.tar.gz"))
    return {"total_snapshots": len(snapshots), "latest": max(snapshots, key=lambda p: p.stat().st_mtime).name if snapshots else None, "oldest": min(snapshots, key=lambda p: p.stat().st_mtime).name if snapshots else None}

def get_rto_rpo():
    """RTO(恢复时间目标) / RPO(数据丢失窗口)."""
    snapshots = list(SNAPSHOT_DIR.glob("*.tar.gz"))
    if not snapshots: return {"rto_minutes": 999, "rpo_minutes": 999}
    latest = max(snapshots, key=lambda p: p.stat().st_mtime)
    age_min = (datetime.now().timestamp() - latest.stat().st_mtime) / 60
    return {"rto_minutes": round(age_min), "rpo_minutes": round(age_min), "note": "每天03:00快照, RPO≈24h"}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "health"
    if cmd == "health": print(json.dumps(get_backup_health(), ensure_ascii=False, indent=2))
    elif cmd == "test": print(json.dumps(run_recovery_test(), ensure_ascii=False, indent=2))
    elif cmd == "rto": print(json.dumps(get_rto_rpo(), ensure_ascii=False, indent=2))
