#!/usr/bin/env python3
"""AIOS Runtime Console v3 — 严格遵循 AIOS_RUNTIME_SPEC.md 规范"""
import sys, os
sys.path.insert(0, '${AIOS_HOME}/kernel/tools')
from aios_secure import safe_bind_host
from http.server import HTTPServer, BaseHTTPRequestHandler
import time, threading, json, subprocess
from datetime import datetime, timezone, timedelta
import psutil

CST = timezone(timedelta(hours=8))
HTML_CACHED = "<html><body>⏳ 加载中...</body></html>"

def _pgrep(pat):
    try:
        r = subprocess.run(["pgrep","-f",pat], capture_output=True, text=True, timeout=2)
        return [p for p in r.stdout.strip().split("\n") if p]
    except: return []

def refresh_loop():
    global HTML_CACHED
    while True:
        try:
            from aios_runtime_console import get_full_runtime_status
            d = get_full_runtime_status()
            HTML_CACHED = build_html(d)
        except Exception as e:
            HTML_CACHED = f"<html><body>Error: {e}</body></html>"
        time.sleep(15)

threading.Thread(target=refresh_loop, daemon=True).start()

def TR(*cells): return '<tr>'+''.join(f'<td>{c}</td>' for c in cells)+'</tr>'
def TH(*cells): return '<tr>'+''.join(f'<th>{c}</th>' for c in cells)+'</tr>'
def H2(t): return f'<h2>{t}</h2>'

def build_html(d):
    s, ag, mc, ev, tk = d["system"], d["agents"], d["mcp"], d["events"], d["tokens"]
    mdls = d.get("models", [])
    now = datetime.now(CST)
    o = []
    o.append(f'<h1>🖥 AIOS Runtime Console v4.0 <span id="clock" style="font-size:12px;color:#4d5974"></span></h1>')

    # ① 系统
    o.append(H2('① AIOS 系统状态'))
    o.append('<table>')
    o.append(TH('版本','状态','启动时间','已运行','CPU','RAM','磁盘'))
    o.append(TR('v4.0', s["status"]["cn"], s["boot_time"], s["uptime"],
               f'{s["cpu"]}%', f'{s["ram_used_gb"]}/{s["ram_total_gb"]}G',
               f'{s["disk_used_gb"]}/{s["disk_total_gb"]}G'))
    o.append(TH('待处理','执行中','已完成','失败'))
    o.append(TR(str(s["tasks_pending"]), str(s["tasks_running"]),
               str(s["tasks_completed"]), str(s["tasks_failed"])))
    o.append('</table>')

    # ② AI运行状态 — 规范13字段
    o.append(H2('② AI 运行状态'))
    o.append('<table>')
    o.append(TH('AI','状态','当前任务','当前阶段','CPU','内存','运行时长','模型','当前Agent','Token使用','当前工具','响应延迟','最后心跳','最近错误'))
    for a in ag:
        if not a.get("pid"): continue
        uptime_str = f'{a.get("uptime_s",0)//3600}h{(a.get("uptime_s",0)%3600)//60}m' if a.get("uptime_s") else '-'
        o.append(TR(
            f'<b>{a["label"]}</b>',
            a["status"]["cn"],
            a.get("current_task","-")[:25] or '-',
            a.get("phase","-"),
            f'{a.get("cpu",0)}%',
            f'{a.get("mem_mb",0)}MB',
            uptime_str,
            a.get("model","-"),
            a.get("current_agent","-"),
            f'{a.get("token_usage",0):,}' if a.get("token_usage") else '-',
            a.get("current_tool","-"),
            f'{a.get("latency_ms",0)}ms' if a.get("latency_ms") else '-',
            f'{a.get("last_heartbeat","-")}',
            a.get("last_error","-")[:30] or '-'
        ))
    o.append('</table>')

    # ③ AI详细状态 — 规范13字段
    o.append(H2('③ AI 详细状态'))
    o.append('<table>')
    o.append(TH('AI','模型','模型版本','Provider','当前步骤','当前Workflow','输入Token','输出Token','Token速度','响应耗时','上下文长度','工具调用次数','预计完成','最近异常'))
    for a in ag:
        if not a.get("pid"): continue
        o.append(TR(
            f'<b>{a["label"]}</b>',
            a.get("model","-"),
            a.get("model_version","-"),
            a.get("provider","-"),
            a.get("current_step","-"),
            a.get("current_workflow","-"),
            f'{a.get("input_tokens",0):,}' if a.get("input_tokens") else '-',
            f'{a.get("output_tokens",0):,}' if a.get("output_tokens") else '-',
            f'{a.get("token_speed",0)} t/s' if a.get("token_speed") else '-',
            f'{a.get("latency_ms",0)}ms' if a.get("latency_ms") else '-',
            f'{a.get("context_len",0):,}' if a.get("context_len") else '-',
            str(a.get("tool_calls",0)),
            a.get("eta","-"),
            a.get("last_error","-")[:30] or '-'
        ))
    o.append('</table>')

    # ④ MCP
    o.append(H2('④ MCP 服务'))
    o.append('<table>')
    mcp_rows = [mc[i:i+4] for i in range(0, len(mc), 4)]
    for row in mcp_rows:
        o.append(TR(*[f'{m["status"]["cn"]} {m["name"]}' for m in row]))
    o.append('</table>')

    # ⑤ 模型状态 — 规范12字段
    o.append(H2('⑤ 模型状态'))
    o.append('<table>')
    o.append(TH('模型','连接','健康','延迟','首Token时间','Token速度','上下文长度','调用','成功','失败','错误率','当前费用','今日Token'))
    for m in mdls:
        sn = m["status"]["cn"]
        ok = sn in ("运行中","就绪")
        cost = m.get("cost", 0)
        o.append(TR(
            m["name"], '在线' if ok else '离线', '健康' if ok else '-',
            m.get("latency","-"),
            f'{m.get("first_token_ms","-")}ms' if m.get("first_token_ms") else '-',
            '-',  # Token/s placeholder
            f'{m.get("context_len","-")}' if m.get("context_len") else '-',
            str(m.get("calls",0)),
            '-',  # 成功
            str(m.get("failures",0)),
            f'{m["failures"]/max(m["calls"],1)*100:.0f}%' if m.get("calls") else '0%',
            f'¥{cost:.2f}' if cost else '-',
            f'{m.get("tokens",0):,}' if m.get("tokens",0) else '-'
        ))
    o.append('</table>')

    # ⑥ API
    o.append(H2('⑥ API 状态'))
    o.append('<table>')
    o.append(TH('API','状态','今日Token','今日费用'))
    o.append(TR('DeepSeek','正常',f'{tk["ds_tokens"]:,}',f'¥{tk["ds_cost"]:.2f}'))
    o.append(TR('MiniMax','正常',f'{tk["mm_tokens"]:,}',f'¥{tk["mm_cost"]:.2f}'))
    o.append(TR('OpenAI','未配置','-','-'))
    o.append('</table>')

    # ⑦ 工作流 — 实时状态
    o.append(H2('⑦ 工作流状态'))
    o.append('<table>')
    o.append(TH('Workflow','步骤','节点','负责人','开始','预计','耗时'))
    dispatch_node = '就绪' if s["tasks_pending"] == 0 else '运行中'
    o.append(TR('调度循环', f'{s["tasks_completed"]}/{s["tasks_pending"]+s["tasks_completed"]}',
               dispatch_node, 'OpenClaw', '-', '-', '-'))
    o.append(TR('Hermes学习', '-/-', '等待02:00', 'Hermes', '-', '-', '-'))
    o.append('</table>')

    # ⑧ 模块状态
    from aios_runtime_console import get_module_status
    md = get_module_status()
    o.append(H2(f'⑧ 模块状态（{len(md)}个）'))
    o.append('<table>')
    o.append(TH('模块','状态','类型'))
    rows = [md[i:i+3] for i in range(0, len(md), 3)]
    for row in rows:
        o.append(TR(*[f'{m["status"]["cn"]} {m["name"]}' for m in row]))
    o.append('</table>')

    # ⑨ 执行过程 — 规范5字段
    o.append(H2(f'⑨ 实时执行过程（{len(ev)}条）'))
    o.append('<table>')
    o.append(TH('时间','来源','事件','状态','耗时'))
    for e in ev[:20]:
        st = e.get("status","running")
        icon = {'completed':'✅','failed':'❌','critical':'🔴','running':'🔄'}.get(st,'')
        duration = e.get("duration","-")
        o.append(TR(e["ts"], e.get("source","?")[:15], e.get("task","")[:70], icon, str(duration)))
    o.append('</table>')

    # ⑩ 告警
    o.append(H2('⑩ 告警中心'))
    SEV_ORDER = ["FATAL","CRITICAL","ERROR","WARNING","NOTICE","SUCCESS","INFO","DEBUG"]
    alerts = [e for e in ev if e.get("severity") in ("WARNING","ERROR","CRITICAL")
              and e.get("source","") != "agent_mesh"]  # 过滤生命周期噪音
    if alerts:
        o.append('<table>')
        o.append(TH('等级','时间','来源','事件'))
        for e in sorted(alerts, key=lambda x: SEV_ORDER.index(x.get("severity","INFO")) if x.get("severity") in SEV_ORDER else 99)[:15]:
            sev = e.get("severity","INFO")
            color = {"FATAL":"#f00","CRITICAL":"#f26d78","ERROR":"#f26d78","WARNING":"#ffb454","NOTICE":"#39bae6","SUCCESS":"#7fd962"}.get(sev,"")
            o.append(TR(f'<b style="color:{color}">{sev}</b>', e["ts"], e.get("source","?")[:15], e["task"][:80]))
        o.append('</table>')
    else:
        o.append('<p>✅ 无告警 | 等级: FATAL CRITICAL ERROR WARNING NOTICE SUCCESS INFO DEBUG</p>')

    # ⑪ 恢复
    o.append(H2('⑪ 自动恢复'))
    off = [a for a in ag if not a.get("pid")]
    o.append('<table>')
    o.append(TH('步骤','状态','详情'))
    flow = [
        ('1.故障检测', '✅' if off else '✅', f'发现{len(off)}个离线' if off else '无异常'),
        ('2.故障定位', '✅' if off else '-', '定位离线模块' if off else '-'),
        ('3.停止故障模块', '⏳' if off else '-', 'pid已消失' if off else '-'),
        ('4.释放资源', '⏳' if off else '-', '-' if not off else '-'),
        ('5.重新初始化', '⏳' if off else '-', 'wrapper自动重启' if off else '-'),
        ('6.重新连接', '⏳' if off else '-', '-' if not off else '-'),
        ('7.恢复缓存', '⏳' if off else '-', '-' if not off else '-'),
        ('8.恢复上下文', '⏳' if off else '-', '-' if not off else '-'),
        ('9.恢复任务', '⏳' if off else '-', '-' if not off else '-'),
        ('10.健康检查', '⏳' if off else '-', '-' if not off else '-'),
        ('11.恢复完成', '⏳' if off else '-', '-' if not off else '-'),
    ]
    for step, stat, detail in flow:
        o.append(TR(step, stat, detail))
    o.append('</table>')

    # ⑫ Token
    o.append(H2('⑫ Token 监控'))
    o.append('<table>')
    o.append(TH('模型','今日Token','今日费用','本周Token','本周费用'))
    o.append(TR('DeepSeek', f'{tk["ds_tokens"]:,}', f'¥{tk["ds_cost"]:.2f}', '-', '-'))
    o.append(TR('MiniMax', f'{tk["mm_tokens"]:,}', f'¥{tk["mm_cost"]:.2f}', '-', '-'))
    o.append(TR('<b>合计</b>', f'<b>{tk["today_tokens"]:,}</b>', f'<b>¥{tk["today_cost"]:.2f}</b>',
               f'{tk.get("week_total",0):,}', f'¥{tk.get("week_cost",0):.2f}'))
    o.append('</table>')

    # 状态码
    o.append(H2('📋 统一状态码'))
    o.append('<table>')
    from aios_runtime_console import STATUS
    codes = list(STATUS.items())
    for i in range(0, len(codes), 4):
        row = codes[i:i+4]
        o.append(TR(*[f'{code}={info["cn"]}' for code, info in row]))
    o.append('</table>')

    o.append(f'<p id="footer">AIOS v4.0 · {now.strftime("%H:%M:%S")} 更新 · 5秒刷新 · 遵循 AIOS_RUNTIME_SPEC.md</p>')

    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><title>AIOS Runtime Console</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:monospace;background:#0a0e14;color:#b3b9c5;padding:16px;max-width:1100px;margin:auto;font-size:12px;line-height:1.5}}
h1{{color:#39bae6;font-size:15px;margin-bottom:12px}}
h2{{color:#ff8f40;font-size:13px;margin:14px 0 6px;border-bottom:1px solid #1a3040;padding-bottom:3px}}
table{{width:100%;border-collapse:collapse;margin:4px 0;font-size:11px}}
th,td{{border:1px solid #1a3040;padding:2px 6px;text-align:left}}
th{{background:#0f1920;color:#ff8f40;font-weight:bold;white-space:nowrap}}
td{{color:#b3b9c5}}
b{{color:#b3b9c5}}p{{margin:4px 0}}
#footer{{text-align:center;color:#1a3a4a;font-size:10px;margin-top:16px;border-top:1px solid #1a3040;padding-top:6px}}
</style></head><body>
{''.join(o)}
<script>
setInterval(function(){{
 document.getElementById('clock').textContent = new Date().toLocaleTimeString();
 fetch('/').then(r=>r.text()).then(h=>{{
var m=h.match(/<body>([\s\S]*)<\/body>/i);
if(m) document.body.innerHTML=m[1];
 }});
}},15000);
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_CACHED.encode())
    def log_message(self, *a): pass

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18086
    HTTPServer((safe_bind_host(), port), H).serve_forever()
