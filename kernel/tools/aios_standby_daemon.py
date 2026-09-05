#!/usr/bin/env python3
"""
AIOS v4.0 调度热备守护 (Standby Daemon)
========================================
角色: 周期性检查 OpenClaw 调度器心跳, 检测宕机后自动激活备选执行器接管.

用法:
  python3 aios_standby_daemon.py              # 启动热备监控
  python3 aios_standby_daemon.py --once       # 单次检查
"""

import sys, os, time, signal, json
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import standby_cycle, snapshot_orchestrator_state, _is_available

CHECK_INTERVAL = 30
SHUTDOWN = False


def handle_signal(sig, frame):
    global SHUTDOWN
    print("\n🛑 [standby] 收到停止信号, 退出...")
    SHUTDOWN = True


def main():
    global SHUTDOWN
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if not _is_available():
        print("❌ [standby] Redis 不可用, 退出")
        sys.exit(1)

    print(f"🔄 [standby] 热备守护启动, 检查间隔={CHECK_INTERVAL}s")
    print(f"   Ctrl+C 停止\n")

    cycles = 0
    while not SHUTDOWN:
        cycles += 1
        try:
            result = standby_cycle()
            status = result.get("orchestrator", "?")
            if result.get("activated"):
                print(f"🚨 [standby] 热备接管! {result}")
            elif cycles % 10 == 0:
                print(f"💚 [standby] 周期检查 (#{cycles}): orchestrator={status}")
        except Exception as e:
            print(f"⚠️ [standby] 检查异常: {e}")

        for _ in range(CHECK_INTERVAL):
            time.sleep(1)
            if SHUTDOWN:
                break

    print("[standby] 守护退出")


if __name__ == "__main__":
    if "--once" in sys.argv:
        result = standby_cycle()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        main()
