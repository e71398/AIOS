#!/usr/bin/env python3
"""Manage replaceable AI tool chips through their adapter boundary."""
import argparse, json, os, subprocess, tempfile
from pathlib import Path

HOME=Path(os.getenv("AIOS_HOME","${AIOS_HOME}"))
CONFIG=HOME/"config/tool_adapters.json"
SERVICES={"opencode":"aios-executor-opencode.service","claude":"aios-executor-claude.service",
          "codex":"aios-executor-codex.service"}

def load():
 d=json.loads(CONFIG.read_text())
 if d.get("contract_version")!="1.0" or not isinstance(d.get("tools"),dict):
  raise ValueError("invalid adapter contract")
 return d
def save(d):
 fd,name=tempfile.mkstemp(prefix="tool-adapters-",suffix=".json",dir=CONFIG.parent)
 try:
  with os.fdopen(fd,"w") as f: json.dump(d,f,ensure_ascii=False,indent=2); f.write("\n")
  os.chmod(name,0o600); os.replace(name,CONFIG)
 finally:
  if os.path.exists(name): os.unlink(name)
def status():
 d=load(); out=[]
 for name,cfg in d["tools"].items():
  exe=Path(cfg.get("executable","")); unit=SERVICES.get(name); active=None
  if unit:
   p=subprocess.run(["systemctl","--user","is-active",unit],capture_output=True,text=True); active=p.stdout.strip()
  out.append({"chip":name,"role":cfg.get("role"),"enabled":cfg.get("enabled",False),
              "installed":exe.is_file(),"service":unit,"active":active})
 print(json.dumps({"contract_version":d["contract_version"],"chips":out},ensure_ascii=False,indent=2))
def set_enabled(name,enabled):
 d=load()
 if name not in d["tools"]: raise KeyError(name)
 if name in ("openclaw","hermes") and not enabled:
  raise ValueError(f"{name} has a protected architectural role; change requires contract review")
 d["tools"][name]["enabled"]=enabled; save(d)
 unit=SERVICES.get(name)
 if unit: subprocess.run(["systemctl","--user","start" if enabled else "stop",unit],check=True)
 print(json.dumps({"ok":True,"chip":name,"enabled":enabled,"service":unit}))

if __name__=="__main__":
 ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True); sub.add_parser("status")
 for cmd in ("enable","disable"): p=sub.add_parser(cmd); p.add_argument("chip")
 a=ap.parse_args(); status() if a.cmd=="status" else set_enabled(a.chip,a.cmd=="enable")
