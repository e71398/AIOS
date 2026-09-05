#!/usr/bin/env python3
"""
Executor Daemon Pin Adapter
============================
芯片引脚层：将执行器守护进程注册到 AIOS 总线。
支持 opencode / claude / codex 三个执行器。
仅此文件与主板对接，daemon.py 内部不做任何修改。
"""
import sys
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import register_pin


def _lazy_executor():
    """延迟加载执行器主体"""
    if "aios_executor_daemon" not in sys.modules:
        main = Path(__file__).parent / "daemon.py"
        ns = {}
        exec(compile(open(main, encoding="utf-8").read(), main, "exec"), ns)
        sys.modules["aios_executor_daemon"] = type(sys)("aios_executor_daemon")
        for k, v in ns.items():
            if callable(v) or not k.startswith("_"):
                setattr(sys.modules["aios_executor_daemon"], k, v)
    return sys.modules["aios_executor_daemon"]


def _handle_run_once(executor: str):
    m = _lazy_executor()
    return m.run_once(executor)


def _handle_run_loop(executor: str):
    m = _lazy_executor()
    return m.run_loop(executor)


def register():
    """注册执行器引脚到 AIOS 总线。"""
    register_pin("executor.run_once", _handle_run_once,
                 "Executor: 执行单个任务 (opencode/claude/codex)")
    register_pin("executor.run_loop", _handle_run_loop,
                 "Executor: 持续监听队列循环")


if __name__ == "__main__":
    register()
    print("🔌 Executor pins registered")
