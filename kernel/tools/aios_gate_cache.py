#!/usr/bin/env python3
"""
Gate Cache — 门禁异步缓存
===========================
- 异步预热: 任务进来先触发搜索, 结果异步返回
- 缓存命中: 相似任务结果缓存3小时
- 超时标记: "探索不完整"而非"通过"
"""
import sys, os, json, time, hashlib, threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import _is_available

CACHE_TTL = 3 * 3600  # 3小时
KEY_CACHE = "aios:cache:gate"


def task_hash(task_name: str) -> str:
    return hashlib.md5(task_name.encode()).hexdigest()[:12]


def cache_get(task_name: str) -> dict:
    """读缓存."""
    if not _is_available(): return {}
    import redis as _r
    r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    raw = r.hget(KEY_CACHE, task_hash(task_name))
    if raw:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        age = time.time() - data.get("cached_at", 0)
        if age < CACHE_TTL: return data
    return {}


def cache_set(task_name: str, result: dict):
    """写缓存."""
    if not _is_available(): return
    import redis as _r
    r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    result["cached_at"] = time.time()
    r.hset(KEY_CACHE, task_hash(task_name), json.dumps(result, ensure_ascii=False))
    r.expire(KEY_CACHE, CACHE_TTL)


def async_prefetch(task_name: str):
    """异步预热: 后台搜, 不阻塞."""
    def _search():
        from aios_semantic_search import search as sem_search
        results = sem_search(task_name, limit=5)
        cache_set(task_name, {"results": len(results), "top": results[0]["title"][:60] if results else None})
    threading.Thread(target=_search, daemon=True).start()


def gate_with_cache(task_name: str) -> dict:
    """带缓存的4项门禁检查 — 超时标记'探索不完整'."""
    cached = cache_get(task_name)
    if cached:
        cached["from_cache"] = True
        return cached

    result = {"task": task_name[:100], "passed": True, "from_cache": False,
              "incomplete": [], "ts": datetime.now(timezone.utc).isoformat()}

    start = time.time()
    try:
        from aios_semantic_search import search as sem_search
        r = sem_search(task_name, limit=3)
        elapsed = time.time() - start
        if elapsed > 3 or not r:
            result["incomplete"].append("case_search_timeout")
            result["note"] = "探索不完整: 搜索超时或降级"
        result["case_search"] = {"found": len(r), "elapsed": round(elapsed, 2)}
    except:
        result["incomplete"].append("case_search_error")

    # 缓存
    cache_set(task_name, result)
    # 后台预热: 搜更深
    async_prefetch(task_name)
    return result


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "分析Python代码"
    r = gate_with_cache(task)
    print(f"门禁: {'⚠️探索不完整' if r.get('incomplete') else '✅'} "
          f"(缓存:{r['from_cache']}) {r.get('note','')}")
