"""
Cleanup Engine — TTL过期 + 低分淘汰 + 归档压缩 + 去重合并
"""
import sqlite3, json, os, shutil
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path("${AIOS_HOME}/kernel/centers/intelligence_growth_center/intel.db")
ARCHIVE_DIR = Path("${AIOS_HOME}/archive/intel")

class CleanupEngine:
    def __init__(self):
        self.archived = 0
        self.purged = 0
        self.merged = 0

    def get_conn(self):
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        return conn

    def expire_cache(self, days: int = 7) -> int:
        """清理超过N天的低分缓存."""
        conn = self.get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn.execute("DELETE FROM intel_items WHERE score < 1.0 AND created_at < ?", (cutoff,))
        count = conn.total_changes
        conn.commit(); conn.close()
        return count

    def archive_low_value(self, threshold: float = 2.0, days: int = 30) -> int:
        """归档低分旧数据到archive/."""
        conn = self.get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        items = conn.execute("SELECT * FROM intel_items WHERE score < ? AND created_at < ?", (threshold, cutoff)).fetchall()
        if items:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            today = datetime.now().strftime("%Y%m%d")
            with open(ARCHIVE_DIR / f"archive_{today}.json", "w") as f:
                json.dump([dict(i) for i in items], f, ensure_ascii=False, default=str)
            ids = [i["id"] for i in items]
            conn.execute(f"DELETE FROM intel_items WHERE id IN ({','.join('?'*len(ids))})", ids)
            conn.commit()
        count = len(items)
        conn.close()
        self.archived = count
        return count

    def remove_duplicates(self) -> int:
        """删除重复URL."""
        conn = self.get_conn()
        conn.execute("DELETE FROM intel_items WHERE id NOT IN (SELECT MIN(id) FROM intel_items GROUP BY url)")
        count = conn.total_changes
        conn.commit(); conn.close()
        self.merged = count
        return count

    def purge_all_expired(self, max_days: int = 60) -> dict:
        """全量清理."""
        from storage import refresh_lifecycle
        lifecycle = refresh_lifecycle()
        self.purged = self.expire_cache(7)
        self.archived = self.archive_low_value(2.0, 30)
        self.merged = self.remove_duplicates()
        conn = self.get_conn()
        conn.execute("DELETE FROM fetch_log WHERE julianday('now')-julianday(fetched_at)>90")
        fetch_logs_purged = conn.total_changes
        conn.commit(); conn.close()
        return {"purged": self.purged, "archived": self.archived,
                "merged": self.merged, "lifecycle": lifecycle,
                "fetch_logs_purged": fetch_logs_purged}

    def get_stats(self) -> dict:
        conn = self.get_conn()
        total = conn.execute("SELECT COUNT(*) as c FROM intel_items").fetchone()["c"]
        low = conn.execute("SELECT COUNT(*) as c FROM intel_items WHERE score < 2.0").fetchone()["c"]
        statuses = {r[0]: r[1] for r in conn.execute(
            "SELECT status,COUNT(*) FROM intel_items GROUP BY status").fetchall()}
        conn.close()
        archive_count = len(list(ARCHIVE_DIR.glob("*.json"))) if ARCHIVE_DIR.exists() else 0
        return {"total": total, "low_quality": low, "archives": archive_count,
                "statuses": statuses}
