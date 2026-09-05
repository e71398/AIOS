# AIOS Agent Module — 独立Agent管理模块

## 位置
`${AIOS_HOME}/kernel/tools/`

## 模块关系
```
Agent Module (独立, 管理所有AI)
  ├── base_agent.py      — Agent协议 (register/heartbeat/dispatch/publish/shutdown)
  ├── agent_wrappers.py  — 5个AI的Wrapper (AI只能调用, 不能修改)
  ├── agent_supervisor.py — 心跳监控+自动重启
  └── 各AI (被管理方)    — Hermes/OpenClaw/OpenCode/ClaudeCode/Codex
```

## 新Agent接入流程 (3步)

### 第1步: 继承BaseAgent
```python
from aios_base_agent import BaseAgent

class MyNewAgent(BaseAgent):
    def __init__(self):
        super().__init__("my_agent", "execution",
                         ["my_capability_1", "my_capability_2"],
                         model="deepseek-v4-pro", version="1.0")

    def on_start(self):
        # Agent启动时的初始化
        pass

    def on_event(self, event):
        # 处理Event Bus事件
        return {"result": "processed"}

    def on_stop(self):
        # Agent关闭时的清理
        pass
```

### 第2步: 启动Agent
```python
from aios_agent_wrappers import boot_one

agent = boot_one("my_agent")  # 自动register+heartbeat+event监听
agent.run()                    # 阻塞运行
```

### 第3步: Supervisor自动发现
```bash
python3 aios_agent_supervisor.py --once  # 手动扫描
python3 aios_agent_supervisor.py         # 持续运行(每60s)
```

## Agent数: 5→100不需要改任何代码

## 每个AI只能调用Agent Module, 不能修改它
