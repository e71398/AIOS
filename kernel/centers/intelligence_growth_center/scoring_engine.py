"""
Scoring Engine — 五维评分: 新鲜度+可信度+相关性+可执行性+风险
"""
from datetime import datetime, timedelta

RELEVANCE_KW = {
    "ai_agent":    ["agent","llm","rag","autonomous","orchestra","multi-agent"],
    "devops":      ["docker","k8s","ci/cd","pipeline","deploy","monitor"],
    "security":    ["security","vuln","audit","firewall","encrypt"],
    "data":        ["database","vector","embedding","search","index"],
    "tooling":     ["cli","sdk","api","plugin","extension"],
}

class ScoringEngine:
    def score(self, item: dict) -> dict:
        title = (item.get("title") or "").lower()
        summary = (item.get("summary") or "").lower()
        source = item.get("source", "")
        category = item.get("category", "")

        freshness = self._freshness(item)
        trust = self._trust(source, category)
        relevance = self._relevance(title, summary, category)
        executable = self._executable(title, summary)
        risk = self._risk(title, summary)

        total = round((freshness * 0.2 + trust * 0.25 + relevance * 0.3 + executable * 0.15 - risk * 0.1), 2)
        total = max(0, min(10, total))

        return {
            "freshness": round(freshness,1), "trust": round(trust,1),
            "relevance": round(relevance,1), "executable": round(executable,1),
            "risk": round(risk,1), "total": total
        }

    def _freshness(self, item: dict) -> float:
        try:
            dt = datetime.fromisoformat(item.get("created_at","")[:19])
            days = (datetime.now() - dt).days
            return max(1, 10 - days * 0.5)
        except (TypeError, ValueError): return 5

    def _trust(self, source: str, category: str) -> float:
        base = {"github_trending": 9, "hackernews": 8, "arxiv_ai": 9, "techcrunch_rss": 7,
                "reddit_programming": 5, "indie_hackers": 5, "lobsters_rss": 6,
                "devto_startups": 6, "producthunt": 5}.get(source, 5)
        if category == "ai_research": base += 1
        return min(10, base)

    def _relevance(self, title: str, summary: str, category: str) -> float:
        score = 1
        text = title + " " + summary
        for domain, kws in RELEVANCE_KW.items():
            if any(kw in text for kw in kws): score += 1.5
        if category == "open_source": score += 2
        if category == "ai_research": score += 2
        return min(10, score)

    def _executable(self, title: str, summary: str) -> float:
        text = title + " " + summary
        score = 1
        if any(kw in text for kw in ["tutorial","guide","how","example","demo","template"]): score += 3
        if any(kw in text for kw in ["api","sdk","cli","plugin"]): score += 2
        return min(10, score)

    def _risk(self, title: str, summary: str) -> float:
        text = title + " " + summary
        score = 0
        risk_kw = ["scam","fake","phishing","malware","trojan","deprecated","abandoned","unmaintained"]
        for kw in risk_kw:
            if kw in text: score += 3
        return min(10, score)
