#!/usr/bin/env python3
"""优化9: 飞书回复格式标准化 — 表格/卡片/emoji/Markdown"""
import sys, json

MAX_LENGTH = 4000

def format_success(result: dict) -> str:
    title = result.get("task", "任务")[:60]
    summary = result.get("summary", "")[:500]
    cost = result.get("cost", "")
    time_str = result.get("time", "")
    lines = [f"✅ **任务完成**: {title}", f"📊 **结果**: {summary}"]
    if cost: lines.append(f"💰 **费用**: {cost}")
    if time_str: lines.append(f"⏱️ **耗时**: {time_str}")
    return _truncate("\n".join(lines))

def format_error(code: str, message: str) -> str:
    emoji_map = {"E001": "⏰", "E002": "🔒", "E003": "💥", "E004": "🔌", "E005": "📝"}
    emoji = emoji_map.get(code, "❌")
    return _truncate(f"{emoji} **错误 [{code}]**: {message[:300]}")

def format_warning(content: str) -> str:
    return _truncate(f"⚠️ **告警**: {content[:300]}")

def format_table(headers: list, rows: list) -> str:
    """生成Markdown表格."""
    hdr = "| " + " | ".join(headers) + " |"
    sep = "|" + "|".join(["---" for _ in headers]) + "|"
    data = "\n".join("| " + " | ".join(str(c)[:50] for c in row) + " |" for row in rows[:10])
    return _truncate(f"{hdr}\n{sep}\n{data}")

def format_card(title: str, fields: dict) -> str:
    """飞书卡片格式."""
    lines = [f"**{title}**", ""]
    for k, v in fields.items(): lines.append(f"▪ {k}: {str(v)[:100]}")
    return _truncate("\n".join(lines))

def _truncate(text: str) -> str:
    if len(text) > MAX_LENGTH: return text[:MAX_LENGTH] + "\n...(已截断)"
    return text

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if cmd == "demo":
        print(format_success({"task": "分析Python代码", "summary": "共3021行, 15个文件", "cost": "$0.05"}))
        print(format_error("E001", "Connection timed out"))
        print(format_table(["Agent", "状态", "Token"], [["hermes","🟢","0"],["claude","🟢","355K"]]))
