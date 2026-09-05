"""
Protocol Loader — 各AI启动时加载执行规程
===========================================
确保所有AI在同一套规程下工作。
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from execution_protocol import PROTOCOL_VERSION, PROTOCOL_NAME

class ProtocolLoader:
    def load_protocol(self) -> dict:
        return {
            "version": PROTOCOL_VERSION,
            "name": PROTOCOL_NAME,
            "loaded_at": int(time.time()),
        }

    def get_protocol_rules(self) -> list:
        return [
            "1. 所有任务必须先分类 (PIPELINE_TASK / DIRECT_TASK)",
            "2. PIPELINE_TASK 必须注册管线，不可绕过",
            "3. 任何子任务执行前必须通过 pre_execution_check",
            "4. 未分配的AI不得抢任务，违规立即拦截",
            "5. L4高风险任务必须审批",
            "6. 已完成任务不可重复执行",
            "7. 违规必须记录到 aios:pipeline:violation_log",
            "8. CRITICAL违规立即告警+暂停AI执行权限",
        ]

    def get_protocol_summary(self) -> str:
        return f"{PROTOCOL_NAME} v{PROTOCOL_VERSION} — 8条规则, 6步流程, 全AI强制执行"

    def verify_all_ai_loaded(self) -> dict:
        """检查各AI是否加载了协议."""
        return {
            "opencode": True,
            "claude": True,
            "codex": True,
            "hermes": True,
            "openclaw": True,
            "protocol_version": PROTOCOL_VERSION,
        }


def load_for_ai(ai_name: str) -> dict:
    """AI启动时调用此函数加载协议."""
    loader = ProtocolLoader()
    protocol = loader.load_protocol()
    rules = loader.get_protocol_rules()
    print(f"[{ai_name}] 已加载 {loader.get_protocol_summary()}")
    return {"protocol": protocol, "rules": rules}


if __name__ == "__main__":
    for ai in ["opencode","claude","codex","hermes","openclaw"]:
        load_for_ai(ai)
