#!/usr/bin/env python3
"""
Knowledge Contribution — 主动分享经验 → Knowledge Center
=========================================================
高贡献Agent获得更多执行权。
"""
import sys, os, json
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
sys.path.insert(0, str(TOOLS))

from aios_bus import check_recent, publish_event, _is_available
from aios_semantic_search import index_document

KEY_CONTRIB = "aios:autonomy:contributions"


def collect_contribution(agent_name: str, task_name: str,
                         result_summary: str, pattern: str = "") -> dict:
    """
    收集Agent的知识贡献.
    Agent完成复杂任务后 → 主动提炼可复用经验 → 写入Knowledge Center.
    """
    # 质量评估
    quality = "high" if len(result_summary) > 100 and pattern else (
        "medium" if len(result_summary) > 50 else "low")

    contrib = {
        "agent": agent_name,
        "task": task_name[:100],
        "summary": result_summary[:500],
        "pattern": pattern[:200] if pattern else "",
        "quality": quality,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    # 写入Redis
    if _is_available():
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        r.zadd(KEY_CONTRIB, {
            json.dumps(contrib, ensure_ascii=False): time.time()
        })

    # 高质量贡献 → 写入知识库索引
    if quality == "high":
        title = f"[{agent_name}] {task_name[:60]}"
        index_document("autonomy", title, result_summary[:1000])
        publish_event("knowledge.updated", {
            "type": "contribution",
            "agent": agent_name,
            "task": task_name[:60],
            "quality": quality,
        }, "knowledge_contribution")

    return {"collected": True, "quality": quality,
            "agent": agent_name, "task": task_name[:60]}


def get_top_contributors(limit: int = 5) -> list:
    """获取贡献排行."""
    if not _is_available():
        return []
    import redis as _r
    r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    raw = r.zrevrange(KEY_CONTRIB, 0, limit - 1, withscores=True)
    results = []
    for data, score in raw:
        try:
            d = json.loads(data.decode() if isinstance(data, bytes) else data)
            d["score"] = int(score)
            results.append(d)
        except: pass
    return results


def auto_extract_pattern(agent_name: str, recent_tasks: int = 20) -> list:
    """自动从最近任务中提取可复用模式."""
    recent = check_recent(limit=recent_tasks)
    agent_tasks = [r for r in recent if r.get("system") == agent_name
                   and r.get("status") == "completed"]

    patterns = []
    task_names = [r.get("task_name", "") for r in agent_tasks]

    # 简单模式: 重复出现的任务类型
    from collections import Counter
    keywords = []
    for name in task_names:
        for kw in name.split():
            if len(kw) > 2:
                keywords.append(kw)
    common = Counter(keywords).most_common(5)

    for kw, count in common:
        if count >= 2:
            patterns.append({
                "keyword": kw,
                "frequency": count,
                "suggestion": f"{agent_name}擅长'{kw}'类任务, 建议优先分配"
            })

    return patterns


# ── CLI ──
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"

    if cmd == "top":
        print("🏆 知识贡献榜:")
        for i, c in enumerate(get_top_contributors(5), 1):
            print(f"  {i}. [{c['agent']}] {c['task'][:50]} q={c['quality']}")

    elif cmd == "patterns":
        agent = sys.argv[2] if len(sys.argv) > 2 else "claude"
        print(f"{agent} 可复用模式:")
        for p in auto_extract_pattern(agent):
            print(f"  🔍 {p['keyword']} (×{p['frequency']}) → {p['suggestion']}")

    elif cmd == "add":
        agent = sys.argv[2] if len(sys.argv) > 2 else "claude"
        task = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "复杂任务分析"
        result = collect_contribution(agent, task, "成功完成, 提炼了可复用经验", "高效模式")
        print(f"✅ 贡献已记录: q={result['quality']}")
