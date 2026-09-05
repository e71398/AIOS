#!/usr/bin/env python3
"""实时Token追踪 — 轮询API余额, 差量计算真实消耗"""
import redis, json, time, urllib.request
import os
from datetime import datetime
from pathlib import Path

r = redis.Redis(host='localhost', port=6379, socket_connect_timeout=2)
STATE_FILE = Path("${AIOS_HOME}/cache/token_state.json")

# DeepSeek余额
def get_deepseek_balance():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    try:
        req = urllib.request.Request("https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {api_key}"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return float(data["balance_infos"][0]["total_balance"])
    except Exception as e:
        return None

# 读取上次状态
prev = {}
if STATE_FILE.exists():
    prev = json.loads(STATE_FILE.read_text())

# 获取当前余额
ds_balance = get_deepseek_balance()
ds = datetime.now().strftime("%Y%m%d")
key = f"aios:bus:governance:daily:{ds}"

if ds_balance:
    prev_balance = prev.get("deepseek_balance", ds_balance)
    cost_delta = round(prev_balance - ds_balance, 6)
    
    if cost_delta > 0:
        # 按DeepSeek V4 Pro价格反算tokens ($0.14/M input, $0.28/M output, 平均$0.20/M)
        est_tokens = int(cost_delta / 0.0002)  # $0.20/1M = $0.0002/1K
        # 累计到Redis
        current = int(r.hget(key, "claude_tokens") or 0)
        r.hset(key, "claude_tokens", str(current + est_tokens))
        r.hset(key, "claude_cost", str(round(cost_delta, 4)))
        print(f"DeepSeek: ¥{prev_balance:.2f}→¥{ds_balance:.2f} Δ¥{cost_delta:.4f} ≈{est_tokens:,}t")

    # 保存状态
    prev["deepseek_balance"] = ds_balance
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(prev))

print(f"余额: ¥{ds_balance:.2f}")
