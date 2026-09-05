#!/usr/bin/env python3
"""
AIOS 模块注册表 + 状态机 — 沙箱化隔离宪法
===========================================
每个模块独立注册、独立健康、独立状态。故障不传播。

状态机 (8态):
  UNREGISTERED → REGISTERED → HEALTHY
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
               DEGRADED        RESTARTING      OFFLINE
                    │              │              │
                    ▼              ▼              ▼
               HEALTHY         HEALTHY        CRASHED

规则:
  1. 模块间禁止直接 import (除 aios_bus)
  2. 每个模块必须有 health_check() 返回 {ok:bool, state:str, deps:dict}
  3. 启动不依赖其他模块就绪
  4. 挂掉只影响自己, 不影响注册表和其他模块
"""
import json, os, sys, time, subprocess
from pathlib import Path
from datetime import datetime, timezone
from enum import Enum

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))


class ModuleState(Enum):
    UNREGISTERED = "unregistered"
    REGISTERED = "registered"
    HEALTHY = "healthy"
    DEGRADED = "degraded"      # 部分降级但可用
    OFFLINE = "offline"         # 进程消失
    CRASHED = "crashed"         # 确认崩溃
    RESTARTING = "restarting"   # 重启中
    STOPPED = "stopped"         # 主动停止


VALID_TRANSITIONS = {
    ModuleState.UNREGISTERED: [ModuleState.REGISTERED],
    ModuleState.REGISTERED:   [ModuleState.HEALTHY, ModuleState.OFFLINE],
    ModuleState.HEALTHY:      [ModuleState.DEGRADED, ModuleState.OFFLINE, ModuleState.RESTARTING, ModuleState.STOPPED],
    ModuleState.DEGRADED:     [ModuleState.HEALTHY, ModuleState.OFFLINE, ModuleState.RESTARTING],
    ModuleState.OFFLINE:      [ModuleState.HEALTHY, ModuleState.RESTARTING, ModuleState.CRASHED, ModuleState.STOPPED],
    ModuleState.CRASHED:      [ModuleState.RESTARTING, ModuleState.STOPPED],
    ModuleState.RESTARTING:   [ModuleState.HEALTHY, ModuleState.CRASHED],
    ModuleState.STOPPED:      [ModuleState.HEALTHY],  # 仅允许从停止→健康(手动启动)
}


REGISTRY_KEY = "aios:module:registry"
STATE_KEY_PREFIX = "aios:module:state:"


def _get_redis():
    import redis as _r
    return _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)


def register_module(name: str, module_type: str, dependencies: list = None, 
                    startup_command: str = "", resource_limits: dict = None) -> bool:
    """注册模块到注册表. 返回是否首次注册."""
    try:
        r = _get_redis()
        deps = dependencies or []
        limits = resource_limits or {"cpu_max": "50%", "mem_max": "512M"}
        info = {
            "name": name, "type": module_type, "dependencies": json.dumps(deps),
            "startup_command": startup_command,
            "resource_limits": json.dumps(limits),
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "version": "4.0",
        }
        r.hset(f"{STATE_KEY_PREFIX}{name}", mapping=info)
        is_new = r.hsetnx(REGISTRY_KEY, name, module_type) == 1
        transition_state(name, ModuleState.REGISTERED.value if is_new else None)
        return is_new
    except Exception:
        return False


def transition_state(name: str, new_state: str = None, reason: str = "") -> bool:
    """状态转移: 验证合法性后执行."""
    try:
        r = _get_redis()
        current = r.hget(f"{STATE_KEY_PREFIX}{name}", "state")
        current_str = current.decode() if current else ModuleState.UNREGISTERED.value

        if new_state and current_str != ModuleState.UNREGISTERED.value:
            if new_state not in [s.value for s in VALID_TRANSITIONS.get(
                ModuleState(current_str), [])]:
                return False

        target = new_state or current_str
        r.hset(f"{STATE_KEY_PREFIX}{name}", mapping={
            "state": target,
            "last_state_change": datetime.now(timezone.utc).isoformat(),
            "state_reason": reason or "",
        })
        r.hset(REGISTRY_KEY, f"{name}_state", target)
        return True
    except Exception:
        return False


def health_check(name: str) -> dict:
    """对模块执行健康检查."""
    result = {
        "name": name, "ok": False, "state": ModuleState.UNREGISTERED.value,
        "dependencies": {}, "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        r = _get_redis()
        info = r.hgetall(f"{STATE_KEY_PREFIX}{name}")
        if not info:
            return result

        info = {k.decode(): v.decode() for k, v in info.items()}
        result["state"] = info.get("state", ModuleState.UNREGISTERED.value)

        # 检查进程是否存在
        kind, enabled, probe_ok = _probe_module(name)
        result["kind"] = kind
        result["enabled"] = enabled
        if not enabled:
            result["ok"] = True
            result["state"] = ModuleState.STOPPED.value
            transition_state(name, ModuleState.STOPPED.value,
                             "intentionally disabled by feature policy")
        elif probe_ok:
            result["pid"] = kind == "process"
            result["ok"] = True
            if result["state"] != ModuleState.HEALTHY.value:
                transition_state(name, ModuleState.HEALTHY.value,
                                 "runtime probe succeeded")
                result["state"] = ModuleState.HEALTHY.value
        elif info.get("state") in (ModuleState.STOPPED.value,):
            result["ok"] = True  # 主动停止不算故障
        else:
            transition_state(name, ModuleState.OFFLINE.value, f"{kind} probe failed")
            result["state"] = ModuleState.OFFLINE.value

        # 检查依赖
        deps = json.loads(info.get("dependencies", "[]"))
        for dep in deps:
            dep_info = r.hgetall(f"{STATE_KEY_PREFIX}{dep}")
            if dep_info:
                dep_state = dep_info.get(b"state", b"unknown").decode()
                result["dependencies"][dep] = dep_state
    except Exception:
        result["state"] = "unavailable"

    return result


def health_check_all() -> dict:
    """批量健康检查所有注册模块."""
    try:
        r = _get_redis()
        modules = r.hgetall(REGISTRY_KEY)
        if not modules:
            return {"total": 0, "healthy": 0, "modules": {}}

        results = {"total": 0, "healthy": 0, "degraded": 0, "stopped": 0, "offline": 0, "modules": {}}
        for name_bytes, _ in modules.items():
            name = name_bytes.decode()
            if name.endswith("_state"):  # skip state keys
                continue
            hc = health_check(name)
            results["modules"][name] = {"state": hc["state"], "ok": hc["ok"], "kind": hc.get("kind", "unknown"), "pid": hc.get("pid", False)}
            results["total"] += 1
            # Categories are mutually exclusive; their sum must equal total.
            if hc["state"] == "degraded":
                results["degraded"] += 1
            elif hc["state"] == "stopped":
                results["stopped"] += 1
            elif not hc["ok"] or hc["state"] in ("offline", "unavailable"):
                results["offline"] += 1
            else:
                results["healthy"] += 1
        return results
    except Exception as e:
        return {"error": str(e)}


def get_module_state(name: str) -> dict:
    try:
        r = _get_redis()
        info = r.hgetall(f"{STATE_KEY_PREFIX}{name}")
        if not info:
            return {"name": name, "state": ModuleState.UNREGISTERED.value}
        return {
            "name": name,
            "state": info.get(b"state", b"unknown").decode(),
            "type": info.get(b"type", b"").decode(),
            "dependencies": json.loads(info.get(b"dependencies", b"[]").decode()),
            "registered_at": info.get(b"registered_at", b"").decode(),
            "health": health_check(name),
        }
    except Exception as e:
        return {"name": name, "state": "error", "error": str(e)}


def list_modules() -> list:
    try:
        r = _get_redis()
        modules = r.hgetall(REGISTRY_KEY)
        result = []
        for name_bytes, type_bytes in modules.items():
            name = name_bytes.decode()
            if name.endswith("_state"):
                continue
            mod_type = type_bytes.decode()
            state_info = r.hgetall(f"{STATE_KEY_PREFIX}{name}")
            state = "unregistered"
            if state_info:
                state = state_info.get(b"state", b"unregistered").decode()
            result.append({"name": name, "type": mod_type, "state": state})
        return result
    except Exception:
        return []


def _check_process(name: str) -> bool:
    pats = {
        "aios_monitor": "aios_monitor.py",
        "aios_web": "aios_web.py",
        "aios_dispatcher": "aios_dispatcher|openclaw.agent",
        "aios_executor_daemon": "aios_executor_daemon",
        "aios_entry_gateway": "aios_entry_gateway",
        "aios_event_daemon": "aios_event_daemon",
        "aios_entry_gw": "aios_entry_gateway.py",
        "aios_executor": "aios_executor_daemon.py",
        "aios_enforcer": "aios_enforcer_daemon.py",
        "redis": "redis-server",
        "openclaw_gateway": "openclaw.*gateway",
        "hermes_llama": "llama_cpp.server",
        "codex_relay": "codex-relay",
        "claude_code": r"claude\b",
    }
    pat = pats.get(name, name)
    try:
        result = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=2)
        return bool(result.stdout.strip())
    except Exception:
        return False


DISABLED_MODULES = {"hermes_llama", "codex_relay"}
FILE_MODULES = {
    "aios_hermes": TOOLS / "aios_hermes_learn.py",
    "aios_knowledge": Path("${AIOS_HOME}/knowledge"),
    "aios_memory_gd": TOOLS / "aios_memory_guardian.py",
}
IMPORT_MODULES = {"aios_bus", "aios_dispatcher", "aios_agent_mesh"}


def _probe_module(name: str):
    """Return (kind, enabled, ok) without treating libraries as daemons."""
    if name in DISABLED_MODULES:
        return "optional", False, True
    if name in FILE_MODULES:
        return "feature", True, FILE_MODULES[name].exists()
    if name in IMPORT_MODULES:
        try:
            __import__(name)
            if name == "aios_bus":
                _get_redis().ping()
            return "library", True, True
        except Exception:
            return "library", True, False
    return "process", True, _check_process(name)


def enforce_isolation_rules(name: str, import_target: str) -> bool:
    """检查模块间import是否违反隔离宪法."""
    ALLOWED_IMPORTS = ["aios_bus", "aios_module_registry", "protocols", "config"]
    if import_target in ALLOWED_IMPORTS:
        return True
    return False


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        mods = list_modules()
        print(f"📋 注册模块: {len(mods)}")
        for m in mods:
            icon = {"healthy":"🟢","degraded":"🟡","offline":"🔴","stopped":"⚫","registered":"🔵"}
            print(f"  {icon.get(m['state'],'⚪')} {m['name']:25s} {m['type']:15s} [{m['state']}]")
    elif cmd == "health":
        name = sys.argv[2] if len(sys.argv) > 2 else ""
        if name:
            hc = health_check(name)
            print(json.dumps(hc, ensure_ascii=False, indent=2))
        else:
            all_hc = health_check_all()
            print(json.dumps(all_hc, ensure_ascii=False, indent=2))
    elif cmd == "register":
        name = sys.argv[2] if len(sys.argv) > 2 else "unknown"
        mtype = sys.argv[3] if len(sys.argv) > 3 else "system"
        ok = register_module(name, mtype)
        print(f"{'✅' if ok else '⚠️ (exists)'} {name}")
    elif cmd == "state":
        name = sys.argv[2] if len(sys.argv) > 2 else ""
        if name:
            print(json.dumps(get_module_state(name), ensure_ascii=False, indent=2))
