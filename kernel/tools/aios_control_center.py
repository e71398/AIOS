import sys
sys.path.insert(0, "${AIOS_HOME}/kernel/tools")
from aios_secure import safe_bind_host, cors_origin
#!/usr/bin/env python3
"""AI Control Center V1 — 10页全功能 AI 监控管理中心"""
import json, sys, os, time, subprocess, threading, re
from pathlib import Path
from socketserver import ThreadingMixIn
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import get_queue_status, get_queue_details, check_recent, init_registry
from aios_observability import get_observability_summary

# ── 增强 get_full_status: 补充 Control Center JS 所需的字段 ──
def _augment_full_status(d):
    """
    补齐 aios_control_center.js render 函数所需的字段:
      - queue:        pending/running/completed/failed
      - queue_details: 任务详情列表
      - alerts:       = events (timeline/trace/alert 三 tab 共用)
      - costs:        {today, this_week, month, projection} 简化结构
      - tokens: 补全 (mm/ds calls/cost)
    返回新 dict (不修改原对象).
    """
    if not isinstance(d, dict):
        d = {}
    out = dict(d)

    # queue stats + details
    if "queue" not in out:
        out["queue"] = get_queue_status()
    if "queue_details" not in out:
        out["queue_details"] = get_queue_details(status_filter="") or []
    # alerts (timeline/trace/alert 三个 tab 共用 d.alerts = events)
    if "alerts" not in out and "events" in out:
        out["alerts"] = out["events"]

    # costs: 简化的 from tokens 数据
    if "costs" not in out and "tokens" in out:
        tk = out["tokens"] or {}
        out["costs"] = {
            "today":       {"tokens": tk.get("today_tokens", 0),
                            "cost":   tk.get("today_cost",   0)},
            "this_week":    {"tokens": tk.get("week_total", 0),
                            "cost":   tk.get("week_cost",   0)},
            "this_month":   {"tokens": 0,  "cost":   0},  # 暂用 0 占位
            "projection":   {"tokens": 0,  "cost":   0},
        }
    # tokens: 补全 mm_calls/mm_cost/ds_calls/ds_cost (默认 0)
    if "tokens" in out:
        tk = out["tokens"]
        for k in ("mm_calls", "mm_cost", "ds_calls", "ds_cost",
                  "today_tokens", "today_cost", "week_total", "week_cost"):
            tk.setdefault(k, 0)
    else:
        out["tokens"] = {"mm_calls": 0, "mm_cost": 0, "ds_calls": 0,
                         "ds_cost": 0, "today_tokens": 0, "today_cost": 0,
                         "week_total": 0, "week_cost": 0,
                         "mm_tokens": 0, "ds_tokens": 0}

    return out

# ─── 统一 Agent 状态映射 ───
AGENT_NAMES = ["hermes","openclaw","claude","codex","opencode"]
AGENT_LABEL = {"hermes":"Hermes","openclaw":"OpenClaw","claude":"Claude Code","codex":"Codex","opencode":"OpenCode"}
AGENT_MODEL = {"hermes":"MiniMax-M3","openclaw":"MiniMax-M3","claude":"DeepSeek V4","codex":"DeepSeek V4","opencode":"免费模型"}
AGENT_PROVIDER = {"hermes":"MiniMax","openclaw":"MiniMax","claude":"DeepSeek","codex":"DeepSeek","opencode":"Free"}
AGENT_ICON = {"hermes":"⚡","openclaw":"⚡","claude":"🧠","codex":"🧠","opencode":"🆓"}
# 统一状态机
AGENT_STATUS_MAP = {
    "offline": "Offline", "starting": "Starting", "idle": "Idle",
    "planning": "Planning", "thinking": "Thinking", "tool_call": "Tool Calling",
    "coding": "Coding", "testing": "Testing", "waiting": "Waiting",
    "completed": "Completed", "failed": "Failed",
}
AGENT_STATUS_COLOR = {
    "offline":"red","starting":"yellow","idle":"dim","planning":"blue",
    "thinking":"yellow","tool_call":"accent","coding":"green","testing":"purple",
    "waiting":"dim","completed":"green","failed":"red",
}
AGENT_STATUS_DOT = {
    "offline":"🔴","starting":"🟡","idle":"⚪","planning":"🔵",
    "thinking":"🟡","tool_call":"🔷","coding":"🟢","testing":"🟣",
    "waiting":"⚪","completed":"🟢","failed":"🔴",
}

# ─── HTML (10页单页应用) ───
HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Control Center</title>
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
<h1>🤖 AI Control <small>V1 · 10页</small></h1>
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
</div>

<script>
// ── 导航 ──
document.querySelectorAll('.nav-item').forEach(el=>{
  el.addEventListener('click',()=>{
    document.querySelectorAll('.nav-item,.page').forEach(x=>x.classList.remove('active'));
    el.classList.add('active');
    document.getElementById('pg-'+el.dataset.p).classList.add('active');
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
  completed:['bdg-g','✅'],failed:['bdg-r','❌'],
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
es.onerror=()=>{document.getElementById('clock').textContent+=' ⚠️ 重连中'};
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
}

// ═══ 1. 总览 ═══
function renderOverview(d){
  const ag=d.agents||[],tk=d.tokens||{},q=d.queue||{};
  const main=ag.filter(a=>a.type==='main');
  const online=main.filter(a=>a.alive).length;
  const busy=main.filter(a=>a.working).length;
  const total=ag.length;
  document.getElementById('ov-stats').innerHTML=
    `<div class="card statbox"><div class="stat-val">${online}/${total}</div><div class="stat-label">🟢 在线</div></div>
     <div class="card statbox"><div class="stat-val">${busy}</div><div class="stat-label">⚡ 运行中</div></div>
     <div class="card statbox"><div class="stat-val">${nf(tk.today_tokens)}</div><div class="stat-label">📄 今日 Token</div></div>
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
  const system=ag.filter(a=>a.type!=='main');
  let h='<h3 style="color:var(--accent);margin:8px 0">🤖 主要 AI</h3>';
  for(const a of main){
    const n=a.name; const st=a.status||(a.alive?'idle':'offline'); const hb=a.hb_age||999;
    h+=`<div class="card">
      <div class="flex jcsb aic"><div><b style="font-size:15px">${agentLabel(n)}</b> ${statusHtml(st)}</div>
      <div class="dim">💓 ${hb}s</div></div>
      <div class="flex gap2 mt" style="flex-wrap:wrap">
        <div><span class="dim">模型</span><br>${agentModel(n)}</div>
        <div><span class="dim">进程</span><br>${a.pid?'🟢 运行':'🔴 未启'}</div>
        <div><span class="dim">活动</span><br>${a.working?'🔄 执行':'💤 空闲'}</div>
        <div><span class="dim">心跳</span><br><span style="color:var(--${hb<30?'green':(hb<120?'yellow':'red')})">${hb}s</span></div>
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
  const evs=d.alerts||[];
  let h='';
  for(const e of evs){
    const ic={completed:'✅',failed:'❌',running:'🔄',pending:'⏳'};
    h+=`<div class="tl-item"><span class="tl-time">${e.ts||''}</span><span class="tl-agent">${e.system||'?'}</span><span class="tl-msg">${ic[e.status]||'❓'} ${(e.task||'').slice(0,60)}</span></div>`;
  }
  if(!h)h='<p class="dim tc">无记录</p>';
  document.getElementById('tl-body').innerHTML=h;
}

// ═══ 8. 链路 ═══
function renderTrace(d){
  const evs=(d.alerts||[]).filter(e=>e.task).slice(0,10);
  let h='<table><thead><tr><th>时间</th><th>来源</th><th>事件</th></tr></thead><tbody>';
  for(const e of evs)h+=`<tr><td class="dim">${e.ts||''}</td><td>${e.system||'?'}</td><td class="dim">${(e.task||'').slice(0,60)}</td></tr>`;
  if(evs.length===0)h='<p class="dim tc">等待 Trace 数据接入</p>';
  else h+='</tbody></table>';
  document.getElementById('trace-body').innerHTML=h;
}

// ═══ 9. 告警 ═══
function renderAlert(d){
  const fails=(d.alerts||[]).filter(a=>a.status==='failed');
  const running=(d.alerts||[]).filter(a=>a.status==='running');
  const q=d.queue||{};
  document.getElementById('alert-cards').innerHTML=
    `<div class="card statbox"><div class="stat-val red">${fails.length}</div><div class="stat-label">❌ 今日失败</div></div>
     <div class="card statbox"><div class="stat-val yellow">${q.pending||0}</div><div class="stat-label">⏳ 待处理</div></div>
     <div class="card statbox"><div class="stat-val red">${(q.failed||0)>2?'⚠️ 异常':'✅ 正常'}</div><div class="stat-label">📊 队列健康</div></div>`;
  if(!fails.length){document.getElementById('alert-list').innerHTML='<p class="dim tc">✅ 无告警</p>';return}
  let tbl='<table><thead><tr><th>时间</th><th>来源</th><th>详情</th></tr></thead><tbody>';
  for(const a of fails.slice(0,20))tbl+=`<tr><td class="dim">${a.ts||''}</td><td style="color:var(--red)">${a.system||'?'}</td><td class="dim">${(a.task||'').slice(0,60)}</td></tr>`;
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
function resetToday(){
  if(!confirm('确认清零今日统计？历史数据保留'))return;
  fetch('/api/reset-today',{method:'POST'}).then(r=>r.json()).then(d=>alert(d.ok?'✅ 已清零':'❌ 失败'));
}
</script></body></html>"""

# ─── HTTP 服务器 ───
class ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/sse": return self._sse()
        if p == "/api/export": return self._export()
        if p.startswith("/api/"): return self._json({"error":"not found"},404)
        self._html()

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/reset-today": return self._reset_today()
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
            d = _augment_full_status(d)
            self.wfile.write(f"data: {json.dumps(d,ensure_ascii=False,default=str)}\n\n".encode()); self.wfile.flush()
            while True:
                m = ps.get_message(timeout=5)
                if m and m["type"]=="message":
                    try:
                        push = json.loads(m["data"].decode())
                    except Exception:
                        continue
                    push = _augment_full_status(push)
                    self.wfile.write(f"data: {json.dumps(push,ensure_ascii=False,default=str)}\n\n".encode()); self.wfile.flush()
                else:
                    self.wfile.write(": hb\n\n".encode()); self.wfile.flush()
        except: pass

    def _export(self):
        qs = parse_qs(urlparse(self.path).query)
        fmt = qs.get("fmt",["json"])[0]
        data = get_full_status()
        if fmt == "csv":
            lines = ["AI,Status,Model,Alive,Heartbeat_s,Tokens_Today,Cost_Today"]
            for a in data.get("agents",[]):
                n=a["name"]; tk=data.get("tokens",{}).get("agent_tokens",{}).get(n,0)
                co=data.get("tokens",{}).get("agent_cost",{}).get(n,0)
                model = AGENT_MODEL.get(n, "?")
                lines.append(f'{n},{a.get("status","?")},{model},{a.get("alive",0)},{a.get("hb_age",0)},{tk},{co}')
            csv = "\n".join(lines)
            self.send_response(200)
            self.send_header("Content-Type","text/csv; charset=utf-8")
            self.send_header("Content-Disposition",'attachment; filename="ai_control_export.csv"')
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
    pats = {"hermes":"hermes_cli.main gateway","openclaw":"openclaw-gateway|dist/index.*gateway",
            "claude":r"claude\b","codex":"codex-relay|codex-pal","opencode":r"opencode\b"}
    try:
        r = subprocess.run(["pgrep","-f",pats.get(name,name)],capture_output=True,text=True,timeout=3)
        return [p for p in r.stdout.strip().split("\n") if p]
    except: return []

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
    except:
        return AGENT_NAMES[:]

def get_full_status():
    obs = get_observability_summary()
    oa_map = {a.get("name",""):a for a in obs.get("agents",[])}
    discovered = _discover_agents()

    # Agent 状态 (统一状态机)
    agents = []
    for n in discovered:
        oa = oa_map.get(n,{})
        pids = _find_agent_pid(n)
        recent_fail = any(e.get("source")==n and e.get("severity") in ("error","crit") for e in obs.get("timeline",[])[:20])
        agent_type = "main" if n in AGENT_NAMES else ("zodiac" if n.startswith("lingying") else "system")
        agents.append({
            "name":n,"type":agent_type,"pid":bool(pids),"alive":oa.get("alive",bool(pids)),
            "working":oa.get("status")=="running" or bool(pids),
            "recent_fail":recent_fail,"hb_age":int(oa.get("heartbeat_age_s",999)),
            "status":_resolve_agent_status(oa,bool(pids),oa.get("status")=="running",recent_fail),
        })

    # Token
    tk = obs.get("tokens",{})
    dr = tk.get("daily",[])
    te = dr[-1] if dr else {"total_tokens":0,"total_cost":0,"by_provider":{},"call_count":0}

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
    except: pass

    bp = te.get("by_provider",{})
    token_info = {
        "today_tokens":te.get("total_tokens",0),"today_cost":te.get("total_cost",0),"today_calls":te.get("call_count",0),
        "ds_tokens":bp.get("deepseek",{}).get("total_tokens",0),"ds_cost":bp.get("deepseek",{}).get("cost",0),
        "ds_calls":bp.get("deepseek",{}).get("call_count",0),
        "mm_tokens":bp.get("minimax",{}).get("total_tokens",0),"mm_cost":bp.get("minimax",{}).get("cost",0),
        "mm_calls":bp.get("minimax",{}).get("call_count",0),
        "prompt_tokens":bp.get("deepseek",{}).get("prompt_tokens",0) + bp.get("minimax",{}).get("prompt_tokens",0),
        "completion_tokens":bp.get("deepseek",{}).get("completion_tokens",0) + bp.get("minimax",{}).get("completion_tokens",0),
        "agent_tokens":{},"agent_cost":{},"days":dr,
        "week_total":sum(d.get("total_tokens",0) for d in dr),"week_cost":sum(d.get("total_cost",0) for d in dr),
    }
    # Governance 覆盖
    if gov.get("hermes_tokens",0) > 0:
        token_info["mm_tokens"] = int(gov.get("hermes_tokens",0) + gov.get("openclaw_tokens",0))
        token_info["mm_cost"] = gov.get("hermes_cost",0) + gov.get("openclaw_cost",0)
    for n in AGENT_NAMES:
        token_info["agent_tokens"][n] = int(gov.get(f"{n}_tokens",0))
        token_info["agent_cost"][n] = round(gov.get(f"{n}_cost",0),4)

    # Timeline
    alerts = []
    for ev in obs.get("timeline",[])[:40]:
        pld = ev.get("payload",{})
        s = "running"
        if ev["type"].startswith("task.completed") or ev["type"]=="model.call.end": s="completed"
        elif "fail" in ev["type"] or ev.get("severity") in ("error","crit"): s="failed"
        alerts.append({
            "ts":ev.get("ts","")[:19].replace("T"," "),
            "system":ev.get("source","?"),"status":s,
            "task":f"{pld.get('task',pld.get('model',pld.get('tool',ev['type'])))}",
        })

    qs = get_queue_status()
    qd = get_queue_details()
    return {"agents":agents,"queue":qs,"queue_details":qd,"tokens":token_info,"alerts":alerts}

# ─── 后台线程 ───
def publisher_loop():
    """每 5s 推一次 get_full_status 到 Redis 频道 aios:sse:push.
    SSE 订阅者 (_sse) 会读到 augmented JSON."""
    import redis as _r
    r = _r.Redis(host='localhost',port=6379,socket_connect_timeout=2)
    while True:
        try:
            d = _augment_full_status(get_full_status())
            r.publish("aios:sse:push", json.dumps(d, ensure_ascii=False, default=str))
        except: pass
        time.sleep(5)

def heartbeat_daemon():
    import redis as _r
    r = _r.Redis(host='localhost',port=6379,socket_connect_timeout=2)
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()
            for n in AGENT_NAMES:
                if _find_agent_pid(n):
                    r.set(f"aios:bus:system:{n}:heartbeat",now,ex=300)
        except: pass
        time.sleep(15)

if __name__ == "__main__":
    port = 8080
    for i,a in enumerate(sys.argv):
        if a == "--port" and i+1 < len(sys.argv): port = int(sys.argv[i+1])
    init_registry()
    threading.Thread(target=publisher_loop,daemon=True).start()
    threading.Thread(target=heartbeat_daemon,daemon=True).start()
    print(f"🌐 AI Control Center V1: http://localhost:{port}")
    ThreadingServer((safe_bind_host(), port),Handler).serve_forever()
