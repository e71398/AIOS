#!/usr/bin/env python3
"""
AIOS Knowledge Importer — 跨AI知识共享引擎
===========================================
扫描5个AI的会话历史 → 提取可复用知识 → 索引到共享知识库
"""
import json, os, sys, re
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
AIOS_HOME = Path(os.environ.get("AIOS_HOME", "${AIOS_HOME}"))
sys.path.insert(0, str(TOOLS))

from aios_semantic_search import index_document, search, search_with_scores
from aios_bus import _is_available, publish_event

# 各AI数据源
AI_SOURCES = {
    "openclaw": {
        "path": Path("${HOME}/.openclaw/agents"),
        "type": "sessions_json",
        "pattern": "**/sessions/*.json",
        "extractor": "_extract_openclaw_session",
    },
    "hermes": {
        "path": Path("${HOME}/.hermes/sessions"),
        "type": "request_dumps",
        "pattern": "request_dump_*.json",
        "extractor": "_extract_hermes_session",
    },
    "codex": {
        "path": Path("${HOME}/.codex/sessions"),
        "type": "session_json",
        "pattern": "*.json",
        "extractor": "_extract_codex_session",
    },
    "claude": {
        "path": Path("${HOME}/.claude/projects"),
        "type": "project_files",
        "pattern": "**/*",
        "extractor": "_extract_claude_project",
    },
    "opencode": {
        "path": Path("${HOME}/.local/share/opencode/opencode.db"),
        "type": "sqlite_db",
        "extractor": "_extract_opencode_db",
    },
}

IMPORT_STATE_FILE = AIOS_HOME / "cache" / "knowledge_import_state.json"


def mark_as_extracted(source: str, item_key: str) -> bool:
    """标记某条记忆已被提取到共享知识库."""
    state = _load_import_state()
    extracted = state.setdefault("extracted", {})
    src_list = extracted.setdefault(source, [])
    if item_key not in src_list:
        src_list.append(item_key)
        if len(src_list) > 1000:
            src_list.pop(0)
    _save_import_state(state)
    return True


def is_extracted(source: str, item_key: str) -> bool:
    """检查某条记忆是否已被提取."""
    state = _load_import_state()
    extracted = state.get("extracted", {})
    return item_key in extracted.get(source, [])


def get_extraction_stats() -> dict:
    """获取提取统计."""
    state = _load_import_state()
    extracted = state.get("extracted", {})
    stats = {}
    for src, items in extracted.items():
        stats[src] = len(items)
    return stats


def _extract_openclaw_session(filepath: Path) -> list:
    try:
        data = json.loads(filepath.read_text())
        if not isinstance(data, dict):
            return []
        results = []
        agent_name = filepath.parent.parent.name if hasattr(filepath, 'parent') else "openclaw"
        for agent_key, session in data.items():
            if not isinstance(session, dict):
                continue
            final_text = session.get("pendingFinalDeliveryText", "")
            task_summary = session.get("systemSent", "")
            if final_text and len(final_text) > 100:
                # 去重: 过滤纯心跳报告(大部分都是)
                keywords = ["heartbeat", "心跳", "HEARTBEAT", "cron", "disk", "磁盘", "memory", "内存"]
                is_heartbeat = all(kw.lower() in final_text.lower() for kw in keywords[:3] if kw.lower() in final_text.lower())
                if not is_heartbeat or len(final_text) > 500:
                    results.append({
                        "title": f"[OpenClaw/{agent_key.split(':')[0]}] {str(final_text)[:60].replace(chr(10),' ')}",
                        "content": str(final_text)[:3000],
                        "meta": {"agent": agent_key, "task": str(task_summary)[:100]},
                    })
        return results
    except Exception:
        return []


def _extract_hermes_session(filepath: Path) -> list:
    try:
        data = json.loads(filepath.read_text())
        content = str(data) if isinstance(data, dict) else ""
        task_name = data.get("task", data.get("task_name", "")) if isinstance(data, dict) else ""
        title = f"[Hermes] {str(task_name)[:60]}" if task_name else f"[Hermes] {filepath.name[:60]}"
        return [{"title": title, "content": content[:3000], "meta": {"task": str(task_name)[:100]}}]
    except Exception:
        return []


def _extract_codex_session(filepath: Path) -> list:
    try:
        data = json.loads(filepath.read_text())
        if not isinstance(data, dict):
            return []
        content = data.get("content", str(data)[:1000])
        task = data.get("task", data.get("goal", ""))
        title = f"[Codex] {str(task)[:60]}" if task else f"[Codex] {filepath.stem[:60]}"
        return [{"title": title, "content": str(content)[:2000], "meta": {"task": str(task)[:100]}}]
    except Exception:
        return []


def _extract_claude_project(filepath: Path) -> list:
    if not filepath.is_file():
        return []
    try:
        content = filepath.read_text()[:3000]
        ext = filepath.suffix
        if ext in [".json", ".py", ".md", ".txt", ".yaml", ".toml", ".sh", ".js", ".ts"]:
            return [{"title": f"[Claude] {filepath.name}", "content": content[:2000], "meta": {"path": str(filepath)}}]
    except Exception:
        pass
    return []


def _extract_opencode_db(filepath: Path) -> list:
    """从 OpenCode SQLite 数据库提取会话知识."""
    import sqlite3
    if not filepath.exists():
        return []
    results = []
    try:
        conn = sqlite3.connect(str(filepath))
        conn.row_factory = sqlite3.Row

        # 获取有内容的会话(过滤掉空会话和explore子任务)
        sessions = conn.execute("""
            SELECT s.id, s.title, s.model,
                   COUNT(DISTINCT m.id) as msg_count,
                   MAX(m.time_created) as last_msg
            FROM session s
            JOIN message m ON m.session_id = s.id
            WHERE s.title IS NOT NULL AND length(s.title) > 5
            AND s.title NOT LIKE '%@explore%'
            GROUP BY s.id
            ORDER BY last_msg DESC
            LIMIT 50
        """).fetchall()

        for s in sessions:
            # 提取核心对话内容(user prompt + assistant summary)
            messages = conn.execute("""
                SELECT m.data, GROUP_CONCAT(p.data, '|||') as parts_data
                FROM message m
                LEFT JOIN part p ON p.message_id = m.id
                WHERE m.session_id = ?
                GROUP BY m.id
                ORDER BY m.time_created
                LIMIT 20
            """, (s["id"],)).fetchall()

            user_prompts = []
            assistant_responses = []
            for msg in messages:
                try:
                    mdata = json.loads(msg["data"]) if msg["data"] else {}
                    role = mdata.get("role", "")
                    content = mdata.get("content", "")

                    # parts 中有实际工具输出
                    if msg["parts_data"]:
                        for part_str in msg["parts_data"].split("|||"):
                            try:
                                p = json.loads(part_str)
                                if p.get("type") == "text" and p.get("text"):
                                    content += "\n" + str(p["text"])[:500]
                            except: pass

                    if role == "user" and content and len(str(content)) > 20:
                        user_prompts.append(str(content)[:300])
                    elif role == "assistant" and content and len(str(content)) > 50:
                        assistant_responses.append(str(content)[:300])
                except: pass

            if user_prompts:
                title = f"[OpenCode] {s['title'][:80]}"
                content = f"用户目标: {'; '.join(user_prompts[:3])}"
                if assistant_responses:
                    content += f"\nAI回复摘要: {'; '.join(assistant_responses[:2])}"
                results.append({"title": title, "content": content[:2500], "meta": {"model": str(s["model"]), "msgs": s["msg_count"]}})

        conn.close()
    except Exception:
        pass
    return results


def _load_import_state() -> dict:
    if IMPORT_STATE_FILE.exists():
        return json.loads(IMPORT_STATE_FILE.read_text())
    return {"sources": {}}


def _save_import_state(state: dict):
    IMPORT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    IMPORT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def scan_all_sources(days: int = 30, max_per_source: int = 50) -> dict:
    state = _load_import_state()
    imported = 0
    results = {}

    for source_name, config in AI_SOURCES.items():
        source_path = config["path"]
        if not source_path.exists():
            results[source_name] = {"status": "not_found", "imported": 0}
            continue

        extractor = globals().get(config["extractor"])
        if not extractor:
            results[source_name] = {"status": "no_extractor", "imported": 0}
            continue

        # 不同类型用不同方式获取文件列表
        stype = config.get("type", "")
        if stype == "sqlite_db":
            files = [source_path]  # SQLite DB文件本身
        elif "pattern" in config:
            files = sorted(source_path.glob(config["pattern"]),
                          key=lambda f: f.stat().st_mtime if f.is_file() else 0, reverse=True)
        else:
            files = [source_path] if source_path.is_file() else []

        source_state = state["sources"].get(source_name, {"last_import": "", "imported_files": []})
        imported_files = set(source_state.get("imported_files", []))

        count = 0
        for filepath in files:
            if count >= max_per_source:
                break
            file_key = str(filepath)
            if file_key in imported_files:
                continue

            entries = extractor(filepath)
            for entry in entries:
                index_document(
                    source=f"imported:{source_name}",
                    title=entry["title"],
                    content=entry["content"],
                )
                count += 1
                imported += 1

            # 提取后打标记: 这条记忆已安全导入
            mark_as_extracted(source_name, str(filepath))
            imported_files.add(file_key)

        state["sources"][source_name] = {
            "last_import": datetime.now(timezone.utc).isoformat(),
            "imported_files": list(imported_files)[-500:],
            "total_imported": count,
        }
        results[source_name] = {"status": "ok", "imported": count}

    _save_import_state(state)
    return {"total_imported": imported, "sources": results}


def scan_url(url: str, title: str = "") -> dict:
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "AIOS-Knowledge-Importer/4.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode(errors="ignore")[:5000]
        idx = index_document("imported:url", title or url, content)
        return {"status": "ok", "url": url, "imported_chars": len(content)}
    except Exception as e:
        return {"status": "error", "url": url, "error": str(e)}


def scan_directory(directory: str, recursive: bool = True) -> dict:
    """Scan a directory tree and index every supported file with provenance.

    2026-08-17 P1-KNW-001 closure: PDF / DOCX / XLSX / HTML are now
    first-class formats via :mod:`aios_knowledge_formats`.  Plain text
    formats stay readable directly.  Every indexed chunk carries
    provenance (``source_path``, ``sha256``, ``mtime_ns``, ``size_bytes``,
    ``ingested_at``) so it can be deleted by path / sha on retraction.
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        return {"status": "not_found", "directory": directory}

    from aios_knowledge_formats import extract_any
    from aios_semantic_search import index_document_with_meta

    imported = 0
    by_format: dict = {}
    pattern = "**/*" if recursive else "*"
    for filepath in dir_path.glob(pattern):
        if not filepath.is_file():
            continue
        ext = filepath.suffix.lower()
        try:
            fmt_result = extract_any(filepath)
            if fmt_result is None:
                # Fallback: plain text / code
                if ext not in [".txt", ".md", ".json", ".py", ".csv", ".yaml",
                               ".toml", ".log", ".sh", ".js", ".ts"]:
                    continue
                raw = filepath.read_text(errors="replace")[:3000]
                if len(raw) < 20:
                    continue
                entry = {
                    "title": filepath.name,
                    "content": raw,
                    "meta": {"format": ext.lstrip(".")},
                    "format": ext.lstrip("."),
                }
            else:
                if not fmt_result.get("content"):
                    continue
                entry = fmt_result
            meta = dict(entry.get("meta") or {})
            meta.setdefault("source_path", str(filepath))
            meta.setdefault("format", entry.get("format") or ext.lstrip("."))
            index_document_with_meta(
                source=f"imported:dir:{directory}",
                title=entry.get("title") or filepath.name,
                content=entry.get("content") or "",
                meta=meta,
            )
            imported += 1
            by_format[entry.get("format") or ext.lstrip(".")] = (
                by_format.get(entry.get("format") or ext.lstrip("."), 0) + 1
            )
        except Exception:
            pass

    return {
        "status": "ok",
        "directory": directory,
        "imported": imported,
        "by_format": by_format,
    }


def delete_directory(directory: str) -> dict:
    """Withdraw every doc indexed from ``directory``.

    P1-KNW-001 closure: provenance-aware deletion.  Uses the
    ``source_path`` recorded at ingest time, so even if the file
    moves the deletion still hits every chunk that came from it.
    Returns the deletion count.
    """
    from aios_semantic_search import delete_by_path
    dir_path = Path(directory)
    if not dir_path.exists():
        return {"status": "not_found", "directory": directory}
    n = 0
    for path in dir_path.rglob("*"):
        if path.is_file():
            n += delete_by_path(str(path))
    return {"status": "ok", "directory": directory, "deleted": n}


def enrich_task_with_knowledge(task_text: str, limit: int = 3) -> dict:
    results = search_with_scores(task_text, limit=limit)
    if not results:
        return {"enriched": False, "task": task_text[:100], "sources": []}

    snippets = []
    for r in results:
        snippets.append({
            "id": r["id"],
            "source": r["source"],
            "title": r["title"],
            "snippet": r.get("snippet", "")[:200],
            "score": r["decayed_score"],
        })
    # 用过的知识轻微加分
    for s in snippets:
        try:
            from aios_semantic_search import adjust_score
            adjust_score(s["id"], 0.1, "referenced")
        except Exception:
            pass
    return {"enriched": True, "task": task_text[:100], "sources": snippets}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "scan":
        result = scan_all_sources(days=30, max_per_source=30)
        print(f"✅ 导入 {result['total_imported']} 条知识")
        for src, r in result["sources"].items():
            print(f"  {src}: {r['status']} ({r['imported']}条)")
    elif cmd == "url":
        url = sys.argv[2] if len(sys.argv) > 2 else ""
        if url:
            result = scan_url(url)
            print(json.dumps(result, ensure_ascii=False))
    elif cmd == "dir":
        directory = sys.argv[2] if len(sys.argv) > 2 else "."
        result = scan_directory(directory)
        print(json.dumps(result, ensure_ascii=False))
    elif cmd == "enrich":
        text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if text:
            result = enrich_task_with_knowledge(text)
            print(json.dumps(result, ensure_ascii=False, indent=2))
