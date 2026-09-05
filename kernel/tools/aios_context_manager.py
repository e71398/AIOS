#!/usr/bin/env python3
"""优化3: 上下文窗口管理 — >50轮自动压缩, 提取摘要"""
import sys, json, hashlib
from pathlib import Path; from datetime import datetime, timezone
TOOLS = Path("${AIOS_HOME}/kernel/tools"); sys.path.insert(0, str(TOOLS))
from aios_bus import _is_available, publish_event

KEY_CTX = "aios:context"
MAX_ROUNDS = 50

def get_context(session_id: str) -> list:
    if not _is_available(): return []
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    raw = r.lrange(f"{KEY_CTX}:{session_id}", 0, -1)
    return [json.loads(m.decode() if isinstance(m, bytes) else m) for m in raw]

def add_message(session_id: str, role: str, content: str):
    if not _is_available(): return
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    msg = {"role": role, "content": content[:2000], "ts": datetime.now(timezone.utc).isoformat()}
    r.rpush(f"{KEY_CTX}:{session_id}", json.dumps(msg, ensure_ascii=False))
    r.expire(f"{KEY_CTX}:{session_id}", 86400)
    if r.llen(f"{KEY_CTX}:{session_id}") > MAX_ROUNDS: compress_if_needed(session_id)

def compress_if_needed(session_id: str) -> dict:
    if not _is_available(): return {}
    ctx = get_context(session_id)
    if len(ctx) <= MAX_ROUNDS: return {"compressed": False}
    # 保留前10条+后10条, 中间提取摘要
    head = ctx[:10]; tail = ctx[-10:]
    middle_content = " ".join(m["content"][:500] for m in ctx[10:-10])
    summary = f"[压缩{len(ctx)-20}轮对话] 关键点: {middle_content[:500]}"
    compressed = head + [{"role": "system", "content": summary, "ts": datetime.now(timezone.utc).isoformat()}] + tail
    import redis as _r; r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
    r.delete(f"{KEY_CTX}:{session_id}")
    for m in compressed: r.rpush(f"{KEY_CTX}:{session_id}", json.dumps(m, ensure_ascii=False))
    r.hset(f"{KEY_CTX}:summary", session_id, json.dumps({"compressed": True, "original_rounds": len(ctx), "compressed_to": len(compressed)}, ensure_ascii=False))
    publish_event("knowledge.updated", {"type": "context_compressed", "session": session_id, "from": len(ctx), "to": len(compressed)}, "context_manager")
    return {"compressed": True, "original": len(ctx), "now": len(compressed)}

def force_compress(session_id: str): return compress_if_needed(session_id)

if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "test_session"
    cmd = sys.argv[2] if len(sys.argv) > 2 else "add"
    if cmd == "add":
        for i in range(55): add_message(sid, "user", f"消息{i}")
        print(f"添加55轮 → 压缩: {compress_if_needed(sid)}")
    elif cmd == "view":
        ctx = get_context(sid); print(f"{len(ctx)}条消息")
        for m in ctx[:3]: print(f"  [{m['role']}] {m['content'][:80]}")
