"""
Web Dashboard — FastAPI :8848
=============================
AIOS v4.0 Intelligence & Growth Center
"""
import sys, os, json
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from storage import get_items, get_stats, cleanup_old
from open_source_radar import scan as scan_opensource, get_trending
from global_intel_radar import scan as scan_intel, get_intel
from opportunity_radar import scan as scan_opportunity, get_opportunities

HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><title>AIOS 情报中心</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui;background:#0a0e14;color:#b3b9c5;padding:16px;max-width:1000px;margin:auto;font-size:14px}
h1{color:#39bae6;margin-bottom:4px}.sub{font-size:12px;color:#4d5974}
.nav{display:flex;gap:4px;margin:12px 0}
.nav button{padding:6px 16px;border:none;border-radius:4px;cursor:pointer;background:#1a3040;color:#b3b9c5;font-size:13px}
.nav button.active{background:#39bae6;color:#000}.card{background:#0f1920;border:1px solid #1a3040;border-radius:6px;padding:12px;margin:8px 0}
.card h3{color:#ff8f40;font-size:14px;margin-bottom:4px}.card p{font-size:13px;line-height:1.5}.card .meta{font-size:11px;color:#4d5974;margin-top:4px}
.dim{color:#4d5974}.hi{color:#39bae6}.warn{color:#ffb454}.ok{color:#7fd962}.err{color:#f26d78}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;margin-right:4px}
.tag-h{background:#1a3a2a;color:#7fd962}.tag-m{background:#3a2a1a;color:#ffb454}.tag-l{background:#3a1a1a;color:#f26d78}
a{color:#39bae6;text-decoration:none}a:hover{text-decoration:underline}
#content{min-height:300px}.refresh{font-size:11px;color:#4d5974;text-align:center;margin-top:12px}
</style></head><body>
<h1>🛰 AIOS 情报与增长中心</h1>
<p class="sub">开源增强 · 全球情报 · 机会发现 | 每6小时自动扫描</p>
<div class="nav">
 <button class="active" onclick="load('overview',event)">📊 总览</button>
 <button onclick="load('opensource',event)">📦 开源增强</button>
 <button onclick="load('intel',event)">🌍 全球情报</button>
 <button onclick="load('opportunity',event)">💡 机会发现</button>
 <button onclick="load('evolution',event)">🧬 进化提案</button>
</div>
<div id="content">加载中...</div>
<div class="refresh" id="refresh"></div>
<script>
const SRC_CN={github_trending:'GitHub热门',github_trending_weekly:'GitHub周榜',hackernews:'HackerNews',techcrunch_rss:'TechCrunch',arxiv_ai:'arXiv AI',lobsters_rss:'Lobsters',devto_startups:'DEV创业',producthunt:'ProductHunt'};
const CAT_CN={open_source:'开源项目',tech:'科技资讯',ai_research:'AI研究',opportunity:'商业机会'};
const API='/api/items?module=';
async function load(m,ev){
 if(ev){document.querySelectorAll('.nav button').forEach(b=>b.classList.remove('active'));ev.target.classList.add('active');}
 if(m==='overview'){
  const s=await fetch('/api/stats').then(r=>r.json());
  const c=await fetch('/api/cleanup/stats').then(r=>r.json());
  document.getElementById('content').innerHTML=`
   <div class="card"><h3>📊 数据总览</h3>
    <p>总条目: <b>${s.total||0}</b> | 新增: <b>${s.new||0}</b> | 归档: <b>${c.archives||0}</b></p>
    <p>低质条目: <b>${c.low_quality||0}</b></p>
   </div>
   <div class="card"><h3>⏱ 抓取频率</h3>
    <p>开源: 每12h | 情报: 每2h | 机会: 每6h | 清理: 每日</p>
   </div>
   <div class="card"><h3>🛡 风控规则</h3>
    <p>黑名单: 暴利承诺/拉人头/无来源/币圈喊单/标题党</p>
   </div>`;
 }else if(m==='evolution'){
  const p=await fetch('/api/evolution/proposals').then(r=>r.json());
  const s=await fetch('/api/evolution/status').then(r=>r.json());
  const h=p.map(x=>`<div class="card"><h3>${x.title}</h3>
   <p>${x.summary||''}</p><div class="meta">${x.proposal_id} | ${x.status} | ${x.risk}</div>
   ${x.status==='pending_approval'?`<button onclick="evolve('${x.proposal_id}','approve')">批准制作候选</button> <button onclick="evolve('${x.proposal_id}','reject')">拒绝</button>`:''}
   ${x.status==='awaiting_deploy_approval'?`<button class="warn" onclick="evolve('${x.proposal_id}','deploy')">二次确认部署</button>`:''}</div>`).join('');
  document.getElementById('content').innerHTML=`<div class="card"><h3>🧬 受控进化</h3><p>工具内部学习/升级自动进行；只有核心与适配器合同变更进入此处。自动部署：${s.auto_deploy?'是':'否'}</p></div>`+(h||'<p class="dim">暂无核心变更提案</p>');
 }else{
  const r=await fetch(API+m);
  const items=await r.json();
  const h=items.map(i=>`
  <div class="card">
   <h3><a href="${i.url}" target="_blank">${i.title||'?'}</a></h3>
   <p>${i.summary||'暂无摘要'}</p>
   <div class="meta">
    <span class="tag tag-${i.trust_level=='high'?'h':'m'}">${i.trust_level=='high'?'高可信':'中可信'}</span>
    <span>评分:${i.score||0} | ${SRC_CN[i.source]||i.source||'?'} | ${CAT_CN[i.category]||i.category||''} | ${(i.created_at||'').substring(0,10)}</span>
   </div>
  </div>`).join('');
  document.getElementById('content').innerHTML=h||'<p class="dim">暂无数据</p>';
 }
 document.getElementById('refresh').textContent='🕐 '+new Date().toLocaleTimeString()+' 刷新';
}
async function evolve(id,action){
 if(action==='deploy'&&!confirm('确认把已验证候选部署到AIOS？失败会自动回滚。'))return;
 if(action==='approve'&&!confirm('批准工具AI只在sandbox制作候选？'))return;
 const r=await fetch(`/api/evolution/${id}/${action}`,{method:'POST'});
 const body=await r.json(); if(!r.ok) alert(body.detail||JSON.stringify(body));
 load('evolution',null);
}
load('overview',null);
setInterval(()=>{const a=document.querySelector('.nav button.active');load(a.textContent.includes('总览')?'overview':a.textContent.includes('开源')?'opensource':a.textContent.includes('情报')?'intel':a.textContent.includes('机会')?'opportunity':a.textContent.includes('进化')?'evolution':'overview')},30000);
</script></body></html>"""

def make_app():
    from fastapi import FastAPI, Query
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn, threading

    app = FastAPI(title="AIOS Intel Center")
    evolution_path = Path("${AIOS_HOME}/kernel/centers/evolution_center")
    if str(evolution_path) not in sys.path: sys.path.insert(0, str(evolution_path))

    @app.get("/", response_class=HTMLResponse)
    def index(): return HTML

    @app.get("/health")
    def health():
        try:
            stats = get_stats()
            return {"ok": True, "service": "aios-intel", "database": True,
                    "items": stats.get("total", 0),
                    "last_fetch": stats.get("last_fetch"),
                    "fetch_failures_24h": stats.get("fetch_failures_24h", 0)}
        except Exception as exc:
            return JSONResponse({"ok": False, "service": "aios-intel",
                                 "database": False, "error": str(exc)[:200]}, status_code=503)

    @app.get("/api/items")
    def api_items(module: str = "opensource", limit: int = 30):
        if module == "opensource": items = get_trending(limit)
        elif module == "intel": items = get_intel(limit)
        elif module == "opportunity": items = get_opportunities(limit)
        else: items = get_items(limit=limit)
        return JSONResponse([{k: str(v) for k, v in i.items()} for i in items])

    @app.get("/api/stats")
    def api_stats(): return JSONResponse(get_stats())

    @app.get("/api/tools/health")
    def api_tools_health():
        import sys
        sys.path.insert(0, "${AIOS_HOME}/kernel/tools")
        from aios_tool_evolution import health_all
        tools = health_all()
        return JSONResponse({
            "ok": all(item.get("fully_operational") for item in tools.values()),
            "infrastructure_ok": all(item.get("infrastructure_ok") for item in tools.values()),
            "registered": len(tools),
            "tools": tools,
        })

    @app.post("/api/scan")
    def api_scan(module: str = "all"):
        results = {}
        if module in ("all","opensource"): results["opensource"] = scan_opensource()
        if module in ("all","intel"): results["intel"] = scan_intel()
        if module in ("all","opportunity"): results["opportunity"] = scan_opportunity()
        return JSONResponse(results)

    @app.get("/api/cleanup/stats")
    def api_cleanup_stats():
        from cleanup_engine import CleanupEngine
        c = CleanupEngine()
        return JSONResponse(c.get_stats())

    @app.post("/api/cleanup")
    def api_cleanup():
        cleanup_old(30)
        return JSONResponse({"status": "ok"})

    @app.get("/api/evolution/status")
    def evolution_status():
        from evolution_controller import status
        return JSONResponse(status())

    @app.get("/api/evolution/proposals")
    def evolution_proposals(status: str = ""):
        from evolution_controller import list_proposals
        return JSONResponse(list_proposals(status))

    @app.post("/api/evolution/review")
    def evolution_review():
        from evolution_controller import review
        return JSONResponse(review())

    @app.post("/api/evolution/{proposal_id}/approve")
    def evolution_approve(proposal_id: str):
        from fastapi import HTTPException
        from evolution_controller import approve
        try: return JSONResponse(approve(proposal_id, "local-owner-ui"))
        except Exception as exc: raise HTTPException(400, str(exc))

    @app.post("/api/evolution/{proposal_id}/reject")
    def evolution_reject(proposal_id: str):
        from fastapi import HTTPException
        from evolution_controller import reject
        try: return JSONResponse(reject(proposal_id, "owner rejected in 8848", "local-owner-ui"))
        except Exception as exc: raise HTTPException(400, str(exc))

    @app.post("/api/evolution/{proposal_id}/deploy")
    def evolution_deploy(proposal_id: str):
        from fastapi import HTTPException
        from evolution_controller import deploy
        try: return JSONResponse(deploy(proposal_id, f"DEPLOY:{proposal_id}", "local-owner-ui"))
        except Exception as exc: raise HTTPException(400, str(exc))

    return app

if __name__ == "__main__":
    import uvicorn, sys, os
    sys.path.insert(0, "${AIOS_HOME}/kernel/tools")
    try:
        from aios_secure import safe_bind_host
        bind = safe_bind_host()
    except Exception:
        bind = "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19090
    print(f"🛰 Intel Center → http://{bind}:{port}")
    uvicorn.run(make_app(), host=bind, port=port, log_level="warning")
