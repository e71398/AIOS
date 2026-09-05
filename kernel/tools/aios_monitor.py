import sys
sys.path.insert(0, "${AIOS_HOME}/kernel/tools")
from aios_secure import safe_bind_host, cors_origin
#!/usr/bin/env python3
"""AIOS Monitor — 全功能 AI 监控管理中心 (AIOS 内核模块)"""
import json, sys, os, time, subprocess, threading, re, socket, urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
from socketserver import ThreadingMixIn
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, unquote
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import get_queue_status, get_queue_details, check_recent, init_registry
from aios_observability import get_observability_summary

# ─── 动态 Agent 注册 (不再硬编码) ───
# 新增Agent只需在 module_registry.register_module() 中注册即可
# 下面的字典作为默认值, registry中有则以registry为准

AGENT_DEFAULTS = {
    "hermes":    {"label":"Hermes","model":"MiniMax-M3","provider":"MiniMax","icon":"⚡"},
    "openclaw":  {"label":"OpenClaw","model":"MiniMax-M3","provider":"MiniMax","icon":"⚡"},
    "claude_code":{"label":"Claude Code","model":"DeepSeek V4","provider":"DeepSeek","icon":"🧠"},
    "codex_relay":{"label":"Codex","model":"DeepSeek V4","provider":"DeepSeek","icon":"🧠"},
    "opencode":  {"label":"OpenCode","model":"免费模型","provider":"Free","icon":"🆓"},
    "claude":    {"label":"Claude Code","model":"DeepSeek V4","provider":"DeepSeek","icon":"🧠"},
    "codex":     {"label":"Codex","model":"DeepSeek V4","provider":"DeepSeek","icon":"🧠"},
}

def _adapter_metadata():
    try:
        from aios_tool_adapter import load_adapters
        return {name: {"label": a.config.get("label", name),
                       "model": a.config.get("model", "unknown"),
                       "provider": a.config.get("provider", "unknown"),
                       "icon": a.config.get("icon", "🔧")}
                for name, a in load_adapters().items()}
    except Exception:
        return {}

AGENT_DEFAULTS.update(_adapter_metadata())

def _get_agent_names():
    """动态发现所有注册的AI agent."""
    try:
        from aios_module_registry import list_modules
        configured = list(_adapter_metadata())
        mods = list_modules()
        agent_types = ("ai_service","execution","evolution","orchestration")  # 只取AI自身
        agents = [m["name"] for m in mods if m.get("type") in agent_types]
        # 映射: registry名→agent名 (claude_code→claude, codex_relay→codex)
        name_map = {"claude_code":"claude","codex_relay":"codex","openclaw_gateway":"openclaw",
                     "hermes_llama":"hermes","aios_executor":"opencode"}
        result = list(configured)
        seen = set(result)
        for a in agents:
            mapped = name_map.get(a, a)
            if mapped not in seen:
                result.append(mapped)
                seen.add(mapped)
        # All future tool chips come from the adapter registry; no core edit needed.
        return result
    except Exception:
        return ["hermes","openclaw","claude","codex","opencode"]

def _get_agent_label(name):
    return AGENT_DEFAULTS.get(name, {}).get("label", name)

def _get_agent_model(name):
    return AGENT_DEFAULTS.get(name, {}).get("model", "?")

def _get_agent_icon(name):
    return AGENT_DEFAULTS.get(name, {}).get("icon", "🔧")

# 旧引用兼容
AGENT_NAMES = _get_agent_names()
AGENT_LABEL = {n: AGENT_DEFAULTS.get(n,{}).get("label",n) for n in AGENT_NAMES}
AGENT_MODEL = {n: AGENT_DEFAULTS.get(n,{}).get("model","?") for n in AGENT_NAMES}
AGENT_PROVIDER = {n: AGENT_DEFAULTS.get(n,{}).get("provider","?") for n in AGENT_NAMES}
AGENT_ICON = {n: AGENT_DEFAULTS.get(n,{}).get("icon","🔧") for n in AGENT_NAMES}
def _agent_units():
    try:
        data = json.loads((Path("${AIOS_HOME}/config/tool_lifecycle.json")).read_text(encoding="utf-8"))
        return {name: cfg.get("service", "") for name, cfg in data.get("tools", {}).items()}
    except Exception:
        return {}

# 统一状态机，配置驱动；新增工具不修改控制面代码。
AGENT_UNITS = _agent_units()
CORE_WORKLOAD_UNITS = [
    "aios-entry-gateway.service", "aios-web.service", "aios-event-daemon.service",
    "aios-enforcer-daemon.service",
    "aios-verification-gate.service", "aios-result-push.service",
    "aios-runtime.service", "aios-intel.service", "aios-model-gateway.service",
    "aios-api-proxy.service", "aios-feishu-entry.service",
]
WORKLOAD_UNITS = list(dict.fromkeys(CORE_WORKLOAD_UNITS + [u for u in AGENT_UNITS.values() if u]))


def _systemctl(action: str, *units: str, timeout: int = 30):
    if action not in ("start", "stop", "restart"):
        raise ValueError("unsupported systemd action")
    return subprocess.run(["systemctl", "--user", action, *units],
                          capture_output=True, text=True, timeout=timeout)


def _unit_active(unit: str) -> bool:
    result = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit],
                            timeout=5)
    return result.returncode == 0


AGENT_STATUS_MAP = {
    "offline": "Offline", "starting": "Starting", "idle": "Idle",
    "planning": "Planning", "thinking": "Thinking", "tool_call": "Tool Calling",
    "coding": "Coding", "testing": "Testing", "waiting": "Waiting",
    "completed": "Completed", "failed": "Failed", "degraded": "Degraded",
}
AGENT_STATUS_COLOR = {
    "offline":"red","starting":"yellow","idle":"dim","planning":"blue",
    "thinking":"yellow","tool_call":"accent","coding":"green","testing":"purple",
    "waiting":"dim","completed":"green","failed":"red","degraded":"yellow",
}
AGENT_STATUS_DOT = {
    "offline":"🔴","starting":"🟡","idle":"⚪","planning":"🔵",
    "thinking":"🟡","tool_call":"🔷","coding":"🟢","testing":"🟣",
    "waiting":"⚪","completed":"🟢","failed":"🔴","degraded":"🟡",
}

# ─── HTML (10页单页应用) ───
HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIOS Control Center</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;--fg:#c9d1d9;--dim:#8b949e;--accent:#58a6ff;--orange:#f0883e;--green:#3fb950;--red:#f85149;--yellow:#d2991d;--purple:#bc8cff}
body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--fg);display:flex;min-height:100vh}
/* ── 侧边栏 ── */
.sidebar{width:210px;background:var(--bg2);border-right:1px solid var(--border);padding:16px 0;flex-shrink:0;overflow-y:auto}
.sidebar h1{font-size:15px;color:var(--accent);padding:0 16px 14px;border-bottom:1px solid var(--border);margin-bottom:4px}
.sidebar h1 small{display:block;font-size:10px;color:var(--dim);font-weight:400;margin-top:2px}
.nav-item{padding:8px 16px;cursor:pointer;display:flex;align-items:center;gap:8px;font-size:13px;color:var(--dim);transition:.1s;border-left:3px solid transparent}
.nav-item:hover{background:var(--bg3);color:var(--fg)}
.nav-item.active{color:var(--fg);background:var(--bg3);border-left-color:var(--accent);font-weight:600}
.nav-item .ico{font-size:15px;width:18px;text-align:center}
/* ── 主区域 ── */
.main{flex:1;padding:20px;overflow-y:auto;min-width:0}
.page{display:none}.page.active{display:block}
.page h2{font-size:17px;color:var(--orange);margin-bottom:12px;display:flex;align-items:center;gap:8px}
.page h2 .sub{font-size:12px;font-weight:400;color:var(--dim)}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:14px}
.card-title{font-size:12px;color:var(--dim);margin-bottom:8px;text-transform:uppercase;letter-spacing:.3px}
/* ── 网格 ── */
.grid{display:grid;gap:12px}.g4{grid-template-columns:repeat(4,1fr)}.g3{grid-template-columns:repeat(3,1fr)}.g2{grid-template-columns:repeat(2,1fr)}
@media(max-width:900px){.g4,.g3{grid-template-columns:repeat(2,1fr)}}
.statbox{text-align:center;padding:12px}
.stat-val{font-size:22px;font-weight:700}
.stat-label{font-size:11px;color:var(--dim);margin-top:2px}
/* ── 表格 ── */
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{border:1px solid var(--border);padding:7px 10px;text-align:left}
th{background:var(--bg3);color:var(--orange);font-weight:600;font-size:12px}
tr:hover td{background:rgba(255,255,255,.02)}
/* ── 徽标 ── */
.bdg{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.bdg-g{background:#1a3a2a;color:var(--green)}.bdg-r{background:#3a1a1a;color:var(--red)}
.bdg-y{background:#3a2a1a;color:var(--yellow)}.bdg-b{background:#1a2a3a;color:var(--accent)}
.bdg-d{background:var(--bg3);color:var(--dim)}.bdg-p{background:#2a1a3a;color:var(--purple)}
/* ── 进度条 ── */
.pbar{height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;margin:4px 0}
.pfill{height:100%;border-radius:3px;transition:width .5s}
/* ── 工具 ── */
.flex{display:flex}.jcsb{justify-content:space-between}.aic{align-items:center}
.gap{gap:8px}.gap2{gap:16px}.mt{margin-top:8px}.mt2{margin-top:16px}
.tc{text-align:center}.tr{text-align:right}
input,select,textarea{background:var(--bg);border:1px solid var(--border);color:var(--fg);padding:5px 10px;border-radius:4px;font-size:13px}
input:focus{border-color:var(--accent);outline:none}
.btn{background:var(--accent);color:#fff;border:none;padding:5px 14px;border-radius:4px;cursor:pointer;font-size:12px}.btn:hover{opacity:.85}
.btn-r{background:var(--red)}.btn-g{background:var(--green)}
.upd{font-size:11px;color:var(--dim)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
/* ── Timeline ── */
.tl-item{padding:10px 0;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:start}
.tl-time{min-width:52px;font-size:11px;color:var(--dim)}
.tl-agent{font-weight:600;min-width:80px;font-size:13px}
.tl-msg{color:var(--dim);font-size:13px;flex:1}
/* ── 响应式 ── */
@media(max-width:700px){.sidebar{width:56px}.sidebar h1 span{display:none}.nav-item span.nav-lbl{display:none}.g4,.g3{grid-template-columns:repeat(2,1fr)}}
</style></head><body>
<div class="sidebar">
<h1>📊 AIOS Control Center <small>总控中心 · 10页</small></h1>
<div class="ctrl-panel" style="padding:8px 12px;border-bottom:1px solid var(--border);margin-bottom:4px">
  <div style="font-size:10px;color:var(--dim);margin-bottom:4px">⚙ 系统控制</div>
  <div style="display:flex;gap:4px;flex-wrap:wrap">
    <button onclick="sysAction('start')" class="btn btn-g" style="font-size:10px;padding:3px 8px">▶ 启动</button>
    <button onclick="sysAction('stop')" class="btn btn-r" style="font-size:10px;padding:3px 8px">⏹ 停止</button>
    <button onclick="sysAction('reset')" class="btn" style="font-size:10px;padding:3px 8px;background:#d2991d">↺ 复位</button>
  </div>
  <div id="sys-msg" style="font-size:10px;color:var(--green);margin-top:3px;min-height:14px"></div>
</div>
<div class="nav-item active" data-p="overview"><span class="ico">📊</span><span class="nav-lbl">总览</span></div>
<div class="nav-item" data-p="agents"><span class="ico">🤖</span><span class="nav-lbl">AI 监控</span></div>
<div class="nav-item" data-p="models"><span class="ico">🧠</span><span class="nav-lbl">模型监控</span></div>
<div class="nav-item" data-p="tasks"><span class="ico">📋</span><span class="nav-lbl">任务中心</span></div>
<div class="nav-item" data-p="tokens"><span class="ico">💰</span><span class="nav-lbl">Token</span></div>
<div class="nav-item" data-p="costs"><span class="ico">💳</span><span class="nav-lbl">费用</span></div>
<div class="nav-item" data-p="timeline"><span class="ico">📜</span><span class="nav-lbl">动态</span></div>
<div class="nav-item" data-p="traces"><span class="ico">🔍</span><span class="nav-lbl">链路</span></div>
<div class="nav-item" data-p="alerts"><span class="ico">⚠️</span><span class="nav-lbl">告警</span></div>
<div class="nav-item" data-p="settings"><span class="ico">⚙️</span><span class="nav-lbl">设置</span></div>
<div class="nav-item" data-p="taskconsole" style="color:var(--green)"><span class="ico">🚀</span><span class="nav-lbl">提交任务</span></div>
<div class="nav-item" data-p="intel" style="color:var(--accent)"><span class="ico">🛰</span><span class="nav-lbl">情报中心</span></div>
<div class="nav-item" data-p="runtime" style="color:var(--accent)"><span class="ico">🖥</span><span class="nav-lbl">Runtime</span></div>
</div>

<div class="main" id="main">
<p class="upd tr" id="clock">--</p>

<!-- ===== 1. 总览 ===== -->
<div class="page active" id="pg-overview">
<h2>📊 总览 <span class="upd" id="ov-upd"></span></h2>
<div class="grid g4" id="ov-stats"></div>
<div class="card"><div class="card-title">🤖 AI 运行状态</div><div id="ov-agent-tbl"></div></div>
<div class="grid g2"><div class="card"><div class="card-title">📌 模型分配</div><div id="ov-model-tbl"></div></div>
<div class="card"><div class="card-title">⏱ 最新动态</div><div id="ov-timeline"></div></div></div>
</div>

<!-- ===== 2. AI 监控 ===== -->
<div class="page" id="pg-agents">
<h2>🤖 AI 监控详情</h2><div id="agent-detail"></div>
</div>

<!-- ===== 3. 模型监控 ===== -->
<div class="page" id="pg-models">
<h2>🧠 模型监控</h2>
<div class="grid g2" id="model-cards"></div>
<div class="card"><div class="card-title">📈 7 天趋势</div><div id="model-trend" class="tc dim" style="padding:20px">等待数据...</div></div>
</div>

<!-- ===== 4. 任务中心 ===== -->
<div class="page" id="pg-tasks">
<h2>📋 任务中心</h2>
<div class="grid g4" id="task-stats"></div>
<div class="card"><div class="card-title">📋 任务列表</div><div id="task-list"></div></div>
<div class="card"><div class="card-title">⏳ 待处理</div><div id="task-pending"></div></div>
</div>

<!-- ===== 5. Token ===== -->
<div class="page" id="pg-tokens">
<h2>💰 Token 统计</h2>
<div class="grid g4" id="tk-stats"></div>
<div class="card"><div class="card-title">📊 逐日趋势</div><div id="tk-trend" class="tc dim" style="padding:20px">等待数据...</div></div>
<div class="card"><div class="card-title">📋 按 AI 拆分</div><div id="tk-by-agent"></div></div>
</div>

<!-- ===== 6. 费用 ===== -->
<div class="page" id="pg-costs">
<h2>💳 费用统计</h2>
<div class="grid g4" id="cost-stats"></div>
<div class="card"><div class="card-title">📊 费用分布(按 AI)</div><div id="cost-by-agent"></div></div>
<div class="card"><div class="card-title">📋 按模型</div><div id="cost-by-model"></div></div>
</div>

<!-- ===== 7. 动态 ===== -->
<div class="page" id="pg-timeline">
<h2>📜 最新动态 <span class="upd" id="tl-upd"></span></h2>
<div class="card" id="tl-body" style="max-height:70vh;overflow-y:auto"></div>
</div>

<!-- ===== 8. 链路 ===== -->
<div class="page" id="pg-traces">
<h2>🔍 调用链路</h2>
<div class="card"><div class="card-title">最近请求</div><div id="trace-body"></div></div>
</div>

<!-- ===== 9. 告警 ===== -->
<div class="page" id="pg-alerts">
<h2>⚠️ 告警中心</h2>
<div class="grid g3" id="alert-cards"></div>
<div class="card"><div class="card-title">告警列表</div><div id="alert-list"></div></div>
</div>

<!-- ===== 10. 设置 ===== -->
<div class="page" id="pg-settings">
<h2>⚙️ 设置</h2>
<div class="card"><div class="card-title">AI 配置</div><div id="set-ai-cfg"></div></div>
<div class="card"><div class="card-title">数据管理</div><div class="flex gap" style="flex-wrap:wrap">
<button class="btn" onclick="exportCSV()">📥 CSV</button>
<button class="btn" onclick="exportJSON()">📥 JSON</button>
<button class="btn btn-r" onclick="resetToday()">🗑️ 清零今日</button>
</div></div>
</div>

<!-- ===== 11. 任务提交 (8080嵌入) ===== -->
<div class="page" id="pg-taskconsole">
<h2>🚀 任务提交 <span class="sub">— AIOS Task Console</span></h2>
<div class="card" style="padding:0;overflow:hidden">
<iframe src="http://localhost:8080" style="width:100%;height:600px;border:none"></iframe>
</div>
<div class="dim tc mt" style="font-size:11px">或直接访问 <a href="http://localhost:8080" target="_blank" style="color:var(--accent)">http://localhost:8080</a></div>
</div>

<!-- ===== 12. Runtime Console ===== -->
<div class="page" id="pg-runtime">
<h2>🖥 AIOS Runtime Console <span class="sub">— 实时运行状态</span></h2>
<div id="runtime-body" style="height:80vh"><iframe src="http://localhost:18086" style="width:100%;height:100%;border:none"></iframe></div>
</div>

<!-- ===== 13. Intelligence Center ===== -->
<div class="page" id="pg-intel">
<h2>🛰 Intelligence & Growth Center <span class="sub">— 开源/情报/机会</span></h2>
<div style="height:80vh"><iframe src="http://localhost:8848" style="width:100%;height:100%;border:none"></iframe></div>
</div>
</div>

<script>
// ── 导航 ──
document.querySelectorAll('.nav-item').forEach(el=>{
  el.addEventListener('click',()=>{
    document.querySelectorAll('.nav-item,.page').forEach(x=>x.classList.remove('active'));
    el.classList.add('active');
    document.getElementById('pg-'+el.dataset.p).classList.add('active');
    if(dc.agents) renderAll(dc);
  });
});
const MAIN_AGENTS=['hermes','openclaw','claude','codex','opencode'];
const AG={'hermes':'Hermes','openclaw':'OpenClaw','claude':'Claude Code','codex':'Codex','opencode':'OpenCode'};
const AM={'hermes':'⚡ MiniMax','openclaw':'⚡ MiniMax','claude':'🧠 DeepSeek','codex':'🧠 DeepSeek','opencode':'🆓 Free'};
function agentLabel(n){return AG[n]||n.charAt(0).toUpperCase()+n.slice(1)}
function agentModel(n){return AM[n]||'❓'}
const ST={
  offline:['bdg-r','🔴'],starting:['bdg-y','🟡'],idle:['bdg-d','⚪'],
  planning:['bdg-b','🔵'],thinking:['bdg-y','🟡'],tool_call:['bdg-b','🔷'],
  coding:['bdg-g','🟢'],testing:['bdg-p','🟣'],waiting:['bdg-d','⏳'],
  completed:['bdg-g','✅'],failed:['bdg-r','❌'],degraded:['bdg-y','🟡'],
};
function statusHtml(s){const t=ST[s]||ST.offline;return`<span class="bdg ${t[0]}">${t[1]} ${s}</span>`}
function nf(v){return(v||0).toLocaleString()}
function cf(v){const c=(v||0)*7.2;return c<0.01?'¥0':`¥${c.toFixed(2)}`}
function bar(pct,color){return`<div class="pbar"><div class="pfill" style="width:${Math.min(pct,100)}%;background:${color||'var(--accent)'}"></div></div>`}
const dc={};let tid;

// ── SSE ──
const es=new EventSource('/sse');
es.onmessage=function(e){
  try{
    const d=JSON.parse(e.data);
    Object.assign(dc,d);
    renderAll(d);
  }catch(e){}
};
es.onerror=()=>{
  const clk=document.getElementById('clock');
  if(clk)clk.textContent+=' ⚠️ SSE断开, 轮询恢复中';
  es.close();
};
// fallback: 每30秒不走SSE就轮询
let lastSseUpdate=Date.now();
setInterval(()=>{
  if(Date.now()-lastSseUpdate>30000){
    fetch('/api/export?fmt=json').then(r=>r.json()).then(d=>{
      Object.assign(dc,d); renderAll(d); lastSseUpdate=Date.now();
    }).catch(()=>{});
  }
},5000);
setInterval(()=>{document.getElementById('clock').textContent=new Date().toLocaleString()},1000);

function renderAll(d){
  renderOverview(d);
  renderAgentDetail(d);
  renderModels(d);
  renderTasks(d);
  renderToken(d);
  renderCost(d);
  renderTimeline(d);
  renderTrace(d);
  renderAlert(d);
  renderSettings(d);
  renderRuntime(d);
}

// ═══ 1. 总览 ═══
function renderOverview(d){
  const ag=d.agents||[],tk=d.tokens||{},q=d.queue||{};
  const main=ag.filter(a=>a.type==='main');
  const online=main.filter(a=>a.alive).length;
  const busy=main.filter(a=>a.working).length;
  const total=ag.length;
  const completed = q.completed||0; const failed = q.failed||0;
  const failRate = (completed+failed)>0 ? failed/(completed+failed) : 0;
  const allOk = online>=3 && failRate < 0.2 && failed < 20;
  document.getElementById('ov-stats').innerHTML=
    `<div class="card" style="text-align:center;margin-bottom:14px;padding:8px;font-size:14px;font-weight:bold;color:var(--${allOk?'green':'red'});background:var(--bg3)">
       ${allOk?'🟢 AIOS 系统运行正常':'🔴 系统异常 — 查看告警页'}
     </div>
     <div class="card statbox"><div class="stat-val">${online}/${total}</div><div class="stat-label">🟢 在线</div></div>
     <div class="card statbox"><div class="stat-val">${busy}</div><div class="stat-label">⚡ 运行中</div></div>
     <div class="card statbox"><div class="stat-val">${nf(tk.today_tokens)}</div><div class="stat-label">📄 ${tk.is_today===false?'昨日':'今日'} Token</div></div>
     <div class="card statbox"><div class="stat-val">${cf(tk.today_cost)}</div><div class="stat-label">💰 今日费用</div></div>`;
  let rt='<table><thead><tr><th>AI</th><th>状态</th><th>模型</th><th>活动</th><th>心跳</th><th>今日 Token</th><th>费用</th></tr></thead><tbody>';
  for(const a of main){
    const n=a.name; const st=a.status||(a.alive?'idle':'offline');
    const hb=a.hb_age||999; const hc=hb<30?'green':(hb<120?'yellow':'red');
    const atk=(tk.agent_tokens||{})[n]||0; const aco=(tk.agent_cost||{})[n]||0;
    rt+=`<tr><td><b>${agentLabel(n)}</b></td><td>${statusHtml(st)}</td><td class="dim">${agentModel(n)}</td>
      <td>${a.working?'🔄':'💤'}</td><td style="color:var(--${hc})">${hb}s</td>
      <td class="accent">${nf(atk)}</td><td class="orange">${cf(aco)}</td></tr>`;
  }
  rt+='</tbody></table>';
  document.getElementById('ov-agent-tbl').innerHTML=rt;
  let mt='<table><thead><tr><th>AI</th><th>模型</th><th>提供商</th></tr></thead><tbody>';
  for(const a of main)mt+=`<tr><td><b>${agentLabel(a.name)}</b></td><td>${agentModel(a.name)}</td><td class="dim">${a.name==='opencode'?'—':'MiniMax/DeepSeek'}</td></tr>`;
  mt+='</tbody></table>';
  document.getElementById('ov-model-tbl').innerHTML=mt;
  const evs=(d.alerts||[]).slice(0,6);
  let el='';
  for(const e of evs)el+=`<div class="tl-item" style="padding:6px 0"><span class="tl-time">${e.ts||''}</span><span class="tl-agent">${e.system||'?'}</span><span class="tl-msg">${(e.task||'').slice(0,40)}</span></div>`;
  if(!el)el='<p class="dim tc">暂无</p>';
  document.getElementById('ov-timeline').innerHTML=el;
}

// ═══ 2. AI 监控 ═══
function renderAgentDetail(d){
  const ag=d.agents||[];
  const main=ag.filter(a=>a.type==='main');
  const system=[];
  const libs=ag.filter(a=>!MAIN_AGENTS.includes(a.name) && a.type!=='main' && a.pid===false);
  let h='<h3 style="color:var(--accent);margin:8px 0">🤖 主要 AI</h3>';
  for(const a of main){
    const n=a.name; const st=a.status||(a.alive?'idle':'offline'); const hb=a.hb_age||999;
    h+=`<div class="card">
      <div class="flex jcsb aic"><div><b style="font-size:15px">${agentLabel(n)}</b> ${statusHtml(st)}</div>
      <div class="dim">💓 ${hb}s</div></div>
      <div class="flex gap2 mt" style="flex-wrap:wrap">
        <div><span class="dim">模型</span><br>${a.model||agentModel(n)}<br><span class="dim">${a.model_state||'unverified'}</span></div>
        <div><span class="dim">进程</span><br>${a.pid?'🟢 运行':'🔴 未启'}</div>
        <div><span class="dim">活动</span><br>${a.working?'🔄 执行':'💤 空闲'}</div>
        <div><span class="dim">心跳</span><br><span style="color:var(--${hb<30?'green':(hb<120?'yellow':'red')})">${hb}s</span></div>
      </div>
      <div class="flex gap mt" style="border-top:1px solid var(--border);padding-top:8px">
        <button onclick="agentAction('${n}','start')" class="btn btn-g" style="font-size:10px;padding:2px 8px">▶ 启动</button>
        <button onclick="agentAction('${n}','stop')" class="btn btn-r" style="font-size:10px;padding:2px 8px">⏹ 停止</button>
        <button onclick="agentAction('${n}','reset')" class="btn" style="font-size:10px;padding:2px 8px;background:#d2991d">↺ 复位</button>
        <button onclick="agentAction('${n}','terminal')" class="btn" style="font-size:10px;padding:2px 8px;background:#1a3a5a">🖥 终端</button>
        <span id="agent-msg-${n}" style="font-size:10px;margin-left:8px"></span>
      </div>
    </div>`;
  }
  if(system.length){
    h+='<h3 style="color:var(--dim);margin:12px 0 8px">🔧 系统 AI <span class="upd">('+system.length+' 个)</span></h3>';
    for(const a of system){
      const n=a.name; const st=a.status||(a.alive?'idle':'offline'); const hb=a.hb_age||999;
      h+=`<div class="card" style="opacity:.75">
        <div class="flex jcsb aic"><div><b style="font-size:14px">${agentLabel(n)}</b> ${statusHtml(st)}</div>
        <div class="dim">💓 ${hb}s</div></div>
        <div class="flex gap2 mt" style="flex-wrap:wrap">
          <div><span class="dim">类型</span><br>${a.type||'system'}</div>
          <div><span class="dim">进程</span><br>${a.pid?'🟢 运行':'🔴 未启'}</div>
          <div><span class="dim">活动</span><br>${a.working?'🔄':'💤'}</div>
          <div><span class="dim">心跳</span><br><span style="color:var(--${hb<30?'green':(hb<120?'yellow':'red')})">${hb}s</span></div>
        </div>
      </div>`;
    }
  }
  document.getElementById('agent-detail').innerHTML=h;
}

// ═══ 3. 模型监控 ═══
function renderModels(d){
  const tk=d.tokens||{};
  const mk=tk.mm_tokens||0,dk=tk.ds_tokens||0;
  document.getElementById('model-cards').innerHTML=
     `<div class="card"><b style="font-size:14px">⚡ MiniMax</b>
       <div class="flex jcsb mt"><span class="dim">调用</span><span>${nf(tk.mm_calls)}</span></div>
       <div class="flex jcsb"><span class="dim">Token</span><span class="accent">${nf(mk)}</span></div>
       <div class="flex jcsb"><span class="dim">费用</span><span class="orange">${cf(tk.mm_cost)}</span></div>
       <div class="flex jcsb"><span class="dim">状态</span><span class="bdg bdg-g">✅ 正常</span></div></div>
     <div class="card"><b style="font-size:14px">🧠 DeepSeek</b>
       <div class="flex jcsb mt"><span class="dim">调用</span><span>${nf(tk.ds_calls)}</span></div>
       <div class="flex jcsb"><span class="dim">Token</span><span class="accent">${nf(dk)}</span></div>
       <div class="flex jcsb"><span class="dim">费用</span><span class="orange">${cf(tk.ds_cost)}</span></div>
       <div class="flex jcsb"><span class="dim">状态</span><span class="bdg bdg-g">✅ 正常</span></div></div>`;
   if(tk.days&&tk.days.length){
     const mx=Math.max(...tk.days.map(d=>d.total_tokens),1);
     let c='<div style="display:flex;gap:2px;align-items:end;height:80px;padding:10px 0">';
     for(const d of tk.days){
       const p=Math.round(d.total_tokens/mx*100),h=Math.max(4,Math.round(p*0.7));
      c+=`<div style="flex:1;text-align:center;font-size:10px"><div style="height:${80-h}px"></div><div style="height:${h}px;background:var(--accent);border-radius:2px;margin:0 1px" title="${d.date}: ${nf(d.tokens)}"></div><div class="dim">${d.date.slice(4)}</div></div>`;
    }
    c+='</div>';
    document.getElementById('model-trend').innerHTML=c;
  }
}

// ═══ 4. 任务中心 ═══
function renderTasks(d){
  const q=d.queue||{};
  document.getElementById('task-stats').innerHTML=
    `<div class="card statbox"><div class="stat-val blue">${q.pending||0}</div><div class="stat-label">⏳ 待处理</div></div>
     <div class="card statbox"><div class="stat-val accent">${q.running||0}</div><div class="stat-label">🔄 运行中</div></div>
     <div class="card statbox"><div class="stat-val green">${q.completed||0}</div><div class="stat-label">✅ 已完成</div></div>
     <div class="card statbox"><div class="stat-val red">${q.failed||0}</div><div class="stat-label">❌ 失败</div></div>`;
  const qd=d.queue_details||[];
  if(!qd.length){document.getElementById('task-list').innerHTML='<p class="dim tc">暂无任务</p>';return}
  let tbl='<table><thead><tr><th>任务</th><th>来源</th><th>状态</th><th>执行器</th></tr></thead><tbody>';
  for(const t of qd.slice(0,15)){
    const sc=t.status==='failed'?'red':(t.status==='running'?'green':(t.status==='pending'?'yellow':'dim'));
    tbl+=`<tr><td class="dim">${(t.name||t.task_id||'?').slice(0,35)}</td><td class="dim">${t.source||'?'}</td><td style="color:var(--${sc})">${t.status}</td><td class="dim">${t.executor||'-'}</td></tr>`;
  }
  tbl+='</tbody></table>';
  document.getElementById('task-list').innerHTML=tbl;
  const pd=qd.filter(t=>t.status==='pending');
  if(!pd.length){document.getElementById('task-pending').innerHTML='<p class="dim tc">无待处理任务</p>';return}
  let phtml='';
  for(const t of pd.slice(0,10))phtml+=`<div class="tl-item" style="padding:6px 0"><span class="tl-time">⏳</span><span class="tl-agent">${t.source||'?'}</span><span class="tl-msg">${(t.name||'?').slice(0,45)}</span></div>`;
  document.getElementById('task-pending').innerHTML=phtml;
}

// ═══ 5. Token ═══
function renderToken(d){
  const tk=d.tokens||{};
  const tot=tk.today_tokens||0;
  const pt=tk.prompt_tokens||0, ct=tk.completion_tokens||0;
  document.getElementById('tk-stats').innerHTML=
    `<div class="card statbox"><div class="stat-val accent">${nf(pt)}</div><div class="stat-label">📝 Prompt</div></div>
     <div class="card statbox"><div class="stat-val accent">${nf(ct)}</div><div class="stat-label">✅ Completion</div></div>
     <div class="card statbox"><div class="stat-val accent">${nf(tot)}</div><div class="stat-label">📄 今日 Total</div></div>
     <div class="card statbox"><div class="stat-val accent">${nf((tk.week_total||0)*4)}</div><div class="stat-label">📅 月预估</div></div>`;
  if(tk.days&&tk.days.length){
     const mx=Math.max(...tk.days.map(d=>d.total_tokens),1);
     let c='<div style="display:flex;gap:2px;align-items:end;height:100px;padding:10px 0">';
     for(const d of tk.days){
       const p=Math.round(d.total_tokens/mx*100),h=Math.max(4,Math.round(p*0.9));
      c+=`<div style="flex:1;text-align:center;font-size:10px"><div style="height:${100-h}px"></div><div style="height:${h}px;background:var(--accent);border-radius:2px" title="${d.date}: ${nf(d.tokens)}"></div><div class="dim">${d.date.slice(4)}</div></div>`;
    }
    c+='</div>';
    document.getElementById('tk-trend').innerHTML=c;
  }
  let tbl='<table><thead><tr><th>AI</th><th>Tokens</th><th>占比</th></tr></thead><tbody>';
  for(const n of MAIN_AGENTS){
    const atk=(tk.agent_tokens||{})[n]||0;
    const pct=tot>0?(atk/tot*100).toFixed(1):'0.0';
    tbl+=`<tr><td><b>${agentLabel(n)}</b></td><td class="accent">${nf(atk)}</td><td>${bar(pct,'var(--accent)')} ${pct}%</td></tr>`;
  }
  tbl+='</tbody></table>';
  document.getElementById('tk-by-agent').innerHTML=tbl;
}

// ═══ 6. 费用 ═══
function renderCost(d){
  const tk=d.tokens||{};
  const tdc=tk.today_cost||0,wc=tk.week_cost||0;
  document.getElementById('cost-stats').innerHTML=
    `<div class="card statbox"><div class="stat-val orange">${cf(tdc)}</div><div class="stat-label">今日</div></div>
     <div class="card statbox"><div class="stat-val orange">${cf(wc)}</div><div class="stat-label">本周</div></div>
     <div class="card statbox"><div class="stat-val orange">${cf(wc*4)}</div><div class="stat-label">月预估</div></div>
     <div class="card statbox"><div class="stat-val orange">${cf(tk.mm_cost+tk.ds_cost)}</div><div class="stat-label">按模型</div></div>`;
  const ac=tk.agent_cost||{};
  const total=Object.values(ac).reduce((a,b)=>a+b,0)||1;
  let tbl='<table><thead><tr><th>AI</th><th>费用</th><th>占比</th></tr></thead><tbody>';
  for(const n of MAIN_AGENTS){
    const c=ac[n]||0;
    const p=(c/total*100).toFixed(1);
    tbl+=`<tr><td><b>${agentLabel(n)}</b></td><td class="orange">${cf(c)}</td><td>${bar(p,'var(--orange)')} ${p}%</td></tr>`;
  }
  tbl+='</tbody></table>';
  document.getElementById('cost-by-agent').innerHTML=tbl;
  let mt='<table><thead><tr><th>模型</th><th>Token</th><th>费用</th></tr></thead><tbody>';
  mt+=`<tr><td>⚡ MiniMax</td><td class="accent">${nf(tk.mm_tokens)}</td><td class="orange">${cf(tk.mm_cost)}</td></tr>`;
  mt+=`<tr><td>🧠 DeepSeek</td><td class="accent">${nf(tk.ds_tokens)}</td><td class="orange">${cf(tk.ds_cost)}</td></tr>`;
  mt+='</tbody></table>';
  document.getElementById('cost-by-model').innerHTML=mt;
}

// ═══ 7. 动态 ═══
function renderTimeline(d){
  const evs=d.timeline||d.alerts||[];
  let h='';
  for(const e of evs.slice(0,40)){
    const ic={completed:'✅',failed:'❌',running:'🔄',pending:'⏳'};
    h+=`<div class="tl-item"><span class="tl-time">${e.ts||''}</span><span class="tl-agent">${e.system||'?'}</span><span class="tl-msg">${ic[e.status]||'❓'} ${(e.task||'').slice(0,60)}</span></div>`;
  }
  if(!h)h='<p class="dim tc">等待事件...</p>';
  document.getElementById('tl-body').innerHTML=h;
}

function renderTrace(d){
  const evs=(d.traces||d.alerts||[]).filter(e=>e.task).slice(0,20);
  let h='<table><thead><tr><th>时间</th><th>来源</th><th>事件</th></tr></thead><tbody>';
  for(const e of evs)h+=`<tr><td class="dim">${e.ts||''}</td><td>${e.system||'?'}</td><td class="dim">${(e.task||'').slice(0,60)}</td></tr>`;
  if(evs.length===0)h='<p class="dim tc">等待链路数据...</p>';
  else h+='</tbody></table>';
  document.getElementById('trace-body').innerHTML=h;
}

// ═══ 9. 告警 ═══
function renderAlert(d){
    // 告警: 只取alert事件或异常
    const alertEvts = (d.alerts||[]).filter(e=>e.status==='failed'||(e.type||'').startsWith('alert'));
    // 2. 动态: 全部事件
    const timelineEvts = d.timeline||d.alerts||[];
    // 3. 链路: 只取任务相关
    const traceEvts = (d.traces||[]).filter(e=>e.task&&!e.task.startsWith('agent.'));

    const qAlert = d.queue||{};
    document.getElementById('alert-cards').innerHTML=
      `<div class="card statbox"><div class="stat-val red">${alertEvts.length}</div><div class="stat-label">❌ 今日失败</div></div>
       <div class="card statbox"><div class="stat-val yellow">${qAlert.pending||0}</div><div class="stat-label">⏳ 待处理</div></div>
       <div class="card statbox"><div class="stat-val ${(qAlert.failed||0)>2?'red':'green'}">${(qAlert.failed||0)>2?'⚠️ 异常':'✅ 正常'}</div><div class="stat-label">📊 队列健康</div></div>`;
    if(!alertEvts.length){document.getElementById('alert-list').innerHTML='<p class="dim tc">✅ 无告警</p>';return}
    let tbl='<table><thead><tr><th>时间</th><th>来源</th><th>状态</th><th>详情</th></tr></thead><tbody>';
    for(const a of alertEvts.slice(0,30)){
      tbl+=`<tr><td class="dim">${a.ts||''}</td><td style="color:var(--accent)">${a.system||'?'}</td><td>❌</td><td class="dim">${(a.task||'').slice(0,60)}</td></tr>`;
    }
    tbl+='</tbody></table>';
    document.getElementById('alert-list').innerHTML=tbl;
  }

// ═══ 10. 设置 ═══
function renderSettings(d){
  const ag=d.agents||[];
  let h='<table><thead><tr><th>AI</th><th>模型</th><th>提供商</th><th>状态</th></tr></thead><tbody>';
  for(const a of ag){
    const n=a.name;
    const st=a.status||'offline';
    h+=`<tr><td><b>${agentLabel(n)}</b></td><td>${agentModel(n)}</td><td class="dim">${n==='opencode'?'—':(a.type==='main'?'LiteLLM':'—')}</td><td>${statusHtml(st)}</td></tr>`;
  }
  h+='</tbody></table>';
  document.getElementById('set-ai-cfg').innerHTML=h;
}

function exportCSV(){window.location.href='/api/export?fmt=csv'}
function exportJSON(){window.location.href='/api/export?fmt=json'}
function agentAction(name,action){
  const labels={start:'启动 '+agentLabel(name)+'？',stop:'停止 '+agentLabel(name)+'？',reset:'复位 '+agentLabel(name)+'？⚠'};
  if(!confirm(labels[action]||'确认 '+action+' '+name+'？'))return;
  const msg=document.getElementById('agent-msg-'+name);
  if(msg){msg.style.color='var(--yellow)';msg.textContent='⏳'}
  fetch('/api/agent/'+action+'/'+name,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(msg){msg.style.color=d.ok?'var(--green)':'var(--red)';msg.textContent=(d.ok?'✅':'❌')+' '+(d.msg||action)}
    setTimeout(()=>{if(msg)msg.textContent='';},3000);
  }).catch(e=>{if(msg){msg.style.color='var(--red)';msg.textContent='❌'}});
}
function sysAction(action){
  const labels={start:'启动 AIOS 业务系统？',stop:'停止 AIOS 业务系统？8086 控制台将保持在线。',reset:'安全复位业务服务？队列和历史不会清空。'};
  if(!confirm(labels[action]||'确认执行 '+action+'？'))return;
  const msg=document.getElementById('sys-msg');
  msg.style.cssText='color:var(--yellow);font-size:12px;font-weight:bold;min-height:20px';
  msg.textContent='⏳ '+action+' 执行中...';
  fetch('/api/system/'+action,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok){
      msg.style.color='var(--green)';
      msg.textContent='✅ '+(d.msg||action+' 完成');
      setTimeout(()=>msg.textContent='',10000);
    }else{
      msg.style.color='var(--red)';
      msg.textContent='❌ '+(d.error||action+' 失败');
      setTimeout(()=>msg.textContent='',10000);
    }
  }).catch(e=>{msg.style.color='var(--red)';msg.textContent='❌ 网络错误';});
}
function resetToday(){
  if(!confirm('确认清零今日统计？历史数据保留'))return;
  fetch('/api/reset-today',{method:'POST'}).then(r=>r.json()).then(d=>alert(d.ok?'✅ 已清零':'❌ 失败'));
}

// ═══ 12. Runtime Console ═══
// 注意: Runtime面板使用iframe嵌入独立服务 :18086, 不做inline渲染
function renderRuntime(d){
  // iframe已存在, 只需确保可见 (无需额外操作)
}
</script></body></html>"""

# ─── HTTP 服务器 ───
class ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

# SEC-PROBE-01: probe endpoint hardening
_PROBE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROBE_LOCKS_META = threading.Lock()
_PROBE_LOCKS: dict[str, threading.Lock] = {}
_PROBE_ALLOWED_FIELDS = ("ok", "tool", "state", "cached", "checked_at")


def _probe_get_lock(name: str) -> threading.Lock:
    with _PROBE_LOCKS_META:
        lock = _PROBE_LOCKS.get(name)
        if lock is None:
            lock = threading.Lock()
            _PROBE_LOCKS[name] = lock
        return lock


def _probe_release_lock(name: str) -> None:
    """Drop lock entry if and only if it is currently unlocked.

    Prevents permanent accumulation of unused tool entries while a probe
    is in flight.
    """
    with _PROBE_LOCKS_META:
        lock = _PROBE_LOCKS.get(name)
        if lock is not None and not lock.locked():
            _PROBE_LOCKS.pop(name, None)


def _probe_safe_summary(adapter_result: dict) -> dict:
    """Project adapter probe result to a strict client-facing whitelist.

    Only fields that are safe to expose are included; secret/path/stderr
    blobs are stripped even if the adapter returned them.
    """
    state = adapter_result.get("model_state") or adapter_result.get("state")
    cached = bool(adapter_result.get("probe_required") is False)
    return {
        "ok": True,
        "tool": adapter_result.get("tool", ""),
        "state": state if isinstance(state, str) else None,
        "cached": cached,
        "checked_at": adapter_result.get("checked_at"),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/sse": return self._sse()
        if p == "/api/export": return self._export()
        if p == "/api/health": return self._json({"ok":True,"service":"aios-monitor"})
        if p == "/api/health/v2":
            try:
                status = get_full_status().get("health", {})
                overall = status.get("overall_status", STATUS_UNKNOWN)
                http_code = 200 if overall in (STATUS_HEALTHY, STATUS_DEGRADED) else 503
                return self._json(status, http_code)
            except Exception as exc:
                return self._json({"ok":False,"error":str(exc)},500)
        if p == "/api/tools/health":
            try:
                from aios_tool_evolution import health_all
                tools = health_all()
                return self._json({"ok": all(x.get("fully_operational") for x in tools.values()),
                                   "infrastructure_ok": all(x.get("infrastructure_ok") for x in tools.values()),
                                   "tools": tools})
            except Exception as exc:
                return self._json({"ok":False,"error":str(exc)},500)
        if p == "/api/modules":
            if not self._check_auth(): return self._json({"ok":False,"error":"unauthorized"},403)
            return self._json(self._get_modules())
        if p == "/api/modules/health": return self._json(self._get_module_health())
        if p == "/api/runtime": return self._json(self._get_runtime())
        if p == "/api/executors": return self._json(self._get_executors())
        if p == "/api/queue": return self._json(self._get_queue())
        if p.startswith("/api/"): return self._json({"error":"not found"},404)
        self._html()

    def _check_auth(self) -> bool:
        """验证 API Key. 控制端点必须有 X-AIOS-Key 头.

        严格语义 (SEC-MONITOR-AUTH-01):
          * auth_required = AIOS_AUTH_REQUIRED ∈ {1, true, yes, on}
                              (大小写不敏感, 空白被 strip)
          * auth_required=False -> dev 模式, 直接放行
          * auth_required=True  -> 仅使用 AIOS_MONITOR_API_KEY
                                    缺失 / 为空 / 为 'aios-internal' 都视为
                                    未配置, 失败关闭
                                    头部缺失 / 不匹配都拒绝
                                    使用 hmac.compare_digest 防时序攻击
          * 不再 fallback 到 AIOS_API_KEY 或任何其他变量
        """
        import os, hmac
        auth_required = (
            os.environ.get("AIOS_AUTH_REQUIRED", "0").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        if not auth_required:
            # dev 模式: 显式未启用鉴权才放行
            return True
        expected = os.environ.get("AIOS_MONITOR_API_KEY", "").strip()
        if not expected or expected == "aios-internal":
            # 鉴权已启用但密钥未配置或为历史不安全占位值 -> 失败关闭
            return False
        provided = self.headers.get("X-AIOS-Key", "")
        if not provided:
            return False
        return hmac.compare_digest(provided, expected)

    def do_POST(self):
        p = urlparse(self.path).path

        if p.startswith("/api/tools/probe/"):
            if not self._check_auth(): return self._json({"ok":False,"error":"unauthorized"},403)
            raw_name = p.split("/")[-1]
            name = unquote(raw_name)
            if not _PROBE_NAME_RE.match(name or ""):
                return self._json({"ok":False,"error":"invalid tool name"},400)
            # Registry membership check before any adapter / probe call.
            # load_adapters() is safe: it only reads the JSON config and
            # instantiates dataclass objects (no subprocess, no probe).
            try:
                from aios_tool_adapter import registered_adapter_names, get_adapter
            except Exception as exc:
                print(f"[monitor-probe] adapter import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                return self._json({"ok":False,"error":"tool registry unavailable"},503)
            try:
                # Lightweight name lookup: does NOT construct ToolAdapter,
                # does NOT read secrets, does NOT run subprocess, does NOT
                # write cache. Unregistered names short-circuit before any
                # adapter object exists.
                names = registered_adapter_names()
            except Exception as exc:
                print(f"[monitor-probe] tool registry unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
                return self._json({"ok":False,"error":"tool registry unavailable"},503)
            if name not in names:
                return self._json({"ok":False,"error":"tool not available"},404)
            lock = _probe_get_lock(name)
            if not lock.acquire(blocking=False):
                return self._json({"ok":False,"error":"probe already running"},409)
            try:
                # Only now, for a registered name, fetch the (single) adapter
                # and run a normal-cached probe.
                adapter = get_adapter(name)
                result = adapter.probe(force=False)
                summary = _probe_safe_summary(result)
                summary["tool"] = name
                return self._json(summary, 200)
            except Exception as exc:
                print(f"[monitor-probe] tool={name} exc={type(exc).__name__}", file=sys.stderr)
                return self._json({"ok":False,"error":"probe failed"},500)
            finally:
                lock.release()
                _probe_release_lock(name)

        if p == "/api/reset-today": return self._reset_today()
        if p == "/api/system/start": return self._system_start()
        if p == "/api/system/stop": return self._system_stop()
        if p == "/api/system/reset": return self._system_reset()
        if p.startswith("/api/system/restart/"):
            agent = p.split("/")[-1]
            return self._restart_single(agent)
        if p.startswith("/api/agent/start/"):
            agent = p.split("/")[-1]
            return self._agent_start(agent)
        if p.startswith("/api/agent/stop/"):
            agent = p.split("/")[-1]
            return self._agent_stop(agent)
        if p.startswith("/api/agent/reset/"):
            agent = p.split("/")[-1]
            return self._agent_reset(agent)
        if p.startswith("/api/agent/terminal/"):
            agent = p.split("/")[-1]
            return self._agent_terminal(agent)
        if p.startswith("/api/kb/feedback/"):
            parts = p.split("/")
            doc_id = int(parts[-2]) if len(parts) > 2 else 0
            action = parts[-1] if len(parts) > 1 else ""
            return self._kb_feedback(doc_id, action)
        self._json({"error":"not found"},404)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type","text/event-stream")
        self.send_header("Cache-Control","no-cache")
        self.send_header("Connection","keep-alive")
        co=cors_origin()
        if co: self.send_header("Access-Control-Allow-Origin", co)
        self.end_headers()
        try:
            import redis as _r
            r = _r.Redis(host='localhost',port=6379,socket_connect_timeout=2)
            ps = r.pubsub(); ps.subscribe("aios:sse:push")
            d = get_full_status()
            self.wfile.write(f"data: {json.dumps(d,ensure_ascii=False)}\n\n".encode()); self.wfile.flush()
            while True:
                m = ps.get_message(timeout=5)
                if m and m["type"]=="message":
                    self.wfile.write(f"data: {m['data'].decode()}\n\n".encode()); self.wfile.flush()
                else:
                    self.wfile.write(": hb\n\n".encode()); self.wfile.flush()
        except Exception: pass

    def _export(self):
        qs = parse_qs(urlparse(self.path).query)
        fmt = qs.get("fmt",["json"])[0]
        data = get_full_status()
        if fmt == "csv":
            main_names = {"hermes","openclaw","claude","codex","opencode"}
            lines = ["AI,Status,Model,Alive,Heartbeat_s,Tokens_Today,Cost_Today"]
            for a in data.get("agents",[]):
                n=a["name"]
                if n not in main_names: continue
                tk=data.get("tokens",{}).get("agent_tokens",{}).get(n,0)
                co=data.get("tokens",{}).get("agent_cost",{}).get(n,0)
                model = AGENT_MODEL.get(n, "?")
                label = AGENT_LABEL.get(n, n)
                st = a.get("status","?")
                al = a.get("alive",0)
                hb = a.get("hb_age",0)
                lines.append(f'{label},{st},{model},{al},{hb},{tk},{co}')
            csv = "\n".join(lines)
            self.send_response(200)
            self.send_header("Content-Type","text/csv; charset=utf-8")
            self.send_header("Content-Disposition",'attachment; filename="aios_monitor_export.csv"')
            self.end_headers()
            self.wfile.write(csv.encode())
        else:
            self._json(data)

    def _reset_today(self):
        try:
            import redis as _r
            r = _r.Redis(host='localhost',port=6379,socket_connect_timeout=1)
            today = datetime.now().strftime('%Y%m%d')
            r.delete(f"aios:bus:governance:daily:{today}")
            self._json({"ok":True})
        except Exception as e:
            self._json({"error":str(e)},500)

    def _get_executors(self):
        try:
            from aios_bus import list_registered_executors, _is_available
            if not _is_available():
                return {"error":"redis unavailable"}
            execs = list_registered_executors()
            stats = {}
            for ex, cfg in execs.items():
                from aios_enforcer import is_executor_halted
                stats[ex] = {"name":ex,"config":cfg,"halted":is_executor_halted(ex)}
            return {"executors":stats}
        except Exception as e:
            return {"error":str(e)}

    def _get_queue(self):
        try:
            from aios_bus import get_queue_status, get_queue_details, _is_available
            if not _is_available():
                return {"error":"redis unavailable"}
            return {"status":get_queue_status(),"details":get_queue_details()}
        except Exception as e:
            return {"error":str(e)}

    def _get_modules(self):
        try:
            from aios_module_registry import list_modules
            return {"modules": list_modules()}
        except Exception as e:
            return {"error": str(e)}

    def _get_module_health(self):
        try:
            from aios_module_registry import health_check_all
            return health_check_all()
        except Exception as e:
            return {"error": str(e)}

    def _get_runtime(self):
        try:
            from aios_runtime_console import get_full_runtime_status
            return get_full_runtime_status()
        except Exception as e:
            return {"error": str(e)}

    def _system_start(self):
        try:
            result = _systemctl("start", "aios-core.target")
            if result.returncode != 0:
                return self._json({"ok":False,"error":result.stderr.strip()},500)
            from aios_bus import publish_event
            publish_event("system.status", {"status":"AIOS workload started"}, "aios_monitor")
            self._json({"ok":True,"action":"start","control_plane":"preserved",
                        "active":_unit_active("aios-core.target")})
        except Exception as e:
            self._json({"error":str(e)},500)

    def _system_stop(self):
        try:
            subprocess.Popen(["bash","${AIOS_HOME}/stop.sh"],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            from aios_bus import publish_event
            publish_event("system.status", {"status":"AIOS workload stopping"}, "aios_monitor")
            self._json({"ok":True,"action":"stop","msg":"workload stopping; 8086 stays online",
                        "control_plane":"preserved"})
        except Exception as e:
            self._json({"error":str(e)},500)

    def _system_reset(self):
        """Safe workload reconciliation; never clears queues or task history."""
        try:
            before = get_queue_status()
            result = _systemctl("restart", *WORKLOAD_UNITS, timeout=60)
            if result.returncode != 0:
                return self._json({"ok":False,"action":"reset",
                                   "error":result.stderr.strip(),"queue":before},500)
            time.sleep(2)
            states = {unit:_unit_active(unit) for unit in WORKLOAD_UNITS}
            after = get_queue_status()
            self._json({"ok":all(states.values()),"action":"reset",
                        "mode":"safe-reconcile","queues_preserved":before == after,
                        "queue_before":before,"queue_after":after,"units":states,
                        "local_model_touched":False,"history_cleared":False})
        except Exception as e:
            self._json({"ok":False,"action":"reset","error":str(e)},500)

    def _doctor_check(self):
        results = []
        try:
            from aios_self_diagnose import run as diagnose_run
            diag = diagnose_run()
            ok_count = diag.get("ok",0) if isinstance(diag,dict) else 0
            fail_count = diag.get("fail",0) if isinstance(diag,dict) else 0
            results.append(f"🔍 doctor: {ok_count}通过/{fail_count}失败")
        except Exception as e:
            results.append(f"⚠️ doctor跳过: {e}")
        return results

    def _restart_dead_agents(self):
        results = []
        for name in AGENT_NAMES:
            pids = _find_agent_pid(name)
            if not pids:
                ok = self._restart_agent(name)
                results.append(f"{'✅' if ok else '❌'} restart {name}")
        if not any("restart" in r for r in results):
            results.append("✅ all agents alive")
        return results

    def _restart_agent(self, name):
        unit = AGENT_UNITS.get(name)
        if not unit:
            return False
        try:
            result = _systemctl("restart", unit)
            return result.returncode == 0 and _unit_active(unit)
        except Exception:
            return False

    def _agent_terminal(self, name):
        if name not in AGENT_NAMES:
            self._json({"error":f"unknown: {name}"},400); return
        terminal_cmds = {
            "opencode":  "opencode",
            "claude":    "claude",
            "codex":     "codex -m deepseek-v4-pro",
            "hermes":    "${HOME}/.hermes/hermes-agent/venv/bin/hermes",
            "openclaw":  "openclaw",
        }
        cmd = terminal_cmds.get(name, "")
        if not cmd:
            self._json({"error":"no terminal cmd"}); return
        try:
            term = os.environ.get("TERMINAL","x-terminal-emulator")
            subprocess.Popen([term, "-e", "bash", "-lc", f"{cmd}; exec bash"], shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._json({"ok":True,"agent":name,"action":"terminal"})
        except Exception:
            try:
                subprocess.Popen(["gnome-terminal","--","bash","-c",f"{cmd}; exec bash"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._json({"ok":True,"agent":name,"action":"terminal","fallback":"gnome-terminal"})
            except Exception as e2:
                self._json({"error":f"no terminal: {e2}"},500)

    def _kb_feedback(self, doc_id: int, action: str):
        try:
            from aios_semantic_search import adjust_score
            if action == "useful":
                score = adjust_score(doc_id, 5, "useful")
                self._json({"ok":True,"doc_id":doc_id,"action":"useful","new_score":score})
            elif action == "useless":
                score = adjust_score(doc_id, -3, "useless")
                self._json({"ok":True,"doc_id":doc_id,"action":"useless","new_score":score})
            else:
                self._json({"error":"unknown action"},400)
        except Exception as e:
            self._json({"error":str(e)},500)

    def _restart_single(self, name):
        if name not in AGENT_NAMES:
            self._json({"error":f"unknown agent: {name}"},400); return
        try:
            ok = self._restart_agent(name)
            self._json({"ok":ok,"agent":name,"action":"restart"})
        except Exception as e:
            self._json({"error":str(e)},500)

    def _agent_start(self, name):
        if name not in AGENT_NAMES:
            self._json({"error":f"unknown agent: {name}"},400); return
        unit = AGENT_UNITS.get(name)
        if not unit:
            return self._json({"ok":False,"error":"no managed unit"},400)
        if _unit_active(unit):
            return self._json({"ok":True,"agent":name,"unit":unit,
                               "action":"start","msg":"already active"})
        result = _systemctl("start", unit)
        self._json({"ok":result.returncode == 0 and _unit_active(unit),
                    "agent":name,"unit":unit,"action":"start",
                    "error":result.stderr.strip()})

    def _agent_stop(self, name):
        if name not in AGENT_NAMES:
            self._json({"error":f"unknown agent: {name}"},400); return
        unit = AGENT_UNITS.get(name)
        if not unit:
            return self._json({"ok":False,"error":"no managed unit"},400)
        result = _systemctl("stop", unit)
        return self._json({"ok":result.returncode == 0 and not _unit_active(unit),
                           "agent":name,"unit":unit,"action":"stop",
                           "error":result.stderr.strip()})
        try:
            for agent_name, pat in []:
                if name == agent_name:
                    pass
            # Legacy branch retained unreachable for compatibility.
            if name in ("claude","codex"):
                self._json({"ok":True,"agent":name,"action":"stop","msg":"claude/codex需手动停止 (独立进程)"})
                return
            self._json({"ok":True,"agent":name,"action":"stop"})
        except Exception as e:
            self._json({"error":str(e)},500)

    def _agent_reset(self, name):
        if name not in AGENT_NAMES:
            self._json({"error":f"unknown agent: {name}"},400); return
        unit = AGENT_UNITS.get(name)
        if not unit:
            return self._json({"ok":False,"error":"no managed unit"},400)
        result = _systemctl("restart", unit)
        return self._json({"ok":result.returncode == 0 and _unit_active(unit),
                           "agent":name,"unit":unit,"action":"reset",
                           "mode":"systemd-restart","state_cleared":False,
                           "local_model_touched":False,"error":result.stderr.strip()})
        steps = []
        try:
            self._agent_stop(name)
            steps.append("✅ stopped")
        except: steps.append("⚠️ stop skipped")
        try:
            import redis as _r
            r = _r.Redis(host='localhost',port=6379,socket_connect_timeout=1)
            for k in r.scan_iter(match=f"aios:bus:exec:*{name}*"):
                r.delete(k)
            steps.append("✅ state cleared")
        except: steps.append("⚠️ state clear skipped")
        try:
            ok = self._restart_agent(name)
            steps.append(f"{'✅' if ok else '❌'} restarted")
        except: steps.append("❌ restart failed")
        self._json({"ok":True,"agent":name,"action":"reset","steps":steps})

    def _json(self,d,c=200):
        self.send_response(c)
        self.send_header("Content-Type","application/json")
        self.end_headers()
        self.wfile.write(json.dumps(d,ensure_ascii=False).encode())

    def _html(self):
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def log_message(self,*a): pass

# ─── 数据层 ───
def _find_agent_pid(name):
    unit = AGENT_UNITS.get(name)
    if unit:
        try:
            if _unit_active(unit):
                result = subprocess.run(
                    ["systemctl", "--user", "show", "-p", "MainPID", "--value", unit],
                    capture_output=True, text=True, timeout=5)
                pid = result.stdout.strip()
                return [pid if pid and pid != "0" else f"systemd:{unit}"]
            return []
        except Exception:
            return []
    """进程发现: 先试pgrep, 失败就用ps扫描."""
    pats = {
        "hermes": r"hermes_cli\.main|hermes.*gateway", "openclaw": r"node.*openclaw.*gateway", "claude": r"claude\b",
        "codex": r"codex\b", "opencode": "executor_daemon",
    }
    pat = pats.get(name, name)
    # 方法1: pgrep
    try:
        r = subprocess.run(["pgrep","-f",pat], capture_output=True, text=True, timeout=3)
        pids = [p for p in r.stdout.strip().split("\n") if p]
        if pids: return pids
    except Exception: pass
    # 方法2: ps aux扫描
    try:
        r = subprocess.run(["ps","aux"], capture_output=True, text=True, timeout=3)
        pids = []
        for line in r.stdout.split("\n"):
            if pat in line and "grep" not in line:
                parts = line.split()
                if len(parts) > 1:
                    pids.append(parts[1])
        return list(set(pids))
    except Exception: pass
    return []

def _resolve_agent_status(oa: dict, has_pid: bool, working: bool, recent_fail: bool) -> str:
    """统一状态解析: 聚合 observability + 进程 + CPU 活跃度"""
    s = oa.get("status","").lower()
    if not has_pid: return "offline"
    if s in ("error","failed","dead"): return "failed"
    if s == "running": return "running"
    if working: return "running"
    if s == "idle": return "idle"
    if s == "starting": return "starting"
    if s in ("planning","thinking","tool_call","coding","testing","waiting","completed"): return s
    if recent_fail: return "failed"
    return "idle"

AMS = {"hermes":"⚡ MiniMax","openclaw":"⚡ MiniMax","claude":"🧠 DeepSeek","codex":"🧠 DeepSeek","opencode":"🆓 Free"}

def _discover_agents():
    try:
        import redis as _r
        rc = _r.Redis(host='localhost',port=6379,socket_connect_timeout=1)
        keys = rc.keys("aios:bus:agent:*")
        names = sorted(set(k.decode().replace("aios:bus:agent:","") if isinstance(k,bytes) else k.replace("aios:bus:agent:","") for k in keys))
        main = [n for n in AGENT_NAMES if n in names]
        return main + [n for n in names if n not in main]
    except Exception:
        return AGENT_NAMES[:]


# 2026-08-17 P1-MON-001 closure — module-level filter helper, exported
# so unit tests can exercise the lifecycle-vs-alert classification
# without spinning up the whole monitor.
LIFECYCLE_NOISE_PREFIXES = ("lingying_", "star_")
LIFECYCLE_NOISE_EXACT = {"agent_mesh", "aios-ecosystem"}


def _filter_observability_events(obs, now_str=""):
    """Return the subset of ``obs["timeline"]`` events that should
    reach the timeline / alert / trace surfaces.

    Behaviour (P1-MON-001):

    * ``agent.offline`` / ``agent.online`` from a known no-op source
      (``lingying_*``, ``star_*``, ``agent_mesh``, ``aios-ecosystem``)
      is lifecycle noise and is dropped.
    * ``alert.critical`` / ``alert.error`` / ``alert.warning`` /
      ``alert.queue`` / ``alert.agent`` from ANY source is kept and
      bucketed into the alert surface.
    * ``agent.offline`` / ``agent.online`` from a real executor is
      kept in the timeline (NOT the alert bucket); its type prefix
      is preserved so the page-level renderAlert filter keeps its
      behaviour for real liveness events.
    """
    kept = []
    for ev in (obs or {}).get("timeline", [])[:80]:
        src = str(ev.get("source", "") or "")
        ev_type = str(ev.get("type", "") or "")
        is_alert = ev_type.startswith("alert.")
        is_lifecycle_noise = (
            any(src.startswith(p) for p in LIFECYCLE_NOISE_PREFIXES)
            or src in LIFECYCLE_NOISE_EXACT
        ) and not is_alert
        if is_lifecycle_noise:
            continue
        if ev_type in ("agent.offline", "agent.online") and not is_alert:
            kept.append({
                "ts": now_str, "system": src or "?",
                "status": "running", "task": ev_type,
                "type": ev_type,
                "_bucket": "timeline",
            })
            continue
        kept.append({
            "ts": ev.get("ts", "") or now_str,
            "system": src or "?",
            "type": ev_type,
            "payload": ev.get("payload", {}) or {},
            "_bucket": "alert" if is_alert else "timeline",
        })
    return kept


def get_full_status():
    obs = get_observability_summary()
    oa_map = {a.get("name",""):a for a in obs.get("agents",[])}
    discovered = _discover_agents()

    try:
        from aios_tool_evolution import health_all as _tool_health_all
        tool_health = _tool_health_all()
    except Exception:
        tool_health = {}

    # Agent 状态 (统一状态机 + 真实模型可用性)
    agents = []
    for n in discovered:
        oa = oa_map.get(n,{})
        pids = _find_agent_pid(n)
        recent_fail = any(e.get("source")==n and e.get("severity") in ("error","crit") for e in obs.get("timeline",[])[:20])
        agent_type = "main" if n in AGENT_NAMES else ("zodiac" if n.startswith("lingying") else "system")
        th = tool_health.get(n, {})
        resolved = _resolve_agent_status(oa,bool(pids),oa.get("status")=="running",recent_fail)
        if th and th.get("infrastructure_ok") and not th.get("fully_operational"):
            resolved = "degraded"
        agents.append({
            "name":n,"type":agent_type,"pid":bool(pids),
            "alive":bool(pids) or oa.get("alive",False),
            "working":oa.get("status")=="running" or bool(pids),
            "recent_fail":recent_fail,"hb_age":int(oa.get("heartbeat_age_s",999)),
            "status":resolved,
            "infrastructure_ok":th.get("infrastructure_ok"),
            "fully_operational":th.get("fully_operational"),
            "model_state":th.get("model_state", "unverified"),
            "model":th.get("model", _get_agent_model(n)),
            "provider":th.get("provider", AGENT_PROVIDER.get(n,"unknown")),
            "health_reason":th.get("reason", ""),
        })

    # Token
    tk = obs.get("tokens",{})
    dr = tk.get("daily",[])
    today_str = datetime.now().strftime('%Y%m%d')
    te = None
    for day_entry in dr:
        if isinstance(day_entry, dict) and str(day_entry.get("date","")) == today_str:
            te = day_entry
            break
    if te is None:
        te = {"total_tokens":0,"total_cost":0,"by_provider":{},"call_count":0,"is_today":False}
    else:
        te["is_today"] = True

    bp_te = te.get("by_provider",{})
    ds_calls = bp_te.get("deepseek",{}).get("call_count",0)
    mm_calls = bp_te.get("minimax",{}).get("call_count",0)

    DS = datetime.now().strftime('%Y%m%d')
    gov = {}
    try:
        import redis as _r
        rc = _r.Redis(host='localhost',port=6379,socket_connect_timeout=1)
        for k,v in (rc.hgetall(f"aios:bus:governance:daily:{DS}") or {}).items():
            gov[k.decode() if isinstance(k,bytes) else k] = float(v.decode() if isinstance(v,bytes) else v)
    except Exception: pass

    bp = te.get("by_provider",{})
    token_info = {
        "today_tokens":te.get("total_tokens",0),"today_cost":te.get("total_cost",0),"today_calls":te.get("call_count",0),
        "is_today":te.get("is_today",True),
        "ds_tokens":bp.get("deepseek",{}).get("total_tokens",0),"ds_cost":bp.get("deepseek",{}).get("cost",0),
        "ds_calls":bp.get("deepseek",{}).get("call_count",0),
        "mm_tokens":bp.get("minimax",{}).get("total_tokens",0),"mm_cost":bp.get("minimax",{}).get("cost",0),
        "mm_calls":bp.get("minimax",{}).get("call_count",0),
        "prompt_tokens":bp.get("deepseek",{}).get("prompt_tokens",0) + bp.get("minimax",{}).get("prompt_tokens",0),
        "completion_tokens":bp.get("deepseek",{}).get("completion_tokens",0) + bp.get("minimax",{}).get("completion_tokens",0),
        "agent_tokens":{},"agent_cost":{},"days":dr,
        "week_total":sum(d.get("total_tokens",0) for d in dr),"week_cost":sum(d.get("total_cost",0) for d in dr),
    }
    # Governance 覆盖 + 补充 (gov hash 是真实数据源)
    gov_total_tokens = int(sum(v for k,v in gov.items() if 'tokens' in k and 'cost' not in k))
    gov_total_cost = round(sum(v for k,v in gov.items() if 'cost' in k), 4)
    if gov_total_tokens > 0:
        if token_info["today_tokens"] == 0:
            token_info["today_tokens"] = gov_total_tokens
        if token_info["today_cost"] == 0:
            token_info["today_cost"] = gov_total_cost
    # 从 gov 补充 DeepSeek tokens (claude) 和 MiniMax tokens (openclaw+hermes)
    if token_info["ds_tokens"] == 0:
        token_info["ds_tokens"] = int(gov.get("claude_tokens", 0))
        token_info["ds_cost"] = round(gov.get("claude_cost", 0), 4)
    if token_info["mm_tokens"] == 0:
        token_info["mm_tokens"] = int(gov.get("openclaw_tokens", 0) + gov.get("hermes_tokens", 0))
        token_info["mm_cost"] = round(gov.get("openclaw_cost", 0) + gov.get("hermes_cost", 0), 4)
    if gov.get("hermes_tokens",0) > 0 or gov.get("openclaw_tokens",0) > 0:
        token_info["mm_tokens"] = int(gov.get("hermes_tokens",0) + gov.get("openclaw_tokens",0))
        token_info["mm_cost"] = round(gov.get("hermes_cost",0) + gov.get("openclaw_cost",0), 4)
    for n in AGENT_NAMES:
        token_info["agent_tokens"][n] = int(gov.get(f"{n}_tokens",0))
        token_info["agent_cost"][n] = round(gov.get(f"{n}_cost",0),4)

    # ── 队列数据 (在生成事件前获取) ──
    qs = get_queue_status()
    qd = get_queue_details()

    # ── 实时事件生成 (从系统状态, 不只是历史) ──
    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    now_str = now_bj.strftime("%H:%M:%S")

    alert_events = []
    timeline_events = []
    trace_events = []

    # 1. 从observability历史事件 (过滤虚拟agent噪声)
    #
    # P1-MON-001 closure (2026-08-17): the previous filter unconditionally
    # skipped every ``agent.offline`` / ``agent.online`` event, which
    # also swallowed real ``alert.critical`` payloads published by the
    # same sources.  The fix:
    #
    #  * ``agent.offline`` / ``agent.online`` are treated as lifecycle
    #    noise ONLY when the event is from a known no-op source
    #    (``lingying_*``, ``star_*``, ``agent_mesh``).  Real executor /
    #    gateway alerts with the same type prefix still surface.
    #  * Every event whose ``type`` starts with ``alert.`` is kept
    #    regardless of source — the page-level filter
    #    ``renderAlert`` still buckets them into the alert lane.
    LIFECYCLE_NOISE_PREFIXES = ("lingying_", "star_")
    LIFECYCLE_NOISE_EXACT = {"agent_mesh", "aios-ecosystem"}
    for ev in obs.get("timeline",[])[:80]:
        src = ev.get("source","")
        ev_type = ev.get("type","")
        is_alert = ev_type.startswith("alert.")
        is_lifecycle_noise = (
            any(src.startswith(p) for p in LIFECYCLE_NOISE_PREFIXES)
            or src in LIFECYCLE_NOISE_EXACT
        ) and not is_alert
        if is_lifecycle_noise:
            continue
        # agent.offline / agent.online from a real executor is still
        # shown in the timeline (NOT in alerts — renderAlert filters
        # by status==='failed'||type.startswith('alert.')).  We keep
        # only the alert-worthy ones here.
        if ev_type in ("agent.offline", "agent.online") and not is_alert:
            timeline_events.append({
                "ts": now_str, "system": src or "?",
                "status": "running", "task": ev_type,
                "type": ev_type,
            })
            continue
        pld = ev.get("payload",{})
        s = "completed" if ev_type.startswith("task.completed") else ("failed" if "fail" in ev_type else "running")
        
        ts_raw = ev.get("ts","")
        display_ts = ts_raw
        if ts_raw:
            try:
                dt = datetime.fromisoformat(ts_raw.replace("Z","+00:00"))
                display_ts = (dt + timedelta(hours=8)).strftime("%H:%M:%S")
            except: pass
        
        # 中文友好显示: 优先用payload里的task字段
        task_desc = pld.get('task','') or pld.get('module','') or pld.get('status','') or ev_type
        entry = {"ts":display_ts,"system":ev.get("source","?"),"status":s,
                 "task":str(task_desc)[:80],"type":ev_type}
        timeline_events.append(entry)
        if ev_type.startswith("alert.") or s=="failed":
            alert_events.append(entry)
        if ev_type.startswith("task."):
            trace_events.append(entry)

    # 2. 从当前队列和Agent状态生成实时事件
    # Agent状态变化
    for a in agents:
        if a.get("status") == "running" and a.get("pid"):
            timeline_events.insert(0,{"ts":now_str,"system":a["name"],"status":"running",
                "task":f"{a['name']} 正在执行任务","type":"agent.running"})
        elif a.get("status") == "failed":
            timeline_events.insert(0,{"ts":now_str,"system":a["name"],"status":"failed",
                "task":f"{a['name']} 状态异常","type":"agent.failed"})
            alert_events.insert(0,{"ts":now_str,"system":a["name"],"status":"failed",
                "task":f"{a['name']} 检测到异常","type":"alert.agent"})

    # 3. 队列积压警告
    if qs.get("pending",0) > 20:
        timeline_events.insert(0,{"ts":now_str,"system":"system","status":"running",
            "task":f"队列积压 {qs['pending']} 个任务待处理","type":"queue.pending"})
    if qs.get("failed",0) > 5:
        alert_events.insert(0,{"ts":now_str,"system":"system","status":"failed",
            "task":f"累计 {qs['failed']} 个失败任务","type":"alert.queue"})

    # 4. Token消耗
    if token_info.get("today_tokens",0) > 0:
        timeline_events.insert(0,{"ts":now_str,"system":"governance","status":"completed",
             "task":f"今日消耗 {token_info['today_tokens']:,} tokens","type":"token.usage"})

    health = get_health_report(agents, qs, obs)
    return {"agents":agents,"queue":qs,"queue_details":qd,"tokens":token_info,
            "alerts":alert_events,"timeline":timeline_events,"traces":trace_events,
            "health": health}


# ─── P4 Health Model integration ───
# Build the seven-dimension health report consumed by both the
# lightweight monitor loop and the dedicated /api/health/v2 route. The
# data sources are *already* collected inside get_full_status so we
# never call AI providers from this layer.

from aios_health_model import (
    HealthDimension,
    HealthReport,
    STATUS_HEALTHY, STATUS_DEGRADED, STATUS_FAILED, STATUS_UNKNOWN,
    aggregate_status, probe_service_state, probe_endpoint_reachability,
    probe_capability, probe_authoritative_state, probe_latest_e2e_acceptance,
    probe_runtime_revision, evaluate_runtime_dependency,
    assemble_health_report,
    DEFAULT_MAX_ACCEPTANCE_AGE_SECONDS,
)
AIOS_TOOLS_DIR = Path("${AIOS_HOME}/kernel/tools")
ACCEPTANCE_REPORTS_DIR = Path("${AIOS_HOME}/logs/acceptance")


def _orchestrator_process_started_at() -> float:
    """Return the orchestrator process start time as epoch seconds."""
    try:
        for proc_dir in Path("/proc").iterdir():
            if not proc_dir.name.isdigit():
                continue
            try:
                stat = proc_dir.stat()
            except OSError:
                continue
            try:
                with (proc_dir / "cmdline").open("rb") as handle:
                    cmdline = handle.read().replace(b"\x00", b" ").decode(
                        "utf-8", errors="replace",
                    ).strip()
            except (OSError, PermissionError):
                continue
            if ("aios_orchestrator.py" in cmdline and "--daemon" in cmdline):
                return float(stat.st_ctime)
    except Exception:
        pass
    return 0.0


def _orchestrator_main_pid() -> int:
    """Return the orchestrator service MainPID via systemctl."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", "aios-orchestrator.service",
             "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        return int((proc.stdout or "0").strip() or 0)
    except Exception:
        return 0


# P7F errata: explicit failure-ownership buckets for the capability
# matrix. External failures (provider quota / plan / rate-limit /
# token-plan / region / auth / external network / external cooldown /
# free-model service / transient provider) MUST surface as
# ``DEGRADED_EXTERNAL`` so the monitor does not silently attribute
# upstream failures to local infrastructure. Only signals that
# explicitly point at local process / adapter / IPC / server code
# (``model_error``, ``adapter_error``, ``ipc_error``, ``local_*``)
# may resolve to ``DEGRADED_INTERNAL``. ``evidence_freshness`` is
# tracked separately (FRESH / STALE / UNKNOWN) and MUST NOT change
# the failure-ownership bucket.
EXTERNAL_FAILURE_STATES = frozenset({
    "quota_exhausted", "plan_exhausted", "rate_limited",
    "token_plan", "region", "auth_failed",
    "network_error", "cooldown",
    "free_model_service_error", "transient_provider_error",
    "external_free_model_service", "external_network",
    "external_rate_limit_or_cooldown",
})

INTERNAL_FAILURE_STATES = frozenset({
    "model_error", "adapter_error", "ipc_error",
    "local_server_error", "local_server_unreachable",
    "local_adapter_exception", "local_opencode_server",
    "process_error",
})


def _build_capability_matrix_from_agents(agents_list) -> dict:
    """Translate the agents list into the capability matrix used by health.

    P7F errata: classify ``failure_ownership`` separately from
    ``evidence_freshness``. External failures are explicit and never
    collapse into ``DEGRADED_INTERNAL``; only signals that point at
    local process / adapter / IPC / server code may do so.
    """
    matrix = {}
    for agent in agents_list or []:
        name = str(agent.get("name", "")).lower()
        if not name:
            continue
        if name in ("openclaw", "hermes", "opencode", "claude", "codex"):
            model_state = str(agent.get("model_state", "")).lower()
            fully = bool(agent.get("fully_operational"))
            infra = agent.get("infrastructure_ok")
            status = str(agent.get("status", "")).lower()
            if fully and infra is True:
                matrix[name] = "AVAILABLE"
            elif model_state in EXTERNAL_FAILURE_STATES:
                matrix[name] = "DEGRADED_EXTERNAL"
            elif model_state in INTERNAL_FAILURE_STATES:
                matrix[name] = "DEGRADED_INTERNAL"
            elif status in ("failed", "offline") or fully is False:
                # No explicit external signal AND the local process is
                # not fully operational → safe default is internal.
                matrix[name] = "DEGRADED_INTERNAL"
            else:
                matrix[name] = "UNVERIFIED"
    # Provide not-configured entries for the canonical five tools so the
    # matrix is always observable and reviewers know what is *expected*.
    expected = {"openclaw", "hermes", "opencode", "claude", "codex"}
    for name in expected:
        matrix.setdefault(name, "NOT_CONFIGURED")
    return matrix


def _probe_runtime_dependency_for_orchestrator() -> HealthDimension:
    pid = _orchestrator_main_pid()
    started = _orchestrator_process_started_at()
    deps = [
        AIOS_TOOLS_DIR / "aios_orchestrator.py",
        AIOS_TOOLS_DIR / "aios_verification_gate.py",
        AIOS_TOOLS_DIR / "aios_runtime_revision.py",
        AIOS_TOOLS_DIR / "aios_bus.py",
    ]
    return probe_runtime_revision(
        pid=pid or None,
        process_started_at=started or None,
        dependency_paths=deps,
    )


def _probe_authoritative_state() -> HealthDimension:
    """Look up the latest parent/result/verification authoritative state.

    P5F: the dimension is no longer "FAILED when no active workflow is
    running". AIOS is idle most of the time; the new rule treats the
    state as **idempotent**: once a real canary reported a complete
    parent/result/verification triplet, the system stays HEALTHY until
    a NEW authoritative event invalidates that verdict. The idleness
    information is now passed in via ``latest_acceptance`` so the probe
    can report ``IDLE_WITH_RECENT_AUTHORITATIVE_SUCCESS`` rather than
    (incorrectly) FAILED.
    """
    parent_present = False
    result_present = False
    verification_present = False
    redis_reachable = True
    latest_acceptance_state: Dict[str, Any] | None = None
    try:
        import redis as _redis
        client = _redis.Redis(host="localhost", port=6379,
                              socket_connect_timeout=2)
        # parent_id is whatever the most recent canary / acceptance task used.
        latest = max(ACCEPTANCE_REPORTS_DIR.glob("canary_*.json"),
                     key=lambda path: path.stat().st_mtime,
                     default=None)
        parent_id = ""
        core_result = ""
        acceptance_age = 0
        if latest is not None:
            try:
                payload = json.loads(latest.read_text(encoding="utf-8"))
                parent_id = str(payload.get("e2e_task_id") or "").strip()
                core_result = str(payload.get("core_result") or "").upper()
                acceptance_age = int(max(
                    0.0, time.time() - latest.stat().st_mtime,
                ))
            except Exception:
                parent_id = ""
        if parent_id:
            parent_present = client.zcard(f"aios:trace:{parent_id}") > 0
            result_present = client.exists(f"aios:bus:task:result:{parent_id}") > 0
            verification_present = client.exists(
                f"aios:bus:task:verification:{parent_id}") > 0
        if latest is not None and core_result:
            latest_acceptance_state = {
                "task_id": parent_id,
                "core_result": core_result,
                "age_seconds": acceptance_age,
                "path": str(latest),
            }
    except Exception:
        redis_reachable = False
    return probe_authoritative_state(
        parent_present=parent_present,
        result_present=result_present,
        verification_present=verification_present,
        redis_reachable=redis_reachable,
        latest_acceptance=latest_acceptance_state,
    )


def _probe_service_state() -> HealthDimension:
    units = [
        "aios-entry-gateway.service",
        "aios-orchestrator.service",
        "aios-verification-gate.service",
        "aios-monitor.service",
        "aios-acceptance.timer",
    ]
    return probe_service_state(units, _unit_active)


def _probe_endpoints() -> HealthDimension:
    def gateway_ok() -> bool:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:18801/health", timeout=2,
            ) as response:
                return response.status == 200
        except Exception:
            return False

    def redis_ok() -> bool:
        try:
            import redis as _redis
            client = _redis.Redis(host="localhost", port=6379,
                                  socket_connect_timeout=2)
            return bool(client.ping())
        except Exception:
            return False

    def monitor_self_ok() -> bool:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8086/api/health", timeout=2,
            ) as response:
                return response.status == 200
        except Exception:
            return False

    return probe_endpoint_reachability({
        "gateway": gateway_ok,
        "redis": redis_ok,
        "monitor_self": monitor_self_ok,
    })


def get_health_report(agents_list=None, queue_status=None, observability=None) -> dict:
    """Return the seven-dimension P4 health report.

    All data sources are *already* cached inside the monitor loop; this
    function never calls an AI provider and is safe to invoke at any
    frequency.
    """
    agents_list = agents_list if agents_list is not None else []
    service_dim = _probe_service_state()
    endpoint_dim = _probe_endpoints()
    runtime_dim = _probe_runtime_dependency_for_orchestrator()
    capability_matrix = _build_capability_matrix_from_agents(agents_list)
    executor_dim = probe_capability(
        "executor_capability", capability_matrix, mandatory=True,
    )
    reviewer_dim = probe_capability(
        "reviewer_capability", capability_matrix, mandatory=True,
    )
    authoritative_dim = _probe_authoritative_state()
    acceptance_dim = probe_latest_e2e_acceptance(
        ACCEPTANCE_REPORTS_DIR,
        max_age_seconds=DEFAULT_MAX_ACCEPTANCE_AGE_SECONDS,
        kind="canary",
    )
    dimensions = {
        service_dim.unit: service_dim,
        endpoint_dim.unit: endpoint_dim,
        runtime_dim.unit: runtime_dim,
        executor_dim.unit: executor_dim,
        reviewer_dim.unit: reviewer_dim,
        authoritative_dim.unit: authoritative_dim,
        acceptance_dim.unit: acceptance_dim,
    }
    report = assemble_health_report(dimensions)
    payload = report.to_dict()
    # P8C-U: extend the monitor payload with dynamic tool × model
    # routing information. The monitor MUST NOT probe any Provider
    # to compute this; everything below is derived from local
    # registries / cooldown state.
    try:
        from aios_routing_policy import (
            get_default_routing_engine,
        )
        from aios_tool_failover import (
            get_default_tool_engine,
        )
        from aios_qwen_provider import (
            compute_qwen_status, get_last_qwen_status,
        )
        engine = get_default_routing_engine()
        tool_engine = get_default_tool_engine()
        statuses = tool_engine.all_tool_statuses()
        snapshot = engine.health_snapshot()
        payload["p8c_u"] = {
            "registered_tools": [m.to_dict() for m in
                                  engine._tool_engine._registry.list_all()],  # type: ignore[attr-defined]
            "enabled_tools": [m.tool_id for m in
                              engine._tool_engine._registry.list_enabled()],  # type: ignore[attr-defined]
            "tool_effective_status": [s.to_dict() for s in statuses],
            "verified_model_bindings": sorted({
                b.binding_id for s in statuses
                for b in [
                    engine._model_engine._bindings.get(bid)  # type: ignore[attr-defined]
                    for bid in s.verified_bindings
                ] if b is not None
            }),
            "blocked_model_bindings": sorted({
                b.binding_id for s in statuses
                for b in [
                    engine._model_engine._bindings.get(bid)  # type: ignore[attr-defined]
                    for bid in s.blocked_bindings
                ] if b is not None
            }),
            "shared_model_resources": [
                r.to_dict()
                for r in engine._model_engine._resources.list_all()  # type: ignore[attr-defined]
            ],
            "resource_cooldowns": engine._model_engine.to_dict().get(  # type: ignore[attr-defined]
                "resources", {}),
            "binding_cooldowns": engine._model_engine.to_dict().get(  # type: ignore[attr-defined]
                "bindings", {}),
            "role_routes": snapshot.get("coverage", {}),
            "feature_flags": snapshot.get("feature_flags", {}),
            "shadow_log_size": snapshot.get("shadow_log_size", 0),
            "qwen_status": (
                get_last_qwen_status().to_dict()
                if get_last_qwen_status() else
                compute_qwen_status().to_dict()
            ),
        }
        # P8D: enrich p8c_u payload with the per-tool effective
        # binding the routing engine currently uses.  The field is
        # sourced from ``tool_effective_status[*].effective_binding``
        # so consumers can spot ``claude:minimax`` /
        # ``codex:minimax`` without re-deriving it.
        try:
            eff = {}
            for s in statuses:
                eff[s.tool_id] = {
                    "status": s.status,
                    "effective_binding": s.effective_binding,
                    "primary_binding": s.primary_binding,
                    "fallback_ready": s.fallback_ready,
                    "verified_bindings": list(s.verified_bindings),
                    "blocked_bindings": list(s.blocked_bindings),
                    "reason": s.reason,
                }
            payload["p8c_u"]["tool_effective_binding"] = eff
        except Exception:
            pass
    except Exception:
        payload["p8c_u"] = {"error": "p8c_u_unavailable"}
    return payload


# ─── P8D public payload helper (used by tests and reports) ───

def _tool_effective_binding_payload(tool_engine) -> dict:
    """Return ``{tool_id: {status, effective_binding, primary_binding,
    fallback_ready, verified_bindings, blocked_bindings, reason}}``
    for every registered enabled tool.  This is the P8D §十四 view
    used by tests / acceptance / monitor consumers that need the
    *currently effective* model binding for each tool, sourced from
    the same routing layer the orchestrator would use at runtime.
    """
    payload: dict = {}
    try:
        statuses = tool_engine.all_tool_statuses()
    except Exception:
        return payload
    for s in statuses:
        payload[s.tool_id] = {
            "status": s.status,
            "effective_binding": s.effective_binding,
            "primary_binding": s.primary_binding,
            "fallback_ready": s.fallback_ready,
            "verified_bindings": list(s.verified_bindings),
            "blocked_bindings": list(s.blocked_bindings),
            "reason": s.reason,
        }
    return payload


# ─── 后台线程 ───
def publisher_loop():
    import redis as _r
    r = _r.Redis(host='localhost',port=6379,socket_connect_timeout=2)
    while True:
        try:
            r.publish("aios:sse:push",json.dumps(get_full_status(),ensure_ascii=False))
        except Exception: pass
        time.sleep(15)

def heartbeat_daemon():
    import redis as _r
    r = _r.Redis(host='localhost',port=6379,socket_connect_timeout=2)
    refresh_count = 0
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()
            # 每60秒刷新一次agent列表(支持动态新增)
            if refresh_count % 4 == 0:
                current_agents = _get_agent_names()
            else:
                current_agents = AGENT_NAMES
            for n in current_agents:
                if _find_agent_pid(n):
                    r.set(f"aios:bus:system:{n}:heartbeat",now,ex=300)
                    r.hset(f"aios:bus:agent:{n}",mapping={"status":"RUNNING","last_heartbeat":now,"name":n})
            refresh_count += 1
        except Exception: pass
        time.sleep(15)

if __name__ == "__main__":
    port = 8080
    for i,a in enumerate(sys.argv):
        if a == "--port" and i+1 < len(sys.argv): port = int(sys.argv[i+1])
    init_registry()
    from aios_bus import register_pin
    register_pin("monitor.status", get_full_status, "AIOS Monitor: 获取全量监控状态")
    register_pin("monitor.page", lambda: {"title":"AIOS Monitor","pages":10,"port":port}, "AIOS Monitor: 信息页")
    threading.Thread(target=publisher_loop,daemon=True).start()
    threading.Thread(target=heartbeat_daemon,daemon=True).start()
    # P9B: keep the tool-health cache fresh.  Without a background
    # publisher the ``cache/tool_health/<tool>.json`` record is only
    # updated by the slow ``probe()`` path; production has no
    # scheduled probe and the cache drifts to ``stale: true`` between
    # real tasks, pinning ``fully_operational`` to false.  The
    # publisher is a long-lived in-process daemon thread that writes
    # a fresh lightweight observation every 60s (configurable).  A
    # single tool failure does NOT block the rest of the sweep and
    # no real model API is ever invoked from this thread.
    try:
        from aios_health_publisher import start_publisher
        _pub = start_publisher()
        print(f"🩺 AIOS Health Publisher: started "
              f"(interval={int(_pub.interval)}s, thread={_pub.is_alive})")
    except Exception as _exc:
        print(f"⚠️ AIOS Health Publisher: failed to start ({_exc})")
    print(f"📊 AIOS Monitor: http://localhost:{port}  (10 pages)")
    ThreadingServer((safe_bind_host(), port),Handler).serve_forever()
