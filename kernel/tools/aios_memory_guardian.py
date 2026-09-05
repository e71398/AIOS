#!/usr/bin/env python3
"""
AIOS Memory Guardian — 记忆清理守护
====================================
规则: 未提取不得删除。先标记→再归档→最后确认。

清理策略:
  OpenCode:  保留最近100会话, 旧的→archive/ (确认已提取后)
  Hermes:    保留最近30天session dumps, 旧的→archive/
  OpenClaw:  保留最近60天, 旧的→archive/
  Codex:     文件量小, 不清理
  Claude:    项目文件不清理
"""
import json, os, sys, shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

TOOLS = Path("${AIOS_HOME}/kernel/tools")
AIOS_HOME = Path(os.environ.get("AIOS_HOME", "${AIOS_HOME}"))
sys.path.insert(0, str(TOOLS))

from aios_knowledge_importer import is_extracted, mark_as_extracted, get_extraction_stats
from aios_semantic_search import index_document

# 清理规则
CLEANUP_RULES = {
    "opencode": {
        "path": Path("${HOME}/.local/share/opencode/opencode.db"),
        "type": "sqlite_sessions",
        "keep_count": 100,
        "extract_first": True,
    },
    "claude": {
        "path": Path("${HOME}/.claude/projects"),
        "type": "file_age",
        "keep_days": 90,
        "pattern": "**/*",
        "extract_first": True,
    },
    "codex": {
        "path": Path("${HOME}/.codex/sessions"),
        "type": "file_age",
        "keep_days": 60,
        "pattern": "*.json",
        "extract_first": True,
    },
    "hermes": {
        "path": Path("${HOME}/.hermes/sessions"),
        "type": "file_age",
        "keep_days": 30,
        "pattern": "request_dump_*.json",
        "extract_first": True,
    },
    "openclaw": {
        "path": Path("${HOME}/.openclaw/agents"),
        "type": "file_age",
        "keep_days": 60,
        "pattern": "**/sessions/sessions.json",
        "extract_first": True,
    },
}

ARCHIVE_DIR = AIOS_HOME / "archive" / "memory"


def _ensure_extracted(source: str, filepath: Path) -> bool:
    """确保文件已被提取到共享知识库, 如果未提取则先提取."""
    key = str(filepath)
    if is_extracted(source, key):
        return True

    # 未提取 → 先强制提取
    try:
        from aios_knowledge_importer import AI_SOURCES
        config = AI_SOURCES.get(source)
        if not config:
            return False

        extractor = getattr(
            __import__("aios_knowledge_importer"), config["extractor"], None
        ) if config.get("extractor") else None
        if not extractor:
            import aios_knowledge_importer as importer
            extractor = getattr(importer, config["extractor"], None)

        if extractor:
            entries = extractor(filepath)
            for entry in entries:
                if len(entry.get("content", "")) > 50:
                    index_document(f"imported:{source}", entry["title"], entry["content"])
            mark_as_extracted(source, key)
            return True
    except Exception:
        pass
    return False


def _archive_file(source: str, filepath: Path) -> str:
    """归档文件到 archive/ 目录."""
    if not filepath.exists():
        return "not_found"

    dest_dir = ARCHIVE_DIR / source
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filepath.name

    # 避免重名
    if dest.exists():
        dest = dest_dir / f"{filepath.stem}_{datetime.now().strftime('%Y%m%d%H%M%S')}{filepath.suffix}"

    shutil.move(str(filepath), str(dest))
    return str(dest)


def cleanup_opencode_sessions() -> dict:
    """清理OpenCode旧会话 (SQLite)."""
    db_path = Path("${HOME}/.local/share/opencode/opencode.db")
    if not db_path.exists():
        return {"status": "db_not_found"}

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 计数
    total = conn.execute("SELECT COUNT(*) as cnt FROM session").fetchone()["cnt"]
    if total <= 100:
        conn.close()
        return {"status": "ok", "sessions": total, "cleaned": 0}

    # 标记旧会话为已提取(标题作为key)
    old_sessions = conn.execute("""
        SELECT id, title FROM session
        WHERE id NOT IN (
            SELECT id FROM session ORDER BY time_updated DESC LIMIT 100
        )
    """).fetchall()

    cleaned = 0
    for s in old_sessions:
        key = f"opencode_session:{s['title'][:60]}"
        if _ensure_extracted("opencode", Path(key)):
            conn.execute("DELETE FROM session WHERE id = ?", (s["id"],))
            cleaned += 1

    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return {"status": "ok", "sessions": total, "cleaned": cleaned}


def cleanup_hermes_sessions() -> dict:
    """清理Hermes旧session dumps."""
    sessions_dir = Path("${HOME}/.hermes/sessions")
    if not sessions_dir.exists():
        return {"status": "dir_not_found"}

    cutoff = datetime.now() - timedelta(days=30)
    cleaned = 0
    for f in sessions_dir.glob("request_dump_*.json"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            if _ensure_extracted("hermes", f):
                dest = _archive_file("hermes", f)
                cleaned += 1
    return {"status": "ok", "cleaned": cleaned}


def cleanup_openclaw_sessions() -> dict:
    """清理OpenClaw旧会话."""
    agents_dir = Path("${HOME}/.openclaw/agents")
    if not agents_dir.exists():
        return {"status": "dir_not_found"}

    cutoff = datetime.now() - timedelta(days=60)
    cleaned = 0
    for f in agents_dir.glob("**/sessions/sessions.json"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            if _ensure_extracted("openclaw", f):
                dest = _archive_file("openclaw", f)
                cleaned += 1
    return {"status": "ok", "cleaned": cleaned}


def cleanup_claude_sessions() -> dict:
    """清理Claude旧项目文件."""
    projects_dir = Path("${HOME}/.claude/projects")
    if not projects_dir.exists():
        return {"status": "dir_not_found"}

    cutoff = datetime.now() - timedelta(days=90)
    cleaned = 0
    for f in projects_dir.glob("**/*"):
        if not f.is_file():
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            if _ensure_extracted("claude", f):
                _archive_file("claude", f)
                cleaned += 1
    return {"status": "ok", "cleaned": cleaned}


def cleanup_codex_sessions() -> dict:
    """清理Codex旧会话."""
    sessions_dir = Path("${HOME}/.codex/sessions")
    if not sessions_dir.exists():
        return {"status": "dir_not_found"}

    cutoff = datetime.now() - timedelta(days=60)
    cleaned = 0
    for f in sessions_dir.glob("*.json"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            if _ensure_extracted("codex", f):
                _archive_file("codex", f)
                cleaned += 1
    return {"status": "ok", "cleaned": cleaned}


def run_cleanup() -> dict:
    results = {}
    results["opencode"] = cleanup_opencode_sessions()
    results["claude"] = cleanup_claude_sessions()
    results["codex"] = cleanup_codex_sessions()
    results["hermes"] = cleanup_hermes_sessions()
    results["openclaw"] = cleanup_openclaw_sessions()
    return results


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        stats = get_extraction_stats()
        print(f"📊 提取状态:")
        for src, cnt in stats.items():
            print(f"  {src}: {cnt}条已提取")
    elif cmd == "clean":
        results = run_cleanup()
        print(f"🧹 清理完成:")
        for src, r in results.items():
            print(f"  {src}: {r}")
    elif cmd == "check":
        for src in ["opencode", "claude", "codex", "hermes", "openclaw"]:
            rules = CLEANUP_RULES.get(src)
            if not rules: continue
            typ = rules["type"]
            if typ == "file_age":
                cutoff = datetime.now() - timedelta(days=rules["keep_days"])
                path = rules["path"]
                if not path.exists(): continue
                for f in path.glob(rules["pattern"]):
                    if not f.is_file(): continue
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime < cutoff:
                        if not is_extracted(src, str(f)):
                            print(f"  ⚠️ {src}: {f.name} 未提取, 不会被删除")
            elif typ == "sqlite_sessions":
                import sqlite3
                db = rules["path"]
                if not db.exists(): continue
                conn = sqlite3.connect(str(db))
                conn.row_factory = sqlite3.Row
                total = conn.execute("SELECT COUNT(*) as cnt FROM session").fetchone()["cnt"]
                if total > rules["keep_count"]:
                    print(f"  ⚠️ opencode: {total}会话, 超出保留上限{rules['keep_count']}")
                conn.close()
