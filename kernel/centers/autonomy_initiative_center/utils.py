"""Utility functions."""
import hashlib, json, time

def normalize_query(query: str) -> str:
    return query.strip().lower()[:200]

def fingerprint_text(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]

def now_ts() -> int:
    return int(time.time())

def safe_json_dumps(data: dict) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except:
        return "{}"

def compute_complexity_level(task: dict) -> str:
    name = (task.get("task_name") or task.get("task") or "").lower()
    high_kw = ["架构","重构","安全","审计","设计","复杂"]
    medium_kw = ["分析","优化","处理","批量","迁移"]
    if any(k in name for k in high_kw):
        return "high"
    if any(k in name for k in medium_kw):
        return "medium"
    return "low"
