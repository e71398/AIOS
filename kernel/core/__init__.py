"""AIOS v4.0 Core Execution Protocol — 宪法级执行规程."""
from .execution_protocol import ExecutionProtocol, protocol, PROTOCOL_VERSION, PROTOCOL_NAME
from .protocol_validator import ProtocolValidator
from .protocol_loader import ProtocolLoader, load_for_ai
