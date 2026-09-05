"""
Autonomy Policy — 自治等级 L0-L4 + 审批规则 + 限制策略
========================================================
AIOS v4.0 Autonomy & Initiative Center

设计原则:
  L0 - 仅建议, 不做任何自动动作
  L1 - 自动查资料 (内部知识库优先)
  L2 - 自动生成方案 + 多方案对比
  L3 - 自动执行低风险动作 (清理/归档/评分)
  L4 - 高风险动作需人工审批
"""
import os, sys, json

# 自治等级定义
LEVELS = {
    "L0": {"name": "仅建议",     "auto_search": False, "auto_plan": False, "auto_act": False, "require_approval": False},
    "L1": {"name": "自动查资料", "auto_search": True,  "auto_plan": False, "auto_act": False, "require_approval": False},
    "L2": {"name": "自动生成方案","auto_search": True,  "auto_plan": True,  "auto_act": False, "require_approval": False},
    "L3": {"name": "自动执行低风险","auto_search": True, "auto_plan": True,  "auto_act": True,  "require_approval": False},
    "L4": {"name": "需人工审批",  "auto_search": True,  "auto_plan": True,  "auto_act": True,  "require_approval": True},
}

# 任务复杂度 → 自治等级映射
COMPLEXITY_LEVEL_MAP = {
    "low":     "L1",
    "medium":  "L2",
    "high":    "L3",
}

# 高风险动作白名单 (L4 必须审批)
HIGH_RISK_ACTIONS = [
    "modify_config", "delete_critical_file", "external_message",
    "system_shutdown", "database_drop", "token_budget_override",
]

# 默认限制
DEFAULT_LIMITS = {
    "daily_token_limit": 500000,
    "background_job_limit": 20,
    "max_auto_tasks_per_hour": 5,
    "cleanup_whitelist_dirs": [
        "/tmp/aios_cache",
        "${AIOS_HOME}/cache",
        "${AIOS_HOME}/logs/diagnosis",
    ],
    "knowledge_decay_days": 30,
    "context_max_turns": 50,
}

class AutonomyPolicy:
    def __init__(self, config_path: str = None):
        self.config = DEFAULT_LIMITS.copy()
        if config_path and os.path.exists(config_path):
            try:
                self.config.update(json.load(open(config_path)))
            except: pass

    def get_autonomy_level(self, task: dict) -> str:
        """根据任务复杂度返回自治等级."""
        complexity = task.get("complexity", task.get("logic_depth", "low"))
        return COMPLEXITY_LEVEL_MAP.get(complexity, "L1")

    def is_action_allowed(self, action: str, task: dict) -> bool:
        """检查动作是否被允许."""
        level = self.get_autonomy_level(task)
        if level == "L0":
            return False
        if action in HIGH_RISK_ACTIONS:
            return level == "L4"
        return LEVELS[level]["auto_act"] if action not in ("search", "plan") else True

    def requires_approval(self, action: str) -> bool:
        """是否需要人工审批."""
        return action in HIGH_RISK_ACTIONS

    def get_daily_token_limit(self) -> int:
        return self.config.get("daily_token_limit", 500000)

    def get_background_job_limit(self) -> int:
        return self.config.get("background_job_limit", 20)

    def get_cleanup_whitelist(self) -> list:
        return self.config.get("cleanup_whitelist_dirs", [])

    def should_trigger_curiosity(self, task: dict) -> bool:
        level = self.get_autonomy_level(task)
        return LEVELS[level]["auto_search"]

    def should_trigger_divergence(self, task: dict) -> bool:
        level = self.get_autonomy_level(task)
        return LEVELS[level]["auto_plan"] and task.get("complexity", "low") in ("medium", "high")

    def can_auto_act(self, task: dict) -> bool:
        level = self.get_autonomy_level(task)
        return LEVELS[level]["auto_act"]
