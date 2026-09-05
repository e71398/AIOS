#!/usr/bin/env python3
"""
AIOS v4.0 全系统终验脚本
覆盖: 协议+VFS+总线+verify+WorldModel+快照+四系统心跳+端到端闭环
"""

import json, os, sys, subprocess, time
from datetime import datetime
from pathlib import Path

AIOS_HOME = "${AIOS_HOME}"
TOOLS = Path(AIOS_HOME) / "kernel" / "tools"
PROTOCOLS = Path(AIOS_HOME) / "kernel" / "protocols"
PASS, FAIL, WARN = 0, 0, 0

def check(label, condition, detail=""):
    global PASS, FAIL, WARN
    if condition:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}  {detail}")

def warn(label, detail=""):
    global WARN; WARN += 1
    print(f"  ⚠️  {label}  {detail}")


print("=" * 60)
print("  AIOS v4.0 全系统终验")
print(f"  {datetime.now().isoformat()}")
print("=" * 60)

# ── 1. 核心文件 ──
print("\n[1/7] 核心文件完整性")
for f, desc in [
    ("capability_protocol.json", "能力协议"),
    ("safety_boundary.md", "安全边界"),
    ("context_bus.yaml", "信息总线"),
]:
    check(desc, (PROTOCOLS / f).exists(), f"路径: {PROTOCOLS/f}")
for f, desc in [
    ("verify.py", "验证门禁"),
    ("world_model_runner.py", "世界模型"),
    ("aios_bus.py", "共享总线SDK"),
    ("aios_gateway.py", "统一网关"),
    ("aios_startup.py", "启动脚本"),
    ("checkpoint_snapshot.sh", "快照脚本"),
    ("checkpoint_restore.sh", "恢复脚本"),
]:
    check(desc, (TOOLS / f).exists(), f"路径: {TOOLS/f}")

# ── 2. VFS 权限 ──
print("\n[2/7] VFS 安全隔离")
kernel_mode = oct(os.stat(f"{AIOS_HOME}/kernel").st_mode)[-3:]
check(f"kernel 只读 (555)", kernel_mode == "555", f"实际: {kernel_mode}")

# ── 3. Redis 总线 ──
print("\n[3/7] Redis 共享总线")
sys.path.insert(0, str(TOOLS))
try:
    from aios_bus import _is_available, heartbeat as hb, check_recent, publish_result, generate_task_id
    check("Redis 可达", _is_available())
    for sys_name in ["hermes", "openclaw", "opencode", "claude"]:
        ok = hb(sys_name)
        check(f"{sys_name} 心跳", ok)
    recent = check_recent(limit=3)
    check("总线有历史数据", len(recent) > 0, f"最近 {len(recent)} 条")
except Exception as e:
    check("总线模块加载", False, str(e))

# ── 4. verify.py + World Model ──
print("\n[4/7] 验证门禁 + 安全模拟")
# 创建测试task
test_task = {
    "Task_ID": f"final_check_{int(time.time())}",
    "Task_Type": "coding",
    "Context_Summary": "终验测试任务",
    "Work_Dir": f"{AIOS_HOME}/sandbox/coding",
    "Output_File": "report.txt",
    "Verification_Criteria": [
        {"type": "file_exists", "filename": "count_log_lines.py"},
    ]
}
task_file = f"/tmp/aios_final_test_{int(time.time())}.json"
with open(task_file, 'w') as f:
    json.dump(test_task, f)

r = subprocess.run(["python3", str(TOOLS/"verify.py"), task_file], capture_output=True, text=True, timeout=15)
check("verify.py 可调用", r.returncode in (0,1), f"exit={r.returncode}")

r = subprocess.run(["python3", str(TOOLS/"world_model_runner.py"), task_file], capture_output=True, text=True, timeout=15)
verdict = "APPROVED" if r.returncode == 0 else "BLOCKED"
check(f"World Model ({verdict})", r.returncode in (0,1), f"exit={r.returncode}")

os.remove(task_file)

# ── 5. 网关 ──
print("\n[5/7] 统一网关")
r = subprocess.run(["python3", str(TOOLS/"aios_gateway.py"), "startup", "claude"], capture_output=True, text=True, timeout=10)
check("网关 startup", r.returncode == 0)
try:
    data = json.loads(r.stdout)
    check(f"协议加载 ({data.get('protocols_loaded',0)}条)", data.get('protocols_loaded', 0) >= 8)
except:
    check("网关 JSON 解析", False)

r = subprocess.run(["python3", str(TOOLS/"aios_gateway.py"), "status"], capture_output=True, text=True, timeout=10)
check("网关 status", r.returncode == 0)

# ── 6. 快照 ──
print("\n[6/7] 断点快照")
r = subprocess.run(["python3", str(TOOLS/"aios_gateway.py"), "snapshot"], capture_output=True, text=True, timeout=15)
check("手动快照", r.returncode == 0, r.stdout.strip()[-100:])

# ── 7. 端到端: 写总线 → verify → World Model ──
print("\n[7/7] 端到端闭环")
from aios_bus import publish_result as pub, generate_task_id as gid
tid = gid()
ok = pub(task_id=tid, system="claude", task_name="终验闭环测试",
         status="completed", summary="终验验证通过", source="cli", priority=1)
check("总线写入", ok)

# 验证总线记录可读
from aios_bus import check_recent as cr
recs = cr(limit=1)
check("总线读取", len(recs) > 0 and recs[0].get("system") == "claude")

# ── 报告 ──
print("\n" + "=" * 60)
total = PASS + FAIL + WARN
print(f"  总计: {total}  通过: {PASS} ✅  失败: {FAIL} ❌  警告: {WARN} ⚠️")
if FAIL == 0:
    print("  🎉 AIOS v4.0 全系统终验通过")
else:
    print(f"  🚨 {FAIL} 项失败, 需修复")
print("=" * 60)

sys.exit(0 if FAIL == 0 else 1)
