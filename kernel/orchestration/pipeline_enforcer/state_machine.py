"""
State Machine — 管线状态机
===========================
INIT→REGISTERED→DISPATCHING→RUNNING→VALIDATING→COMPLETED
"""
import sys
sys.path.insert(0, '${AIOS_HOME}/kernel/tools')
from aios_bus import _redis_client, _is_available

STATES = ["INIT","REGISTERED","DISPATCHING","RUNNING","VALIDATING","COMPLETED"]
TERMINAL = {"COMPLETED","FAILED","ARCHIVED"}
VALID_TRANSITIONS = {
    "INIT": ["REGISTERED"], "REGISTERED": ["DISPATCHING","FAILED"],
    "DISPATCHING": ["RUNNING","BLOCKED"], "RUNNING": ["VALIDATING","BLOCKED","FAILED"],
    "VALIDATING": ["COMPLETED","PARTIALLY_COMPLETED"], "BLOCKED": ["ROLLING_BACK","FAILED"],
    "ROLLING_BACK": ["FAILED","DISPATCHING"],
}

class PipelineStateMachine:
    def __init__(self):
        self.prefix = "aios:pipeline"

    def get_state(self, pipeline_id: str) -> str:
        if not _is_available(): return "UNKNOWN"
        s = _redis_client.hget(f"{self.prefix}:registry:{pipeline_id}", "status")
        return s.decode() if s else "INIT"

    def transition_state(self, pipeline_id: str, new_state: str, reason: str = ""):
        if new_state not in STATES and new_state not in ("BLOCKED","FAILED","PARTIALLY_COMPLETED","ROLLING_BACK","ARCHIVED"):
            return False
        current = self.get_state(pipeline_id)
        if new_state not in VALID_TRANSITIONS.get(current, []):
            return False
        if _is_available():
            _redis_client.hset(f"{self.prefix}:registry:{pipeline_id}", mapping={
                "status": new_state, "transition_reason": reason[:200]})
        return True

    def is_terminal(self, state: str) -> bool:
        return state in TERMINAL
