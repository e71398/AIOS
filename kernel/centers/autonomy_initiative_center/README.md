# Autonomy & Initiative Center

> AIOS v4.0 主动性与能动中心

## 定位

为 AIOS 增加四种系统级能力：
- **主动探索**: 执行前查知识/案例/外部资料
- **主动发散**: 复杂任务自动生成多方案 (快/稳/省)
- **主动增强**: 从成功/失败中提炼经验
- **主动清理**: 清理过期/重复/低价值内容

## 目录

| 文件 | 功能 |
|---|---|
| `autonomy_center.py` | 主入口，串联所有引擎 |
| `curiosity_engine.py` | 三级检索：内部知识→历史任务→外部 |
| `divergence_engine.py` | 三方案生成：A最快/B最稳/C最低成本 |
| `proactivity_score.py` | Agent 评分，写入 Redis |
| `autonomy_policy.py` | L0-L4 自治等级 + 审批规则 |

## 自治等级

| 等级 | 自动查资料 | 自动方案 | 自动执行 | 需审批 |
|---|---|---|---|---|
| L0 | ❌ | ❌ | ❌ | - |
| L1 | ✅ | ❌ | ❌ | - |
| L2 | ✅ | ✅ | ❌ | - |
| L3 | ✅ | ✅ | ✅ | ❌ |
| L4 | ✅ | ✅ | ✅ | ✅ |

## 与其他模块关系

- **Orchestration Center**: 接收任务 → 返回 autonomy_plan
- **Execution Center**: 下发执行前参考包 + 推荐方案
- **Hermes**: 读取复盘日志 → 写入模式提炼
- **Knowledge Center**: 查询/写入知识
- **Observability Center**: 暴露评分/排行/清理状态

## 测试

```python
from autonomy_center import AutonomyCenter
ac = AutonomyCenter()
task = {"task_name": "优化AIOS系统性能", "complexity": "medium"}
plan = ac.check_and_plan(task)
print(plan)
```
