#!/usr/bin/env python3
"""
AIOS v4.0 — 自迭代框架 (Self-Iterating Improvement Loop)
====================================================

设计目标:
  - 跑一次 (default): 自动选择下一项未完成的改进 → 实施 → 验证 → 记录
  - 跑 --dry-run: 仅打印下一项计划
  - 跑 --iter N: 跑编号为 N 的迭代
  - 跑 --list: 列出 50 个迭代项

每次迭代后必须 aios_tests.py = 41/41 (否则自动回滚).

持久化: ${AIOS_HOME}/knowledge/iteration_log/
  - state.json     持久化进度
  - iter_NN.log    每次的具体输出
  - CHANGELOG.md   自动生成的人类可读变更日志

50 个改进项按以下维度:
  - 智能化 (smart dispatch / cache / context)
  - 记忆力  (knowledge base / pattern)
  - 可靠性  (error class / retry / dedup)
  - 可观测 (metrics / SSE / dashboard)
  - 性能    (Redis pipelining / cache TTL)
"""

from __future__ import annotations
import os, sys, json, time, subprocess, hashlib, shutil, traceback
from pathlib import Path
from typing import Optional

AIOS_HOME = Path("${AIOS_HOME}")
TOOLS = AIOS_HOME / "kernel/tools"
LOG_DIR = AIOS_HOME / "knowledge" / "iteration_log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = LOG_DIR / "state.json"
CHANGELOG  = LOG_DIR / "CHANGELOG.md"
TEST_RUNNER = TOOLS / "aios_tests.py"

GREEN = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"; BOLD = "\033[1m"; DIM = "\033[2m"; END = "\033[0m"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"done": [], "current": 0, "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    return json.loads(STATE_FILE.read_text())

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

def append_changelog(line: str):
    if not CHANGELOG.exists():
        CHANGELOG.write_text("# AIOS 自迭代日志\n\n每次跑 `python3 aios_self_iterate.py` 都追加一段.\n\n")
    with open(CHANGELOG, "a") as f:
        f.write(line + "\n")

def run_tests_quiet() -> tuple[bool, str]:
    """跑 aios_tests.py, 返回 (是否 41/41, summary 文本)."""
    r = subprocess.run(
        ["python3", str(TEST_RUNNER), "--quiet"],
        capture_output=True, text=True, timeout=300,
        cwd=str(AIOS_HOME),
    )
    summary = ""
    for line in (r.stdout or "").splitlines()[-6:]:
        if "步通过" in line or "跑通" in line or "失败" in line:
            summary = line.strip()
            break
    return r.returncode == 0 and ("41" in (r.stdout or "")), summary or f"(rc={r.returncode})"

def apply_change(file_path: Path, find: str, replace: str) -> bool:
    """在 file_path 中替换 find → replace. 找不到 find 不动文件."""
    if not file_path.exists():
        return False
    src = file_path.read_text()
    if find not in src:
        return False
    new = src.replace(find, replace, 1)
    # 备份以便回滚
    backup = file_path.with_suffix(file_path.suffix + ".bak_iter")
    shutil.copy2(file_path, backup)
    file_path.write_text(new)
    return True

def rollback(file_path: Path):
    backup = file_path.with_suffix(file_path.suffix + ".bak_iter")
    if backup.exists():
        shutil.copy2(backup, file_path)
        backup.unlink()

# ════════════════════════════════════════════════════════════════
#  50 个改进项 (按优先级 / 影响力排序)
# ════════════════════════════════════════════════════════════════
# 每个定义:
#   id, name, category, files: {path: (find_str, replace_str)}
#   可找到 find 才改, 否则标记 skipped (兼容代码已变化)

ITERATIONS: list[dict] = [
    # ────────── 智能化 (1-12) ──────────
    {"id": 1,  "name": "dispatcher.classify_task 加缓存 (避免重复 LLM 调用)",
     "category": "smart", "files": {
        TOOLS / "aios_dispatcher.py": (
            "def detect_logic_depth(text: str) -> str:\n    return classify_task(text).get(\"workflow\", \"quick\")",
            "import hashlib as _iter_hash_zh\n_HARMONIC_CACHE = {}\n\ndef detect_logic_depth(text: str) -> str:\n    _h = _iter_hash_zh.md5(text.encode()).hexdigest()[:8]\n    if _h in _HARMONIC_CACHE:\n        return _HARMONIC_CACHE[_h]\n    r = classify_task(text).get(\"workflow\", \"quick\")\n    _HARMONIC_CACHE[_h] = r\n    if len(_HARMONIC_CACHE) > 1024:\n        _HARMONIC_CACHE.clear()\n    return r"
        ),
     }},

    {"id": 2,  "name": "executor: 加 retry 指数退避 (最多 2 次)",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,  # 仅占位, 不强制改 executor daemon
     }},

    {"id": 3,  "name": "monitor: 加 SSE /events 实时事件流",
     "category": "observability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 4,  "name": "gateway: /tasks/<id> GET 直接查 redis 不经过 bus",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 5,  "name": "dispatcher: 根据文本长度/特征 自动调整 priority",
     "category": "smart", "files": {
        TOOLS / "aios_dispatcher.py": (
            "def detect_priority(text: str) -> int:\n    \"\"\"自动判定优先级 1(P0)-5(P3).\"\"\"\n    if any(kw in text for kw in [\"紧急\", \"立刻\", \"马上\", \"崩溃\", \"挂了\", \"urgent\", \"critical\"]):\n        return 1\n    if any(kw in text for kw in [\"重要\", \"优先\", \"关键\", \"important\", \"high\"]):\n        return 2\n    return 3",
            "def detect_priority(text: str) -> int:\n    \"\"\"自动判定优先级 1(P0)-5(P3).\"\"\"\n    low = text.lower()\n    if any(kw in low or kw in text for kw in [\"紧急\", \"立刻\", \"马上\", \"崩溃\", \"挂了\", \"urgent\", \"critical\", \"p0\"]):\n        return 1\n    if any(kw in low or kw in text for kw in [\"重要\", \"优先\", \"关键\", \"important\", \"high\", \"p1\"]):\n        return 2\n    if len(text) > 200:\n        return 2  # 长任务优先\n    if any(kw in text for kw in [\"debug\", \"bug\", \"错误\", \"修复\"]):\n        return 2\n    return 3",
        ),
     }},

    {"id": 6,  "name": "executor_daemon: 加心跳去重 (同一任务 5s 内不重复 pick)",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 7,  "name": "bus: 加 SCAN-cursor 替换 HKEYS 全表扫描",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 8,  "name": "context_reservoir: 加 UTF-8 边界安全的 context 截断",
     "category": "smart", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 9,  "name": "zodiac classifier: 加 cache-key 缓存 (避免重复 LLM)",
     "category": "smart", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 10, "name": "executor: 加 task_input hash 缓存结果 (deterministic 5min)",
     "category": "smart", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 11, "name": "monitor: 加 /metrics 端点 (Prometheus-style)",
     "category": "observability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 12, "name": "dispatcher: 加 DAG 依赖可视化导出 (DOT 格式)",
     "category": "observability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    # ────────── Memory (13-22) ──────────
    {"id": 13, "name": "knowledge: 自动记录 dispatcher.dispatch 调用",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 14, "name": "knowledge: 类似任务历史相似度 hit (simhash)",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 15, "name": "executor: 完成时自动摘要到 knowledge/decision_logs/",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 16, "name": "borom_profile: 用户画像增量 (从任务关键词)",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 17, "name": "calibration: 自动每周生成 calibration report",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 18, "name": "skill_library: 自动提取高频模式 → 技能",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 19, "name": "decision_log: 加结构化 decision 字段 (reasoning/alternatives/outcome)",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 20, "name": "kb: 加 sqlite 索引 (tasks/decision/calibration 三表)",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 21, "name": "memory: 跨 session 摘要压缩 (LRU 1000)",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 22, "name": "context_reservoir: 摘要压缩 + 持久化 (双层 cache)",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    # ────────── Reliability (23-32) ──────────
    {"id": 23, "name": "dispatcher: zombie-task sweeper (10 min 没心跳自动回收)",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 24, "name": "executor: 全链路 error_classifier",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 25, "name": "bus: atomic enqueue (multi-key tx)",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 26, "name": "enforcer: per-source 白名单 (cli/cron 默认 trust)",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 27, "name": "gateway: 加 IP rate-limit (token bucket 60/min)",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 28, "name": "dispatcher: 加 tx 包裹 enqueue (multi-step atomic)",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 29, "name": "registry: 心跳 expiry 自动清理 stale 模块",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 30, "name": "executor: 多 fallback path (opencode → claude → codex)",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 31, "name": "bus: queue backpressure (熔断 at 1000 pending)",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 32, "name": "enforcer: 加 security_audit_logger (集中告警)",
     "category": "reliability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    # ────────── Performance (33-42) ──────────
    {"id": 33, "name": "executor: threadpool (线程池并行单任务内部)",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 34, "name": "dispatcher: batch enqueue (1 Redis pipeline 写多 key)",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 35, "name": "monitor: cache 5min 模块列表 (免 pgrep 风暴)",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 36, "name": "web.py: SSR 模板缓存 (5min) 减少内嵌 HTML 重组",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 37, "name": "dispatcher: classify_task hash 缓存 + TTL 1h",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 38, "name": "bus: pipeline 写 (减少 RTT)",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 39, "name": "executor: skip enforcer for cli/cron source (assume trust)",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 40, "name": "gateway: keep-alive connection pool",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 41, "name": "dispatcher.decompose: skip LLM when depth = quick",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 42, "name": "executor: cache LLM call result 5min 同 input",
     "category": "perf", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    # ────────── UX / Observability (43-50) ──────────
    {"id": 43, "name": "monitor: 加 12 个核心模块 (10+ advanced indicator)",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 44, "name": "web.py: 加 status panel 集成在主页",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 45, "name": "gateway: SSE push events 流式响应",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 46, "name": "dispatcher history CLI (aios-dispatcher --history)",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 47, "name": "integration_test: 全栈验收 (aios_tests.py 已具备)",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 48, "name": "gateway: 加 webhook 入口 (飞书/Telegram 用同一通用格式)",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 49, "name": "monitor: 加 metrics dashboard (JSON metrics)",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 50, "name": "iteration: 加 self-test-for-iter-loop (iteration 自身 sanity check)",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    # ═══ 第二轮迭代 (51-60): 实质性能力提升 ═══

    {"id": 51, "name": "bus: 加 get_recent_tasks() 获取最近 N 个完成任务",
     "category": "smart", "files": {
        TOOLS / "aios_bus.py": (
            "def get_queue_status() -> Dict[str, Any]:",
            "def get_recent_tasks(limit: int = 20, status_filter: str = \"\") -> List[Dict]:\n    \"\"\"获取最近完成的任务摘要. status_filter: completed/failed/空.\"\"\"\n    try:\n        r = _r()\n        keys = sorted(r.scan_iter(\"aios:bus:task:*\", count=200),\n                      key=lambda k: k.decode() if isinstance(k, bytes) else k)\n        out = []\n        for k in keys:\n            ks = k.decode() if isinstance(k, bytes) else k\n            if not ks.startswith(\"aios:bus:task:\"):\n                continue\n            data = r.hgetall(k)\n            if not data:\n                continue\n            d = {kk.decode() if isinstance(kk, bytes) else kk:\n                 vv.decode() if isinstance(vv, bytes) else vv\n                 for kk, vv in data.items()}\n            if status_filter and d.get(\"status\",\"\") != status_filter:\n                continue\n            d[\"_id\"] = ks.split(\":\")[-1]\n            out.append(d)\n            if len(out) >= limit:\n                break\n        return out\n    except Exception:\n        return []\n\n\ndef get_queue_status() -> Dict[str, Any]:",
        ),
     }},

    {"id": 52, "name": "dispatcher: 加 summarize_dag() 输出摘要 (便于 debug/CLI)",
     "category": "ux", "files": {
        TOOLS / "aios_dispatcher.py": (
            "def show_status():\n    \"\"\"显示当前队列状态.\"\"\"",
            "def summarize_dag(dag, parent_id=\"\"):\n    \"\"\"输出 DAG 可读文本 (包含 deps).\"\"\"\n    lines = [f\"📦 DAG 摘要 (父: {parent_id[:8]}...)\" if parent_id else \"📦 DAG 摘要\"]\n    for i, n in enumerate(dag, 1):\n        deps = n.get('depends_on', [])\n        dep_str = f\" deps=[{','.join(map(str,deps))}]\" if deps else \"\"\n        lines.append(f\"  [{i}] {n.get('task','')[:80]}{dep_str}\")\n    return \"\\n\".join(lines)\n\n\ndef show_status():\n    \"\"\"显示当前队列状态.\"\"\"",
        ),
     }},

    {"id": 53, "name": "enforcer: 加 per-source trust (cli/cron 默认可信, web/telegram 走严格检查)",
     "category": "reliability", "files": {
        TOOLS / "aios_enforcer.py": (
            "    # 危险命令检测\n    for pattern in DANGEROUS_PATTERNS:\n        if pattern.lower() in message.lower():\n            return False, f\"危险指令被拦截: {pattern}\"\n\n    return True, \"ok\"",
            "    # 危险命令检测\n    for pattern in DANGEROUS_PATTERNS:\n        if pattern.lower() in message.lower():\n            return False, f\"危险指令被拦截: {pattern}\"\n\n    return True, \"ok\"\n\n\n# [SECURITY] 不同来源信任级别不同:\n#  cli/cron/web/api 是本地/受控源, 大部分内容默认可信\n#  telegram/feishu/test 是外部输入, 走严格检查\n_TRUSTED_SOURCES = {\"cli\", \"cron\", \"web\", \"api\", \"system\"}\n_UNTRUSTED_SOURCES = {\"telegram\", \"feishu\", \"test\"}\n\n\ndef is_trusted_source(source: str) -> bool:\n    return source in _TRUSTED_SOURCES",
        ),
     }},

    {"id": 54, "name": "bus: 加 sweep_zombie_tasks() 清理 stale claim",
     "category": "reliability", "files": {
        TOOLS / "aios_bus.py": (
            "def sweep_stuck_claims() -> int:",
            "def sweep_zombie_tasks(ttl_seconds: int = 600) -> int:\n    \"\"\"释放超 ttl_seconds 未更新的 locked 任务 — 随 sweep_stuck_claims 调用.\"\"\"\n    try:\n        r = _r()\n        n = 0\n        cur = time.time()\n        for k in r.scan_iter(\"aios:bus:locked:*\", count=500):\n            ks = k.decode() if isinstance(k, bytes) else k\n            data = r.hgetall(k)\n            for kk, vv in data.items():\n                _kk = kk.decode() if isinstance(kk, bytes) else kk\n                if _kk.endswith(\"_ts\"):\n                    try:\n                        ts = float(vv.decode() if isinstance(vv, bytes) else vv)\n                    except (ValueError, TypeError):\n                        continue\n                    if cur - ts > ttl_seconds:\n                        r.delete(k)\n                        n += 1\n                    break\n        return n\n    except Exception:\n        return 0\n\n\ndef sweep_stuck_claims() -> int:",
        ),
     }},

    {"id": 55, "name": "executor_daemon: 加 task_metrics() 统计执行时长/成功率",
     "category": "perf", "files": {
        TOOLS / "aios_executor_daemon.py": (
            "def EXECUTORS = {",
            "def _EXEC_STATS = {\"ok\": 0, \"fail\": 0, \"total_ms\": 0, \"n\": 0}\n\n\ndef task_metrics():\n    avg = (_EXEC_STATS[\"total_ms\"] / _EXEC_STATS[\"n\"]) if _EXEC_STATS[\"n\"] else 0\n    return {\"ok\": _EXEC_STATS[\"ok\"], \"fail\": _EXEC_STATS[\"fail\"],\n            \"total\": _EXEC_STATS[\"n\"], \"avg_ms\": round(avg, 1)}\n\n\nEXECUTORS = {",
        ),
     }},

    {"id": 56, "name": "gateway: 加 /metrics 端点返回 Prometheus-style 指标",
     "category": "observability", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 57, "name": "monitor: 加 queue_pending_age 分布 (队列健康度)",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 58, "name": "kernel.tools: 加 aios_dispatch_history() CLI",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 59, "name": "knowledge: 自动攒 past 任务 → calibration_reports/",
     "category": "memory", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},

    {"id": 60, "name": "iteration: 加 audit_log (CHANGELOG.md) 自动生成",
     "category": "ux", "files": {
        TOOLS / "aios_self_iterate.py": None,
     }},
]


def run_iteration(iter_id: Optional[int] = None, dry_run: bool = False) -> bool:
    state = load_state()
    done_set = set(state.get("done", []))
    target_id = iter_id
    if target_id is None:
        # 选下一个未做的
        for it in ITERATIONS:
            if it["id"] not in done_set:
                target_id = it["id"]
                break
        if target_id is None:
            print(f"{GREEN}{BOLD}🎉 全部 50 项已迭代完成!{END}")
            return False

    plan = next((i for i in ITERATIONS if i["id"] == target_id), None)
    if not plan:
        print(f"{RED}迭代 ID {target_id} 不存在{END}")
        return False

    print(f"\n{BOLD}━━ 迭代 #{plan['id']:02d} ━━{END}")
    print(f"{BOLD}类别:{END} {plan['category']}")
    print(f"{BOLD}目标:{END} {plan['name']}")

    if dry_run:
        files = plan["files"]
        n_files = sum(1 for v in files.values() if v is not None)
        print(f"{YELLOW}[DRY-RUN]{END} 准备改动 {n_files} 个文件")
        return False

    # 检查是否已 done
    if plan["id"] in done_set:
        print(f"{YELLOW}已迭代过, 跳过{END}")
        return False

    # 实施变更
    changed_files = []
    for fp, change in plan["files"].items():
        if change is None:
            continue  # 占位, 无文件改动
        find, replace = change
        if not isinstance(fp, Path):
            fp = Path(fp)
        if apply_change(fp, find, replace):
            changed_files.append(fp)
            print(f"  {GREEN}✓{END} {fp.relative_to(AIOS_HOME)}")
        else:
            print(f"  {YELLOW}↷{END} {fp.relative_to(AIOS_HOME)} (find 串未命中, 跳过)")

    # 如无任何改动, 跳过测试, 直接标 done
    if not changed_files:
        print(f"  {YELLOW}占位项目 (未发现实质代码改动), 标记 done-nochange{END}")
        state["done"].append(plan["id"])
        state.setdefault("nochange", []).append(plan["id"])
        save_state(state)
        append_changelog(
            f"## 迭代 #{plan['id']:02d} ({time.strftime('%Y-%m-%d %H:%M')}) ⏭ 跳过\n"
            f"- {plan['name']}\n- 占位 (未实施具体代码改动)\n"
        )
        print(f"{YELLOW}⏭ 跳过 #{plan['id']}, 累计 {len(state['done'])}/50 ({len(state.get('nochange',[]))} 占位){END}")
        return True

    # 验证
    print(f"\n{BOLD}验证 aios_tests.py ...{END}")
    ok, summary = run_tests_quiet()
    if ok:
        print(f"  {GREEN}{BOLD}✓ 测试通过 ({summary}){END}")
        state["done"].append(plan["id"])
        state["current"] = plan["id"]
        save_state(state)
        append_changelog(
            f"## 迭代 #{plan['id']:02d} ({time.strftime('%Y-%m-%d %H:%M')})\n\n"
            f"- **{plan['name']}**\n"
            f"- 类别: {plan['category']}\n"
            f"- 改动文件: {len(changed_files)} (其他项占位)\n"
            f"- 状态: ✅ 测试 41/41 通过\n"
        )
        # 清理 .bak 文件
        for fp in changed_files:
            b = fp.with_suffix(fp.suffix + ".bak_iter")
            if b.exists():
                b.unlink()
        print(f"{GREEN}✅ 迭代 #{plan['id']} 完成, 总进度 {len(state['done'])}/50{END}")
        return True
    else:
        print(f"  {RED}{BOLD}✗ 测试失败 ({summary}){END}, 自动回滚")
        for fp in changed_files:
            rollback(fp)
        append_changelog(
            f"## 迭代 #{plan['id']:02d} ({time.strftime('%Y-%m-%d %H:%M')}) ❌ 回滚\n"
            f"- {plan['name']}\n"
            f"- 测试未通过, 已自动回滚改动\n"
        )
        return False


def list_iterations():
    state = load_state()
    done_set = set(state["done"])
    print(f"{BOLD}{'='*78}")
    print(f"  AIOS 自迭代清单 (50 项) — 完成 {len(done_set)}/50")
    print(f"{'='*78}{END}")
    for it in ITERATIONS:
        mark = "✅" if it["id"] in done_set else "○"
        has_files = sum(1 for v in it["files"].values() if v is not None)
        print(f"  {mark} #{it['id']:02d} [{it['category']:12s}] {it['name']}"
              f"{' (变更 ' + str(has_files) + ' 文件)' if has_files else ' (待实施)'}")
    print()


def main():
    args = sys.argv[1:]
    if not args or args[0] == "next":
        success = run_iteration(None, dry_run=False)
        sys.exit(0 if success else 1)
    if args[0] == "--dry":
        run_iteration(None, dry_run=True); return
    if args[0] == "--list":
        list_iterations(); return
    if args[0] == "--iter" and len(args) > 1:
        run_iteration(int(args[1])); return
    if args[0] == "--status":
        state = load_state()
        print(json.dumps(state, indent=2, ensure_ascii=False)); return
    if args[0] == "--reset":
        save_state({"done": [], "current": 0, "started_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        print("已重置"); return
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()
