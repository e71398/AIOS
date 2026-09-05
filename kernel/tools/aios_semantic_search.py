#!/usr/bin/env python3
"""
AIOS Semantic Search — SQLite FTS5 + Redis评分
"""
import sqlite3, json, os, sys
from pathlib import Path
from datetime import datetime, timezone

AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")
DB_PATH = Path(AIOS_HOME) / "cache" / "semantic.db"
TOOLS = Path(AIOS_HOME) / "kernel" / "tools"
sys.path.insert(0, str(TOOLS))

def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(source, title, content, ts, tokenize='unicode61')")
    return conn

def index_document(source: str, title: str, content: str, base_score: float = 5.0):
    conn = get_db_with_meta()
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO docs VALUES(?, ?, ?, ?)", (source, title, content[:5000], ts))
    conn.commit()
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    _set_score(doc_id, base_score)
    return doc_id

def _set_score(doc_id: int, score: float):
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=1)
        r.hset("aios:kb:score", str(doc_id), score)
    except Exception: pass

def _get_score(doc_id: int) -> float:
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=1)
        s = r.hget("aios:kb:score", str(doc_id))
        return float(s) if s else 5.0
    except Exception: return 5.0

def adjust_score(doc_id: int, delta: float, feedback: str = ""):
    score = _get_score(doc_id)
    new_score = max(0, min(100, score + delta))
    _set_score(doc_id, new_score)
    if feedback:
        try:
            import redis as _r
            r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=1)
            r.hset("aios:kb:feedback", str(doc_id), feedback)
        except Exception: pass
    return new_score

def search(query: str, limit: int = 10) -> list:
    conn = get_db_with_meta()
    results = []
    for kw in query.split():
        like_q = f"%{kw}%"
        rows = conn.execute(
            "SELECT source, title, substr(content,1,200) as snippet, ts FROM docs "
            "WHERE content LIKE ? OR title LIKE ? LIMIT ?",
            (like_q, like_q, limit)
        ).fetchall()
        for r in rows:
            result = {"source": r["source"], "title": r["title"], "snippet": r["snippet"], "ts": r["ts"]}
            if result not in results:
                results.append(result)
    conn.close()
    return results[:limit]

def search_with_scores(query: str, limit: int = 10) -> list:
    conn = get_db_with_meta()
    results = []
    for kw in query.split():
        like_q = f"%{kw}%"
        rows = conn.execute(
            "SELECT rowid, source, title, substr(content,1,200) as snippet, ts FROM docs "
            "WHERE content LIKE ? OR title LIKE ? LIMIT ?",
            (like_q, like_q, limit)
        ).fetchall()
        for r in rows:
            score = _get_score(r["rowid"])
            days_old = 0
            if r["ts"]:
                try:
                    days_old = (datetime.now(timezone.utc) - datetime.fromisoformat(r["ts"].replace("Z","+00:00"))).days
                except: pass
            decay = score * (0.95 ** days_old)
            results.append({
                "id": r["rowid"], "source": r["source"], "title": r["title"],
                "snippet": r["snippet"], "ts": r["ts"],
                "score": round(score, 1), "decayed_score": round(decay, 1),
            })
    conn.close()
    results.sort(key=lambda x: -x["decayed_score"])
    return results[:limit]

def decay_all_scores():
    conn = get_db_with_meta()
    rows = conn.execute("SELECT rowid, ts FROM docs").fetchall()
    conn.close()
    count = 0
    for r in rows:
        try:
            days_old = 0
            if r["ts"]:
                try:
                    days_old = (datetime.now(timezone.utc) - datetime.fromisoformat(r["ts"].replace("Z","+00:00"))).days
                except: pass
            new_score = max(0.5, _get_score(r["rowid"]) * (0.95 ** max(days_old, 1)))
            _set_score(r["rowid"], round(new_score, 2))
            count += 1
        except: pass
    return count

def index_knowledge():
    kb = Path(AIOS_HOME) / "knowledge"
    indexed = 0
    for f in kb.rglob("*.md"):
        try:
            content = f.read_text()
            index_document("knowledge", str(f.relative_to(kb)), content)
            indexed += 1
        except: pass
    try:
        from aios_bus import check_recent
        for r in check_recent(limit=100):
            title = r.get("task_name", "")[:200]
            content = r.get("summary", "")[:1000]
            if title:
                index_document("bus", title, content)
                indexed += 1
    except: pass
    return indexed

def reindex_all():
    conn = get_db_with_meta()
    conn.execute("DELETE FROM docs")
    conn.execute("INSERT INTO docs(docs) VALUES('rebuild')")
    conn.commit(); conn.close()
    return index_knowledge()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "search"
    if cmd == "index":
        n = reindex_all()
        print(f"✅ 索引 {n} 个文档")
    elif cmd == "search":
        q = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "AIOS"
        results = search(q)
        for r in results:
            print(f"  [{r['source']}] {r['title'][:60]}")
            print(f"    {r['snippet'][:120]}")
        if not results:
            print("  无结果")


# 2026-08-17 P1-KNW-001 closure: provenance + delete-by-provenance.
# FTS5 virtual tables cannot be ALTERed, so provenance lives in a
# side-table ``docs_meta`` keyed by the FTS5 rowid.

PROVENANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs_meta (
    rowid INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_path TEXT,
    sha256 TEXT,
    format TEXT,
    ingested_at TEXT,
    meta_json TEXT
)
"""

PROVENANCE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS docs_meta_source_idx ON docs_meta(source)",
    "CREATE INDEX IF NOT EXISTS docs_meta_source_path_idx ON docs_meta(source_path)",
    "CREATE INDEX IF NOT EXISTS docs_meta_sha256_idx ON docs_meta(sha256)",
]


def _ensure_provenance(conn):
    """Create ``docs_meta`` table + indexes if missing; idempotent."""
    try:
        conn.execute(PROVENANCE_SCHEMA)
        for stmt in PROVENANCE_INDEXES:
            conn.execute(stmt)
        conn.commit()
    except Exception:
        pass


def get_db_with_meta():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(source, title, content, ts, tokenize='unicode61')")
    _ensure_provenance(conn)
    return conn


def index_document_with_meta(source, title, content, meta=None, base_score=5.0):
    """Like :func:`index_document` but persist provenance in side-table."""
    conn = get_db_with_meta()
    ts = datetime.now(timezone.utc).isoformat()
    meta = dict(meta or {})
    cur = conn.execute(
        "INSERT INTO docs (source, title, content, ts) VALUES(?, ?, ?, ?)",
        (source, title, content[:5000], ts),
    )
    doc_id = cur.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO docs_meta (rowid, source, source_path, sha256, format, ingested_at, meta_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            doc_id,
            source,
            str(meta.get("source_path") or ""),
            str(meta.get("sha256") or ""),
            str(meta.get("format") or ""),
            str(meta.get("ingested_at") or ts),
            json.dumps(meta, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()
    _set_score(doc_id, base_score)
    return doc_id


def _delete_with_meta(conn, where_clause, params):
    """Delete from both ``docs`` and ``docs_meta``; returns total removed."""
    cur = conn.execute(f"SELECT rowid FROM docs_meta WHERE {where_clause}", params)
    ids = [r["rowid"] for r in cur.fetchall()]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM docs WHERE rowid IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM docs_meta WHERE rowid IN ({placeholders})", ids)
    conn.commit()
    return len(ids)


def delete_by_source(source):
    """Delete every doc indexed under ``source``."""
    conn = get_db_with_meta()
    n = _delete_with_meta(conn, "source = ?", (source,))
    conn.close()
    return n


def delete_by_path(source_path):
    """Delete every doc whose ``source_path`` matches exactly."""
    conn = get_db_with_meta()
    n = _delete_with_meta(conn, "source_path = ?", (source_path,))
    conn.close()
    return n


def delete_by_sha(sha256):
    """Delete every doc whose ``sha256`` matches exactly."""
    conn = get_db_with_meta()
    n = _delete_with_meta(conn, "sha256 = ?", (sha256,))
    conn.close()
    return n
