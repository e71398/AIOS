#!/usr/bin/env python3
"""
Hermes Pin Adapter
==================
芯片引脚层：将 Hermes 学习能力注册到 AIOS 总线。
仅此文件与主板对接，main.py 内部不做任何修改。
"""
import sys
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import register_pin


def _lazy_hermes():
    """延迟加载 Hermes 主体"""
    if "aios_hermes_learn" not in sys.modules:
        main = Path(__file__).parent / "main.py"
        ns = {}
        exec(compile(open(main, encoding="utf-8").read(), main, "exec"), ns)
        sys.modules["aios_hermes_learn"] = type(sys)("aios_hermes_learn")
        for k, v in ns.items():
            if callable(v) or not k.startswith("_"):
                setattr(sys.modules["aios_hermes_learn"], k, v)
    return sys.modules["aios_hermes_learn"]


def _handle_learning_cycle():
    m = _lazy_hermes()
    return m.run_learning_cycle()


def _handle_quick_learn():
    m = _lazy_hermes()
    return m.quick_learn()


def _handle_push_strategy(analysis):
    m = _lazy_hermes()
    return m.push_strategy_to_redis(analysis)


def register():
    """注册 Hermes 引脚到 AIOS 总线。"""
    register_pin("hermes.learning_cycle", _handle_learning_cycle,
                 "Hermes: 完整学习循环（失败分析→策略校准→技能提取）")
    register_pin("hermes.quick_learn", _handle_quick_learn,
                 "Hermes: 轻量实时学习")
    register_pin("hermes.push_strategy", _handle_push_strategy,
                 "Hermes: 推送调度策略到 Redis")


if __name__ == "__main__":
    register()
    print("🔌 Hermes pins registered")
