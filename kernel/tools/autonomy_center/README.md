# Autonomy Center — 第11中心

## 职责
让5个AI不再"推一下动一下"。每次任务自动探索、发散、沉淀。

## 文件清单

| 文件 | 核心函数 | 职责 |
|:---|:---|:---|
| curiosity_engine.py | similar_case_search, relevant_data_fetch, web_investigation, pre_execution_gate | 执行前4项门禁 |
| divergence_trigger.py | solution_generator, approach_switcher, brainstorm_mode | 多方案生成+自动切换+脑暴 |
| proactivity_tracker.py | score_calculator, reward_trigger, penalty_trigger, leaderboard, daily_reset | 评分+奖惩+排行榜 |
| knowledge_contribution.py | contribution_collector, quality_scorer, knowledge_base_writer | 经验收集+质量评分+写回KB |
| autonomy_center.py | check_and_execute, agent_status | 主入口 |

## 触发条件

| 子模块 | 触发条件 | 时间限制 |
|:---|:---|:---|
| Curiosity Engine | 每次任务启动前 | 每项≤3s, 超时强制继续 |
| Divergence Trigger | 连续2次失败 | 立即触发 |
| Proactivity Tracker | 每个事件 | 实时更新Redis |
| Knowledge Contribution | 任务完成 | 异步, 不阻塞 |

## 评分规则

| 行为 | 分数 |
|:---|:---:|
| 主动搜索 | +1 |
| 找到相似案例 | +2 |
| 提出多方案 | +3 |
| 自我复盘 | +2 |
| 知识贡献 | +5 |
| 无督促完成 | +10 |
| 等指令 | -1 |
| 重复错误 | -3 |
| 忽略知识库 | -2 |
| 空转循环 | -5 |

## 与其他中心协作

```
用户发任务 → Orchestration Center
  → Autonomy Center (门禁+方案)
  → Execution Center (执行)
  → Evolution Center (学习)
  → Knowledge Center (沉淀)
```

## 每日重置
```bash
0 0 * * * python3 autonomy_center/proactivity_tracker.py daily-reset
```
