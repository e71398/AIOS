"""Data models for Autonomy & Initiative Center."""
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class AutonomyRun:
    engine_name: str = ""
    trigger_type: str = ""
    task_id: str = ""
    agent_id: str = ""
    input_summary: str = ""
    output_summary: str = ""
    status: str = "pending"
    confidence_score: float = 0.0
    token_cost: int = 0
    duration_ms: int = 0
    created_at: str = ""

@dataclass
class ProactivityScore:
    agent_id: str = ""
    date: str = ""
    total_score: int = 0
    curiosity_score: int = 0
    divergence_score: int = 0
    improvement_score: int = 0
    cleanup_score: int = 0
    penalty_score: int = 0

@dataclass
class AutonomyRecommendation:
    category: str = ""
    title: str = ""
    recommendation: str = ""
    confidence_score: float = 0.0
    risk_level: str = "low"
    status: str = "new"

@dataclass
class CuriosityReference:
    task_id: str = ""
    query: str = ""
    source_type: str = ""
    source_title: str = ""
    source_url: str = ""
    quality_score: float = 0.0
    used_in_output: bool = False

@dataclass
class CleanupRecord:
    cleanup_type: str = ""
    target_scope: str = ""
    item_count: int = 0
    action: str = "purge"
    result: str = "success"
    summary: str = ""

@dataclass
class CapabilityGap:
    gap: str = ""
    detail: str = ""
    severity: str = "info"
