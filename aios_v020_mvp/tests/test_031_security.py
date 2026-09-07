"""Security / authenticity gates added for closeout 031.

Covers:
  * SINGLE_JSON_PARSE
  * FENCED_JSON_PARSE
  * PREFIX_SUFFIX_JSON_PARSE
  * MULTIPLE_IDENTICAL_JSON_POLICY
  * MULTIPLE_CONFLICTING_JSON_REJECT
  * MALFORMED_JSON_REJECT
  * TRUNCATED_JSON_REJECT
  * Tool-execution sink (AIOS_TOOL_SINK) records host-side executions
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from aios_v020_mvp.structured_contract import (  # noqa: E402
    ContractError,
    parse_role_response,
    resolve_single_json,
    extract_first_json,
)


def _env(data: dict):
    return {"schema_version": "aios-v020-1.0", "role": "planner",
            "status": "ok", "data": data, "notes": ""}


def test_single_json_parse_pass():
    obj, diag = resolve_single_json(json.dumps(_env({"goal": "g", "steps": []})))
    assert obj["role"] == "planner"
    assert diag["parser"] == "exact"


def test_fenced_json_parse_pass():
    text = "```json\n" + json.dumps(_env({"goal": "g", "steps": []})) + "\n```"
    obj, diag = resolve_single_json(text)
    assert obj["role"] == "planner"
    assert diag["parser"] == "exact"


def test_prefix_suffix_text_json_pass():
    text = (
        "Here is the plan you asked for:\n"
        + json.dumps(_env({"goal": "g", "steps": []}))
        + "\nHope that helps."
    )
    obj, diag = resolve_single_json(text)
    assert obj["role"] == "planner"
    assert diag["parser"] == "single_candidate"


def test_multiple_identical_json_accept_but_flagged():
    env = _env({"goal": "g", "steps": []})
    text = json.dumps(env) + "\n" + json.dumps(env)
    obj, diag = resolve_single_json(text)
    assert obj["role"] == "planner"
    assert diag["parser"] == "multiple_identical"
    assert diag["duplicates"] == 2


def test_multiple_conflicting_json_reject():
    a = _env({"goal": "write A", "steps": []})
    b = _env({"goal": "write B", "steps": []})
    text = json.dumps(a) + "\n" + json.dumps(b)
    with pytest.raises(ContractError) as exc:
        resolve_single_json(text)
    assert exc.value.reason == "multiple_conflicting_json"


def test_malformed_json_reject():
    with pytest.raises(ContractError) as exc:
        resolve_single_json('{"schema_version": "aios-v020-1.0", broken')
    assert exc.value.reason in ("no_json_object", "truncated_json", "malformed_json")


def test_truncated_json_reject():
    text = '{"schema_version": "aios-v020-1.0", "role": "planner", "status": "ok", "data": {"goal": "trunc'
    with pytest.raises(ContractError) as exc:
        resolve_single_json(text)
    assert exc.value.reason == "truncated_json"


def test_empty_reject():
    with pytest.raises(ContractError) as exc:
        resolve_single_json("   ")
    assert exc.value.reason == "empty_response"


def test_extract_first_json_returns_none_on_conflict():
    a = _env({"goal": "write A", "steps": []})
    b = _env({"goal": "write B", "steps": []})
    text = json.dumps(a) + "\n" + json.dumps(b)
    assert extract_first_json(text) is None


def test_parse_role_response_passes_envelope():
    env = parse_role_response(json.dumps(_env({"goal": "g", "steps": []})), "planner")
    assert env["role"] == "planner"
    assert env["data"]["goal"] == "g"


def test_parse_role_response_rejects_conflict():
    a = _env({"goal": "write A", "steps": []})
    b = _env({"goal": "write B", "steps": []})
    text = json.dumps(a) + "\n" + json.dumps(b)
    with pytest.raises(ContractError) as exc:
        parse_role_response(text, "planner")
    assert exc.value.reason == "multiple_conflicting_json"


def test_tool_sink_records_host_execution(tmp_path):
    """AIOS_TOOL_SINK must record one JSON line per tool invocation."""
    import threading
    from aios_v020_mvp.persistence import FileResultStore
    from aios_v020_mvp.tools import ToolRegistry, ToolInvocation, register_default_file_tools

    sink_path = tmp_path / "tools.jsonl"
    os.environ["AIOS_TOOL_SINK"] = str(sink_path)
    import aios_v020_mvp.tools.registry as reg
    # Re-run module-level env read by patching the module global.
    reg._TOOL_SINK_PATH = str(sink_path)

    store = FileResultStore(tmp_path / "data")
    registry = ToolRegistry()
    register_default_file_tools(registry)
    registry.invoke(
        ToolInvocation("file_write", {"path": "notes.txt", "content": "hi"}),
        "wf-1", store,
    )
    registry.invoke(
        ToolInvocation("file_read", {"path": "notes.txt"}),
        "wf-1", store,
    )
    registry.invoke(
        ToolInvocation("file_list", {"path": ""}),
        "wf-1", store,
    )
    del os.environ["AIOS_TOOL_SINK"]

    lines = sink_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    recs = [json.loads(l) for l in lines]
    tools = [r["tool"] for r in recs]
    assert tools == ["file_write", "file_read", "file_list"]
    assert all(r["host_executed"] is True for r in recs)
    assert all(r["status"] == "ok" for r in recs)
    assert all(r["workflow_id"] == "wf-1" for r in recs)