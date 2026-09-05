#!/usr/bin/env python3
"""P3: 资源分配器 — GPU/RAM/CPU监控 + 动态降级"""

import sys, json, time
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import _is_available, _redis_client, KEY_PREFIX, publish_event

KEY_RESOURCE = f"{KEY_PREFIX}:resource"

CPU_WARN = 80
CPU_CRIT = 90
RAM_WARN = 80
RAM_CRIT = 90
DISK_WARN = 85
DISK_CRIT = 95


def _get_cpu() -> dict:
    try:
        import psutil
        pct = psutil.cpu_percent(interval=0.5)
        return {"percent": pct, "count": psutil.cpu_count()}
    except Exception:
        return {"percent": 0, "count": 0}


def _get_ram() -> dict:
    try:
        import psutil
        mem = psutil.virtual_memory()
        return {"percent": mem.percent, "total_gb": round(mem.total / 1073741824, 1),
                "available_gb": round(mem.available / 1073741824, 1)}
    except Exception:
        return {"percent": 0, "total_gb": 0, "available_gb": 0}


def _get_disk(path: str = "/") -> dict:
    try:
        import psutil
        d = psutil.disk_usage(path)
        return {"percent": d.percent, "total_gb": round(d.total / 1073741824, 1),
                "free_gb": round(d.free / 1073741824, 1)}
    except Exception:
        return {"percent": 0, "total_gb": 0, "free_gb": 0}


def _get_load() -> dict:
    try:
        import os
        avg = os.getloadavg()
        return {"1min": round(avg[0], 2), "5min": round(avg[1], 2), "15min": round(avg[2], 2)}
    except Exception:
        return {"1min": 0, "5min": 0, "15min": 0}


def snapshot() -> dict:
    """采集当前系统资源快照."""
    cpu = _get_cpu()
    ram = _get_ram()
    disk = _get_disk()
    load = _get_load()

    level = "normal"
    if cpu["percent"] >= CPU_CRIT or ram["percent"] >= RAM_CRIT or disk["percent"] >= DISK_CRIT:
        level = "critical"
    elif cpu["percent"] >= CPU_WARN or ram["percent"] >= RAM_WARN or disk["percent"] >= DISK_WARN:
        level = "warning"

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "cpu": cpu,
        "ram": ram,
        "disk": disk,
        "load": load,
    }


def snapshot_to_redis() -> dict:
    """采集快照并写入 Redis (保留5分钟)."""
    s = snapshot()
    if not _is_available():
        return s
    try:
        _redis_client.setex(f"{KEY_RESOURCE}:latest", 300, json.dumps(s, ensure_ascii=False))
        _redis_client.hset(f"{KEY_RESOURCE}:history", s["ts"], json.dumps(s))
        _redis_client.expire(f"{KEY_RESOURCE}:history", 3600)
        if s["level"] == "critical":
            publish_event("alert.critical", {
                "type": "resource_critical",
                "cpu": s["cpu"]["percent"],
                "ram": s["ram"]["percent"],
                "disk": s["disk"]["percent"],
            }, "resource_allocator")
        elif s["level"] == "warning":
            publish_event("alert.warning", {
                "type": "resource_warning",
                "cpu": s["cpu"]["percent"],
                "ram": s["ram"]["percent"],
                "disk": s["disk"]["percent"],
            }, "resource_allocator")
    except Exception:
        pass
    return s


def get_degradation_advice() -> dict:
    """根据资源压力返回降级建议."""
    s = snapshot()
    advice = {
        "ts": s["ts"],
        "level": s["level"],
        "actions": [],
        "reject_new_tasks": False,
        "prefer_low_cost_model": False,
    }
    if s["level"] == "critical":
        advice["reject_new_tasks"] = True
        advice["prefer_low_cost_model"] = True
        advice["actions"].append("HALT新任务入队")
        advice["actions"].append("切换所有模型到经济版")
        if s["ram"]["percent"] >= RAM_CRIT:
            advice["actions"].append("终止低优先级任务释放内存")
        if s["disk"]["percent"] >= DISK_CRIT:
            advice["actions"].append("清理临时文件释放磁盘")
    elif s["level"] == "warning":
        advice["prefer_low_cost_model"] = True
        advice["actions"].append("新任务优先使用经济模型")
        if s["ram"]["percent"] >= RAM_WARN:
            advice["actions"].append("限制并发任务数")
        if s["disk"]["percent"] >= DISK_WARN:
            advice["actions"].append("触发日志轮转")
    return advice


def check_and_enforce() -> dict:
    """检查资源并触发强制措施 (供 enforcer 集成)."""
    adv = get_degradation_advice()
    if adv["level"] == "critical":
        from aios_enforcer import halt_system
        halt_system(f"资源熔断: CPU={adv.get('cpu',{}).get('percent',0)}% "
                     f"RAM={adv.get('ram',{}).get('percent',0)}% "
                     f"DISK={adv.get('disk',{}).get('percent',0)}%")
    return adv


try:
    from aios_bus import register_pin
    register_pin("resource.snapshot", snapshot_to_redis, "资源分配器: 采集快照")
    register_pin("resource.status", snapshot, "资源分配器: 当前状态")
    register_pin("resource.degradation", get_degradation_advice, "资源分配器: 降级建议")
    register_pin("resource.enforce", check_and_enforce, "资源分配器: 检查+强制HALT")
except Exception:
    pass

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if cmd == "snapshot":
        print(json.dumps(snapshot_to_redis(), ensure_ascii=False, indent=2))
    elif cmd == "degrade":
        print(json.dumps(get_degradation_advice(), ensure_ascii=False, indent=2))
    elif cmd == "enforce":
        print(json.dumps(check_and_enforce(), ensure_ascii=False, indent=2))
