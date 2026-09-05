import sys
sys.path.insert(0, "${AIOS_HOME}/kernel/tools")
from aios_secure import safe_bind_host
#!/usr/bin/env python3
"""AIOS Task Console — 提交任务 + 上传文件 (精简版 Dashboard)"""
import json, sys, os, re, subprocess
from pathlib import Path
from socketserver import ThreadingMixIn
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import init_registry

class ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

HTML = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<title>AIOS Task Console</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px;max-width:800px;margin:auto}
h1{color:#58a6ff;margin-bottom:5px;font-size:22px}
h2{color:#f0883e;margin:20px 0 10px;font-size:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:20px;margin:12px 0}
.dim{color:#8b949e}
.green{color:#3fb950}.red{color:#f85149}.blue{color:#58a6ff}
form textarea{width:100%;padding:10px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;resize:vertical;min-height:80px;font-size:14px;font-family:inherit}
form textarea:focus{border-color:#58a6ff;outline:none}
.upload-zone{border:2px dashed #30363d;border-radius:8px;padding:30px 20px;text-align:center;cursor:pointer;margin:12px 0;transition:border-color .3s;color:#8b949e}
.upload-zone:hover,.upload-zone.dragover{border-color:#58a6ff;background:#1a2332;color:#58a6ff}
.upload-zone input{display:none}
.file-list{list-style:none;padding:0;margin:8px 0}
.file-list li{background:#21262d;padding:6px 12px;margin:4px 0;border-radius:4px;font-size:13px;display:flex;justify-content:space-between;align-items:center}
.file-list .rm{cursor:pointer;color:#f85149;font-weight:bold}
.badge{font-size:10px;padding:2px 8px;border-radius:3px;margin-left:6px}
.badge-text{background:#238636}.badge-img{background:#d2991d}.badge-video{background:#f85149}.badge-doc{background:#58a6ff}.badge-other{background:#8b949e}
.btn{padding:10px 24px;background:#238636;border:none;color:white;border-radius:6px;cursor:pointer;font-size:14px;font-weight:600;float:right}
.btn:hover{background:#2ea043}
.btn:disabled{opacity:.5;cursor:not-allowed}
#status{font-size:13px;display:inline-block;margin-top:10px}
.submit-row{display:flex;justify-content:space-between;align-items:center;margin-top:10px}
.footer{margin-top:30px;text-align:center;font-size:12px;color:#8b949e;border-top:1px solid #30363d;padding-top:12px}
.help-text{font-size:13px;color:#8b949e;margin-bottom:8px}
</style></head><body>
<h1>🎯 AIOS Task Console</h1>
<p class="dim" style="margin-bottom:8px">提交任务或上传文件给 AI Agent 执行</p>

<div class="card" style="margin-top:12px">
<h2>📚 知识导入 <span class="dim" style="font-size:12px">— 导入到AIOS共享知识库</span></h2>
<div class="help-text">粘贴文本、导入URL、扫描外部目录，让5个AI共享知识。</div>
<div class="flex gap" style="flex-wrap:wrap;align-items:end">
  <div style="flex:1;min-width:200px">
    <textarea id="kbText" placeholder="粘贴要导入的知识文本..." rows="2" style="width:100%"></textarea>
  </div>
  <div style="min-width:180px">
    <input id="kbUrl" type="text" placeholder="或输入网页/文档URL" style="width:100%;font-size:13px">
  </div>
  <button onclick="importKnowledge('text')" class="btn" style="font-size:12px;padding:6px 12px;background:#1a3a5a">📚 导入知识</button>
  <button onclick="importKnowledge('scan')" class="btn" style="font-size:12px;padding:6px 12px;background:#238636">🔍 扫描AI会话</button>
</div>
<div id="kb-msg" style="font-size:12px;color:var(--green);margin-top:6px;min-height:18px"></div>
</div>
<form id="taskForm" enctype="multipart/form-data">
<h2>📝 任务描述</h2>
<div class="help-text">输入你要 AI 执行的任务，或上传文件让 AI 处理。</div>
<textarea name="text" placeholder="例如：分析 sandbox/coding/uploads 目录下的最新文件，总结内容要点..." rows="3"></textarea>

<h2 style="margin-top:16px">📎 上传文件</h2>
<div class="upload-zone" id="dropZone">
  📁 拖拽文件到此处，或点击选择
  <input type="file" id="fileInput" name="files" multiple accept=".txt,.py,.json,.csv,.yaml,.md,.sh,.png,.jpg,.jpeg,.gif,.pdf,.docx,.xlsx,.mp4,.avi,.mov,.mkv">
</div>
<ul class="file-list" id="fileList"></ul>

<div class="submit-row">
  <span id="status" class="dim"></span>
  <input type="submit" value="🚀 提交任务" class="btn" id="submitBtn">
</div>
</form></div>

<div id="result" class="card" style="display:none"></div>

<p class="footer">AIOS v4.0 · Task Console</p>
<script>
const dz=document.getElementById('dropZone'),fi=document.getElementById('fileInput'),fl=document.getElementById('fileList');
const tf=document.getElementById('taskForm'),st=document.getElementById('status'),sb=document.getElementById('submitBtn');
const rs=document.getElementById('result');
let files=[];

['dragenter','dragover','dragleave','drop'].forEach(e=>document.addEventListener(e,e=>e.preventDefault()));
dz.addEventListener('click',()=>fi.click());
['dragenter','dragover'].forEach(e=>dz.addEventListener(e,()=>dz.classList.add('dragover')));
['dragleave','drop'].forEach(e=>dz.addEventListener(e,()=>dz.classList.remove('dragover')));
dz.addEventListener('drop',e=>addFiles(e.dataTransfer.files));
fi.addEventListener('change',e=>addFiles(e.target.files));

function badge(name){
  const ext=name.split('.').pop().toLowerCase();
  const m={'badge-text':['txt','py','json','csv','yaml','md','sh'],'badge-img':['png','jpg','jpeg','gif','bmp','svg'],'badge-video':['mp4','avi','mov','mkv','webm'],'badge-doc':['pdf','docx','xlsx','pptx']};
  for(const[k,v]of Object.entries(m))if(v.includes(ext))return'<span class="badge '+k+'">'+k.split('-')[1]+'</span>';
  return'<span class="badge badge-other">other</span>';
}
function addFiles(nf){for(let f of nf){if(!files.find(x=>x.name===f.name)){files.push(f);fl.innerHTML+='<li>'+f.name+badge(f.name)+' <span class=rm onclick="this.parentElement.remove();files=files.filter(x=>x.name!==\''+f.name+'\')">✕</span></li>'}}}

tf.addEventListener('submit',async e=>{
  e.preventDefault();
  const text=tf.querySelector('textarea').value.trim();
  if(!text&&files.length===0){alert('请输入任务描述或上传文件');return}
  const fd=new FormData();
  fd.append('text',text||'处理上传的文件');
  files.forEach(f=>fd.append('files',f));
  sb.disabled=true; sb.value='⏳ 提交中...';
  st.textContent='⏳ 上传中...';
  rs.style.display='none';
  try{
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    const d=await r.json();
    if(d.ok){
      st.innerHTML='✅ 已提交! 创建 '+d.count+' 个子任务';
      rs.style.display='block';
      var taskIds=d.task_ids||[];
      var resultHtml='<div style="text-align:center;padding:10px"><div style="font-size:40px;margin-bottom:8px">⏳</div><div style="font-size:16px;font-weight:600;color:#d2991d">任务执行中...</div><div class="dim" style="margin-top:4px">创建 '+d.count+' 个子任务</div>'+(d.files&&d.files.length?'<div class="dim" style="margin-top:4px">文件: '+d.files.join(', ')+'</div>':'')+'<div id="task-results" class="dim" style="margin-top:8px;font-size:12px"></div></div>';
      rs.innerHTML=resultHtml;
      files=[]; fl.innerHTML=''; tf.querySelector('textarea').value='';
      // 轮询任务结果
      if(taskIds.length>0){
        var pollCount=0;
        var poller=setInterval(function(){
          pollCount++;
          // [FIX] 2026-08-12 entry/web-result-display: the
          // previous 15-iteration cap stopped the page from
          // showing the final user-facing result for any task
          // that took longer than ~30s.  Bounded poll to 60
          // iterations at 3s (180s total) which covers every
          // current production task end-to-end.  The page also
          // reuses the single taskId for the whole poll (no
          // duplicate POST) and reads the new ``task.result``
          // field returned by the augmented /api/task/<id>
          // endpoint so internal ``[executor]`` / sandbox
          // boilerplate is stripped server-side.
          if(pollCount>60){clearInterval(poller);document.getElementById('task-results').innerHTML='\u23f3 \u4ecd\u5728\u6267\u884c,\u5237\u65b0\u9875\u9762\u7ee7\u7eed\u67e5\u770b (task '+taskIds[0]+')';return}
          fetch('/api/task/'+taskIds[0]).then(function(r){return r.json()}).then(function(td){
            if(td.task&&(td.task.status==='completed'||td.task.status==='failed'||td.task.status==='blocked')){
              clearInterval(poller);
              var icon=td.task.status==='completed'?'\u2705':(td.task.status==='failed'?'\u274c':'\u26a0\ufe0f');
              var resultText=td.task.result||td.task.result_summary||'(no result)';
              document.getElementById('task-results').innerHTML=icon+' <b>'+td.task.status+'</b><br><span style="color:#c9d1d9;white-space:pre-wrap">'+resultText.substring(0,2000)+'</span>';
            }else if(td.task){
              document.getElementById('task-results').innerHTML='\u23f3 '+td.task.status+'... ('+pollCount*3+'s)';
            }
          }).catch(function(){});
        },3000);
      }
    }else{
      st.innerHTML='❌ 提交失败';
      rs.style.display='block';
      rs.innerHTML='<div style="text-align:center;padding:10px;color:#f85149">❌ 提交失败</div>';
    }
  }catch(err){
    st.textContent='❌ 网络错误';
    rs.style.display='block';
    rs.innerHTML='<div style="text-align:center;padding:10px;color:#f85149">❌ 网络请求失败，请重试</div>';
  }
  sb.disabled=false; sb.value='🚀 提交任务';
});

async function importKnowledge(action){
  const msg=document.getElementById('kb-msg');
  const text=document.getElementById('kbText').value.trim();
  const url=document.getElementById('kbUrl').value.trim();
  msg.style.color='var(--yellow)'; msg.textContent='⏳ 导入中...';
  let body='';
  if(action==='text'&&text){body='action=text&text='+encodeURIComponent(text)}
  else if(action==='url'&&url){body='action=url&url='+encodeURIComponent(url)+'&title='+encodeURIComponent(url)}
  else if(action==='scan'){body='action=scan'}
  else{msg.style.color='var(--red)';msg.textContent='请输入内容';return}
  try{
    const r=await fetch('/api/knowledge/import',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});
    const d=await r.json();
    if(d.ok||d.total_imported!==undefined){
      msg.style.color='var(--green)';
      msg.textContent='✅ 导入完成: '+((d.total_imported||d.imported||0)+'条');
    }else{msg.style.color='var(--red)';msg.textContent='❌ '+d.error}
  }catch(e){msg.style.color='var(--red)';msg.textContent='❌ 网络错误'}
}
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json({"ok": True, "service": "aios-task-console"})
        elif path.startswith("/api/task/"):
            tid = path.split("/")[-1]
            try:
                from aios_bus import get_task_state
                state = get_task_state(tid)
                if not state:
                    # [FIX] 2026-08-12 entry/web-result-display
                    #   (continuation): the previous handler only
                    #   consulted ``get_task_state`` which reads
                    #   ``aios:bus:state:<id>``.  For parent tasks
                    #   submitted by ``/api/task`` the child state
                    #   hash may not exist (the child executor has
                    #   not yet run, or the task is still queued)
                    #   while the orchestrator workflow hash
                    #   ``aios:orchestrator:workflow:<id>`` is
                    #   already published.  Fall back to that hash
                    #   for the polling endpoint so the Web frontend
                    #   can display a real status during the
                    #   ``planning`` / ``running`` window.
                    try:
                        from aios_bus import _redis_client as _rcl
                        wf = _rcl.hgetall(
                            f"aios:orchestrator:workflow:{tid}")
                        if wf:
                            state = {k.decode() if isinstance(k, bytes) else k:
                                     (v.decode() if isinstance(v, bytes) else v)
                                     for k, v in wf.items()}
                    except Exception:
                        state = {}
                state = state or {}
                # [FIX] 2026-08-12 entry/web-result-display:
                #   the previous /api/task/<id> only returned the
                #   ``get_task_state`` (child) view, so a child
                #   ``result_summary`` leaked its ``[opencode]``
                #   prefix to the Web frontend.  Augment the
                #   response with a cleaned, user-facing ``result``
                #   text (strip executor tags and AIOS sandbox
                #   boilerplate) and a normalised ``terminal``
                #   flag.  No new database or state machine is
                #   introduced; everything is computed from the
                #   already-published bus state.
                import re as _re
                rs = str(state.get("result_summary") or "")
                rs = _re.sub(r"^\s*\[(opencode|codex|claude|hermes)\]\s*", "", rs)
                rs = _re.sub(
                    r"\n+##\s*(System metadata|Acceptance summary|"
                    r"Assigned node|Evidence mode|Bounded repair|"
                    r"SANDBOX CONSTRAINT|FACT-USE|AUTHORITATIVE HOST "
                    r"EVIDENCE|Trusted correction|Copy requested|"
                    r"Sandbox)[\\s\\S]*",
                    "", rs, flags=_re.IGNORECASE,
                )
                # For parent workflow view, normalise the most useful
                # ``result`` field to whatever the orchestrator has
                # persisted (usually the aggregated child deliverable).
                if not rs.strip():
                    rs = str(state.get("final_result") or state.get("result") or "")
                state["result"] = rs.strip()
                state["terminal"] = str(state.get("status", "")).lower() in {
                    "completed", "failed", "blocked", "cancelled", "canceled"
                }
                self._json({"ok": True, "task": state})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        else:
            self._html()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/task":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            text = parse_qs(body).get("text", [""])[0]
            if text.strip():
                try:
                    import json as _j, urllib.request as _u
                    data = _j.dumps({"input": text, "source": "web", "sender": "task_console"}).encode()
                    req = _u.Request("http://localhost:18801/task", data=data,
                                     headers={"Content-Type": "application/json"})
                    resp = _u.urlopen(req, timeout=10)
                    result = _j.loads(resp.read())
                    self._json(result)
                except Exception as e:
                    self._json({"error": f"gateway_unreachable: {e}"}, 500)
            else:
                self._json({"error": "empty"}, 400)
        elif path == "/api/upload":
            self._handle_upload()
        elif path == "/api/knowledge/import":
            self._handle_knowledge_import()
        else:
            self._json({"error": "not found"}, 404)

    def _handle_knowledge_import(self):
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode(errors="ignore")
        try:
            params = parse_qs(body)
            action = params.get("action", ["text"])[0]
            if action == "url":
                url = params.get("url", [""])[0]
                title = params.get("title", [url])[0]
                from aios_knowledge_importer import scan_url
                result = scan_url(url, title)
                self._json(result)
            elif action == "dir":
                directory = params.get("dir", [""])[0]
                from aios_knowledge_importer import scan_directory
                result = scan_directory(directory)
                self._json(result)
            elif action == "scan":
                from aios_knowledge_importer import scan_all_sources
                result = scan_all_sources(max_per_source=20)
                self._json(result)
            else:
                text = params.get("text", [""])[0]
                if text.strip():
                    from aios_semantic_search import index_document
                    index_document("manual", text[:60], text)
                    self._json({"ok": True, "action": "text", "imported": 1})
                else:
                    self._json({"error": "empty"}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_upload(self):
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        AIOS_HOME = TOOLS.parent.parent
        SANDBOX = AIOS_HOME / "sandbox" / "coding" / "uploads"
        SANDBOX.mkdir(parents=True, exist_ok=True)
        body = self.rfile.read(length)
        boundary = ctype.split("boundary=")[1] if "boundary=" in ctype else ""
        if not boundary:
            self._json({"error": "no boundary"}, 400); return
        parts = body.split(b"--" + boundary.encode())
        text, saved = "", []
        for part in parts:
            if b"filename=" in part:
                hdr_end = part.find(b"\r\n\r\n")
                if hdr_end < 0: continue
                fname = re.search(rb'filename="([^"]+)"', part[:hdr_end])
                if fname:
                    fn = Path(fname.group(1).decode()).name  # 防路径穿越
                    if not fn: continue
                    content = part[hdr_end+4:].rstrip(b"\r\n--").rstrip(b"\r\n")
                    if content:
                        (SANDBOX / fn).write_bytes(content)
                        saved.append(fn)
            elif b'name="text"' in part:
                hdr_end = part.find(b"\r\n\r\n")
                if hdr_end >= 0:
                    text = part[hdr_end+4:].decode(errors="ignore").strip().rstrip("--").strip()
        if not saved and not text.strip():
            self._json({"error": "empty"}, 400); return
        task_desc = text or f"处理上传文件: {', '.join(saved)}"
        if saved:
            task_desc += f" (文件在 {SANDBOX}/)"
        try:
            import json as _j, urllib.request as _u
            data = _j.dumps({"input": task_desc, "source": "web", "sender": "task_console"}).encode()
            req = _u.Request("http://localhost:18801/task", data=data,
                             headers={"Content-Type": "application/json"})
            resp = _u.urlopen(req, timeout=10)
            result = _j.loads(resp.read())
            result["files"] = saved
            self._json(result)
        except Exception as e:
            self._json({"error": f"gateway_unreachable: {e}"}, 500)

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def log_message(self, *a): pass

if __name__ == "__main__":
    port = 8080
    for i, a in enumerate(sys.argv):
        if a == "--port" and i+1 < len(sys.argv):
            port = int(sys.argv[i+1])
    try:
        init_registry()
    except Exception:
        print("⚠️ Redis 不可用, 服务降级运行")
    print(f"🎯 AIOS Task Console: http://localhost:{port}")
    print(f"   POST /api/task  — 提交任务")
    print(f"   POST /api/upload — 上传文件")
    try:
        ThreadingServer((safe_bind_host(), port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"❌ server fatal: {e}")
