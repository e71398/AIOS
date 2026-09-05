#!/usr/bin/env python3
"""
AIOS v4.0 统一启动脚本
每个 AI 系统启动时调用此脚本，获得协议摘要和总线状态。
不修改任何 AI 核心代码 — 由各系统的启动流程自主决定是否调用。

用法: python3 aios_startup.py <system_name>
退出码: 0=成功, 1=总线不可用(非致命)
"""

import sys
import json
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

try:
    from aios_gateway import startup as gw_startup
    from aios_bus import heartbeat, get_bus_summary, check_recent
except ImportError as e:
    print(f"⚠️ AIOS 启动: SDK 导入失败 ({e}), 降级运行")
    sys.exit(0)  # 非致命

system = sys.argv[1] if len(sys.argv) > 1 else "claude"

# 1. 加载协议
try:
    gw = gw_startup(system)
    rules = gw.get("key_rules", [])
    print(f"📋 已加载 {gw.get('protocols_loaded', 0)} 条不可变规则:")
    for r in rules[:3]:
        print(f"   • {r}")
except Exception as e:
    print(f"⚠️ 协议加载失败: {e}")
    rules = []

# 2. 心跳
try:
    if heartbeat(system):
        print(f"🟢 {system} 心跳已发送")
except Exception:
    pass

# 3. 最近状态
try:
    recent = check_recent(limit=5)
    if recent:
        print(f"📜 最近5条任务:")
        for r in recent:
            icon = "✅" if r.get("status") == "completed" else "❌"
            sys_name = r.get("system", "?")
            name = r.get("task_name", "")[:50]
            print(f"   {icon} [{sys_name}] {name}")
except Exception:
    pass

# 4. 总线摘要
try:
    summary = get_bus_summary()
    print(f"\n{brief}")
except Exception:
    pass

print(f"\n✅ AIOS 启动完成 ({system})")
sys.exit(0)
