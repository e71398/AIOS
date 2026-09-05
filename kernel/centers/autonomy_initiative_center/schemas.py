"""Schemas — 输入输出结构定义."""
from dataclasses import dataclass, field

@dataclass
class TaskSchema:
    task_id: str = ""
    task_name: str = ""
    complexity: str = "low"
    agent_id: str = ""
    priority: int = 3

@dataclass
class CuriosityPackSchema:
    query: str = ""
    internal_hits: list = field(default_factory=list)
    historical_hits: list = field(default_factory=list)
    external_hits: list = field(default_factory=list)
    reference_pack_score: float = 0.0

@dataclass
class DivergenceSolutionSchema:
    solution_id: str = ""
    solution_type: str = ""
    approach: str = ""
    risk: str = "medium"
    cost: str = "medium"

@dataclass
class RecommendationSchema:
    category: str = ""
    recommendation: str = ""
    confidence: float = 0.0

@dataclass
class ScoreEventSchema:
    agent_id: str = ""
    event_type: str = ""
    delta: int = 0

@dataclass
class CleanupResultSchema:
    expired_found: int = 0
    purged: int = 0
    knowledge_decayed: int = 0
