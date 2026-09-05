"""
Source Registry — 数据源注册表
===============================
AIOS v4.0 Intelligence & Growth Center
"""
SOURCES = {
    "github_trending": {
        "name": "GitHub Trending",
        "url": "https://github.com/trending",
        "api": "https://api.github.com/search/repositories?q=stars:>100+pushed:>2025-01-01&sort=stars&order=desc&per_page=20",
        "type": "api",
        "category": "open_source",
        "trust_level": "high",
        "language": "en",
        "fetch_interval_hours": 12,
    },
    "github_trending_weekly": {
        "name": "GitHub Trending Weekly",
        "url": "https://github.com/trending?since=weekly",
        "type": "web",
        "category": "open_source",
        "trust_level": "high",
        "language": "en",
        "fetch_interval_hours": 24,
    },
    "hackernews": {
        "name": "Hacker News",
        "url": "https://hacker-news.firebaseio.com/v0/topstories.json",
        "type": "api",
        "category": "tech",
        "trust_level": "high",
        "language": "en",
        "fetch_interval_hours": 2,
    },
    "techcrunch_rss": {
        "name": "TechCrunch RSS",
        "url": "https://techcrunch.com/feed/",
        "type": "rss",
        "category": "tech",
        "trust_level": "high",
        "language": "en",
        "fetch_interval_hours": 4,
    },
    "arxiv_ai": {
        "name": "arXiv AI",
        "url": "http://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&max_results=10",
        "type": "api",
        "category": "ai_research",
        "trust_level": "high",
        "language": "en",
        "fetch_interval_hours": 24,
    },
    "reddit_programming": {
        "name": "Reddit Programming",
        "url": "https://www.reddit.com/r/programming/hot.json?limit=15",
        "type": "api",
        "category": "tech",
        "trust_level": "medium",
        "language": "en",
        "fetch_interval_hours": 4,
        "enabled": False,
        "disabled_reason": "blocked from current network",
    },
    "lobsters_rss": {
        "name": "Lobsters",
        "url": "https://lobste.rs/rss",
        "type": "rss",
        "category": "tech",
        "trust_level": "medium",
        "language": "en",
        "fetch_interval_hours": 4,
    },
    "indie_hackers": {
        "name": "Indie Hackers",
        "url": "https://www.indiehackers.com/feed",
        "type": "rss",
        "category": "opportunity",
        "trust_level": "medium",
        "language": "en",
        "fetch_interval_hours": 6,
        "enabled": False,
        "disabled_reason": "endpoint no longer returns RSS",
    },
    "devto_startups": {
        "name": "DEV Community Startups",
        "url": "https://dev.to/feed/tag/startups",
        "type": "rss",
        "category": "opportunity",
        "trust_level": "medium",
        "language": "en",
        "fetch_interval_hours": 6,
    },
    "producthunt": {
        "name": "Product Hunt",
        "url": "https://api.producthunt.com/v2/api/graphql",
        "type": "api",
        "category": "opportunity",
        "trust_level": "medium",
        "language": "en",
        "fetch_interval_hours": 12,
    },
}

BLACKLIST_DOMAINS = [
    "scam.com", "get-rich-quick", "pyramid", "mlm-",
]

BLACKLIST_KEYWORDS = [
    "guaranteed profit", "1000% return", "no risk", "secret method",
    "work from home earn", "make money fast", "crypto signal free",
]
