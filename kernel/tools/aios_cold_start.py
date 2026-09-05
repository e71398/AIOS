#!/usr/bin/env python3
"""优化5: 冷启动优化 — 批量注册+预加载+顺序启动"""
import sys, time, threading
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools"); sys.path.insert(0, str(TOOLS))
from aios_bus import init_registry, heartbeat, _is_available, list_pins

BOOT_ORDER = ["runtime", "governance", "capability", "orchestration", "execution", "evolution", "knowledge"]
AI_AGENTS = ["openclaw", "hermes", "opencode", "claude", "codex"]


def _register_pins():
    """导入 AI 模块触发 pin 注册，确保 call_pin 立即可用。"""
    for module_name in ["aios_dispatcher", "aios_hermes_learn", "aios_executor_daemon",
                         "aios_entry_feishu", "aios_entry_telegram", "aios_standby_daemon",
                         "aios_result_push", "aios_consensus_arbiter",
                         "aios_mid_loop", "aios_resource_allocator",
                         "aios_monitor"]:
        try:
            __import__(module_name)
            print(f"  📦 {module_name} → pins registered")
        except Exception as e:
            print(f"  ⚠️ {module_name} import failed: {e}")


def _start_event_daemon():
    """后台启动事件守护进程（连接 learning.trigger → hermes）。"""
    try:
        from aios_event_daemon import run_loop as event_loop
        t = threading.Thread(target=event_loop, daemon=True,
                             name="event-daemon")
        t.start()
        print(f"  🔄 Event Daemon started (thread: event-daemon)")
        return t
    except Exception as e:
        print(f"  ⚠️ Event Daemon start failed: {e}")
        return None


def _start_enforcer_daemon():
    """后台启动 enforcer 守护进程（合约检查+协议强制）。"""
    try:
        from aios_enforcer_daemon import run_daemon as enforcer_loop
        t = threading.Thread(target=enforcer_loop, daemon=True,
                             name="enforcer-daemon")
        t.start()
        print(f"  🛡️ Enforcer Daemon started (thread: enforcer-daemon)")
        return t
    except Exception as e:
        print(f"  ⚠️ Enforcer Daemon start failed: {e}")
        return None


def _start_result_push_daemon():
    """后台启动结果回推送守护（监听 task.completed→回调推回用户）。"""
    try:
        from aios_result_push import run_loop as push_loop
        t = threading.Thread(target=push_loop, daemon=True,
                             name="result-push")
        t.start()
        print(f"  📬 Result Push Daemon started (thread: result-push)")
        return t
    except Exception as e:
        print(f"  ⚠️ Result Push Daemon start failed: {e}")
        return None


def warm_boot(with_daemons=True):
    """正常启动: 注册引脚 + 初始化中心 + 启动守护（可选）。"""
    print("🔥 AIOS Warm boot...")
    init_registry()

    # Step 1: 注册所有 AI 引脚（导入即注册）
    if _is_available():
        print("\n[1/5] Pin Registration:")
        _register_pins()

        # Step 2: 系统心跳
        print("\n[2/5] Heartbeat:")
        for s in AI_AGENTS:
            try:
                if heartbeat(s):
                    print(f"  💓 {s}")
            except Exception:
                pass

        # Step 3: 网格初始化
        print("\n[3/5] Agent Mesh:")
        try:
            from aios_agent_mesh import init_mesh
            init_mesh()
            print("  ✅ Agent mesh initialized")
        except Exception:
            print("  ⏭️ Agent mesh skipped")

        # Step 4: 语义搜索索引
        print("\n[4/5] Semantic Search:")
        try:
            from aios_semantic_search import reindex_all
            reindex_all()
            print("  ✅ Semantic search indexed")
        except Exception:
            print("  ⏭️ Semantic search skipped")

        # Step 5: 后台守护（可选）
        if with_daemons:
            print("\n[5/5] Background Daemons:")
            _start_event_daemon()
            _start_enforcer_daemon()
            _start_result_push_daemon()
        else:
            print("\n[5/5] Daemons: skipped (--no-daemon)")

        # 显示已注册引脚
        print("\n🔌 Registered Pins:")
        list_pins()
    else:
        print("  ⚠️ Redis not available, pin registration skipped")

    print(f"\n✅ AIOS Warm boot complete ({len(BOOT_ORDER)} centers)")
    return {"status": "warm", "centers": len(BOOT_ORDER), "with_daemons": with_daemons}


def emergency_reboot():
    """紧急重启: 跳过非关键中心, 先恢复调度+执行."""
    print("🚨 Emergency reboot...")
    init_registry()
    for s in ["openclaw", "claude", "codex"]:
        try:
            if heartbeat(s):
                print(f"  💓 {s}")
        except Exception:
            pass
    # 紧急模式下启动事件守护（保证反馈回路）
    _start_event_daemon()
    print("✅ Emergency reboot (minimal)")
    return {"status": "emergency", "centers": 3}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "warm"
    if cmd == "warm":
        warm_boot(with_daemons="--no-daemon" not in sys.argv)
    elif cmd == "emergency":
        emergency_reboot()
    elif cmd == "pins":
        # 只注册引脚，不启动其他
        _register_pins()
        list_pins()
