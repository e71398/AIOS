# 当前任务里程碑 — 2026-07-12

> 触发问题: VSCode 健康诊断 + AIOS 入口定位修复 + 熔断死锁修复
> 状态: 已完成 + 验证通过 + 已落地文档

---

## 触发场景

1. 用户报告 Cline IDE 反复弹 "execute_command without requires_approval. Retrying..." — 根因是 MiniMax-M3 模型工具调用漏传 `requires_approval`，Cline 重试时模型继续漏，导致死循环。
2. openclaw 派了一个 1700 字的 VSCode 健康诊断任务，被 24 星座路由 + DAG 拆解自动拆成 46 个子任务，45 个 failed，1 个 completed。
3. opencode + claude 同时被熔断；Hermes 学习报告 77 次失败，top 错误是 `lock × 42`。

## 修复记录

### P0 — 必须立刻修

| ID | 文件 | 改动 | 影响 |
|----|------|------|------|
| P0-1 | `kernel/tools/aios_dispatcher.py` | `decompose_task()` 加 **800 字护栏**：>800 字任务整段派发不拆 | 1700 字诊断不再被切 46 个碎句 |
| P0-2 | `agents/dispatcher/openclaw/main.py` | 老 dispatcher 整体重写为新版 `aios_dispatcher.py` 的薄包装，**不再 register_pin**，避免同名 pin 双注册竞态 | 解决"有时老有时新"诡异行为 |
| P0-3 | `kernel/tools/aios_dispatcher.py` | `dispatch()` 顶部接 `OpenClawProtocolIntegration.on_task_received()` 的 `force_pipeline` 短路，强制覆盖 `preferred_executor='claude'` + `logic_depth='high'` | 协议层"复杂任务必须 force pipeline"在 hot path 真正生效 |

### P1 — 1 周内修

| ID | 文件 | 改动 | 影响 |
|----|------|------|------|
| P1-1 | `kernel/tools/aios_agent_mesh.py` | 12 星座里 `star_libra`(协调/仲裁), `star_sagittarius`(探索/研究), `star_aquarius`(创新/实验) 各从 `executor:None` 改为 `executor='claude'`，并同步更新 desc | 3 类严肃任务不再被错误降级到 opencode quick |
| P1-2a | `kernel/tools/aios_agent_mesh.py` | `classify_task()` backup 链 `if is_executor_halted(): backups.get(executor, executor)` **改成 while 循环 + _attempts 集合**，最多 3 跳跳出 | backup 链不会卡死：opencode 熔断→ claude 也熔断时还能继续走到 codex |
| P1-2b | `kernel/tools/aios_dispatcher.py` | `enqueue_task()` 前再查 `is_executor_halted(executor_tag)`，熔断时强制兜底到 codex（"codex 是最后兜底"），codex 也熔断则该子任务不入队 | dispatcher 入队环节的纵深防御，呼应 openclaw 报告里的"45 failed"现象 |
| 方案 B | `kernel/tools/aios_enforcer.py` | `EXEC_HALT_TTL: 1800s` 改 `300s` (5 分钟自动恢复) | 30 分钟太长，5 分钟更适配人机协作场景 |

### 已落地文档

| 文件 | 内容 |
|------|------|
| `${AIOS_HOME}/MEMORY.md` | 协议层 R1-R5 硬规则 + 调度层 D1-D4 最佳实践 + 应急手段 E1-E3 + 本次会话修复索引 |
| `${AIOS_HOME}/kernel/state/current_task.md` | 本文件：触发场景 + 修复记录 |

## 验证结果

| 项 | 命令 | 结果 |
|----|------|------|
| 静态语法 | `python3 -m py_compile` × 4 文件 | ✅ 全部通过 |
| Redis 熔断清除 | `python3 -c "import redis"` 读 halt TTL | ✅ opencode/claude/codex 全部已自动解除 |
| 12 星座分类回归 | `classify_task()` × 8 用例 | ✅ 全部命中预期 executor（之前因熔断错乱） |
| 800 字护栏 | 503 字任务入 `decompose_task` | ✅ 输出节点数 1 |
| 短任务拆解 | "查询天气；读配置；总结" | ✅ 输出节点数 3 |
| 单条任务 | "查一下内存使用" | ✅ 输出节点数 1 |
| quick_learn | `scan_failure_patterns` 1 天 | ✅ 76 次失败，top_errors lock × 42 / error × 30 |
| 队列状态 | `--status` | ✅ pending/locked/running/verifying/completed/failed 全 0 |

## 待办 / 后续

- [ ] `aios_bus.claim_next_task()` 端加 `executor_halted` 检查（防御纵深，呼应 MEMORY.md E1）
- [ ] 把 `EXEC_HALT_TTL` 暴露成 config 可读（避免下次修改要改源码）
- [ ] Hermes 报告里 lock × 42 的根因调查（bus.py 锁释放路径）
- [ ] 跑一次完整 dispatch 链路压测（多用户多任务并发）
- [ ] 考虑给 openclaw 加 vscode-healthcheck cron skill（用户原 openclaw 报告里建议）

## 关联文件

- `MEMORY.md` — 长期手册
- `kernel/tools/aios_dispatcher.py` — 新版调度器 (P0-1, P0-3, P1-2b)
- `kernel/tools/aios_agent_mesh.py` — 12 星座路由 (P1-1, P1-2a)
- `kernel/tools/aios_enforcer.py` — 协议 + 熔断 (方案 B)
- `agents/dispatcher/openclaw/main.py` — 老入口薄包装 (P0-2)
- `config/openclaw/protocol_integration.py` — force_pipeline 来源
