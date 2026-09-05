#!/usr/bin/env python3
"""
OpenClaw Pin Adapter
====================
芯片引脚层：将 OpenClaw 的调度能力注册到 AIOS 总线。
仅此文件与主板对接，main.py 内部不做任何修改。
"""
import sys
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import register_pin


def _lazy_dispatcher():
    """延迟加载 OpenClaw 主体，确保不污染主板 import 链"""
    if "aios_dispatcher" not in sys.modules:
        main = Path(__file__).parent / "main.py"
        ns = {}
        exec(compile(open(main, encoding="utf-8").read(), main, "exec"), ns)
        sys.modules["aios_dispatcher"] = type(sys)("aios_dispatcher")
        for k, v in ns.items():
            if callable(v) or not k.startswith("_"):
                setattr(sys.modules["aios_dispatcher"], k, v)
    return sys.modules["aios_dispatcher"]


def _handle_dispatch(user_input, source="feishu", sender_id="local"):
    m = _lazy_dispatcher()
    return m.dispatch(user_input, source=source, sender_id=sender_id)


def _handle_aggregate(task_ids, timeout_seconds=60):
    m = _lazy_dispatcher()
    return m.aggregate_results(task_ids, timeout_seconds=timeout_seconds)


def _handle_status():
    m = _lazy_dispatcher()
    return m.show_status()


def register():
    """注册 OpenClaw 引脚到 AIOS 总线。启动时调用一次即可。"""
    register_pin("openclaw.dispatch", _handle_dispatch,
                 "OpenClaw compatibility channel -> Orchestrator submit")
    register_pin("openclaw.aggregate", _handle_aggregate,
                 "OpenClaw compatibility channel -> Orchestrator result")
    register_pin("openclaw.status", _handle_status,
                 "OpenClaw compatibility status")


if __name__ == "__main__":
    register()
    print("🔌 OpenClaw pins registered")
