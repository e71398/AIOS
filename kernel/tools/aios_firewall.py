#!/usr/bin/env python3
"""
AIOS v4.0 Bus Gate (总线门禁)
==============================
五个AI是独立工具。AIOS不控制它们内部行为。
AIOS只守自己的Redis总线——谁能写什么key。

三模式:
  warn     — 默认, 记录但不拦截
  enforce  — 拦截越界写 + 支持授权
  lockdown — 拦截 + 拒绝授权

授权:
  aios-firewall grant <系统> bus <key> <分钟> <理由>
  aios-firewall revoke <授权ID>
  aios-firewall grants
"""

import json, os, sys, re
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

TOOLS = Path("${AIOS_HOME}/kernel/tools")
AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
sys.path.insert(0, str(TOOLS))
from aios_bus import publish_event, _is_available, generate_task_id

# 每个AI在总线上的自有命名空间
BUS_NAMESPACE = {
    "hermes": ["aios:bus:hermes:", "aios:bus:system:hermes:", "aios:bus:agent:hermes", "aios:bus:task:", "aios:bus:index", "aios:bus:stats:hermes:"],
    "openclaw": ["aios:bus:openclaw:", "aios:bus:system:openclaw:", "aios:bus:agent:openclaw", "aios:bus:queue:", "aios:bus:state:", "aios:bus:task:", "aios:bus:lock:", "aios:bus:registry:", "aios:bus:callback:", "aios:bus:index", "aios:bus:stats:openclaw:"],
    "opencode": ["aios:bus:opencode:", "aios:bus:system:opencode:", "aios:bus:agent:opencode", "aios:bus:task:", "aios:bus:state:", "aios:bus:lock:", "aios:bus:queue:", "aios:bus:index", "aios:bus:stats:opencode:"],
    "claude": ["aios:bus:claude:", "aios:bus:system:claude:", "aios:bus:agent:claude", "aios:bus:task:", "aios:bus:state:", "aios:bus:lock:", "aios:bus:queue:", "aios:bus:index", "aios:bus:stats:claude:"],
    "codex": ["aios:bus:codex:", "aios:bus:system:codex:", "aios:bus:agent:codex", "aios:bus:task:", "aios:bus:state:", "aios:bus:lock:", "aios:bus:queue:", "aios:bus:index", "aios:bus:stats:codex:"],
}

# [SECURITY] 默认改为 enforce — warn 不拦截任何违规. 需要 aiOS_FIREWALL_MODE=warn
# 才退回纯记录模式.
_MODE_DEFAULT = "enforce"
MODE = _MODE_DEFAULT

def get_mode():
    m = os.environ.get("AIOS_FIREWALL_MODE", MODE)
    return m if m in ("warn","enforce","lockdown") else _MODE_DEFAULT

def set_mode(m):
    global MODE; MODE = m; os.environ["AIOS_FIREWALL_MODE"] = m

def check_bus(system: str, key: str) -> Tuple[bool, str]:
    """检查系统能否写这个Redis key."""
    if system not in BUS_NAMESPACE: return True, "unknown"
    mode = get_mode()

    # 检查是否是自己的命名空间
    for ns in BUS_NAMESPACE[system]:
        if key.startswith(ns): return True, "ok"

    # 越界了
    if mode == "warn": return True, f"warn: {system} wrote {key[:50]}"
    if mode == "lockdown": return False, f"lockdown: {system} blocked on {key[:50]}"

    # enforce: 查授权
    for g in _list_grants(system):
        if g.get("type")=="bus" and g.get("active")=="true":
            gt = g.get("target","")
            if gt=="*" or gt in key or key in gt:
                return True, f"ok (grant:{g['grant_id']})"

    publish_event("security.violation", {"type":"bus_acl","system":system,"key":key},"bus-gate")
    return False, f"blocked: {system} on {key[:50]}"

def grant(system, ptype, target, minutes=10, reason=""):
    if not _is_available(): return None
    import redis as r
    gid = f"g_{generate_task_id()[:8]}"
    try:
        c = r.Redis(host='localhost',port=6379,socket_connect_timeout=2)
        c.hset(f"aios:bus:grant:{gid}", mapping={
            "grant_id":gid,"system":system,"type":ptype,"target":target,
            "minutes":str(minutes),"reason":reason,
            "ts":datetime.now(timezone.utc).isoformat(),"active":"true"})
        c.expire(f"aios:bus:grant:{gid}", min(minutes*60, 3600))
        return gid
    except: return None

def revoke(gid):
    if not _is_available(): return False
    try:
        import redis as r
        r.Redis(host='localhost',port=6379,socket_connect_timeout=2).delete(f"aios:bus:grant:{gid}")
        return True
    except: return False

def _list_grants(system=""):
    if not _is_available(): return []
    try:
        import redis as r; c = r.Redis(host='localhost',port=6379,socket_connect_timeout=2)
        grants=[]
        for k in c.scan_iter("aios:bus:grant:*"):
            d=c.hgetall(k)
            if d:
                dd={kk.decode() if isinstance(kk,bytes) else kk: vv.decode() if isinstance(vv,bytes) else vv for kk,vv in d.items()}
                if not system or dd.get("system")==system: grants.append(dd)
        return grants
    except: return []

def list_grants(system=""):
    return _list_grants(system)

def status():
    m=get_mode()
    print(f"AIOS Bus Gate | 模式: {m}")
    print(f"  warn=记录不拦(默认) enforce=拦截+授权 lockdown=封锁")
    print()
    for s, ns in BUS_NAMESPACE.items():
        print(f"  {s:10s} → {', '.join(n.replace('aios:bus:','') for n in ns[:3])}")

if __name__ == "__main__":
    a=sys.argv
    if len(a)<2: status()
    elif a[1]=="mode": print(f"{'✅' if len(a)>2 and set_mode(a[2]) else get_mode()}")
    elif a[1]=="check": ok,msg=check_bus(a[2] if len(a)>2 else "hermes", a[3] if len(a)>3 else ""); print(f"{'✅' if ok else '🚫'} {msg}")
    elif a[1]=="grant":
        g=grant(a[2] if len(a)>2 else "", "bus", a[3] if len(a)>3 else "*", int(a[4]) if len(a)>4 else 10, " ".join(a[5:]) if len(a)>5 else "")
        print(f"✅ {g}" if g else "❌")
    elif a[1]=="revoke": print(f"{'✅' if revoke(a[2]) else '❌'}")
    elif a[1]=="grants":
        for g in list_grants(a[2] if len(a)>2 else ""): print(f"  [{g['system']}]→{g['target'][:30]} ({g.get('minutes','?')}min) {g['grant_id']}")
    elif a[1]=="test":
        print("warn:"); [print(f"  {s}→{k}: {'✅' if check_bus(s,k)[0] else '🚫'}") for s,k in [("hermes","aios:bus:task:1"),("openclaw","aios:bus:task:1")]]
        set_mode("enforce"); print("enforce:"); [print(f"  {s}→{k}: {'✅' if check_bus(s,k)[0] else '🚫'}") for s,k in [("hermes","aios:bus:task:1"),("openclaw","aios:bus:task:1")]]
        set_mode("warn")
    else: status()
