#!/usr/bin/env python3
"""Knowledge Contribution — 经验收集+质量评估+写回Knowledge Center"""
import sys, os, json, time
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
sys.path.insert(0, str(TOOLS))
from aios_bus import check_recent, publish_event, _is_available
from aios_semantic_search import index_document

KEY_CONTRIB = "aios:autonomy:contributions"


def contribution_collector(agent: str, task: str, summary: str, pattern: str = "") -> dict:
    """收集Agent主动分享的经验."""
    quality = quality_scorer(summary, pattern)
    contrib = {"agent": agent, "task": task[:100], "summary": summary[:500],
               "pattern": pattern[:200], "quality": quality,
               "ts": datetime.now(timezone.utc).isoformat()}
    if _is_available():
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
        r.zadd(KEY_CONTRIB, {json.dumps(contrib, ensure_ascii=False): time.time()})
    if quality in ("high", "medium"):
        knowledge_base_writer(agent, task, summary)
        publish_event("knowledge.updated", {"type":"contribution","agent":agent,"quality":quality},"autonomy")
    return {"collected": True, "quality": quality, "agent": agent}


def quality_scorer(summary: str, pattern: str = "") -> str:
    """评估贡献质量: high/medium/low."""
    score = 0
    if len(summary) > 100: score += 3
    elif len(summary) > 50: score += 1
    if pattern: score += 3
    if any(kw in summary for kw in ["经验", "教训", "模式", "复用", "pattern", "可避免"]): score += 2
    return "high" if score >= 6 else ("medium" if score >= 3 else "low")


def knowledge_base_writer(agent: str, task: str, summary: str):
    """写回Knowledge Center索引."""
    index_document("autonomy", f"[{agent}] {task[:60]}", summary[:1000])


def get_contributions(limit: int = 10) -> list:
    """获取贡献列表."""
    if not _is_available(): return []
    import redis as _r
    r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    raw = r.zrevrange(KEY_CONTRIB, 0, limit - 1, withscores=True)
    return [dict(**json.loads(d.decode() if isinstance(d, bytes) else d), score=int(s))
            for d, s in raw]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for c in get_contributions(5):
            print(f"  [{c['agent']}] q={c['quality']} {c['task'][:50]}")
    elif cmd == "add":
        r = contribution_collector(sys.argv[2] if len(sys.argv) > 2 else "claude",
                                   " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "任务",
                                   "成功完成,提炼可复用经验", "高效模式")
        print(f"✅ q={r['quality']}")
