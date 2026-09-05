#!/usr/bin/env python3
"""优化6: 配置漂移检测 — 版本记录+异常告警+回滚"""
import sys, json, time
from pathlib import Path; from datetime import datetime, timezone
TOOLS = Path("${AIOS_HOME}/kernel/tools"); sys.path.insert(0, str(TOOLS))
from aios_bus import publish_event, _is_available, generate_task_id

KEY_CONFIG = "aios:config:versions"

def record_change(key: str, old_value: str, new_value: str, operator: str = "system"):
    if not _is_available(): return
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    version_id = generate_task_id()[:8]
    entry = {"key": key, "version": version_id, "old": old_value[:500], "new": new_value[:500],
             "operator": operator, "ts": datetime.now(timezone.utc).isoformat()}
    r.lpush(f"{KEY_CONFIG}/{key}", json.dumps(entry, ensure_ascii=False))
    r.hset(f"{KEY_CONFIG}:latest", key, json.dumps({"version": version_id, "value": new_value[:500]}, ensure_ascii=False))

def check_drift(key: str) -> dict:
    """检查配置是否有异常变更."""
    if not _is_available(): return {"drift": False}
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    history = [json.loads(m.decode() if isinstance(m, bytes) else m) for m in r.lrange(f"{KEY_CONFIG}/{key}", 0, -1)]
    if len(history) >= 3:
        # 短时间内3次以上变更→告警
        recent = [h for h in history if (time.time() - datetime.fromisoformat(h["ts"].replace("Z","+00:00")).timestamp()) < 3600]
        if len(recent) >= 3:
            publish_event("alert.warning", {"type": "config_drift", "key": key, "changes": len(recent)}, "config_drift")
            return {"drift": True, "changes_1h": len(recent)}
    return {"drift": False}

def rollback(key: str, version_id: str):
    """回滚到指定版本."""
    if not _is_available(): return False
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    history = [json.loads(m.decode() if isinstance(m, bytes) else m) for m in r.lrange(f"{KEY_CONFIG}/{key}", 0, -1)]
    target = next((h for h in history if h["version"] == version_id), None)
    if target:
        record_change(key, "rolled_back", target["old"], "rollback")
        return True
    return False

def get_history(key: str) -> list:
    if not _is_available(): return []
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    return [json.loads(m.decode() if isinstance(m, bytes) else m) for m in r.lrange(f"{KEY_CONFIG}/{key}", 0, 10)]

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "record"
    if cmd == "record":
        record_change("models.toml", "old_value", "new_value", "admin")
        print("✅ recorded"); check_drift("models.toml")
    elif cmd == "history":
        for h in get_history(sys.argv[2] if len(sys.argv) > 2 else "models.toml"):
            print(f"  [{h['version']}] {h['operator']}: {h['old'][:30]}→{h['new'][:30]}")
