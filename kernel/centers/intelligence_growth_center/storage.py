"""Data models + SQLite storage."""
import sqlite3, json, os
from datetime import datetime
from pathlib import Path

DB_PATH = Path("${AIOS_HOME}/kernel/centers/intelligence_growth_center/intel.db")

def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS intel_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT, title TEXT, url TEXT UNIQUE, summary TEXT,
        category TEXT, module TEXT, score REAL DEFAULT 0,
        trust_level TEXT, risk_level TEXT, freshness REAL DEFAULT 1.0,
        tags TEXT, raw_data TEXT, action_suggestion TEXT,
        status TEXT DEFAULT 'new', created_at TEXT, fetched_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fetch_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT, status TEXT, items_fetched INTEGER,
        items_new INTEGER, error TEXT, fetched_at TEXT
    )""")
    conn.commit()
    return conn

def save_item(source: str, title: str, url: str, summary: str = "",
              category: str = "", module: str = "", score: float = 0,
              trust: str = "medium", risk: str = "low", tags: str = "",
              action: str = "") -> bool:
    conn = get_db()
    now = datetime.now().isoformat()
    try:
        conn.execute("""INSERT OR IGNORE INTO intel_items 
            (source,title,url,summary,category,module,score,trust_level,risk_level,tags,action_suggestion,status,created_at,fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, title, url, summary[:500], category, module, score, trust, risk, tags, action, 'new', now, now))
        conn.commit()
        return conn.total_changes > 0
    except sqlite3.Error:
        return False

def touch_item(url: str) -> bool:
    """Record that an existing item was observed again without duplicating it."""
    conn = get_db()
    now = datetime.now().isoformat()
    cur = conn.execute("UPDATE intel_items SET fetched_at=?, freshness=1.0 WHERE url=?", (now, url))
    conn.commit(); conn.close()
    return cur.rowcount > 0

def log_fetch(source: str, status: str, items_fetched: int = 0,
              items_new: int = 0, error: str = "") -> None:
    conn = get_db()
    conn.execute("""INSERT INTO fetch_log
        (source,status,items_fetched,items_new,error,fetched_at)
        VALUES (?,?,?,?,?,?)""",
        (source, status, items_fetched, items_new, error[:500], datetime.now().isoformat()))
    conn.commit(); conn.close()

def refresh_lifecycle() -> dict:
    """Refresh freshness and move old unread data through a simple lifecycle."""
    conn = get_db()
    conn.execute("""UPDATE intel_items SET freshness = CASE
        WHEN julianday('now') - julianday(COALESCE(fetched_at,created_at)) <= 1 THEN 1.0
        WHEN julianday('now') - julianday(COALESCE(fetched_at,created_at)) <= 3 THEN 0.8
        WHEN julianday('now') - julianday(COALESCE(fetched_at,created_at)) <= 7 THEN 0.5
        WHEN julianday('now') - julianday(COALESCE(fetched_at,created_at)) <= 30 THEN 0.2
        ELSE 0.1 END""")
    conn.execute("""UPDATE intel_items SET status='seen'
        WHERE status='new' AND julianday('now') - julianday(created_at) > 7""")
    conn.execute("""UPDATE intel_items SET status='archived'
        WHERE status IN ('new','seen') AND julianday('now') - julianday(created_at) > 30""")
    conn.commit()
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT status,COUNT(*) FROM intel_items GROUP BY status").fetchall()}
    conn.close()
    return counts

def get_items(module: str = "", category: str = "", limit: int = 50, min_score: float = 0) -> list:
    conn = get_db()
    q = "SELECT * FROM intel_items WHERE status!='archived'"
    params = []
    if module: q += " AND module=?"; params.append(module)
    if category: q += " AND category=?"; params.append(category)
    if min_score > 0: q += " AND score>=?"; params.append(min_score)
    q += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
    return [dict(r) for r in conn.execute(q, params).fetchall()]

def get_stats() -> dict:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as c FROM intel_items").fetchone()["c"]
    new = conn.execute("SELECT COUNT(*) as c FROM intel_items WHERE status='new'").fetchone()["c"]
    by_module = {r[0]: r[1] for r in conn.execute(
        "SELECT module,COUNT(*) FROM intel_items GROUP BY module").fetchall()}
    last_fetch = conn.execute("SELECT MAX(fetched_at) FROM fetch_log").fetchone()[0]
    failed_fetches = conn.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE status!='ok' AND fetched_at>=datetime('now','-24 hours')"
    ).fetchone()[0]
    conn.close()
    return {"total": total, "new": new, "by_module": by_module,
            "last_fetch": last_fetch, "fetch_failures_24h": failed_fetches}

def cleanup_old(days: int = 30):
    conn = get_db()
    conn.execute("DELETE FROM intel_items WHERE score < 0.3 AND created_at < date('now', ?)", (f'-{days} days',))
    conn.commit()
