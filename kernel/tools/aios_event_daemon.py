#!/usr/bin/env python3
"""
AIOS v4.0 事件守护进程 (Event Daemon)
=====================================
订阅总线事件，通过 Pin Registry 路由到对应处理器。
这是 Hermes 反馈回路的核心——让 learning.trigger 事件实时触发 quick_learn。

事件路由表:
  learning.trigger     → call_pin("hermes.quick_learn")
  task.completed       → call_pin("openclaw.status") + verification 触发
  task.failed          → call_pin("hermes.quick_learn") 实时学习失败
  security.violation   → 日志 + publish_event 告警
  system.heartbeat     → 状态检查
  contract.halt        → 紧急停机处理
  intel.discovered / opportunity.discovered → independent tool learning
  tool.upgraded        → refresh that tool chip's learning profile

用法:
  python3 aios_event_daemon.py              # 前台运行
  python3 aios_event_daemon.py --daemon     # 后台守护模式
"""
import sys, os, json, time, traceback
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import (
    _is_available, publish_event, subscribe_events, call_pin,
    get_task_state, list_pins,
)
from aios_observability import emit

POLL_INTERVAL = 2.0       # Redis 订阅轮询间隔
LEARN_COOLDOWN = 30       # quick_learn 冷却秒数（避免高频触发）
VERIFY_COOLDOWN = 60      # verification 冷却秒数


class EventRouter:
    """事件路由器：维护冷却状态，通过引脚分发事件。"""

    def __init__(self):
        self._last_learn = 0.0
        self._last_verify = 0.0
        self._event_count = 0

    def route(self, event: dict):
        """根据事件类型路由到对应处理器。"""
        self._event_count += 1
        event_type = event.get("type", "")
        source = event.get("source", "unknown")
        payload = event.get("payload", {})

        handlers = {
            "learning.trigger":   self._on_learning_trigger,
            "task.completed":     self._on_task_completed,
            "task.failed":        self._on_task_failed,
            "security.violation": self._on_security_violation,
            "system.heartbeat":   self._on_heartbeat,
            "contract.halt":      self._on_contract_halt,
            "intel.discovered":   self._on_intelligence,
            "opportunity.discovered": self._on_intelligence,
            "tool.upgraded":      self._on_tool_upgraded,
        }

        handler = handlers.get(event_type, self._on_unknown)
        try:
            handler(event)
        except Exception as e:
            print(f"  ⚠️ event_router: {event_type} handler error: {e}")

    def _on_learning_trigger(self, event: dict):
        """learning.trigger → 实时学习（有冷却）"""
        now = time.time()
        if now - self._last_learn < LEARN_COOLDOWN:
            return  # 冷却中，跳过
        self._last_learn = now

        p = event.get("payload", {})
        executor = p.get("executor", "?")
        status = p.get("status", "?")
        print(f"  🧠 learning.trigger [{executor}/{status}] → hermes.quick_learn")
        ok, result = call_pin("hermes.quick_learn")
        if ok:
            print(f"    ✅ quick_learn done: {json.dumps(result, ensure_ascii=False)[:120]}")
        else:
            print(f"    ⚠️ quick_learn skipped: {result}")

    def _on_task_completed(self, event: dict):
        """Verify only legacy standalone tasks; orchestrated tasks have one gate."""
        p = event.get("payload", {})
        task_id = p.get("task_id", "")
        if event.get("source") == "aios-orchestrator":
            return
        if task_id and get_task_state(task_id).get("parent_id"):
            return
        now = time.time()
        if now - self._last_verify < VERIFY_COOLDOWN:
            return
        self._last_verify = now

        if task_id:
            print(f"  ? legacy task.completed [{task_id[:12]}] ? verification")
            try:
                from aios_verification_gate import verify_specific
                result = verify_specific(task_id)
                if result.get("passed"):
                    print("    ? verify passed")
                else:
                    print(f"    ?? verify issues: {result.get('report', '')[:100]}")
            except Exception as e:
                print(f"    ?? verify error: {e}")
            try:
                sys.path.insert(0, "${AIOS_HOME}/kernel/centers/evolution_center")
                from evolution_controller import reconcile
                reconcile()
            except Exception:
                pass

    def _on_task_failed(self, event: dict):
        """task.failed → 快速学习失败模式（无冷却）"""
        p = event.get("payload", {})
        task_id = p.get("task_id", "")
        if task_id and get_task_state(task_id).get("parent_id"):
            return
        executor = p.get("executor", "?")
        print(f"  ❌ task.failed [{executor}/{task_id[:12]}] → hermes.quick_learn")
        ok, result = call_pin("hermes.quick_learn")
        if ok:
            print(f"    ✅ quick_learn done")
        else:
            print(f"    ⚠️ quick_learn: {result}")

    def _on_security_violation(self, event: dict):
        """security.violation → 事件日志 + 告警"""
        p = event.get("payload", {})
        detail = p.get("detail", "?")
        print(f"  🚨 security.violation: {detail[:200]}")
        emit("alert", source="event_daemon",
             payload={"type": "security", "detail": detail[:200]})

    def _on_heartbeat(self, event: dict):
        """system.heartbeat → 轻量状态"""
        pass  # 心跳事件仅用于保活，不处理

    def _on_contract_halt(self, event: dict):
        """contract.halt → 紧急停机告警"""
        detail = event.get("detail", "紧急规则触发")
        print(f"  🛑 CONTRACT HALT: {detail[:200]}")
        emit("alert", source="event_daemon",
             payload={"type": "halt", "detail": detail[:200]})

    def _on_intelligence(self, event: dict):
        """Feed normal intelligence to tool-local learning; gate core changes."""
        try:
            from aios_tool_evolution import ingest_intelligence
            routed = ingest_intelligence(event)
            print(f"  tool intelligence route: {routed.get('tools', [])}")
        except Exception as exc:
            print(f"  tool intelligence route failed: {exc}")
        payload = event.get("payload", {})
        text = f"{payload.get('title','')} {payload.get('summary','')}".lower()
        core_terms = ("aios core", "aios核心", "event bus", "总线协议",
                      "adapter contract", "适配器合同", "control plane", "控制面")
        if any(term in text for term in core_terms):
            try:
                sys.path.insert(0, "${AIOS_HOME}/kernel/centers/evolution_center")
                from evolution_controller import ingest_event
                ingest_event(event)
            except Exception as exc:
                print(f"  core evolution proposal failed: {exc}")

    def _on_tool_upgraded(self, event: dict):
        tool = event.get("payload", {}).get("tool", "")
        try:
            from aios_tool_evolution import learn
            if tool: learn(tool)
        except Exception:
            pass

    def _on_unknown(self, event: dict):
        """未识别事件 → debug 日志"""
        event_type = event.get("type", "?")
        if event_type and not event_type.startswith("system."):
            pass  # debug 级别


def run_loop():
    """主循环：持续订阅事件并路由。"""
    if not _is_available():
        print("❌ Redis 不可用，事件守护无法启动")
        return

    print("=" * 50)
    print("  AIOS Event Daemon")
    print("  订阅事件: learning.trigger / task.* / security.*")
    print(f"  冷却: learn={LEARN_COOLDOWN}s  verify={VERIFY_COOLDOWN}s")
    print("=" * 50)

    router = EventRouter()
    shutdown = False

    def signal_handler(sig, frame):
        nonlocal shutdown
        print("\n🛑 事件守护收到停止信号")
        shutdown = True

    import signal
    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except ValueError:
        pass  # 非主线程不能设置 signal handler

    try:
        from aios_bus import publish_event as pub
        pub("system.heartbeat", {"daemon": "event", "status": "starting"}, "event_daemon")
    except Exception:
        pass

    while not shutdown:
        try:
            events = subscribe_events(timeout=POLL_INTERVAL)
            for event in events:
                router.route(event)

            # 定期心跳
            if router._event_count % 30 == 0 and router._event_count > 0:
                try:
                    pub("system.heartbeat",
                        {"daemon": "event", "processed": router._event_count},
                        "event_daemon")
                except Exception:
                    pass

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  ⚠️ event loop error: {e}")
            time.sleep(5)

    print(f"\n事件守护退出，共处理 {router._event_count} 个事件")


def run_once():
    """单次订阅并处理事件（用于测试）。"""
    router = EventRouter()
    events = subscribe_events(timeout=3.0)
    for event in events:
        router.route(event)
    return router._event_count


if __name__ == "__main__":
    if "--once" in sys.argv:
        count = run_once()
        print(f"处理 {count} 个事件")
    else:
        run_loop()
