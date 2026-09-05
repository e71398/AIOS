#!/usr/bin/env python3
"""
MiniMax Token Aggregator — 从OpenClaw sessions聚合MiniMax用量→治理数据
==================================================================
crontab: */5 * * * * python3 aios_minimax_tracker.py
"""
import json, sys, os
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import _redis_client, KEY_PREFIX, _is_available

OPENCLAW_AGENTS = Path("${HOME}/.openclaw/agents")
MINIMAX_MODELS = ("MiniMax", "minimax", "MiniMax-M", "MiniMax-M3", "MiniMax-M2.7")


def aggregate_minimax_tokens(today_only: bool = True) -> dict:
    """聚合所有OpenClaw agent session中的MiniMax token用量."""
    if not _is_available():
        return {"error": "redis_unavailable"}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = defaultdict(lambda: {"tokens_input": 0, "tokens_output": 0, "sessions": 0, "cost_est": 0.0})

    for agent_dir in OPENCLAW_AGENTS.iterdir():
        if not agent_dir.is_dir():
            continue
        sessions_file = agent_dir / "sessions" / "sessions.json"
        if not sessions_file.exists():
            continue

        try:
            data = json.loads(sessions_file.read_text())
        except Exception:
            continue

        for agent_key, session in data.items():
            if not isinstance(session, dict):
                continue

            model = str(session.get("model", "")).lower()
            if not any(m.lower() in model for m in MINIMAX_MODELS):
                continue

            started = session.get("startedAt", session.get("sessionStartedAt", ""))
            if today_only and started:
                try:
                    session_date = started[:10]
                    if session_date != today:
                        continue
                except Exception:
                    pass

            total_tokens = int(session.get("totalTokensFresh", 0))
            ctx_tokens = int(session.get("contextTokens", 0))
            if total_tokens == 0:
                total_tokens = ctx_tokens  # 只有contextTokens时用它
            if total_tokens == 0:
                continue

            agent_name = agent_key.split(":")[0] if ":" in agent_key else agent_key
            stats[agent_name]["tokens_input"] += ctx_tokens
            stats[agent_name]["tokens_output"] += max(0, total_tokens - ctx_tokens)
            stats[agent_name]["sessions"] += 1
            # MiniMax M3 ≈ $0.002/1K tokens
            stats[agent_name]["cost_est"] += total_tokens * 0.002 / 1000

    return dict(stats)


def push_to_governance(stats: dict):
    """将MiniMax token数据写入Redis治理层."""
    if not _is_available():
        return False

    today = datetime.now().strftime("%Y%m%d")
    key = f"{KEY_PREFIX}:governance:daily:{today}"

    total_input = sum(s["tokens_input"] for s in stats.values())
    total_output = sum(s["tokens_output"] for s in stats.values())
    total_tokens = total_input + total_output
    total_cost = sum(s["cost_est"] for s in stats.values())

    data = {}
    for agent, s in stats.items():
        total = s["tokens_input"] + s["tokens_output"]
        data[f"{agent}_tokens"] = int(total)
        data[f"{agent}_cost"] = round(s["cost_est"], 4)

    data["hermes_tokens"] = 0
    data["hermes_cost"] = 0
    data["openclaw_tokens"] = int(total_tokens)
    data["openclaw_cost"] = round(total_cost, 4)

    try:
        for k, v in data.items():
            _redis_client.hset(key, k, str(v))
        return True
    except Exception:
        return False


if __name__ == "__main__":
    stats = aggregate_minimax_tokens(today_only=True)
    if stats:
        pushed = push_to_governance(stats)
        total = sum(s["tokens_input"] + s["tokens_output"] for s in stats.values())
        print(f"MiniMax: {total:,} tokens ({len(stats)} agents) → governance {'✅' if pushed else '❌'}")
    else:
        print("MiniMax: 今日无活跃session")
