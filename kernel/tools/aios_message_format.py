#!/usr/bin/env python3
"""
Unified Message Format — 所有中心统一数据格式
===============================================
标准字段: id / ts / source / type / payload
提供 pack() / unpack() / validate()
"""
import json, time, uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional

# 标准Message Schema
MESSAGE_SCHEMA = {
    "id": str,         # UUID — 全局唯一ID
    "ts": str,         # ISO8601 — 时间戳
    "source": str,     # 来源中心/Agent名
    "type": str,       # 事件类型: task.created / agent.heartbeat / token.usage ...
    "payload": dict,   # 具体数据
    "version": str,    # 格式版本
}

VALID_TYPES = [
    "task.created", "task.assigned", "task.running", "task.completed", "task.failed",
    "agent.online", "agent.offline", "agent.heartbeat",
    "token.usage", "token.budget_warning", "token.budget_exceeded",
    "knowledge.updated", "knowledge.contribution",
    "alert.warning", "alert.critical",
    "autonomy.score_change", "autonomy.reward", "autonomy.penalty",
    "security.violation",
]


def pack(source: str, event_type: str, payload: Dict[str, Any],
         msg_id: str = "") -> Dict[str, Any]:
    """
    打包为标准Message格式。
    所有中心发布事件/写Redis时都调这个。
    """
    if event_type not in VALID_TYPES:
        raise ValueError(f"Unknown event type: {event_type}. Valid: {VALID_TYPES}")

    return {
        "id": msg_id or str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "type": event_type,
        "payload": payload,
        "version": "1.0",
    }


def unpack(raw: Any) -> Optional[Dict[str, Any]]:
    """
    解包 — 自动处理 bytes/str/dict 三种输入。
    所有中心接收事件/读Redis时都调这个。
    """
    if raw is None: return None
    if isinstance(raw, dict): return raw
    if isinstance(raw, (bytes, str)):
        try:
            data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, AttributeError): return None
    return None


def validate(msg: Dict[str, Any]) -> bool:
    """验证Message格式是否合法."""
    if not isinstance(msg, dict): return False
    required = ["id", "ts", "source", "type", "payload", "version"]
    for field in required:
        if field not in msg: return False
    if msg["type"] not in VALID_TYPES: return False
    if not isinstance(msg["payload"], dict): return False
    return True


def to_json(msg: Dict[str, Any]) -> str:
    """序列化为JSON字符串(用于Redis发布)."""
    return json.dumps(msg, ensure_ascii=False)


def from_json(raw: str) -> Optional[Dict[str, Any]]:
    """从JSON字符串反序列化."""
    return unpack(raw)


def quick_event(source: str, event_type: str, **payload) -> Dict[str, Any]:
    """快速创建事件 — 最常用接口."""
    return pack(source, event_type, payload)


# ── 示例 ──
if __name__ == "__main__":
    # 所有中心统一用这些接口
    event = quick_event("orchestration", "task.created",
                        task_name="分析代码", priority=2, logic_depth="high")
    print(f"pack:   {to_json(event)[:120]}...")

    parsed = from_json(to_json(event))
    print(f"unpack: {parsed['type']} from {parsed['source']}")

    print(f"valid:  {validate(event)}")

    # 对比旧格式 vs 新格式
    old = {"task_id": "123", "system": "claude", "status": "ok"}
    print(f"\n旧格式: {json.dumps(old)}")
    new = quick_event("execution", "task.completed", task_id="123", executor="claude", status="completed")
    print(f"新格式: {to_json(new)[:150]}...")
