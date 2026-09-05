#!/usr/bin/env python3
"""
AIOS v4.0 统一网关 (Unified Gateway)
=====================================
四个 AI 系统通过此网关获得 AIOS 核心能力：
  - 协议加载:  加载三份宪法, 返回治理摘要
  - 安全验证:  任务完成 → 自动跑 verify.py → 结果写回总线
  - 前置模拟:  新任务 → 自动跑 World Model → 安全判定写回总线
  - 系统心跳:  一键注册, 返回四系统当前状态

用法:
  python3 aios_gateway.py startup <system>    # 系统启动: 加载协议+心跳+状态
  python3 aios_gateway.py verify <task.json>  # 验证门禁
  python3 aios_gateway.py simulate <task.json> # 世界模型模拟
  python3 aios_gateway.py status              # 全系统状态
  python3 aios_gateway.py snapshot            # 手动快照

设计原则:
  - 纯加法, 不修改任何 AI 核心代码
  - Redis 不可用时优雅降级
  - 所有操作原子化, 失败不影响 AI 正常执行
"""

import json
import os
import sys
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
PROTOCOLS = Path(AIOS_HOME) / "kernel" / "protocols"
TOOLS = Path(AIOS_HOME) / "kernel" / "tools"

# -- 协议摘要缓存 (启动时加载一次) --
_protocol_cache: Optional[Dict] = None


def load_protocols() -> Dict[str, Any]:
    """加载三份宪法, 返回关键约束摘要."""
    global _protocol_cache
    if _protocol_cache:
        return _protocol_cache

    summary = {
        "loaded_at": datetime.now(timezone.utc).isoformat(),
        "immutable_rules": [],
        "safety_redlines": [],
        "bus_rules": [],
    }

    # capability_protocol.json
    cap_file = PROTOCOLS / "capability_protocol.json"
    if cap_file.exists():
        try:
            cap = json.loads(cap_file.read_text())
            summary["immutable_rules"] = cap.get("immutable_rules", [])
            summary["hierarchy"] = cap.get("hierarchy", {})
            summary["agent_registry"] = list(cap.get("agent_registry", {}).keys())
        except Exception:
            pass

    # safety_boundary.md
    safety_file = PROTOCOLS / "safety_boundary.md"
    if safety_file.exists():
        try:
            text = safety_file.read_text()
            # 提取关键红线
            for line in text.split("\n"):
                line = line.strip()
                if "禁止" in line or "必须" in line or "HALT" in line:
                    if len(line) < 200:
                        summary["safety_redlines"].append(line)
        except Exception:
            pass

    # context_bus.yaml
    bus_file = PROTOCOLS / "context_bus.yaml"
    if bus_file.exists():
        try:
            text = bus_file.read_text()
            for line in text.split("\n"):
                line = line.strip()
                if "max_chars" in line or "UUID_Pointer" in line or "512" in line:
                    summary["bus_rules"].append(line)
        except Exception:
            pass

    _protocol_cache = summary
    return summary


def startup(system: str) -> Dict[str, Any]:
    """系统启动流程: 加载协议 → 心跳 → 返回状态."""
    protocols = load_protocols()

    # 心跳
    try:
        sys.path.insert(0, str(TOOLS))
        from aios_bus import heartbeat, get_bus_summary
        heartbeat(system)
        bus_status = get_bus_summary()
    except Exception:
        bus_status = "总线不可用"

    return {
        "system": system,
        "protocols_loaded": len(protocols.get("immutable_rules", [])),
        "key_rules": protocols.get("immutable_rules", [])[:5],
        "key_redlines": protocols.get("safety_redlines", [])[:3],
        "bus_status": str(bus_status)[:500],
    }


def verify_task(task_json_path: str) -> Dict[str, Any]:
    """运行 verify.py 验证门禁."""
    verifier = TOOLS / "verify.py"
    if not verifier.exists():
        return {"error": "verify.py 不存在", "passed": False}

    result = subprocess.run(
        ["python3", str(verifier), task_json_path],
        capture_output=True, text=True, timeout=30
    )
    return {
        "exit_code": result.returncode,
        "passed": result.returncode == 0,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-500:] if result.stderr else "",
    }


def simulate_task(task_json_path: str) -> Dict[str, Any]:
    """运行 World Model 安全模拟."""
    wm = TOOLS / "world_model_runner.py"
    if not wm.exists():
        return {"error": "world_model_runner.py 不存在", "verdict": "SKIPPED"}

    result = subprocess.run(
        ["python3", str(wm), task_json_path],
        capture_output=True, text=True, timeout=30
    )
    return {
        "exit_code": result.returncode,
        "verdict": "APPROVED" if result.returncode == 0 else "BLOCKED",
        "stdout": result.stdout[-1000:],
    }


def system_status() -> str:
    """全系统状态."""
    lines = ["=" * 60, "  AIOS v4.0 全系统状态", "=" * 60, ""]

    # 协议
    protocols = load_protocols()
    lines.append(f"📋 协议: {len(protocols.get('immutable_rules',[]))} 条不可变规则已加载")
    lines.append(f"   安全红线: {len(protocols.get('safety_redlines',[]))} 条")

    # 核心文件
    lines.append(f"\n📁 核心文件:")
    for f in ["verify.py", "world_model_runner.py", "aios_bus.py", "aios_gateway.py"]:
        exists = "✅" if (TOOLS / f).exists() else "❌"
        lines.append(f"   {exists} {f}")

    # 总线
    try:
        sys.path.insert(0, str(TOOLS))
        from aios_bus import get_bus_summary
        lines.append(f"\n{bus_summary}")
    except Exception:
        pass

    # VFS
    for path, label in [
        (AIOS_HOME + "/kernel", "kernel"),
        (AIOS_HOME + "/knowledge", "knowledge"),
        (AIOS_HOME + "/sandbox", "sandbox"),
    ]:
        try:
            mode = oct(os.stat(path).st_mode)[-3:]
            lines.append(f"   🔒 {label}: {mode}")
        except Exception:
            pass

    return "\n".join(lines)


def run_snapshot() -> Dict[str, Any]:
    """执行系统快照."""
    script = TOOLS / "checkpoint_snapshot.sh"
    if not script.exists():
        return {"error": "快照脚本不存在", "ok": False}

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True, text=True, timeout=30
    )
    return {"ok": result.returncode == 0, "output": result.stdout[-500:]}


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(system_status())
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "startup":
        system = sys.argv[2] if len(sys.argv) > 2 else "claude"
        result = startup(system)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "verify":
        if len(sys.argv) < 3:
            print("用法: aios_gateway.py verify <task.json>")
            sys.exit(1)
        result = verify_task(sys.argv[2])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result.get("passed") else 1)

    elif cmd == "simulate":
        if len(sys.argv) < 3:
            print("用法: aios_gateway.py simulate <task.json>")
            sys.exit(1)
        result = simulate_task(sys.argv[2])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result.get("verdict") == "APPROVED" else 1)

    elif cmd == "status":
        print(system_status())

    elif cmd == "snapshot":
        result = run_snapshot()
        print("✅ 快照完成" if result.get("ok") else f"❌ {result.get('error')}")

    elif cmd == "protocols":
        result = load_protocols()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        print(f"未知命令: {cmd}")
        print("可用: startup <sys> | verify <json> | simulate <json> | status | snapshot | protocols")
