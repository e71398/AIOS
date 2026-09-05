#!/usr/bin/env python3
"""优化4: 错误分类标准化 — E001~E005标准错误码"""
import sys, json, time
from pathlib import Path; from datetime import datetime, timezone
TOOLS = Path("${AIOS_HOME}/kernel/tools"); sys.path.insert(0, str(TOOLS))
from aios_bus import publish_event, _is_available

ERROR_CODES = {
    "E001": {"name": "网络超时", "handling": "重试3次+指数退避, 仍失败则降级"},
    "E002": {"name": "权限不足", "handling": "检查RBAC配置, 申请临时授权"},
    "E003": {"name": "资源耗尽", "handling": "触发熔断, 降级到轻量模型, 通知用户"},
    "E004": {"name": "Agent无响应", "handling": "标记OFFLINE, 故障转移到Codex"},
    "E005": {"name": "数据格式错误", "handling": "回滚到上一个正确版本, 通知数据提供方"},
}

def classify_error(error_message: str) -> dict:
    msg = error_message.lower()
    if any(kw in msg for kw in ["timeout", "超时", "timed out"]): code = "E001"
    elif any(kw in msg for kw in ["permission", "denied", "权限", "forbidden", "unauthorized"]): code = "E002"
    elif any(kw in msg for kw in ["memory", "disk", "quota", "exceeded", "耗尽", "out of"]): code = "E003"
    elif any(kw in msg for kw in ["no response", "unreachable", "offline", "无响应", "连接"]): code = "E004"
    else: code = "E005"
    return {"code": code, "name": ERROR_CODES[code]["name"], "handling": ERROR_CODES[code]["handling"], "original": error_message[:200]}

def get_handling_guide(error_code: str) -> str:
    return ERROR_CODES.get(error_code, {}).get("handling", "未知错误码")

def log_error(error_code: str, agent: str, detail: str):
    if not _is_available(): return
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    entry = {"code": error_code, "agent": agent, "detail": detail[:200], "ts": datetime.now(timezone.utc).isoformat()}
    r.zadd("aios:logs:error_archive", {json.dumps(entry, ensure_ascii=False): time.time()})
    publish_event("alert.warning", {"type": "error_classified", "code": error_code, "agent": agent}, "error_classifier")

if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Connection timed out after 30s"
    r = classify_error(msg); print(f"{r['code']} {r['name']}: {r['handling']}")
    log_error(r['code'], "test", msg)
