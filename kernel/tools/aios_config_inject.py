#!/usr/bin/env python3
"""
AIOS v4.0 统一配置注入器
=======================
从 ~/.aios/models.toml 读取配置, 注入到各AI子模块。
每个子模块启动时调用 inject(system) 即可。
"""

import os, sys, json
from pathlib import Path

MODELS_CONFIG = Path.home() / ".aios" / "models.toml"

def load_config():
    """加载统一配置."""
    try:
        import tomllib
        return tomllib.loads(MODELS_CONFIG.read_text())
    except ImportError:
        # Python < 3.11 fallback
        import re
        cfg = {}
        current = None
        for line in MODELS_CONFIG.read_text().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"): continue
            m = re.match(r'\[(.+)\]', line)
            if m:
                current = m.group(1)
                cfg[current] = {}
                continue
            if "=" in line and current:
                k, v = line.split("=", 1)
                cfg[current][k.strip()] = v.strip().strip('"')
        return cfg

def get_submodule_config(system: str) -> dict:
    """获取某个子模块的完整配置."""
    cfg = load_config()
    submodules = cfg.get("submodules", {})
    sm = submodules.get(system, {})
    model_name = sm.get("model", "none")
    models = cfg.get("models", {})
    model_cfg = models.get(model_name, {}) if model_name != "none" else {}
    return {
        "system": system,
        "tool": sm.get("tool", system),
        "role": sm.get("role", ""),
        "model_provider": model_name,
        "model_id": sm.get("model_id", ""),
        "api_base": model_cfg.get("api_base", model_cfg.get("api_base_anthropic", "")),
        "api_key": model_cfg.get("api_key", ""),
        "type": model_cfg.get("type", "local"),
    }

def inject_env(system: str) -> bool:
    """注入环境变量 (Claude Code用)."""
    c = get_submodule_config(system)
    if not c["api_key"]: return False
    os.environ[f"AIOS_{system.upper()}_API_KEY"] = c["api_key"]
    os.environ[f"AIOS_{system.upper()}_BASE_URL"] = c["api_base"]
    os.environ[f"AIOS_{system.upper()}_MODEL"] = c["model_id"]
    return True

def print_injection(system: str):
    """打印注入信息."""
    c = get_submodule_config(system)
    print(f"[{system}] {c['tool']}")
    print(f"  角色: {c['role']}")
    print(f"  模型: {c['model_provider']}/{c['model_id']}")
    print(f"  端点: {c['api_base']}")
    print(f"  类型: {c['type']}")

def inject_all():
    """检查所有子模块配置."""
    cfg = load_config()
    submodules = cfg.get("submodules", {})
    for system in submodules:
        print_injection(system)
        print()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print_injection(sys.argv[1])
    else:
        inject_all()
