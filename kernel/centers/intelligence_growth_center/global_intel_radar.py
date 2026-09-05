"""Global Intel Radar — 多源情报聚合"""
import sys, os
from pathlib import Path
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from intel_pipeline import run_pipeline

INTEL_SOURCES = ["hackernews", "techcrunch_rss", "arxiv_ai", "lobsters_rss"]

def scan() -> dict:
    result = run_pipeline(INTEL_SOURCES, module="global_intel")
    result.pop("_new_items", None)
    return result

def get_intel(limit: int = 30) -> list:
    from storage import get_items
    return get_items(module="global_intel", limit=limit)
