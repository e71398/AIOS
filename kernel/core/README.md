# AIOS 执行规程 v1.0

## 目的
确保所有AI按标准流程执行，防止跳过关键步骤或擅自行动。

## 适用
OpenClaw / OpenCode / Claude Code / Codex / Hermes

## 6步流程
1. 任务接收与分类 → PIPELINE_TASK / DIRECT_TASK
2. 管线注册 (PIPELINE_TASK)
3. 执行前检查 (pre_execution_check) — **不通过则拦截**
4. 执行与记录
5. 完成后处理
6. 违规处理

## 禁止
- 跳过执行前检查
- 执行未分配任务
- 重复执行已完成任务
- 绕过L4审批

## 集成
基于 `aios_enforcer.py` + `pipeline_enforcer/`
