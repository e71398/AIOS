"""
Intel Pipeline — 共享底座: Fetch → Normalize → Dedup → Score → Store
======================================================================
AIOS v4.0 Intelligence & Growth Center
"""
import sys, os, json, time, hashlib, re
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from source_registry import SOURCES, BLACKLIST_KEYWORDS, BLACKLIST_DOMAINS
from storage import save_item, get_items, touch_item, log_fetch, refresh_lifecycle
from dedup_engine import DedupEngine
from scoring_engine import ScoringEngine

def _clean_html(text: str) -> str:
    """去除HTML标签和多余空白"""
    import re
    if not text: return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\bDiscussion\b', '', text)
    text = re.sub(r'\s*\|\s*', ' ', text)
    text = re.sub(r'<[a-z/][^\s>]*$', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def fetch_url(url: str, source_type: str = "web") -> str:
    import requests
    headers = {"User-Agent": "AIOS-Intel/4.0"}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    return response.text[:50000]

def fetch_rss(url: str) -> list:
    import feedparser, requests
    response = requests.get(url, headers={"User-Agent": "AIOS-Intel/4.0"}, timeout=15)
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    items = []
    for entry in feed.entries[:10]:
        summary = _clean_html(entry.get("summary", ""))[:300]
        items.append({"title": entry.get("title", ""),
                      "url": entry.get("link", ""), "summary": summary})
    if not items:
        raise RuntimeError("RSS returned no parseable entries")
    return items

def fetch_github_trending(source_name: str = "github_trending") -> list:
    import requests
    if source_name == "github_trending_weekly":
        since = (datetime.now() - timedelta(days=7)).date().isoformat()
        queries = [f"created:>{since}+stars:>5", f"pushed:>{since}+stars:>100"]
    else:
        since = (datetime.now() - timedelta(days=365)).date().isoformat()
        queries = [f"pushed:>{since}+stars:>100", f"created:>{since}+stars:>50"]
    items = []
    for query in queries:
        url = f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page=10"
        response = requests.get(url, headers={"User-Agent": "AIOS-Intel/4.0"}, timeout=15)
        response.raise_for_status()
        data = response.json()
        for repo in data.get("items", []):
            items.append({"title": repo.get("full_name", ""),
                          "url": repo.get("html_url", ""),
                          "summary": (repo.get("description") or "")[:300],
                          "stars": repo.get("stargazers_count", 0),
                          "language": repo.get("language", ""),
                          "updated_at": repo.get("updated_at", "")})
    return items

def fetch_reddit(subreddit: str = "programming", limit: int = 10) -> list:
    import requests
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
    response = requests.get(url, headers={"User-Agent": "AIOS-Intel/4.0"}, timeout=15)
    response.raise_for_status()
    data = response.json()
    items = []
    for post in data.get("data", {}).get("children", []):
        p = post["data"]
        items.append({"title": p.get("title", ""),
                      "url": f"https://reddit.com{p.get('permalink', '')}",
                      "summary": p.get("selftext", "")[:300]})
    return items

def fetch_hackernews() -> list:
    """HackerNews Top Stories"""
    import requests
    response = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15)
    response.raise_for_status(); ids = response.json()[:15]
    items = []
    for iid in ids:
        item_response = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{iid}.json", timeout=10)
        item_response.raise_for_status(); data = item_response.json()
        if data and data.get("title"):
            items.append({"title": data.get("title", ""),
                          "url": data.get("url", "") or f"https://news.ycombinator.com/item?id={iid}",
                          "summary": (data.get("text") or "")[:300]})
    return items

def fetch_arxiv() -> list:
    """arXiv CS.AI 最新论文"""
    # 2026-08-17 P1-SEM-001: switch to defusedxml to harden against
    # XXE / billion-laughs even when the source is well-known (arxiv).
    import requests, defusedxml.ElementTree as ET
    url = "https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&max_results=10"
    response = requests.get(url, timeout=15); response.raise_for_status()
    root = ET.fromstring(response.text); items = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("atom:entry", ns)[:10]:
        title = entry.find("atom:title", ns); link = entry.find("atom:link", ns)
        summary = entry.find("atom:summary", ns)
        items.append({"title": (title.text or "")[:200] if title is not None else "",
                      "url": link.attrib.get("href", "") if link is not None else "",
                      "summary": (summary.text or "")[:300] if summary is not None else ""})
    return items

def fetch_producthunt() -> list:
    """ProductHunt — 通过 RSS feed 获取"""
    return fetch_rss("https://www.producthunt.com/feed")

def normalize_item(raw: dict, source: str, category: str, module: str) -> dict:
    title = _clean_html(str(raw.get("title","")))[:200]
    url = str(raw.get("url",""))[:500]
    summary = _clean_html(str(raw.get("summary","")))[:300]
    if not title or not url: return None
    # 过滤黑名单
    low_title = title.lower()
    for kw in BLACKLIST_KEYWORDS:
        if kw in low_title: return None
    for domain in BLACKLIST_DOMAINS:
        if domain in urlparse(url).netloc: return None
    return {"source": source, "title": title, "url": url, "summary": summary, "category": category, "module": module}

def dedup_key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]

def score_item(item: dict) -> float:
    score = 3.0
    title = item.get("title","").lower()
    summary = item.get("summary","").lower()
    for kw in ["ai","agent","llm","rag","autonomous","open source"]:
        if kw in title: score += 1.5
    for kw in ["agent","llm","rag","autonomous"]:
        if kw in summary: score += 0.5
    if item.get("trust_level") == "high": score += 1.0
    return round(min(10.0, score), 2)

def run_pipeline(sources: list, module: str) -> dict:
    fetched = 0; saved = 0; skipped = 0; errors = {}; new_items = []
    dedup = DedupEngine()
    scorer = ScoringEngine()
    existing = get_items(module=module, limit=500)
    dedup.load_existing(existing)

    for src_name in sources:
        src = SOURCES.get(src_name)
        if not src: continue
        raw_items = []; source_saved = 0
        try:
            if src["type"] == "rss": raw_items = fetch_rss(src["url"])
            elif src_name.startswith("github"): raw_items = fetch_github_trending(src_name)
            elif src_name.startswith("reddit"): raw_items = fetch_reddit()
            elif src_name == "hackernews": raw_items = fetch_hackernews()
            elif src_name == "arxiv_ai": raw_items = fetch_arxiv()
            elif src_name == "producthunt": raw_items = fetch_producthunt()
            elif src["type"] == "web":
                raw_items = [{"title": src_name, "url": src["url"], "summary": "web page"}]
            else: raise RuntimeError(f"unsupported source type: {src['type']}")
            if not raw_items: raise RuntimeError("source returned no items")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors[src_name] = error; log_fetch(src_name, "error", 0, 0, error)
            continue
        fetched += len(raw_items)
        for raw in raw_items:
            item = normalize_item(raw, src_name, src.get("category",""), module)
            if not item: continue
            if dedup.is_duplicate(item.get("url",""), item.get("title",""), item.get("summary","")):
                skipped += 1; touch_item(item.get("url", "")); continue
            dedup.mark_seen(item.get("url",""), item.get("title",""), item.get("summary",""))
            item["score"] = scorer.score(item)["total"]
            item["trust"] = src.get("trust_level","medium")
            if save_item(source=item["source"], title=item["title"], url=item["url"],
                        summary=item.get("summary",""), category=item.get("category",""),
                        module=item.get("module",""), score=item.get("score",0),
                        trust=item.get("trust","medium")):
                saved += 1
                source_saved += 1; new_items.append(item)
        log_fetch(src_name, "ok", len(raw_items), source_saved, "")
    refresh_lifecycle()
    if module != "opportunity" and new_items:
        try:
            sys.path.insert(0, "${AIOS_HOME}/kernel/tools")
            from aios_bus import publish_event
            for item in sorted(new_items, key=lambda row: float(row.get("score", 0)), reverse=True)[:3]:
                publish_event("intel.discovered", {
                    "title": item.get("title", ""), "summary": item.get("summary", ""),
                    "url": item.get("url", ""), "score": item.get("score", 0),
                    "module": module, "source": item.get("source", "")}, "intelligence-center")
        except Exception as exc:
            errors["event_publish"] = f"{type(exc).__name__}: {exc}"
    return {"fetched": fetched, "saved": saved, "skipped": skipped,
            "errors": errors, "module": module, "_new_items": new_items}
