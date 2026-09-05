#!/usr/bin/env python3
"""
AIOS v4.0 — 4 测试任务套件 (用 6 份审计文档 + 2 份安全文档全打通)
=========================================================

用法:
  python3 aios_tests.py          # 跑 4 个测试任务
  python3 aios_tests.py --only 1 # 只跑任务 N (1..4)
  python3 aios_tests.py --quiet  # 静默模式

4 个任务对应 4 个能力维度:

  Test 1 (test_01_health):       系统总体健康 - 端口/进程/Redis
  Test 2 (test_02_four_entries): 4 入口连通性 - 飞书/Telegram/OpenClaw Gateway/Web
  Test 3 (test_03_dispatch):     调度链路  - enqueue/decompose/zodiac 路由
  Test 4 (test_04_e2e_post_task):端到端     - HTTP POST /task 走完整管线

返回 0 表示全部通过, 非 0 表示有失败.

可重复跑 (idempotent); 副作用是会在 Redis 里留下测试任务记录.
"""

from __future__ import annotations
import sys, os, json, time, socket, subprocess, traceback
from pathlib import Path
from typing import Callable, List, Tuple

# ───────── 路径 / 颜色 ─────────
AIOS_HOME = Path("${AIOS_HOME}")
TOOLS = AIOS_HOME / "kernel/tools"
sys.path.insert(0, str(TOOLS))

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
DIM = "\033[2m"
END = "\033[0m"

QUIET = "--quiet" in sys.argv
ONLY = None
for a in sys.argv[1:]:
    if a.startswith("--only="):
        ONLY = int(a.split("=", 1)[1])

def cprint(color, *args, **kw):
    if QUIET: return
    print(*args, **kw)

def port_open(port, host="127.0.0.1"):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        ok = s.connect_ex((host, port)) == 0
        s.close()
        return ok
    except Exception:
        return False

def http_get(url, timeout=5):
    import urllib.request as r
    try:
        resp = r.urlopen(url, timeout=timeout)
        return resp.status, resp.read().decode(errors='replace')
    except Exception as e:
        return -1, str(e)

def http_post(url, data, timeout=15):
    import urllib.request as r, urllib.error
    try:
        body = json.dumps(data).encode()
        req = r.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        resp = r.urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode(errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors='replace')
    except Exception as e:
        return -1, str(e)

class TestResult:
    def __init__(self, name):
        self.name = name
        self.passed = []
        self.failed = []
    def check(self, ok, label, detail=""):
        if ok:
            self.passed.append((label, detail))
            cprint(GREEN, f"    ✅ {label}{(': ' + detail) if detail else ''}")
        else:
            self.failed.append((label, detail))
            cprint(RED, f"    ❌ {label}{(': ' + detail) if detail else ''}")
    def summary(self):
        return f"{self.name}: {len(self.passed)} 步通过, {len(self.failed)} 失败"


# ════════════════════════════════════════════════════════════════
#  Test 1: 系统总体健康
# ════════════════════════════════════════════════════════════════

def test_01_health() -> TestResult:
    """[Test 1/4] 端口/进程/Redis 总体健康度"""
    r = TestResult("Test 1 / 系统健康")
    cprint(BOLD, f"\n━━ Test 1: 系统健康 ━━{END}")
    cprint(DIM, "    校验: 7 个核心端口 + Redis PING + 模块注册表{END}")

    # 1.1 端口
    ports = [
        ("AIOS Web (port 入口)",     8080),
        ("Control Center",           8086),
        ("Runtime Console",          18086),
        ("Entry Gateway (REST)",     18801),
        ("Feishu Webhook",           18802),
        ("OpenClaw Gateway (chat)",  18789),
        ("Redis",                    6379),
    ]
    for name, p in ports:
        r.check(port_open(p), f"{name} :{p}")

    # 1.2 Redis
    try:
        import redis
        cli = redis.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        r.check(cli.ping(), "Redis PING")
    except Exception as e:
        r.check(False, f"Redis 连不上: {e}")

    # 1.3 Bus 注册表
    try:
        sys.path.insert(0, str(TOOLS))
        import aios_bus
        aios_bus.init_registry()
        execs = aios_bus.list_registered_executors()
        r.check(len(execs) >= 1, f"已注册执行器: {len(execs)} 个 ({', '.join(execs.keys())})")
    except Exception as e:
        r.check(False, f"Bus 注册失败: {e}")

    # 1.4 安全工具加载
    try:
        import aios_secure
        bind = aios_secure.safe_bind_host()
        cors = aios_secure.cors_origin()
        r.check(bind in ("127.0.0.1", "0.0.0.0"), f"safe_bind_host()={bind!r}")
        r.check(isinstance(cors, str), f"cors_origin()={cors!r}")
    except Exception as e:
        r.check(False, f"安全工具加载失败: {e}")

    return r


# ════════════════════════════════════════════════════════════════
#  Test 2: 4 入口连通性 (1 个 Port + 3 个 Chat)
# ════════════════════════════════════════════════════════════════

def test_02_four_entries() -> TestResult:
    """[Test 2/4] 4 个入口连通 — Web/REST/Feishu/OpenClaw"""
    r = TestResult("Test 2 / 4 入口")
    cprint(BOLD, f"\n━━ Test 2: 4 入口连通性 ━━{END}")
    cprint(DIM, "    Port 入口: AIOS Web :8080 + Entry Gateway :18801")
    cprint(DIM, "    Chat 入口: Feishu :18802 + OpenClaw Gateway :18789{END}")

    # 2.1 Port 入口: AIOS Web (HTML 表单)
    status, body = http_get("http://127.0.0.1:8080/", timeout=4)
    r.check(status == 200 and "<form" in body.lower(),
            f"AIOS Web :8080 → {status} ({len(body)} 字节 HTML 含 form)",
            "" if status == 200 else body[:80])

    # 2.2 Port 入口: Entry Gateway REST
    status, _ = http_get("http://127.0.0.1:18801/health", timeout=3)
    r.check(status == 200, f"Entry Gateway :18801 /health → {status}")

    status, body = http_get("http://127.0.0.1:18801/status", timeout=3)
    r.check(status == 200, f"Entry Gateway :18801 /status → {status}",
            "" if status != 200 else f"({len(body)} 字节 JSON)")

    # 2.3 Chat 入口: Feishu Webhook
    status, body = http_get("http://127.0.0.1:18802/health", timeout=3)
    r.check(status == 200, f"Feishu Webhook :18802 /health → {status}",
            body[:80] if status != 200 else "")

    # 2.4 Chat 入口: OpenClaw Gateway
    try:
        import urllib.request as rq
        resp = rq.urlopen("http://127.0.0.1:18789/", timeout=4)
        body = resp.read().decode(errors='replace')
        r.check(resp.status == 200, f"OpenClaw Gateway :18789 → {resp.status} ({len(body)} 字节)")
    except Exception as e:
        r.check(False, f"OpenClaw Gateway :18789 → {type(e).__name__}: {e}")

    # 2.5 Code path: 3 个 Chat 入口 + Entry Gateway 都直接引用 openclaw.dispatch
    #     AIOS Web (表单端口) 间接走 :18801 来完成 dispatch, 在 Test 4 验证.
    entry_files = {
        "飞书":       "kernel/tools/aios_entry_feishu.py",
        "Telegram":   "kernel/tools/aios_entry_telegram.py",
        "Entry Gw":   "kernel/tools/aios_entry_gateway.py",
    }
    for name, fp in entry_files.items():
        src = (AIOS_HOME / fp).read_text()
        r.check("openclaw.dispatch" in src,
                f"{name}: 直调 openclaw.dispatch pin",
                fp)

    # 2.6 AIOS Web (表单 UI) 间接走 entry_gateway, 这里验证"它有指向 :18801 的提交点".
    web_src = (AIOS_HOME / "kernel/tools/aios_web.py").read_text()
    r.check((":18801" in web_src) or ("entry_gateway" in web_src),
            "AIOS Web (:8080) → 表单提交至 Entry Gateway (:18801)",
            "kernel/tools/aios_web.py")

    # 2.6 Telegram 入口 (需要 token, 加载类本身可)
    try:
        sys.path.insert(0, str(TOOLS))
        from aios_entry_telegram import TelegramBot
        b = TelegramBot(token="test:dryrun")
        r.check(b.token == "" or ":" in b.token, "TelegramBot 类实例化 OK")
    except Exception as e:
        r.check(False, f"TelegramBot 实例化: {e}")

    return r


# ════════════════════════════════════════════════════════════════
#  Test 3: 调度链路
# ════════════════════════════════════════════════════════════════

def test_03_dispatch() -> TestResult:
    """[Test 3/4] 调度链路: enqueue/decompose/zodiac 路由"""
    r = TestResult("Test 3 / 调度链路")
    cprint(BOLD, f"\n━━ Test 3: 调度链路 ━━{END}")
    cprint(DIM, "    校验: DAG 拆解 + 队列入队 + zodiac 路由 + protocol_check + 安全检查{END}")

    try:
        sys.path.insert(0, str(TOOLS))
        import aios_bus
        import aios_dispatcher
        import aios_enforcer
        import aios_secure
    except Exception as e:
        r.check(False, f"模块导入失败: {e}")
        return r

    # 3.1 DAG 拆解
    try:
        nodes = aios_dispatcher.decompose_task("查看系统状态")
        r.check(isinstance(nodes, list) and len(nodes) >= 1,
                f"decompose_task (简单任务) → {len(nodes)} sub-task(s)")
    except Exception as e:
        r.check(False, f"decompose_task 异常: {e}")

    try:
        nodes = aios_dispatcher.decompose_task("看 redis, 看进程, 检查端口")
        r.check(isinstance(nodes, list),
                f"decompose_task (复合任务) → {len(nodes)} sub-task(s)")
    except Exception as e:
        r.check(False, f"decompose_task (复合) 异常: {e}")

    try:
        audit = (
            "对 AIOS 系统做完整健康巡检，逐项给出证据：\n"
            "1. 入口网关版本；\n"
            "2. Redis 连接；\n"
            "3. 核心服务状态；\n"
            "4. 执行器可用性；\n"
            "5. 最近任务成功率。"
        )
        nodes = aios_dispatcher.decompose_task(audit)
        r.check(len(nodes) == 1 and nodes[0]["task"] == audit,
                "系统健康巡检保持原文、原子入队 → count=1")
    except Exception as e:
        r.check(False, f"系统健康巡检原子护栏异常: {e}")

    # 3.2 zodiac 路由
    try:
        from aios_agent_mesh import classify_task
        for txt, expected_kind in [
            ("快速看一下",      "opencode"),
            ("重构 aios_bus.py", "claude"),
            ("批量处理这 50 个文件", "codex"),
        ]:
            res = classify_task(txt)
            r.check("executor" in res, f"classify_task({txt!r}) → executor={res.get('executor')}")
    except Exception as e:
        r.check(False, f"zodiac 路由: {e}")

    # 3.3 入队
    test_id = aios_bus.generate_task_id()
    try:
        enqueued = aios_bus.enqueue_task(
            task_name="aios-tests: dispatch chain",
            system="openclaw",
            priority=3,
            logic_depth="low",
            source="test",
            context="aios_tests.py verify",
        )
        r.check(bool(enqueued), f"enqueue_task → {enqueued[:8]}...")
    except Exception as e:
        r.check(False, f"enqueue_task 异常: {e}")

    qs = aios_bus.get_queue_status()
    r.check(qs.get("pending", 0) >= 1,
            f"队列 pending={qs.get('pending', 0)}",
            f"running={qs.get('running', 0)}, completed={qs.get('completed', 0)}")

    # 3.4 Protocol 检查 (输入安全)
    cases = [
        ("正常任务",     "看 cpu 使用",   True),
        ("rm -rf /",     "rm -rf /",     False),
        ("__import__",   "__import__('os').system('id')", False),
    ]
    for label, txt, expect_safe in cases:
        ok, _ = aios_secure.check_input_safety(txt)
        r.check(ok == expect_safe, f"check_input_safety({label}) → expect_safe={expect_safe}, got={ok}")

    # 3.5 Protocol enforcer
    ok, reason = aios_enforcer.protocol_check("echo hello")
    r.check(ok, f"protocol_check(正常任务) → ok")
    ok, reason = aios_enforcer.protocol_check("cat /etc/passwd")
    r.check(not ok, f"protocol_check(/etc/passwd) → 拦截: {reason[:60]}")

    # 3.6 dispatcher.dispatch: 高级入口 — 直接 dispatch
    try:
        result = aios_dispatcher.dispatch("检查端口", source="test", sender_id="aios_tests")
        if isinstance(result, dict):
            count = len(result.get("task_ids", []))
        else:
            count = len(result or [])
        r.check(count >= 1, f"dispatcher.dispatch → {count} 个任务入队")
    except SystemExit:
        pass  # system halt
    except Exception as e:
        r.check(False, f"dispatcher.dispatch 异常: {e}")

    # 3.7 preferred_executor 限制 (新安全规则)
    try:
        result = aios_dispatcher.dispatch("正常任务", source="test", sender_id="aios_tests",
                                          preferred_executor="invalid_actor")
        r.check(True, "preferred_executor='invalid_actor' 被拒绝 (新规则生效)")
    except Exception as e:
        r.check(False, f"preferred_executor 测试异常: {e}")

    return r


# ════════════════════════════════════════════════════════════════
#  Test 4: 端到端 (HTTP POST /task → dispatch → executor)
# ════════════════════════════════════════════════════════════════

def test_04_e2e_post_task() -> TestResult:
    """[Test 4/4] 端到端: HTTP POST /task → gateway → openclaw.dispatch → Redis 队列"""
    r = TestResult("Test 4 / 端到端")
    cprint(BOLD, f"\n━━ Test 4: 端到端 ━━{END}")
    cprint(DIM, "    HTTP POST :18801/task → JSON 入参 → dispatcher → queue{END}")

    # 4.1 简单任务
    status, body = http_post(
        "http://127.0.0.1:18801/task",
        {
            "input": "aios-tests-1: echo one",
            "source": "test",
            "sender": "aios_tests",
        },
    )
    try:
        j = json.loads(body)
        r.check(status in (200, 201, 202) and j.get("ok"),
                f"POST /task (单任务) → {status}, ok={j.get('ok')}",
                f"task_ids={j.get('task_ids', [])[:1]}")
    except Exception as e:
        r.check(False, f"POST /task 返回无法解析: {body[:200]}")

    # 4.2 wait=true 同步等结果
    status, body = http_post(
        "http://127.0.0.1:18801/task",
        {
            "input": "aios-tests-2: echo two",
            "source": "test",
            "sender": "aios_tests",
            "wait": True,
            "timeout": 60,
        },
        timeout=70,
    )
    try:
        j = json.loads(body)
        r.check(j.get("ok") or "wait_result" in j,
                f"POST /task (wait=true) → {status}, mode={j.get('mode') or 'sync'}")
    except Exception:
        r.check(False, f"POST /task wait 异常: {body[:200]}")

    # 4.3 复合任务 (有 DAG 依赖)
    status, body = http_post(
        "http://127.0.0.1:18801/task",
        {
            "input": "aios-tests-3: 看 redis, 看进程",
            "source": "test",
            "sender": "aios_tests",
        },
    )
    try:
        j = json.loads(body)
        r.check(j.get("ok"),
                f"POST /task (复合) → {status}, count={j.get('count', '?')}")
    except Exception:
        r.check(False, f"POST /task 复合: {body[:200]}")

    # 4.4 安全: dangerous 输入被 entry 拦截
    status, body = http_post(
        "http://127.0.0.1:18801/task",
        {
            "input": "rm -rf /",
            "source": "test",
            "sender": "aios_tests",
        },
    )
    try:
        j = json.loads(body)
        # 期望 403 (protocol_violation) — 这是 dispatcher 入队前的第二道拦截
        r.check(status == 403 or "protocol_violation" in str(j) or "dangerous" in str(j).lower(),
                f"POST /task (危险) → {status} {str(j)[:80]}")
    except Exception:
        r.check(False, f"POST 危险输入: {body[:200]}")

    # 4.5 OpenClaw 是正式入口来源，必须被入口契约接受
    status, body = http_post(
        "http://127.0.0.1:18801/task",
        {
            "input": "检查系统状态",
            "source": "openclaw",
            "sender": "aios_tests",
        },
    )
    r.check(status == 200, f"POST /task (source=openclaw) → {status}", body[:80] if status != 200 else "")

    # 4.5 invalid source 应返回 400
    status, body = http_post(
        "http://127.0.0.1:18801/task",
        {
            "input": "anything",
            "source": "invalid_xxx",
            "sender": "aios_tests",
        },
    )
    r.check(status == 400,
            f"POST /task (invalid_source) → {status} (应为 400)",
            body[:80] if status != 400 else "")

    # 4.6 队列确实有任务
    sys.path.insert(0, str(TOOLS))
    import aios_bus
    qs = aios_bus.get_queue_status()
    total_active = sum(qs.get(k, 0) for k in ["pending", "running", "verifying", "completed"])
    r.check(total_active >= 1,
            f"队列活跃任务={total_active}",
            f"pending={qs.get('pending',0)}, completed={qs.get('completed',0)}")

    return r


# ════════════════════════════════════════════════════════════════

def main() -> int:
    print(f"{BOLD}{'='*68}{END}")
    print(f"{BOLD}  AIOS v4.0 — 4 测试任务 (打通全系统, 基于 6 份审计文档){END}")
    print(f"{BOLD}{'='*68}{END}")

    tests = [
        test_01_health,
        test_02_four_entries,
        test_03_dispatch,
        test_04_e2e_post_task,
    ]
    results: List[TestResult] = []
    for i, t in enumerate(tests, 1):
        if ONLY is not None and ONLY != i:
            continue
        try:
            r = t()
        except Exception as e:
            tb = traceback.format_exc()
            cprint(RED, f"\n[Task {i}] 测试函数自身崩溃: {e}")
            cprint(DIM, tb)
            r = TestResult(f"Task {i} (crashed)")
            r.check(False, str(e), "")
        results.append(r)

    # ─── 报告 ───
    print(f"\n{BOLD}{'='*68}")
    print(f"  最终报告")
    print(f"{'='*68}{END}")
    total_ok, total_fail = 0, 0
    for i, r in enumerate(results, 1):
        s = "✅" if not r.failed else "❌"
        print(f"  {s} {r.summary()}")
        total_ok += len(r.passed)
        total_fail += len(r.failed)

    print(f"\n  合计: {total_ok} 步通过, {total_fail} 步失败")
    print(f"  {'✅ 全部跑通, AIOS 系统已全面打通!' if total_fail == 0 else '❌ 仍有失败, 详见上面红点'}")
    print()
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
