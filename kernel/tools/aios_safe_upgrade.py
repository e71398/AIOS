#!/usr/bin/env python3
"""Atomic chip upgrade with validation, service health check and rollback."""
import argparse, hashlib, json, os, shutil, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

HOME=Path(os.getenv("AIOS_HOME","${AIOS_HOME}")).resolve()
HISTORY=HOME/"checkpoint/upgrades"
SERVICE_MAP={
 "aios_entry_gateway.py":"aios-entry-gateway.service",
 "aios_entry_feishu.py":"aios-feishu-entry.service",
 "aios_model_gateway.py":"aios-model-gateway.service",
 "aios_gateway_server.py":"aios-model-gateway.service",
 "aios_executor_daemon.py":None,
 "aios_verification_gate.py":"aios-verification-gate.service",
 "aios_result_push.py":"aios-result-push.service",
 "aios_web.py":"aios-web.service",
 "aios_monitor.py":"aios-monitor.service"}

def inside(path):
 p=Path(path).resolve()
 if HOME not in p.parents: raise ValueError("target must be inside AIOS_HOME")
 return p
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def validate(p):
 if p.suffix==".py": return subprocess.run(["python3","-m","py_compile",str(p)],capture_output=True,text=True)
 if p.suffix==".json":
  try: json.loads(p.read_text()); return subprocess.CompletedProcess([],0,"","")
  except Exception as e: return subprocess.CompletedProcess([],1,"",str(e))
 return subprocess.CompletedProcess([],0,"","")
def upgrade(target, candidate, service=None):
 target=inside(target); candidate=Path(candidate).resolve()
 if not target.is_file() or not candidate.is_file(): raise FileNotFoundError()
 v=validate(candidate)
 if v.returncode: raise RuntimeError("candidate validation failed: "+v.stderr)
 HISTORY.mkdir(parents=True,exist_ok=True)
 stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
 backup=HISTORY/f"{target.name}.{stamp}.bak"; shutil.copy2(target,backup)
 staged=target.with_name(target.name+".staged"); shutil.copy2(candidate,staged); os.replace(staged,target)
 unit=service if service is not None else SERVICE_MAP.get(target.name)
 ok=True; evidence="file validation passed"
 if unit:
  subprocess.run(["systemctl","--user","restart",unit],check=False)
  time.sleep(3)
  p=subprocess.run(["systemctl","--user","is-active",unit],capture_output=True,text=True)
  ok=p.stdout.strip()=="active"; evidence=p.stdout+p.stderr
 if not ok:
  shutil.copy2(backup,target)
  if unit: subprocess.run(["systemctl","--user","restart",unit],check=False)
  status="rolled_back"
 else: status="applied"
 record={"ts":datetime.now(timezone.utc).isoformat(),"target":str(target),"backup":str(backup),
         "old_sha256":sha(backup),"new_sha256":sha(target),"service":unit,"status":status,"evidence":evidence.strip()}
 (HISTORY/f"{target.name}.{stamp}.json").write_text(json.dumps(record,indent=2))
 print(json.dumps(record,ensure_ascii=False)); return ok
def rollback(record):
 data=json.loads(Path(record).read_text()); target=inside(data["target"]); backup=Path(data["backup"])
 if not backup.is_file(): raise FileNotFoundError(backup)
 shutil.copy2(backup,target); unit=data.get("service")
 if unit: subprocess.run(["systemctl","--user","restart",unit],check=True)
 print(json.dumps({"ok":True,"target":str(target),"restored":str(backup)}))

if __name__=="__main__":
 ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
 u=sub.add_parser("apply"); u.add_argument("target"); u.add_argument("candidate"); u.add_argument("--service")
 rb=sub.add_parser("rollback"); rb.add_argument("record")
 a=ap.parse_args(); raise SystemExit(0 if (upgrade(a.target,a.candidate,a.service) if a.cmd=="apply" else not rollback(a.record)) else 1)
