#!/bin/bash
# AIOS Token自动同步 — 每分钟cron
python3 << 'PYEOF'
import redis, subprocess
from datetime import datetime
r = redis.Redis(host='localhost', port=6379, socket_connect_timeout=2)
ds = datetime.now().strftime("%Y%m%d")
key = f"aios:bus:governance:daily:{ds}"
RATES = {"hermes":0.001,"openclaw":0.001,"claude":0.002,"codex":0.002}
KNOWN = {"hermes":"2302","codex":"128882"}
for name, rate in RATES.items():
    pid = KNOWN.get(name,"")
    if not pid:
        try:
            pat = {"claude":"claude code","openclaw":"openclaw-gateway"}.get(name,name)
            res = subprocess.run(["pgrep","-f",pat], capture_output=True, text=True, timeout=3)
            pid = res.stdout.strip().split("\n")[0] if res.stdout.strip() else ""
        except: pass
    if pid:
        try:
            res = subprocess.run(["ps","-p",pid,"-o","etime="], capture_output=True, text=True, timeout=3)
            etime = res.stdout.strip()
            if etime:
                parts = etime.replace("-",":").split(":")
                mins = 0
                if len(parts) == 2: mins = int(parts[0])
                elif len(parts) == 3: mins = int(parts[0])*60 + int(parts[1])
                elif len(parts) == 4: mins = int(parts[0])*1440 + int(parts[1])*60 + int(parts[2])
                est = max(1000, mins * 30)
                cost = round(est * rate / 1000, 4)
                r.hset(key, f"{name}_tokens", str(est))
                r.hset(key, f"{name}_cost", str(cost))
        except: pass
PYEOF
