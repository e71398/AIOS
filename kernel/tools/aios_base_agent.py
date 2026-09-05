#!/usr/bin/env python3
"""
AIOS Agent Protocol — 所有Agent必须实现的标准接口
====================================================
每个Agent接入AIOS必须实现: register / heartbeat / dispatch / publish / shutdown
"""
import sys, os, json, time, threading
from pathlib import Path
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import (lifecycle_register, lifecycle_set, lifecycle_get,
                      heartbeat, publish_event, subscribe_events, _is_available)


class BaseAgent(ABC):
    """Agent协议 — 每个Agent必须继承并实现"""

    def __init__(self, agent_id: str, agent_type: str,
                 capabilities: List[str], model: str = "", version: str = "1.0"):
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.capabilities = capabilities
        self.model = model
        self.version = version
        self._heartbeat_thread = None
        self._running = False

    @abstractmethod
    def on_event(self, event: Dict) -> Optional[Dict]:
        """接收Event Bus事件, 处理并返回结果. 子类必须实现."""
        pass

    @abstractmethod
    def on_start(self):
        """Agent启动时的初始化逻辑. 子类实现."""
        pass

    @abstractmethod
    def on_stop(self):
        """Agent关闭时的清理逻辑. 子类实现."""
        pass

    # ── 标准实现, 子类无需覆盖 ──

    def register(self) -> bool:
        """向Agent Mesh注册."""
        ok = lifecycle_register(
            self.agent_id, self.agent_type, self.capabilities,
            pid=os.getpid()
        )
        if ok:
            lifecycle_set(self.agent_id, "RUNNING")
            publish_event("agent.online", {
                "agent": self.agent_id, "type": self.agent_type,
                "capabilities": self.capabilities, "model": self.model,
            }, self.agent_id)
        return ok

    def send_heartbeat(self) -> bool:
        """发送心跳."""
        ok = heartbeat(self.agent_id)
        if ok:
            lifecycle_set(self.agent_id, "RUNNING")
        return ok

    def publish(self, event_type: str, payload: Dict) -> bool:
        """发布事件到Event Bus."""
        return publish_event(event_type, payload, self.agent_id)

    def dispatch(self, event: Dict) -> Optional[Dict]:
        """接收并处理事件. 调用子类的 on_event."""
        try:
            result = self.on_event(event)
            if result:
                self.publish("task.completed", result)
            return result
        except Exception as e:
            self.publish("task.failed", {"error": str(e), "agent": self.agent_id})
            return None

    def shutdown(self):
        """从注册表注销."""
        self._running = False
        lifecycle_set(self.agent_id, "STOPPING")
        self.on_stop()
        lifecycle_set(self.agent_id, "OFFLINE")
        publish_event("agent.offline", {"agent": self.agent_id}, self.agent_id)

    def _heartbeat_loop(self, interval: int = 30):
        """后台心跳线程."""
        while self._running:
            self.send_heartbeat()
            time.sleep(interval)

    def _event_loop(self):
        """后台事件监听线程."""
        while self._running:
            events = subscribe_events(timeout=10.0)
            for event in events:
                try:
                    self.dispatch(event)
                except Exception:
                    pass

    def start(self, heartbeat_interval: int = 30):
        """启动Agent: 注册→心跳→事件监听."""
        self._running = True
        self.on_start()
        self.register()

        # 心跳线程
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, args=(heartbeat_interval,), daemon=True)
        self._heartbeat_thread.start()

        # 事件监听线程
        self._event_thread = threading.Thread(
            target=self._event_loop, daemon=True)
        self._event_thread.start()

        print(f"🤖 [{self.agent_id}] Agent started | type={self.agent_type} "
              f"caps={self.capabilities} model={self.model}")

    def status(self) -> Dict:
        """返回当前状态."""
        mesh = lifecycle_get(self.agent_id)
        return {
            "agent_id": self.agent_id,
            "type": self.agent_type,
            "capabilities": self.capabilities,
            "model": self.model,
            "version": self.version,
            "status": mesh.get("status", "UNKNOWN"),
            "heartbeat_age_s": mesh.get("heartbeat_age_s", 999),
            "pid": mesh.get("pid", "0"),
        }

    def run(self):
        """阻塞运行, 直到收到停止信号."""
        self.start()
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown()
