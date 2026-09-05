#!/usr/bin/env python3
"""
5个AI Wrapper — 每个AI外面套一层Agent协议
============================================
不改AI工具本身, 只加 Wrapper 层:
  register → Agent Mesh
  heartbeat → 每30s
  dispatch → Event Bus事件 → AI处理 → 结果回Event Bus
"""
import sys, os, subprocess, time
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_base_agent import BaseAgent
from aios_bus import publish_event, heartbeat, lifecycle_set


# ── Hermes Wrapper ──
class HermesWrapper(BaseAgent):
    def __init__(self):
        super().__init__("hermes", "evolution",
                         ["learning", "pattern_analysis", "slow_loop", "feishu"],
                         model="MiniMax-M3", version="0.16.0")

    def on_start(self):
        # Hermes有独立进程, 不启动, 仅监控
        pass

    def on_event(self, event):
        etype = event.get("type", "")
        if "task.completed" in etype or "task.failed" in etype:
            # Hermes学习: 记录完成/失败事件
            self.publish("learning.triggered", {
                "event": etype, "data": str(event)[:500],
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        return None

    def on_stop(self): pass


# ── OpenClaw Wrapper ──
class OpenClawWrapper(BaseAgent):
    def __init__(self):
        super().__init__("openclaw", "orchestration",
                         ["task_decompose", "workflow_dispatch", "feishu",
                          "telegram", "multi_agent_coordination"],
                         model="MiniMax-M3", version="latest")

    def on_start(self): pass

    def on_event(self, event):
        etype = event.get("type", "")
        if "task.created" in etype:
            self.publish("task.assigned", {
                "task": event.get("task", ""), "assigned_by": "openclaw",
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        return None

    def on_stop(self): pass


# ── OpenCode Wrapper ──
class OpenCodeWrapper(BaseAgent):
    def __init__(self):
        super().__init__("opencode", "execution",
                         ["shell", "docker", "filesystem", "mcp", "cli"],
                         model="free", version="1.17.13")

    def on_start(self): pass

    def on_event(self, event):
        return None

    def on_stop(self): pass


# ── Claude Code Wrapper ──
class ClaudeWrapper(BaseAgent):
    def __init__(self):
        super().__init__("claude", "execution",
                         ["code_analysis", "architecture", "debugging",
                          "deep_reasoning", "complex_refactor"],
                         model="DeepSeek-V4-Pro", version="latest")

    def on_start(self): pass

    def on_event(self, event):
        return None

    def on_stop(self): pass


# ── Codex Wrapper ──
class CodexWrapper(BaseAgent):
    def __init__(self):
        super().__init__("codex", "execution",
                         ["parallel_task", "async_workflow", "batch", "backup"],
                         model="DeepSeek-V4-Pro", version="1.12.5")

    def on_start(self): pass

    def on_event(self, event):
        return None

    def on_stop(self): pass


# ── 批量启动 ──
WRAPPERS = {
    "hermes": HermesWrapper,
    "openclaw": OpenClawWrapper,
    "opencode": OpenCodeWrapper,
    "claude": ClaudeWrapper,
    "codex": CodexWrapper,
}


def boot_all():
    """启动所有5个Wrapper."""
    agents = {}
    for name, cls in WRAPPERS.items():
        agent = cls()
        agent.start()
        agents[name] = agent
        print(f"  ✅ {name} Wrapper online")
    return agents


def boot_one(name: str):
    """启动单个Wrapper."""
    if name in WRAPPERS:
        agent = WRAPPERS[name]()
        agent.start()
        return agent
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        agent = boot_one(sys.argv[1])
        if agent: agent.run()
    else:
        agents = boot_all()
        # 保持运行
        try:
            while True:
                for name, a in agents.items():
                    s = a.status()
                    icon = "🟢" if s["heartbeat_age_s"] < 90 else "🔴"
                    print(f"  {icon} {name:10s} {s['status']:8s} hb={s['heartbeat_age_s']}s")
                time.sleep(30)
        except KeyboardInterrupt:
            for a in agents.values():
                a.shutdown()
