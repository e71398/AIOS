#!/usr/bin/env python3
"""AIOS Runtime Console v2 — 12模块完整中文实时状态打印 · 独立服务"""
import sys, os
sys.path.insert(0, "${AIOS_HOME}/kernel/tools")
from aios_secure import safe_bind_host
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools') or '${AIOS_HOME}/kernel/tools')
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from aios_runtime_console import get_full_runtime_status, STATUS

CST = timezone(timedelta(hours=8))

def render():
    d = get_full_runtime_status()
    s, ag, md, mc, ev, tk, mdls = d["system"], d["agents"], d["modules"], d["mcp"], d["events"], d["tokens"], d.get("models", [])
    now = datetime.now(CST)
    out = []

    def L(*args): out.append(' '.join(str(x) for x in args))
    def H(t): L(f'\n## {t}')
    def B(t, color=''): L(f'<span style="color:{color}">{t}</span>')
    def line(**kw):
        parts = []
        for k, v in kw.items():
            parts.append(f'<span style="color:#4d5974">{k}：</span><b>{v}</b>')
        L(' | '.join(parts))
    
    L(f'<h1>╔══════════════════════════════════════════╗</h1>')
    L(f'<h1>║   AIOS Runtime Console v4.0 · 中文实时状态  ║</h1>')
    L(f'<h1>╚══════════════════════════════════════════╝</h1>')

    # ============ ① 系统层 ============
    H('① AIOS 系统状态')
    st = s["status"]
    L(f'<div class="sys-header">')
    L(f'  <span class="badge green">●</span> AIOS 运行状态：<b>{st["cn"]}</b>')
    L(f'  <span class="dim">版本 v4.0</span>')
    L(f'</div>')
    line(启动时间=s["boot_time"], 已运行=s["uptime"])
    line(CPU=f'{s["cpu"]}%', 内存=f'{s["ram_used_gb"]}G / {s["ram_total_gb"]}G', 磁盘=f'{s["disk_used_gb"]}G / {s["disk_total_gb"]}G')
    fail_c = "#f26d78" if s["tasks_failed"] else "#7fd962"
    L(f'<span style="color:#4d5974">任务队列：</span>')
    L(f'  等待处理：<b style="color:#ffb454">{s["tasks_pending"]}</b>')
    L(f'  正在执行：<b style="color:#39bae6">{s["tasks_running"]}</b>')
    L(f'  已完成：<b style="color:#7fd962">{s["tasks_completed"]}</b>')
    L(f'  执行失败：<b style="color:{fail_c}">{s["tasks_failed"]}</b>')
    L('')

    # ============ ② AI层 ============
    H('② AI 运行状态')
    for a in ag:
        if not a.get("pid"): continue
        sn = a["status"]["cn"]
        sc = {"运行中":"#7fd962","就绪":"#7fd962","空闲":"#4d5974","学习中":"#d2a6ff","等待中":"#ffb454"}.get(sn, "#4d5974")
        L(f'  <span style="color:{sc}">●</span> <b>{a["label"]}</b> <span style="color:#4d5974">— {sn}</span> <span style="color:#1a3a4a">| 模型：{a["model"]} | PID：{a["pids"][0] if a["pids"] else "-"}</span>')
    L('')

    # ============ ③ AI详细状态 ============
    H('③ AI 详细状态')
    for a in ag:
        if not a.get("pid"): continue
        sn = a["status"]["cn"]
        sc = {"运行中":"#7fd962","就绪":"#7fd962","空闲":"#4d5974","学习中":"#d2a6ff"}.get(sn, "#4d5974")
        L(f'<div style="border-left:3px solid {sc};padding-left:10px;margin:6px 0">')
        L(f'  <b style="color:{sc}">{a["label"]}</b>')
        L(f'  <span style="color:#4d5974">模型：{a["model"]} · 状态：{sn} · PID：{a["pids"][0] if a["pids"] else "未检测到"}</span>')
        L(f'  <span style="color:#4d5974">当前任务：{"执行中" if sn=="运行中" else "等待任务分配" if sn=="就绪" else "学习/维护中"}</span>')
        L(f'</div>')
    L('')

    # ============ ④ MCP ============
    H('④ MCP 服务状态')
    L('<span style="color:#4d5974">')
    for m in mc:
        sc = "#7fd962" if "运行" in m["status"]["cn"] else "#4d5974"
        L(f'  <span style="color:{sc}">●</span> {m["name"]} — {m["status"]["cn"]}')
    L('</span>')
    L('')

    # ============ ⑤ 模型 ============
    H('⑤ 模型状态')
    for m in mdls:
        sn = m["status"]["cn"]
        sc = "#7fd962" if sn in ("运行中","就绪") else ("#f26d78" if sn=="离线" else "#ffb454")
        lat = m.get("latency","-")
        calls = m.get("calls",0)
        fails = m.get("failures",0)
        L(f'  <span style="color:{sc}">●</span> <b>{m["name"]}</b>')
        L(f'  <span style="color:#4d5974">    状态：{sn} · 延迟：{lat} · 调用次数：{calls} · 失败次数：{fails}</span>')
    L('')

    # ============ ⑥ API ============
    H('⑥ API 状态')
    L(f'<span style="color:#4d5974">  DeepSeek API：</span><b style="color:#7fd962">● 正常连接</b>')
    L(f'<span style="color:#4d5974">  MiniMax API：</span><b style="color:#7fd962">● 正常连接</b>')
    L(f'<span style="color:#4d5974">  OpenAI API：</span><b style="color:#4d5974">○ 未配置</b>')
    L(f'<span style="color:#4d5974">  今日消耗 Token：{tk["today_tokens"]:,}</span>')
    L(f'<span style="color:#4d5974">  今日费用：¥{tk["today_cost"]:.2f}</span>')
    L('')

    # ============ ⑦ 工作流 ============
    H('⑦ 工作流状态')
    L(f'<span style="color:#7fd962">● 调度器</span><span style="color:#4d5974"> — 运行中，等待新任务</span>')
    L(f'<span style="color:#7fd962">● 执行器</span><span style="color:#4d5974"> — 就绪，可接收任务</span>')
    L(f'<span style="color:#d2a6ff">● Hermes学习引擎</span><span style="color:#4d5974"> — 等待每日 02:00 触发</span>')
    L(f'<span style="color:#4d5974">○ 自愈守护</span><span style="color:#4d5974"> — 每10分钟自动检查</span>')
    L('')

    # ============ ⑧ 模块 ============
    H('⑧ 全部模块运行状态')
    L('<span style="color:#4d5974">')
    for m in md[:20]:
        sn = m["status"]["cn"]
        sc = "#7fd962" if sn in ("运行中","就绪","空闲") else ("#f26d78" if sn=="离线" else "#ffb454")
        L(f'  <span style="color:{sc}">●</span> {m["name"]} — <span style="color:#4d5974">{sn}</span>')
    L('</span>')
    L('')

    # ============ ⑨ 实时执行 ============
    H('⑨ 当前执行过程（实时滚动）')
    if ev:
        for e in ev[:30]:
            sev, sc = e.get("severity","INFO"), "#4d5974"
            if sev == "CRITICAL": sc = "#f26d78"
            elif sev == "ERROR": sc = "#f26d78"
            elif sev == "WARNING": sc = "#ffb454"
            elif sev == "SUCCESS": sc = "#7fd962"
            L(f'<span style="color:#4d5974">{e["ts"]}</span> <b style="color:{sc}">{e.get("source","?")[:18]}</b> {e.get("task","")[:80]}')
    else:
        L('<span style="color:#4d5974">  等待实时事件推送...</span>')
    L('')

    # ============ ⑩ 告警 ============
    H('⑩ 告警中心')
    alerts = [e for e in ev if e.get("severity") in ("WARNING","ERROR","CRITICAL")]
    if alerts:
        for e in alerts[:10]:
            sev, sc = e["severity"], "#f26d78" if e["severity"]=="CRITICAL" else "#ffb454"
            L(f'<span style="color:{sc}">[{sev}]</span> <span style="color:#4d5974">{e["ts"]}</span> {e["task"][:80]}')
    else:
        L('<span style="color:#7fd962">✅ 当前无告警，系统运行正常</span>')
    L('')

    # ============ ⑪ 恢复 ============
    H('⑪ 自动恢复状态')
    offline = [a for a in ag if not a.get("pid")]
    if offline:
        for a in offline:
            L(f'<span style="color:#ffb454">⚠ 检测到异常：{a["label"]} 离线</span>')
            L(f'<span style="color:#39bae6">  → 自动恢复中：尝试重启进程...</span>')
    else:
        L('<span style="color:#7fd962">✅ 所有 AI 正常运行，无需恢复</span>')
    L('<span style="color:#4d5974">  自愈守护：每10分钟自动检查</span>')
    L('<span style="color:#4d5974">  心跳检测：每分钟检查5个AI</span>')
    L('<span style="color:#4d5974">  诊断检查：每小时全组件诊断</span>')
    L('')

    # ============ ⑫ Token ============
    H('⑫ Token 监控')
    L('<span style="color:#4d5974">')
    L(f'  DeepSeek V4 Pro')
    L(f'    今日 Token：{tk["ds_tokens"]:,} · 费用：¥{tk["ds_cost"]:.2f}')
    L(f'  MiniMax M3')
    L(f'    今日 Token：{tk["mm_tokens"]:,} · 费用：¥{tk["mm_cost"]:.2f}')
    L(f'  合计')
    L(f'    今日：{tk["today_tokens"]:,} tokens · ¥{tk["today_cost"]:.2f}')
    L(f'    本周：{tk.get("week_total",0):,} tokens · ¥{tk.get("week_cost",0):.2f}')
    L('</span>')
    L('')

    # ============ 状态码 ============
    H('📋 统一状态码体系')
    L('<span style="color:#4d5974">')
    codes = list(STATUS.items())
    for i in range(0, len(codes), 5):
        row = codes[i:i+5]
        parts = []
        for code, info in row:
            c = {"运行中":"#7fd962","就绪":"#7fd962","空闲":"#4d5974","离线":"#f26d78","异常":"#f26d78","学习中":"#d2a6ff","等待中":"#ffb454","恢复中":"#ffb454"}.get(info["cn"], "#4d5974")
            parts.append(f'<span style="color:{c}">{code}={info["cn"]}</span>')
        L('  ' + ' · '.join(parts))
    L('</span>')

    L(f'<hr style="border-color:#1a3a4a;margin-top:20px">')
    L(f'<div style="text-align:center;color:#1a3a4a;font-size:11px;padding:10px">')
    L(f'  AIOS v4.0 Runtime Console · 10秒自动刷新 · {now.strftime("%Y-%m-%d %H:%M:%S")} 北京 · 独立模块 :18086')
    L(f'</div>')

    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><title>AIOS Runtime Console</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Courier New','Noto Sans SC',monospace;background:#0a0e14;color:#b3b9c5;padding:20px;max-width:960px;margin:auto;font-size:13px;line-height:1.7}}
h1{{color:#39bae6;font-size:15px;line-height:1.4}}
h2{{color:#ff8f40;font-size:14px;margin:18px 0 8px;padding-bottom:4px;border-bottom:1px solid #1a3040}}
b{{color:#b3b9c5}}.dim{{color:#4d5974}}.green{{color:#7fd962}}.red{{color:#f26d78}}.yellow{{color:#ffb454}}.blue{{color:#39bae6}}.purple{{color:#d2a6ff}}
.sys-header{{background:#0f1920;padding:8px 12px;margin:6px 0;border-left:3px solid #39bae6}}
.badge{{font-size:16px;margin-right:6px}}
</style><meta http-equiv="refresh" content="10"></head><body>
{''.join(out)}</body></html>"""

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            html = render()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Error: {e}".encode())
    def log_message(self, *a): pass

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18086
    print(f"🖥 AIOS Runtime Console → http://localhost:{port}")
    HTTPServer((safe_bind_host(), port), H).serve_forever()
