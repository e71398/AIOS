#!/usr/bin/env python3
"""Verified AIOS backup and non-destructive recovery drill."""
import argparse, hashlib, json, os, shutil, subprocess, tarfile, tempfile
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.getenv("AIOS_HOME", "${AIOS_HOME}"))
DEST = HOME / "checkpoint/verified"
ALERTS = HOME / "logs/alerts"
INCLUDE_DIRS = ["agents", "config", "core", "docs", "kernel", "knowledge", "extensions"]
INCLUDE_FILES = [
    ".gitignore", "ai-launcher.sh", "install.sh", "recover_config.sh", "start.sh", "stop.sh",
    "docker-compose.yml", "docker-compose.langfuse.yml", "pyproject.toml", "setup.py",
    "redis.conf", "CHANGELOG.md", "ITERATE.md", "MEMORY.md", "SECURING.md",
    "SECURITY_DEFAULTS.md", "AIOS_INTEL_SPEC.md", "AIOS_MODULE_REFERENCE.md",
    "AIOS_MULTI_AI_CONSTRAINTS.md", "AIOS_OPERATIONS_GUIDE.md", "AIOS_PORT_GUIDE.md",
    "AIOS_RUNTIME_SPEC.md", "AIOS_SYSTEM_OVERVIEW.md", "AIOS_USER_GUIDE.md",
]

def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024), b""): h.update(block)
    return h.hexdigest()

def create():
    DEST.mkdir(parents=True, exist_ok=True)
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    stage=Path(tempfile.mkdtemp(prefix="aios-backup-")) / "payload"
    stage.mkdir(parents=True)
    try:
        subprocess.run(["redis-cli", "SAVE"], check=True, capture_output=True)
        for rel in INCLUDE_DIRS:
            src=HOME/rel
            if src.exists():
                ignored = ["__pycache__", "*.pyc", "*.pyo"]
                if rel == "extensions":
                    ignored.extend(["venv", "node_modules", "bin", ".git"])
                shutil.copytree(src, stage/rel, dirs_exist_ok=True, symlinks=True,
                                ignore=shutil.ignore_patterns(*ignored))
        for rel in INCLUDE_FILES:
            src=HOME/rel
            if src.is_file():
                dst=stage/rel; dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        # redis-cli --rdb streams a consistent copy without requiring access
        # to Redis' protected data directory.
        subprocess.run(["redis-cli", "--rdb", str(stage/"redis.rdb")],
                       check=True, capture_output=True)
        units=Path.home()/".config/systemd/user"
        if units.exists(): shutil.copytree(units, stage/"systemd-user", dirs_exist_ok=True,
                                           symlinks=True, ignore_dangling_symlinks=True)
        files=sorted(p for p in stage.rglob("*") if p.is_file())
        manifest={"schema":"aios-backup/1.1","created":datetime.now(timezone.utc).isoformat(),
                  "files":{str(p.relative_to(stage)):digest(p) for p in files}}
        (stage/"manifest.json").write_text(json.dumps(manifest,indent=2))
        archive=DEST/f"aios_verified_{stamp}.tar.gz"
        with tarfile.open(archive,"w:gz") as tf: tf.add(stage,arcname="payload")
        os.chmod(archive,0o600)
        for old in sorted(DEST.glob("aios_verified_*.tar.gz"), key=lambda p:p.stat().st_mtime,
                          reverse=True)[5:]:
            old.unlink()
        print(json.dumps({"ok":True,"archive":str(archive),"files":len(files),"sha256":digest(archive)}))
        return archive
    finally: shutil.rmtree(stage.parent,ignore_errors=True)

def drill(archive):
    archive=Path(archive)
    with tempfile.TemporaryDirectory(prefix="aios-restore-drill-") as tmp:
        root=Path(tmp)
        with tarfile.open(archive,"r:gz") as tf:
            for member in tf.getmembers():
                target=(root/member.name).resolve()
                if root.resolve() not in target.parents and target != root.resolve():
                    raise RuntimeError("unsafe archive path")
            tf.extractall(root)
        payload=root/"payload"; manifest=json.loads((payload/"manifest.json").read_text())
        bad=[]
        for rel,want in manifest["files"].items():
            p=payload/rel
            if not p.is_file() or digest(p)!=want: bad.append(rel)
        result={"ok":not bad,"archive":str(archive),"verified":len(manifest["files"])-len(bad),"bad":bad}
        print(json.dumps(result,ensure_ascii=False))
        return not bad

def alert_failure(error):
    """Persist and publish backup failure evidence without hiding the failure."""
    ALERTS.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "backup_restore_drill_failed",
        "ts": datetime.now(timezone.utc).isoformat(),
        "error": str(error)[:2000],
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (ALERTS / f"backup_failure_{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    try:
        from aios_bus import publish_event
        publish_event("alert.critical", payload, "aios-backup")
    except Exception:
        pass

def create_and_drill():
    try:
        archive = create()
        if not drill(archive):
            raise RuntimeError(f"restore drill failed: {archive}")
        print(json.dumps({"ok": True, "mode": "create-and-drill",
                          "archive": str(archive)}, ensure_ascii=False))
        return True
    except Exception as exc:
        alert_failure(exc)
        print(json.dumps({"ok": False, "mode": "create-and-drill",
                          "error": str(exc)}, ensure_ascii=False))
        return False

if __name__ == "__main__":
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    sub.add_parser("create"); sub.add_parser("create-and-drill")
    d=sub.add_parser("drill"); d.add_argument("archive")
    args=ap.parse_args()
    if args.cmd == "create":
        ok = bool(create())
    elif args.cmd == "create-and-drill":
        ok = create_and_drill()
    else:
        ok = drill(args.archive)
    raise SystemExit(0 if ok else 1)
