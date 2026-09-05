"""
Open Source Radar — GitHub高价值项目发现
===========================================
AIOS v4.0 Intelligence & Growth Center
"""
import sys, os
from pathlib import Path
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from intel_pipeline import run_pipeline

RADAR_SOURCES = ["github_trending", "github_trending_weekly"]

def scan() -> dict:
    result = run_pipeline(RADAR_SOURCES, module="open_source")
    result.pop("_new_items", None)
    return result

def get_trending(limit: int = 20) -> list:
    from storage import get_items
    return get_items(module="open_source", limit=limit, min_score=0)
