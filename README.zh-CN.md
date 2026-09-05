# AIOS（中文）

> AIOS 是一个实验性的 AI Agent 操作系统。
> 当前状态：Alpha —— **不可用于生产**。

本仓库发布 v0.1.0-alpha.1 源代码，供外部贡献者共同研究、审计、扩展或加固。

- **公开版本：** v0.1.0-alpha.1
- **内部开发代号：** 5.2.8
- **许可证：** Apache License 2.0
- **成熟度：** Alpha —— 不可用于生产

## 什么是 AIOS

AIOS 是一个长驻服务，通过 HTTP 入口网关接收用户请求，将请求拆解为规划步骤，
分派到执行器守护进程（每个上游 AI 工具一个），收集工具结果后交给评审者，
最终将结果写入 Redis 状态总线和文件系统结果存储。

每个组件（入口、规划、执行、评审、验证、监控、Web）都是独立的小进程，可以单独重启与审计。

## 当前状态（诚实披露）

| 项目 | 当前值 |
| --- | --- |
| 开发阶段 | Alpha |
| 产品就绪度 | NOT_READY |
| `POST /task` 正常入口 E2E 通过率 | 0 / 5 |
| MVP 可用 | NO |
| 默认 Provider | OFF（MiniMax 及所有外部 Provider 默认关闭） |

系统对能力与缺陷均如实披露。下列能力与缺陷均来自本仓库实际代码：

### 已确认能力

- 入口网关 HTTP 服务，接收任务提交与基于身份（identity）的会话请求。
- Orchestrator 守护进程：扫描 Redis 队列并将任务分派到规划 / 执行 / 评审等角色。
- 规划、执行、评审角色的进程骨架与契约级别的角色边界。
- 基于 Redis 的状态总线（`aios:bus:*`）与文件系统结果存储。
- Provider Adapter 接口（见 `docs/PROVIDER_ADAPTER.md`），自带一类 OpenAI 兼容的 HTTP
  Adapter，但**默认关闭**。
- Tool 注册表，默认拒绝（deny-by-default）权限模型。
- 用于本地开发的 systemd unit 模板。
- `examples/` 下的 Demo Provider、Demo Tool 与最小 Workflow —— 均明确标注为 Demo，
  不会发起任何真实模型调用。

### 已知缺陷（贡献前必读）

- systemd Orchestrator 当前报告 Provider Registry 状态不一致；`minimax-official`
  被判定为 `UNVERIFIED`，正常 `POST /task → Orchestrator` 闭环**未**端到端通过。
  本 Alpha 公开版本即建立在此现实之上，已创建对应 Issue。
- Planner 与 Reviewer 输出结构化 JSON 时存在 token 上限，长规划可能被截断。
- Executor 缺少正式的只读系统状态工具，也缺少沙箱写文件工具。
- Canary 与完整回归测试存在已知顺序依赖失败；这些失败在 CI 中**显式**标记为
  `KNOWN_FAILURES`，不会静默跳过，也不会改写统计。
- 当前 Alpha 没有生产级身份认证与多租户隔离。绑定地址默认仅 loopback。
- MiniMax 与所有外部 Provider 在 `features.toml` 中默认 OFF。

## 实际架构（高层）

```
                 ┌──────────────────────────────┐
   客户端  ─►    │  aios_entry_gateway (HTTP)   │  127.0.0.1:18801
                 └──────────────┬───────────────┘
                                │
                                ▼
                       Redis 状态总线（aios:bus:*）
                                │
                                ▼
              ┌─────────────────────────────────────┐
              │  aios_orchestrator（daemon）        │
              │  扫描队列 → 规划/执行/评审/验证/    │
              │  结果推送                            │
              └─────────────────────────────────────┘
                  │           │           │
                  ▼           ▼           ▼
              planner     executor     reviewer
                  │           │           │
                              ▼
                       ToolBus → tools
                              │
                              ▼
                  Provider Adapter（默认关闭）
                              │
                              ▼
                       结果存储 + 历史
```

当前核心阻断：`systemd Orchestrator → Registry 状态不一致 → minimax-official UNVERIFIED →
正常任务闭环阻断`。详见 `docs/CURRENT_LIMITATIONS.md` 与 `ROADMAP.md`。

## 安装（离线友好）

前置：

- Linux（推荐 Ubuntu 22.04+）
- Python 3.10+ 与 `pip`
- `redis-cli` / Redis 6+（本地 Redis 即可）
- 离线启动无需任何 API Key

```bash
git clone https://github.com/<owner>/AIOS.git
cd AIOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # 所有凭证字段均为占位符
./install.sh                      # 仅创建目录，不连接外部服务
./start.sh status
```

## 离线启动（无 API Key，不调用模型）

```bash
AIOS_OFFLINE=1 ./start.sh start
curl -sS http://127.0.0.1:18801/health
```

期望返回 HTTP 200 + `{"status":"ok","offline":true}`。

## Provider 配置

Provider 在 `config/features.toml` 中配置，默认全部关闭。启用 Provider 必须：

1. 在 `.env` 中设置 `EXTERNAL_PROVIDERS_ENABLED=1`。
2. 在 `.env` 中提供 API Key（如 `MINIMAX_API_KEY=YOUR_API_KEY_HERE`）。
3. 在 `features.toml` 中开启对应 Provider（如 `ai_provider_minimax_official = on`）。
4. 重启相关服务。

MiniMax 被视为付费 Provider，启用时须遵守每任务预算上限。详见 `docs/PROVIDER_ADAPTER.md`。

## 安全默认值

- `HTTP_BIND` 默认 `127.0.0.1`；公网绑定需显式开启。
- `EXTERNAL_PROVIDERS_ENABLED=0`（默认）。
- `AIOS_MINIMAX_OFFICIAL_ENABLED=0`（默认）。
- `TOOL_PERMISSION_DEFAULT=deny`（默认）。
- `AUTH_REQUIRED_FOR_NON_LOOPBACK=1`（默认）。
- systemd unit 仅作为模板发布，不包含任何机器专属路径或凭证。

## 测试

```bash
pytest kernel/tools/tests/test_stable_v1_cli_contract.py -q
pytest kernel/tools/tests/test_registry_path_boundary.py -q
./scripts/run_full_regression.sh   # 会报告 KNOWN_FAILURES —— 这些是已记录的，不是隐藏的
```

CI 设计为完全离线，不调用任何 Provider。

## 路线图

首批公开 Issue 见 `ROADMAP.md`，发布后即在公开仓库创建。包含但不限于：

1. 修复 Canonical Registry 与重复模块加载。
2. 打通 `POST /task → Orchestrator` 正常入口。
3. 为 Executor 增加只读 AIOS 状态工具。
4. 为 Executor 增加沙箱写文件工具。
5. 修复 Planner 结构化输出截断。
6. 修复 Reviewer 结构化输出截断。
7. 完整通过正常入口 5/5 E2E。
8. 修复原生 Canary 测试。
9. 修复测试顺序依赖。
10. 增加本地模型 Provider。
11. 完善 Provider Adapter 接口。
12. 完成身份认证与工作区隔离。

## 贡献

欢迎 Issue 与 PR。提交前请阅读 `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md`。

- `CONTRIBUTING.md` —— 分支规范、CI 期望、评审流程。
- `CODE_OF_CONDUCT.md` —— 社区准则。
- `SECURITY.md` —— 漏洞披露流程（请勿在公开 Issue 中提交）。
- `SUPPORT.md` —— 提问与支持范围。

## 许可证

Apache License 2.0，详见 `LICENSE`。第三方归属见 `THIRD_PARTY_NOTICES.md`。项目级 NOTICE 见
`NOTICE`。