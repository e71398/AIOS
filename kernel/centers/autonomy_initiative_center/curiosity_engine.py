"""
Curiosity Engine — 主动检索引擎 (三级: 内部知识→历史任务→外部资料)
==================================================================
AIOS v4.0 Autonomy & Initiative Center

依赖: aios_semantic_search, aios_knowledge_importer, aios_bus
"""
import os, sys, json
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_semantic_search import search as semantic_search, index_document
from aios_bus import check_recent, publish_event

class CuriosityEngine:
    def search_internal_knowledge(self, query: str, limit: int = 5) -> list:
        """搜索内部知识库 (FTS5)."""
        try:
            results = semantic_search(query, limit=limit)
            return [{"source": "internal", "title": r["title"], "snippet": r.get("snippet","")[:200]} for r in results]
        except: return []

    def search_historical_tasks(self, query: str, hours: int = 168, limit: int = 5) -> list:
        """搜索最近任务记录."""
        try:
            recent = check_recent(hours=hours, limit=limit * 3)
            similar = []
            for r in recent:
                task_name = r.get("task_name", "")
                if any(kw in task_name.lower() for kw in query.lower().split() if len(kw) > 2):
                    similar.append({
                        "source": "history",
                        "title": task_name[:80],
                        "snippet": r.get("summary","")[:200],
                        "status": r.get("status","?"),
                    })
            return similar[:limit]
        except: return []

    def search_external_sources(self, query: str, limit: int = 3) -> list:
        """外部资料搜索 (占位, 可接入web search API)."""
        return []

    def collect_minimum_reference_pack(self, task: dict) -> dict:
        """收集最小参考包."""
        task_text = task.get("task_name", task.get("task", ""))
        if not task_text:
            return {"query": "", "internal_hits": [], "historical_hits": [], "external_hits": [], "reference_pack_score": 0}

        internal = self.search_internal_knowledge(task_text)
        historical = self.search_historical_tasks(task_text)
        external = self.search_external_sources(task_text)

        total = len(internal) + len(historical) + len(external)
        score = min(1.0, 0.3 * min(len(internal), 3) + 0.4 * min(len(historical), 3) + 0.3 * min(len(external), 2))

        return {
            "query": task_text[:100],
            "internal_hits": internal,
            "historical_hits": historical,
            "external_hits": external,
            "reference_pack_score": round(score, 2),
            "total_refs": total,
        }

    def evaluate_reference_quality(self, references: list) -> list:
        """评估参考质量."""
        for ref in references:
            snippet = ref.get("snippet", "")
            ref["quality"] = "high" if len(snippet) > 100 else ("medium" if len(snippet) > 30 else "low")
        return references
