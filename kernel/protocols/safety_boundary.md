# AIOS v4.0 真实世界边界协议
## Safety Boundary Protocol v1.0

**版本**: 1.0
**最后更新**: 2026-07-05
**约束对象**: 所有已注册的 Agent（dispatcher, evolution, executor, verifier）

---

## 第一章：确定性原则（Certainty Doctrine）

### 1.1 语言确定性
所有面向用户的输出，严格禁止以下词汇：

| 禁止词汇 | 替代要求 |
|:---|:---|
| "可能" | 必须给出具体概率或明确不确定性范围 |
| "大概" | 替换为具体数值范围 |
| "也许" | 替换为因果链描述 |
| "我认为" | 替换为"根据[数据源]的分析" |

### 1.2 结论确定性
在以下领域，所有输出必须经过 **双签章验证**：
- **工业控制**：PLC 逻辑、GPIO 操作、变频控制
- **法律合规**：合同条款、监管合规、责任归属
- **医疗健康**：诊断建议、用药方案、手术流程
- **金融财务**：报价计算、预算分配、风险评估

### 1.3 不确定性标注
当信息不足无法给出确定性结论时，必须格式如下：
```
[UNABLE_TO_DETERMINE]
- 缺失信息: [具体列出]
- 可能范围: [数值/范围]
- 建议行动: [下一步获取信息的路径]
- 置信度: [低/中/高]
```

---

## 第二章：物理安全红线（Physical Safety Protocol）

### 2.1 硬件操作前必须通过的检查项

所有涉及以下操作的指令，必须在 World Model 中完成前置模拟：

```
检查清单：
□ 时序逻辑验证（Timing Constraint Check）
□ 电压/电流安全边界（Electrical Safety Check）
□ 物理冲击风险（Mechanical Impact Risk）
□ 环境温湿度容忍度（Environmental Tolerance）
□ 紧急停止路径（Emergency Stop Path）
□ 信号隔离验证（Signal Isolation Verification）
```

### 2.2 工业控制专项约束

```yaml
plc_safety_rules:
  frequency_switching:
    max_rate_per_second: 5
    min_delay_ms: 200
    violation_action: "BLOCK_AND_ALERT"

  gpio_output:
    voltage_check: true
    max_current_ma: 500
    short_circuit_protection: "MANDATORY"

  emergency_stop:
    always_available: true
    override_all: true
    manual_reset_required: true

  analog_signal:
    range_validation: true
    noise_filter_required: true
```

### 2.3 危险操作分级与响应

| 危险等级 | 操作类型 | 响应动作 |
|:---:|:---|:---|
| **L0（禁止）** | 并行双电源切换 | 直接拒绝，发送 [SYSTEM_HALT] |
| **L1（强验证）** | 修改 PLC 控制逻辑 | World Model 模拟 + Hermes 复核 |
| **L2（审批）** | 批量修改 BOM 表 | 暂停，等待人类审批 |
| **L3（记录）** | 普通文档修改 | 执行但记录到 audit_log |

---

## 第三章：经济熔断线（Economic Circuit Breaker）

### 3.1 成本阈值配置

```yaml
cost_limits:
  per_task:
    token_budget_usd: 10.00
    compute_time_minutes: 30
    gpu_memory_mb: 16384

  per_hour:
    total_token_budget_usd: 50.00
    total_compute_minutes: 120

  escalation_rules:
    - trigger: "token_cost > $10"
      action: "HALT_AND_REQUEST_HUMAN_APPROVAL"

    - trigger: "task_duration > 30min"
      action: "SEND_PROGRESS_REPORT_AND_WAIT"

    - trigger: "gpu_memory > 80%"
      action: "DEGRADE_TO_CPU_FALLBACK_MODEL"

    - trigger: "consecutive_failures >= 3"
      action: "SYSTEM_HALT_AND_NOTIFY_USER"
```

---

## 第四章：责任追溯机制（Accountability Protocol）

### 4.1 决策存证要求

每个 Agent 的每个动作必须记录以下字段：

```
{
  "timestamp": "ISO8601格式",
  "agent_id": "执行者唯一标识",
  "task_id": "关联任务ID",
  "action_type": "动作类型",
  "input_summary": "输入摘要（512字内）",
  "output_pointer": "输出数据UUID",
  "reasoning_chain": "推理链（完整因果）",
  "confidence_score": "置信度（0-1）",
  "verification_status": "PASS/FAIL/PENDING"
}
```

### 4.2 溯源链条要求

输出给用户的结果必须包含：
```
最终交付物
├── 基于: [知识库文件列表]
├── 分析: [Agent 执行链路]
├── 验证: [Verifier 签章记录]
└── 可追溯: [完整推理链链接]
```

---

## 第五章：熔断机制（Circuit Breaker Protocol）

### 5.1 系统级熔断触发条件

```python
CIRCUIT_BREAKER_RULES = {
    "consecutive_verification_failures": 3,    # → [SYSTEM_HALT]
    "token_budget_exceeded": True,             # → [HALT_PENDING_APPROVAL]
    "world_model_simulation_conflict": True,   # → [BLOCK_AND_REVIEW]
    "hermes_pattern_mismatch_count": 10,       # → [SLOW_LOOP_ALERT]
    "human_approval_timeout_hours": 24,        # → [AUTO_ESCALATE]
}
```

### 5.2 熔断恢复流程

```
熔断触发
  ↓
发送 [SYSTEM_HALT] 通知到飞书
  ↓
保存当前状态到 Checkpoint
  ↓
等待人类审批（最长24小时）
  ↓
人类决策：继续 / 修改参数 / 终止
  ↓
根据决策恢复或关闭任务
```

---

## 第六章：人类干预通道（Human-in-the-Loop Channel）

### 6.1 必须触发人工审批的场景

```yaml
mandatory_human_approval:
  - "涉及物理硬件的直接控制"
  - "单次任务成本超过 $10"
  - "涉及核心知识库修改"
  - "系统连续失败 3 次仍无解"
  - "跨行业首次任务（系统未见过的领域）"
  - "安全等级 L0-L1 的操作"
```

### 6.2 审批格式规范

```markdown
## 人类审批请求

**任务ID**: [Task_ID]
**问题类型**: [分类]
**系统建议**: [来自 Agent 的推荐方案]
**理由**: [Reasoning_Log 摘要]

---
**您的决策**:
- [ ] 批准，继续执行
- [ ] 修改参数后继续（请在下方说明）
- [ ] 拒绝，终止任务
- [ ] 转交其他 Agent 处理

**您的修改/指令**:
[填写区域]
```

---

## 第七章：环境边界（Environmental Boundary）

### 7.1 文件系统访问规则

```yaml
filesystem_rules:
  read_only_zones:
    - "${AIOS_HOME}/kernel"           # 核心协议
    - "${AIOS_HOME}/knowledge/specs"   # 行业规范

  read_write_zones:
    - "${AIOS_HOME}/sandbox"          # 工作区
    - "${AIOS_HOME}/logs"             # 日志

  no_access_zones:
    - "/etc"                               # 系统配置
    - "/root"                              # 根目录
    - "/home/*/.*"                         # 用户私人文件

  sandbox_rules:
    max_file_size_mb: 100
    allowed_extensions: [".py", ".md", ".yaml", ".json", ".sh", ".sql"]
    blocked_extensions: [".exe", ".dll", ".bat", ".ps1"]
```

### 7.2 网络访问规则

```yaml
network_rules:
  allowed_outbound:
    - "api.deepseek.com"           # 模型调用
    - "api.minimaxi.com"           # 模型调用
    - "claude.ai"                  # 模型调用

  blocked_outbound:
    - "*.internal.corp"            # 内部网络
    - "bank.*.com"                 # 金融接口（防泄露）

  no_network_zones:
    - "plc_control_layer"
    - "hardware_io_layer"
```

---

## 附录：快速参考卡片

```
┌────────────────────────────────────────────────────────┐
│                   AIOS 安全边界速查                     │
├────────────────────────────────────────────────────────┤
│ ⚠️  物理操作 → 必须过 World Model 模拟                  │
│ 💰  单次 >$10 → 暂停，等人类审批                        │
│ 🔒  核心资产 → 只读，禁止直接写入                        │
│ 🔄  失败3次 → [SYSTEM_HALT]                            │
│ 📋  所有决策 → 必须附带 Reasoning_Log                   │
│ 📨  结果交付 → 必须通过 Verifier Exit 0                 │
│ 🔗  Agent通信 → 必须用 UUID 指针，禁止明文长传           │
└────────────────────────────────────────────────────────┘
```
