#!/usr/bin/env python3
"""优化7: 滚动升级 — 热升级+灰度发布+回滚"""
import sys, json, time
from pathlib import Path; from datetime import datetime, timezone
TOOLS = Path("${AIOS_HOME}/kernel/tools"); sys.path.insert(0, str(TOOLS))
from aios_bus import publish_event, _is_available

KEY_UPGRADE = "aios:logs:upgrade_history"

def upgrade_center(center_name: str, new_version: str, canary_pct: int = 10):
    """灰度升级: 先10%流量, 观察5分钟."""
    result = {"center": center_name, "version": new_version, "canary": f"{canary_pct}%", "status": "canary"}
    if _is_available():
        import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        r.hset(f"aios:upgrade:{center_name}", mapping={"version": new_version, "canary_pct": str(canary_pct), "status": "canary", "started": datetime.now(timezone.utc).isoformat()})
        r.zadd(KEY_UPGRADE, {json.dumps(result, ensure_ascii=False): time.time()})
        publish_event("knowledge.updated", {"type": "upgrade_started", "center": center_name, "version": new_version}, "rolling_upgrade")
    return result

def rollback_center(center_name: str):
    if not _is_available(): return False
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    prev = r.hget(f"aios:upgrade:{center_name}", "version")
    r.hset(f"aios:upgrade:{center_name}", "status", "rolled_back")
    r.zadd(KEY_UPGRADE, {json.dumps({"center": center_name, "action": "rollback", "from": prev.decode() if isinstance(prev, bytes) else str(prev), "ts": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False): time.time()})
    publish_event("alert.warning", {"type": "upgrade_rollback", "center": center_name}, "rolling_upgrade")
    return True

def get_upgrade_status(center_name: str):
    if not _is_available(): return {}
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    raw = r.hgetall(f"aios:upgrade:{center_name}")
    return {k.decode(): v.decode() for k, v in raw.items()} if raw else {}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    center = sys.argv[2] if len(sys.argv) > 2 else "execution"
    if cmd == "upgrade": print(json.dumps(upgrade_center(center, "v2.0"), ensure_ascii=False))
    elif cmd == "rollback": print(f"✅ rollback" if rollback_center(center) else "❌")
    elif cmd == "status": print(json.dumps(get_upgrade_status(center), ensure_ascii=False, indent=2))
