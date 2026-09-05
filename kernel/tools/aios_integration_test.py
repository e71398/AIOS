#!/usr/bin/env python3
"""
AIOS v4.0 End-to-End Integration Test
======================================
验证6个Phase全部就绪: 从任务提交到验证的全链路。

测试项:
  1. 核心文件完整性
  2. 所有服务运行状态
  3. Entry Gateway API (创建/查询/验证)
  4. Protocol Enforcer (拦截危险指令)
  5. Task State Machine (状态转换)
  6. Contract Enforcer (7条规则)
  7. Error Classifier (E001-E005)
  8. Bus读写 + 事件发布
  9. 全链路: 提交 → 拦截/放行 → 入队
"""

import sys, os, json, time, subprocess
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
sys.path.insert(0, str(TOOLS))

PASS, FAIL = 0, 0

def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}  {detail}")

def http_get(path):
    import urllib.request
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:18801{path}", timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def http_post(path, data):
    import urllib.request
    from urllib.error import HTTPError
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(f"http://127.0.0.1:18801{path}",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=10)
        return json.loads(r.read())
    except HTTPError as e:
        # 非2xx 响应也是有效响应 — 读取body里的JSON
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}", "ok": False}
    except Exception as e:
        return {"error": str(e)}


print("=" * 60)
print("  AIOS v4.0 端到端集成测试")
print(f"  {datetime.now().isoformat()}")
print("=" * 60)

# ── 1. 核心文件 ──
print("\n[1/9] 核心文件完整性")
for f in ["aios_bus.py", "aios_dispatcher.py", "aios_entry_gateway.py",
           "aios_enforcer.py", "aios_contract_enforcer.py", "aios_firewall.py",
           "aios_hermes_learn.py", "aios_error_classifier.py", "aios_verification_gate.py",
           "verify.py", "world_model_runner.py", "aios_gateway.py",
           "aios_final_check.py"]:
    check(f, (TOOLS / f).exists())

for f in ["capability_protocol.json", "safety_boundary.md", "context_bus.yaml"]:
    check(f, (Path(AIOS_HOME) / "kernel" / "protocols" / f).exists())

# ── 2. 服务状态 ──
print("\n[2/9] 服务运行状态")
services_to_check = [
    ("Entry Gateway", 18801),
    ("Model Gateway", 9998),
    ("Control Center", 8080),
]
for name, port in services_to_check:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    result = s.connect_ex(("127.0.0.1", port))
    s.close()
    check(f"{name} (:{port})", result == 0, f"port {port} {'open' if result==0 else 'closed'}")

# ── 3. Entry Gateway API ──
print("\n[3/9] Entry Gateway API")

# 3a. Health check
health = http_get("/health")
check("GET /health returns ok", health.get("ok") is True, f"response: {health.get('status','?')}")
check("Redis connected", health.get("redis") is True)

# 3b. Status
status = http_get("/status")
check("GET /status returns ok", status.get("ok") is True)
check("Queue info present", "queue" in status)
check("Executors registered", status.get("executors", {}).get("count", 0) >= 5,
      f"got {status.get('executors',{}).get('count',0)}")

# 3c. Create task
task_resp = http_post("/task", {"input": "端到端集成测试验证", "source": "test", "sender": "integration_test"})
check("POST /task creates tasks", task_resp.get("ok") is True and task_resp.get("count", 0) > 0,
      f"got {task_resp.get('count', 0)} tasks")

# 3d. Check task status
task_ids = task_resp.get("task_ids", [])
if task_ids:
    status_resp = http_get(f"/task/{task_ids[0]}")
    check("GET /task/<id> returns status", status_resp.get("ok") is True)
    check("Task status is tracked", status_resp.get("task", {}).get("status") in ("pending", "locked", "running", "completed", "failed"))

# ── 4. Protocol Enforcer ──
print("\n[4/9] Protocol Enforcer")

# 4a. Block dangerous commands
blocked = http_post("/task", {"input": "rm -rf / important files", "source": "test", "sender": "test"})
check("危险指令被拦截", blocked.get("ok") is False and "protocol_violation" in blocked.get("error", ""),
      f"response: {blocked.get('error','?')}")

# 4b. Block empty messages
empty = http_post("/task", {"input": "", "source": "test", "sender": "test"})
check("空消息被拦截", empty.get("ok") is False and "missing_input" in empty.get("error", ""))

# 4c. Allow normal messages
normal = http_post("/task", {"input": "检查系统日志", "source": "test", "sender": "test"})
check("正常消息放行", normal.get("ok") is True, f"response: {normal}")

# ── 5. Task State Machine ──
print("\n[5/9] 任务状态机")
from aios_bus import transition_task_state, get_task_state, generate_task_id, TASK_STATES, STATE_TRANSITIONS
# 用注册中心的任务来测状态, 避免手动序列
# 正确的状态转换链: created → queued → locked → running → verifying → completed
state_chain = ["created", "queued", "locked", "running", "verifying", "completed"]
tid = generate_task_id()
for state in state_chain:
    ok, reason = transition_task_state(tid, state, executor="test")
    check(f"状态转换到 {state}", ok, reason)
# 验证最终状态
final = get_task_state(tid)
check(f"最终状态 = completed", final.get("status") == "completed", f"got {final.get('status','?')}")

# ── 6. Contract Enforcer ──
print("\n[6/9] Contract Enforcer")
from aios_contract_enforcer import check_all_rules, enforce as contract_enforce, RULES
violations = check_all_rules()
result = contract_enforce(violations)
check(f"合约规则检查 ({result.get('checked',0)}条)", result.get('status') in ('CLEAN', 'VIOLATIONS', 'HALT'),
      f"status={result.get('status','?')} violations={result.get('violations',0)}")

# ── 7. Error Classifier ──
print("\n[7/9] 错误分类器")
from aios_error_classifier import classify_error, log_error, ERROR_CODES
for code, info in ERROR_CODES.items():
    # Create a message that should match this code
    test_msgs = {
        "E001": "connection timed out after 30s",
        "E002": "permission denied for user",
        "E003": "memory quota exceeded",
        "E004": "no response from agent",
        "E005": "invalid json format in request",
    }
    msg = test_msgs.get(code, "unknown error")
    r = classify_error(msg)
    check(f"{code} {info['name']}", r["code"] == code, f"got {r['code']} for '{msg}'")

# ── 8. Bus Event System ──
print("\n[8/9] 总线事件系统")
from aios_bus import _is_available, publish_event, get_event_log, check_recent, publish_result
check("Redis 总线可用", _is_available())

# Publish test event (use valid event type from EVENT_TYPES)
ev_ok = publish_event("task.created", {"message": "integration test", "test": True}, "integration_test")
check("事件发布 (task.created)", ev_ok)

# Check event log
events = get_event_log(hours=1, limit=5)
check("事件日志可读", len(events) > 0, f"got {len(events)} events")

# Publish result with valid system name
test_tid = generate_task_id()
pub_ok = publish_result(task_id=test_tid, system="hermes", task_name="integration_test",
                         status="completed", summary="集成测试通过")
check("总线写入 (system=hermes)", pub_ok)

recent = check_recent(hours=1, limit=5)
check("总线读取", len(recent) > 0, f"got {len(recent)} results")

# ── 9. 全链路验证 ──
print("\n[9/9] 全链路验证")

# 完整链路: 正常任务
full_ok = True

# Step 1: Submit task via gateway
step1 = http_post("/task", {"input": "全链路测试 - 验证完整流程", "source": "test", "sender": "e2e"})
full_ok = full_ok and step1.get("ok") is True
check("步骤1: 网关接受任务", step1.get("ok") is True)

# Step 2: Task was dispatched
task_ids = step1.get("task_ids", [])
full_ok = full_ok and len(task_ids) > 0
check("步骤2: 任务已分配", len(task_ids) > 0, f"got {len(task_ids)} tasks")

# Step 3: Task state is trackable
if task_ids:
    state = get_task_state(task_ids[0])
    has_state = state.get("status") in ("pending", "locked", "running", "completed", "failed")
    full_ok = full_ok and has_state
    check("步骤3: 状态可追踪", has_state, f"status={state.get('status','?')}")

# Step 4: Dangerous task blocked (security — 403 response expected)
step4 = http_post("/task", {"input": "run rm -rf / on the server", "source": "test", "sender": "e2e"})
is_blocked = step4.get("ok") is False and ("protocol_violation" in step4.get("error", "") or "protocol" in str(step4))
full_ok = full_ok and is_blocked
check("步骤4: 危险任务拦截", is_blocked, f"response: {json.dumps(step4)[:100]}")

# Step 5: System status consistent
step5 = http_get("/status")
has_executors = step5.get("executors", {}).get("count", 0) >= 5
has_queue = "queue" in step5
full_ok = full_ok and has_executors and has_queue
check("步骤5: 系统状态一致", has_executors and has_queue,
      f"executors={step5.get('executors',{}).get('count','?')} queue={'yes' if has_queue else 'no'}")

# ── 结果 ──
print("\n" + "=" * 60)
total = PASS + FAIL
print(f"  总计: {total}  通过: {PASS} ✅  失败: {FAIL} ❌")
if FAIL == 0:
    print("  🎉 AIOS v4.0 端到端集成测试通过")
    print("  6个Phase全部就绪!")
else:
    print(f"  🚨 {FAIL} 项失败, 需修复")

print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
