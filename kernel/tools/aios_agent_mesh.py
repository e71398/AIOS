#!/usr/bin/env python3
"""
Agent Mesh — 第十中心
======================
统一Agent生命周期管理: 注册/心跳/状态/超时/12生肖+12星座路由
"""

import sys, json
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import (
    lifecycle_register, lifecycle_set, lifecycle_get, lifecycle_list_all,
    check_agent_timeout, heartbeat, publish_event, generate_task_id
)

# 12生肖Agent (OpenClaw子Agent)
ZODIAC_AGENTS = [
    "lingying_zi", "lingying_chou", "lingying_yin", "lingying_mao",
    "lingying_chen", "lingying_si", "lingying_wu", "lingying_wei",
    "lingying_shen", "lingying_you", "lingying_xu", "lingying_hai",
]

# ── 12生肖分类引擎 ──────────────────────────────────────
# 将任务文本映射到生肖分类 + Workflow + 执行器
# 设计依据: AIOS系统布局方案.md P0 "12生肖Agent改造为调度分类器"

ZODIAC_MAP = {
    "lingying_zi": {"label":"鼠", "keywords":["查询","搜索","找","查","看看","显示","列出","获取","多少",
            "check","find","search","query","status","状态","时间","天气",
            "吗","呢","什么","怎么","为什么","如何","谁","哪"],
        "workflow":"quick", "executor":"opencode", "desc":"快速信息查询，单步响应"},
    "lingying_chou": {"label":"牛", "keywords":["处理","计算","分析","统计","汇总","整理","清洗","转换",
            "process","compute","analyze","aggregate","ETL","数据迁移","import","export"],
        "workflow":"standard", "executor":"opencode", "desc":"重数据处理，需验证结果"},
    "lingying_yin": {"label":"虎", "keywords":["危险","高风险","物理","硬件","电气","PLC","马达","变频",
            "紧急停止","安全","rm -rf","delete","DROP","TRUNCATE","format","hazard"],
        "workflow":"complex", "executor":"claude", "desc":"高风险操作，必须过World Model"},
    "lingying_mao": {"label":"兔", "keywords":["简单","快速","改一下","小改动","别名","快捷","alias",
            "touch","mkdir","echo","一键","小修改","快速修改","简单修改"],
        "workflow":"quick", "executor":"opencode", "desc":"快速简单任务，秒级完成"},
    "lingying_chen": {"label":"龙", "keywords":["架构","重构","重写","设计","模块","接口","框架",
            "architecture","refactor","redesign","系统设计","方案","选型",
            "写代码","编写程序","写函数","写脚本","实现算法","开发","coding",
            "python函数","python脚本","python程序","python代码","实现一个","编写一个",
            "斐波那契","算法","递归",
            "天气","台风","新闻","事件","分析","解读","解释","含义",
            "验证一下","验证系统","验证流程","检查系统","检查流程",
            "系统执行","执行流程","AIOS系统",
            "架构设计","概要设计","详细设计","技术方案","技术选型","模块设计",
            "系统架构","底层设计","核心设计","设计方案"],
        "workflow":"complex", "executor":"claude", "desc":"复杂架构/重构/Coding/深度分析，需Claude处理"},
    "lingying_si": {"label":"蛇", "keywords":["安全","审计","漏洞","权限","加密","XSS","SQL注入",
            "security","audit","vulnerability","渗透","合规"],
        "workflow":"standard", "executor":"claude", "desc":"安全审计，需专业分析+验证"},
    "lingying_wu": {"label":"马", "keywords":["批量","批量处理","批量列出","批量查找","并行","爬取","采集","大规模","迁移","遍历",
            "batch","parallel","bulk","全部文件","并发","async","多线程"],
        "workflow":"batch", "executor":"codex", "desc":"批量/并行处理，拆为子任务并发"},
    "lingying_wei": {"label":"羊", "keywords":["文档","说明","注释","README","手册","教程",
            "document","write","describe","Markdown","md"],
        "workflow":"quick", "executor":"opencode", "desc":"文档/写作，内容生产"},
    "lingying_shen": {"label":"猴", "keywords":["测试","单元测试","pytest","unittest","CI","QA",
            "test","testing","验证","断言","mock","覆盖率","回归测试"],
        "workflow":"standard", "executor":"opencode", "desc":"测试/QA，需验证通过"},
    "lingying_you": {"label":"鸡", "keywords":["定时","cron","调度","周期","每天","每周","计划任务",
            "crontab","schedule","periodic","定时任务","提醒","通知"],
        "workflow":"batch", "executor":"codex", "desc":"定时调度任务，由Codex管理"},
    "lingying_xu": {"label":"狗", "keywords":["监控","告警","日志","watch","monitor","alert",
            "健康检查","健康状态","状态","health","uptime","ping","存活","metrics","指标"],
        "workflow":"quick", "executor":"opencode", "desc":"监控/告警，持续运行+异常上报"},
    "lingying_hai": {"label":"猪", "keywords":["存储","备份","归档","存档","压缩","打包",
            "storage","backup","archive","compress","tar","zip","save","清理"],
        "workflow":"standard", "executor":"opencode", "desc":"存储/归档，需完整性验证"},
}

# ── 12星座路由 (并行维度) ──────────────────────────────────
# 与12生肖并列, 部分executor留空表示暂未分配, 需用时补上即可
CONSTELLATION_AGENTS = [
    "star_aries", "star_taurus", "star_gemini", "star_cancer",
    "star_leo", "star_virgo", "star_libra", "star_scorpio",
    "star_sagittarius", "star_capricorn", "star_aquarius", "star_pisces",
]

CONSTELLATION_MAP = {
    "star_aries": {"label":"白羊座", "keywords":["启动","新项目","创建","init","begin","start","kickoff",
            "新建","初始化","开创","begin","genesis"],
        "workflow":"quick", "executor":"opencode", "desc":"启动/新建类任务, 快速响应"},
    "star_taurus": {"label":"金牛座", "keywords":["持久","稳定","长期","反复","daily","keep","maintain",
            "维护","持续","守护","坚守","坚守"],
        "workflow":"standard", "executor":"codex", "desc":"持久稳定型任务, Codex托管"},
    "star_gemini": {"label":"双子座", "keywords":["多个","并发","同时","对比","比较","差异","diff",
            "compare","multiple","both","versus","vs","区别","variant"],
        "workflow":"batch", "executor":"codex", "desc":"多路并发/对比任务, Codex并行处理"},
    "star_cancer": {"label":"巨蟹座", "keywords":["保护","安全","备份","隔离","防护","加密","权限",
            "protect","secure","backup","isolation","safeguard"],
        "workflow":"standard", "executor":"claude", "desc":"安全/保护类, 需Claude审核"},
    "star_leo": {"label":"狮子座", "keywords":["高风险","重大","决策","审批","approve","critical",
            "关键的","决定","批准","危机","recover"],
        "workflow":"complex", "executor":"claude", "desc":"高风险决策, 需World Model审核"},
    "star_virgo": {"label":"处女座", "keywords":["审计","精校","检查","核对","验证","合规","review",
            "audit","proofread","校对","质检","审核","验收"],
        "workflow":"standard", "executor":"claude", "desc":"审计/精校类, 需细致分析"},
    "star_libra": {"label":"天秤座", "keywords":["协调","平衡","分配","权衡","调解","仲裁","consensus",
            "balance","coordinate","negotiate","consensus","公平"],
        # [P1-1 修复 2026-07-12] 原本留 None 走 opencode fallback，但协调/仲裁类是复杂决策
        # 工作，opencode (quick执行器) 顶不住，强制走 claude 复杂管线。
        "workflow":"standard", "executor":"claude", "desc":"协调/仲裁类, Claude 深度协商"},
    "star_scorpio": {"label":"天蝎座", "keywords":["深挖","溯源","根因","trace","root cause","investigate",
            "侦察","追踪","深入分析","穿透","解剖"],
        "workflow":"complex", "executor":"claude", "desc":"深度溯源分析, 需Claude深挖"},
    "star_sagittarius": {"label":"射手座", "keywords":["探索","研究","调研","预研","research","explore",
            "前沿","新技术","趋势","horizon","scan"],
        # [P1-1 修复 2026-07-12] 同上：探索/研究是严肃深度调研，不能走 opencode quick 执行器。
        "workflow":"standard", "executor":"claude", "desc":"探索/研究类, Claude 深度调研"},
    "star_capricorn": {"label":"摩羯座", "keywords":["架构","规划","蓝图","roadmap","strategy","长期规划",
            "战略","里程碑","milestone","演进"],
        "workflow":"complex", "executor":"claude", "desc":"架构/战略规划, 需Claude深度设计"},
    "star_aquarius": {"label":"水瓶座", "keywords":["创新","实验","尝试","pilot","prototype","实验性",
            "颠覆","新颖","创新方案","实验项目","explore"],
        # [P1-1 修复 2026-07-12] 同上：创新/实验需要深度的原创思路，opencode 顶不住创新设计。
        "workflow":"quick", "executor":"claude", "desc":"创新/实验类, Claude 深度创新"},
    "star_pisces": {"label":"双鱼座", "keywords":["创意","文案","设计","灵感","idea","creative",
            "构思","策划","方案","文案","品牌","vision"],
        "workflow":"quick", "executor":"opencode", "desc":"创意/文案类, opencode快速产出"},
}

WORKFLOWS = {
    "quick":    {"steps": ["entry","execute","return"], "verify": False, "wm": False},
    "standard": {"steps": ["entry","dispatch","execute","verify","return"], "verify": True, "wm": False},
    "complex":  {"steps": ["entry","dispatch","decompose","parallel_execute","merge","verify","return"],
                 "verify": True, "wm": True},
    "batch":    {"steps": ["entry","dispatch","batch_execute","verify","return"], "verify": True, "wm": False},
}


_KEYWORD_SPECIFICITY = {}
def _build_specificity():
    kw_counts = {}
    for info in list(ZODIAC_MAP.values()) + list(CONSTELLATION_MAP.values()):
        for kw in info["keywords"]:
            kw_counts[kw] = kw_counts.get(kw, 0) + 1
    for kw, cnt in kw_counts.items():
        _KEYWORD_SPECIFICITY[kw] = 1.0 / cnt
_build_specificity()

def _classify_single(text_lower: str, mapping: dict, default_id: str) -> dict:
    """通用分类器: 对单个映射表做关键词匹配, 返回 {id, label, workflow, executor, confidence, desc}."""
    scores = []
    for cid, info in mapping.items():
        matched = [kw for kw in info["keywords"] if kw in text_lower]
        if matched:
            specificity = sum(_KEYWORD_SPECIFICITY.get(kw, 0) for kw in matched)
            kw_total_len = sum(len(kw) for kw in matched)  # 长关键词更精准
            scores.append((cid, len(matched), specificity, kw_total_len))

    # 排序: 匹配数↓ → 特异性↓ → 关键词总长度↓ (长关键词更精准)
    # 平手时: complex > standard > batch > quick
    _wf_priority = {"complex": 4, "standard": 3, "batch": 2, "quick": 1}
    scores.sort(key=lambda x: (-x[1], -x[2], -x[3], -_wf_priority.get(mapping.get(x[0],{}).get("workflow",""), 0)))

    if not scores:
        cid, confidence = default_id, 0.3
    else:
        cid = scores[0][0]
        c = scores[0][1]
        confidence = 0.85 if c >= 3 else (0.7 if c == 2 else 0.5)

    info = mapping[cid]
    executor = info.get("executor")
    if executor is None:
        executor = "opencode"  # 留空槽位默认走 opencode

    return {
        "id": cid, "label": info["label"],
        "workflow": info["workflow"], "executor": executor,
        "confidence": confidence, "description": info.get("desc", ""),
    }


_CLASSIFY_MODEL = "MiniMax-M3"


def _llm_reclassify(task_text: str, kw_result: dict) -> dict:
    """对低置信度任务走 LLM 语义分类, 返回跟关键词分类同样的 dict 结构."""
    if kw_result["confidence"] >= 0.7:
        return kw_result

    _prompt = (
        "你是一个任务分类器。请将以下任务分类到最合适的 AIOS 执行器。\n"
        "分类规则：\n"
        "- opencode: 信息查询、文件操作、简单脚本、快速执行、监控检查、shell命令\n"
        "- claude: 架构设计、重构、复杂逻辑、安全审计、深度分析、系统设计\n"
        "- codex: 批量处理、并行任务、大规模爬取、数据迁移、定时调度\n"
        f"\n任务: {task_text}\n"
        "\n请只返回一个词: opencode / claude / codex"
    )
    try:
        import json, urllib.request as _req
        _payload = json.dumps({"model": _CLASSIFY_MODEL, "input": _prompt}).encode()
        _r = _req.Request(
            "http://127.0.0.1:4444/v1/responses",
            data=_payload,
            headers={"Content-Type": "application/json"},
        )
        with _req.urlopen(_r, timeout=30) as _resp:
            _body = json.loads(_resp.read().decode())
        _texts = []
        for _msg in _body.get("output", []):
            for _c in _msg.get("content", []):
                if _c.get("type") == "output_text":
                    _texts.append(_c.get("text", ""))
        _reply = " ".join(_texts).strip().lower()
    except Exception:
        return kw_result

    _executor_map = {"opencode": "opencode", "claude": "claude", "codex": "codex"}
    _matched = [e for e in _executor_map if e in _reply]
    if not _matched:
        return kw_result

    _llm_executor = _matched[0]
    _executor_to_workflow = {"opencode": "quick", "claude": "complex", "codex": "batch"}
    _executor_to_depth = {"opencode": "low", "claude": "high", "codex": "batch"}
    _wf = _executor_to_workflow.get(_llm_executor, "quick")
    _steps = WORKFLOWS.get(_wf, WORKFLOWS["quick"])["steps"]

    return {
        "zodiac_id": f"llm/{_llm_executor}",
        "zodiac": _llm_executor,
        "workflow": _wf,
        "executor": _llm_executor,
        "logic_depth": _executor_to_depth.get(_llm_executor, "low"),
        "confidence": 0.85,
        "description": f"LLM语义分类: {_llm_executor}",
        "steps": _steps,
        "verify_required": WORKFLOWS[_wf]["verify"],
        "world_model_required": WORKFLOWS[_wf]["wm"],
        "route_source": "llm",
    }


def classify_task(task_text: str, hermes_strategy: dict | None = None) -> dict:
    """混合分类引擎: 同时匹配12生肖 + 12星座, 取置信度高者.

    如果关键词匹配置信度 < 0.7, 走 LLM 语义重分类.
    """
    text_lower = task_text.lower()
    _LOW_COST_HINTS = ("当前系统时间", "查询时间", "查看状态", "检查端口",
                       "读取文件", "列出文件", "格式转换")
    _force_low = any(hint in task_text for hint in _LOW_COST_HINTS)

    z_result = _classify_single(text_lower, ZODIAC_MAP, "lingying_mao")
    c_result = _classify_single(text_lower, CONSTELLATION_MAP, "star_aries")

    # 取置信度高者; 若星座executor留空则降级用生肖
    if c_result["confidence"] > z_result["confidence"] and c_result["executor"] != "opencode":
        chosen = c_result
        tag = "constellation"
        if c_result["executor"] == "opencode" and c_result["confidence"] == 0.3:
            if z_result["confidence"] > 0.3:
                chosen = z_result
                tag = "zodiac"
    elif z_result["confidence"] > c_result["confidence"]:
        chosen = z_result
        tag = "zodiac"
    else:
        chosen = z_result if z_result["confidence"] >= c_result["confidence"] else c_result
        tag = "zodiac" if z_result["confidence"] >= c_result["confidence"] else "constellation"
        if c_result["executor"] == "opencode" and z_result["executor"] != "opencode":
            chosen = z_result
            tag = "zodiac"

    if chosen["confidence"] < 0.7 and not _force_low:
        llm_result = _llm_reclassify(task_text, chosen)
        if llm_result.get("route_source") == "llm":
            return llm_result

    info_key = chosen["id"]
    wf = WORKFLOWS[chosen["workflow"]]
    executor = chosen["executor"]

    # Deterministic cost guardrails take precedence over probabilistic
    # reclassification for obvious low-risk utility work.
    if _force_low:
        executor = "opencode"

    # Hermes策略调整
    if hermes_strategy and chosen["confidence"] > 0.5:
        # Hermes is advisory and offline-only. Historical failures must never
        # override live routing. Only an explicit, expiring operations policy
        # may disable an executor; the live halt probe below remains decisive.
        disabled = hermes_strategy.get("disabled_executors", [])
        if executor in disabled:
            backups = {"opencode": "claude", "claude": "codex", "codex": "opencode"}
            executor = backups.get(executor, executor)

    # [P1-2 修复 2026-07-12] 递归 backup 链 — 避免 backup 后再次熔断死锁
    # 原代仅仅只调 is_executor_halted() 一次, 如果 backup 后的 executor 也熔断,
    # 会造成任务永远跳不出被踩中的执行器。改为踏环: 顺着 backup 链多走几步, 最多 3 跳。
    from aios_enforcer import is_executor_halted
    _FALLBACK_ORDER = {
        "opencode": ["opencode", "claude", "codex"],
        "claude": ["claude", "codex", "opencode"],
        "codex": ["codex", "opencode", "claude"],
    }
    _original_executor = executor
    _attempts = []
    for candidate in _FALLBACK_ORDER.get(executor, [executor]):
        _attempts.append(candidate)
        if not is_executor_halted(candidate):
            executor = candidate
            break
    # [Debug] 记录 backup 跳距, 方便后续观察是否还有被熔断扣在原位的情况
    if len(_attempts) > 1:
        import sys as _sys
        _sys.path.insert(0, "${AIOS_HOME}/kernel/tools")
        try:
            from aios_bus import publish_event
            publish_event("dispatcher.executor_rebalance",
                          {"original": list(_attempts)[0], "final": executor,
                           "hops": len(_attempts) - 1, "task_text": task_text[:80]},
                          "agent_mesh")
        except Exception:
            pass

    # 执行器 → 认领深度 (与 aios_executor_daemon.EXECUTOR_DEPTH 对齐)
    _EXECUTOR_DEPTH_MAP = {"opencode": "low", "claude": "high", "codex": "batch"}
    logic_depth = _EXECUTOR_DEPTH_MAP.get(executor, "low")

    return {
        "zodiac_id": info_key, "zodiac": chosen["label"],
        "workflow": chosen["workflow"], "executor": executor,
        "logic_depth": logic_depth,  # 与 executor daemon 过滤器对齐
        "confidence": chosen["confidence"], "description": chosen["description"],
        "steps": wf["steps"], "verify_required": wf["verify"],
        "world_model_required": wf["wm"],
        "route_source": tag,  # 标明本次路由来源: zodiac / constellation
    }


def classify_by_constellation(task_text: str) -> dict:
    """纯12星座分类: 输入任务文本 → (星座, workflow, 执行器, 置信度)."""
    text_lower = task_text.lower()
    result = _classify_single(text_lower, CONSTELLATION_MAP, "star_aries")
    wf = WORKFLOWS[result["workflow"]]
    return {
        "constellation_id": result["id"], "constellation": result["label"],
        "workflow": result["workflow"], "executor": result["executor"],
        "confidence": result["confidence"], "description": result["description"],
        "steps": wf["steps"], "verify_required": wf["verify"],
        "world_model_required": wf["wm"],
    }


def zodiac_classify_batch(tasks: list, hermes_strategy: dict | None = None) -> list:
    return [classify_task(t, hermes_strategy) for t in tasks]


def zodiac_list() -> list:
    result = []
    for i, (zid, info) in enumerate(ZODIAC_MAP.items(), 1):
        result.append({
            "id": zid, "index": i, "label": info["label"],
            "workflow": info["workflow"], "executor": info["executor"],
            "description": info["desc"],
        })
    return result


def constellation_list() -> list:
    result = []
    for i, (cid, info) in enumerate(CONSTELLATION_MAP.items(), 1):
        result.append({
            "id": cid, "index": i, "label": info["label"],
            "workflow": info["workflow"],
            "executor": info["executor"] if info["executor"] else "unassigned",
            "description": info["desc"],
        })
    return result


def init_mesh():
    # 5主Agent
    main_agents = {
        "hermes":    {"type": "main", "caps": ["evolution", "learning", "feishu"]},
        "openclaw":  {"type": "main", "caps": ["orchestration", "dispatch", "feishu", "telegram"]},
        "opencode":  {"type": "main", "caps": ["mcp_execution", "cli", "filesystem"]},
        "claude":    {"type": "main", "caps": ["advanced_coding", "architecture", "debugging"]},
        "codex":     {"type": "main", "caps": ["batch", "parallel", "async"]},
    }
    for name, info in main_agents.items():
        lifecycle_register(name, info["type"], info["caps"])

    # 12生肖 (OpenClaw子Agent)
    for name in ZODIAC_AGENTS:
        lifecycle_register(name, "zodiac", ["specialized_task"], parent="openclaw")

    # 12星座 (OpenClaw子Agent, 部分executor留空)
    for name in CONSTELLATION_AGENTS:
        lifecycle_register(name, "constellation", ["specialized_task"], parent="openclaw")

    # 全部设为RUNNING
    for name in list(main_agents.keys()) + ZODIAC_AGENTS + CONSTELLATION_AGENTS:
        lifecycle_set(name, "RUNNING")
        heartbeat(name)

    print(f"✅ Agent Mesh: {len(main_agents)}主 + {len(ZODIAC_AGENTS)}生肖 + {len(CONSTELLATION_AGENTS)}星座 = 29 Agents")

def mesh_status():
    agents = lifecycle_list_all()
    mains = [a for a in agents if a.get("type") == "main"]
    zodiacs = [a for a in agents if a.get("type") == "zodiac"]
    constellations = [a for a in agents if a.get("type") == "constellation"]

    print("=" * 60)
    print("  Agent Mesh — 第十中心")
    print("=" * 60)
    print(f"\n  主Agent ({len(mains)}):")
    for a in mains:
        age = a.get("heartbeat_age_s", 999)
        icon = "🟢" if age < 90 else ("🟡" if age < 300 else "🔴")
        print(f"    {icon} {a['name']:10s} {a.get('status','?'):8s} {age}s ago")

    print(f"\n  12生肖 ({len(zodiacs)}):")
    for a in zodiacs[:6]:
        age = a.get("heartbeat_age_s", 999)
        icon = "🟢" if age < 90 else "🔴"
        print(f"    {icon} {a['name']:15s} {a.get('status','?'):8s} parent={a.get('parent','?')}")
    if len(zodiacs) > 6:
        print(f"    ... 及其他 {len(zodiacs)-6} 个")

    print(f"\n  12星座 ({len(constellations)}):")
    for a in constellations[:6]:
        age = a.get("heartbeat_age_s", 999)
        icon = "🟢" if age < 90 else "🔴"
        print(f"    {icon} {a['name']:15s} {a.get('status','?'):8s} parent={a.get('parent','?')}")
    if len(constellations) > 6:
        print(f"    ... 及其他 {len(constellations)-6} 个")

    offline = [a for a in agents if a.get("status") == "OFFLINE"]
    if offline:
        print(f"\n  ⚠️ OFFLINE: {len(offline)}个 → {[a['name'] for a in offline]}")

    timeout = check_agent_timeout()
    if timeout["restart_triggered"]:
        print(f"  🔄 触发重启: {timeout['restart_triggered']}")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "init": init_mesh()
    elif cmd == "status": mesh_status()
    elif cmd == "check":
        r = check_agent_timeout()
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif cmd == "classify":
        text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if text:
            r = classify_task(text)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print("用法: python3 aios_agent_mesh.py classify <任务文本>")
    elif cmd == "classify_c":
        text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if text:
            r = classify_by_constellation(text)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print("用法: python3 aios_agent_mesh.py classify_c <任务文本>")
    elif cmd == "list":
        print("--- 12生肖 ---")
        for z in zodiac_list():
            ex = z['executor'] or "unassigned"
            print(f"  {z['index']:2d}. {z['label']}({z['id']:15s}) → {ex:12s} [{z['workflow']:8s}] {z['description']}")
        print("\n--- 12星座 ---")
        for c in constellation_list():
            ex = c['executor'] or "unassigned"
            print(f"  {c['index']:2d}. {c['label']}({c['id']:15s}) → {ex:12s} [{c['workflow']:8s}] {c['description']}")
    else: mesh_status()


# ── Pin Registration ──────────────────────────────────
try:
    from aios_bus import register_pin
    register_pin("mesh.classify", classify_task, "12生肖+12星座混合分类: 任务文本 → zodiac/workflow/executor")
    register_pin("mesh.classify_constellation", classify_by_constellation, "纯12星座分类: 任务文本 → constellation/workflow/executor")
    register_pin("mesh.classify_batch", zodiac_classify_batch, "12生肖批量分类")
    register_pin("mesh.zodiac_list", zodiac_list, "列出所有12生肖分类")
    register_pin("mesh.constellation_list", constellation_list, "列出所有12星座分类")
    register_pin("mesh.status", mesh_status, "Agent Mesh 网格状态")
except Exception:
    pass
