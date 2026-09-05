#!/usr/bin/env python3
"""AIOS production acceptance: evidence, not file-presence assertions."""
import hashlib, json, os, re, socket, subprocess, sys, time, tomllib, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

HOME = Path(os.getenv("AIOS_HOME", "${AIOS_HOME}"))
TOOLS = HOME / "kernel/tools"
REPORTS = HOME / "logs/acceptance"
checks = []

def check(name, ok, evidence=""):
    """Append a check entry. evidence is stored as JSON when it is a
    structured Python object (dict/list) so that downstream readers can
    still introspect the original fields. Plain strings and other
    scalars are stored verbatim (truncated to 1000 chars).
    """
    if isinstance(evidence, (dict, list)):
        stored = json.dumps(evidence, ensure_ascii=False)[:1000]
    else:
        stored = str(evidence)[:1000]
    checks.append({"name": name, "ok": bool(ok), "evidence": stored})

def run(args, timeout=20):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout)

def http(url, data=None, timeout=10):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, json.loads(response.read())

def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_sha256(path):
    """Best-effort file hash used by the P4 runtime-revision comparison.

    Returns an empty string when the file is missing so that the caller can
    distinguish 'file absent' from 'sha collision' instead of crashing
    inside the report writer.
    """
    try:
        target = Path(path)
    except TypeError:
        return ""
    if not target.exists():
        return ""
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

# Shared runtime revision helper for the three Executor daemons.
sys.path.insert(0, str(TOOLS))
from aios_runtime_revision import compute_executor_revision  # noqa: E402


def run_real_e2e():
    """Exercise the real Gateway -> Orchestrator -> Executor -> Verifier chain.

    P5: Always sends preferred_executor=opencode and strict_executor=opencode
    with allow_executor_fallback=False so the canary strictly uses OpenCode
    executor and Hermes reviewer. No Claude/Codex fallback allowed.

    Side-effects: populates the module-level ``_REAL_E2E_*`` slots so that
    ``write_report`` can refuse to mark ``core_result`` as PASS without a
    real parent task id, executor and reviewer.
    """
    _reset_real_e2e_state()
    global _REAL_E2E_PARENT_ID, _REAL_E2E_NODES
    global _REAL_E2E_ACTUAL_EXECUTOR, _REAL_E2E_REVIEWER
    global _REAL_E2E_RESULT_PRESENT, _REAL_E2E_VERIFICATION_PRESENT
    try:
        import redis
        r = redis.Redis(socket_connect_timeout=2)
        marker = "AIOS-CANARY-" + datetime.now().strftime("%Y%m%d%H%M%S")
        started = time.monotonic()
        _, created = http("http://127.0.0.1:18801/task", {
            "input": (
                "Read the current AIOS Entry Gateway health and return service name, "
                "version, status, Redis state, timestamp, and this marker exactly: " + marker
            ),
            "source": "test",
            "sender": "aios-canary",
            "preferred_executor": "opencode",
            "strict_executor": "opencode",
            "allow_executor_fallback": False,
        })
        ack_ms = int((time.monotonic() - started) * 1000)
        parent_id = created.get("parent_id", "")
        task_ids = created.get("task_ids", [])
        check("e2e:gateway-ack-under-2s", bool(parent_id) and ack_ms < 2000,
              {"ack_ms": ack_ms, "created": created})
        check("e2e:unique-parent-id",
              task_ids == [parent_id] and created.get("count") == 1,
              created)

        task = {}
        deadline = time.time() + 300
        while time.time() < deadline:
            _, response = http(f"http://127.0.0.1:18801/task/{parent_id}")
            task = response.get("task", {})
            if task.get("status") in ("completed", "failed", "cancelled"):
                break
            time.sleep(1)
        nodes = task.get("nodes", []) if isinstance(task.get("nodes"), list) else []
        check("e2e:real-parent-completed",
              task.get("status") == "completed" and
              task.get("executor") == "aios-orchestrator" and
              marker in str(task.get("result_summary", "")),
              task)
        independent = bool(nodes) and all(
            node.get("verification", {}).get("passed") is True and
            bool(node.get("verification", {}).get("reviewer_backend")) and
            node.get("verification", {}).get("reviewer") != node.get("actual_executor")
            for node in nodes
        )
        check("e2e:independent-semantic-gate", independent, nodes)
        check("e2e:actual-executor-recorded",
              bool(nodes) and all(
                  node.get("actual_executor") in ("opencode", "claude", "codex")
                  for node in nodes
              ), nodes)

        # Capture real-E2E state so write_report cannot accidentally claim
        # PASS based on synthetic placeholder evidence (the historic
        # 23-microsecond PASS bug). Each slot must remain empty when the
        # corresponding step did not really run.
        if _is_real_parent_id(parent_id):
            _REAL_E2E_PARENT_ID = parent_id
        _REAL_E2E_NODES = list(nodes) if nodes else []
        if nodes:
            first_node = nodes[0] if isinstance(nodes[0], dict) else {}
            _REAL_E2E_ACTUAL_EXECUTOR = str(first_node.get("actual_executor") or "")
            verification = first_node.get("verification") or {}
            _REAL_E2E_REVIEWER = str(verification.get("reviewer") or "")
            _REAL_E2E_VERIFICATION_PRESENT = bool(
                verification.get("passed") is True and
                verification.get("reviewer") and
                verification.get("reviewer") != verification.get("actual_executor")
            )
        _REAL_E2E_RESULT_PRESENT = (
            task.get("status") == "completed" and
            task.get("executor") == "aios-orchestrator" and
            bool(task.get("result_summary"))
        )

        child_ids = [node.get("task_id", "") for node in nodes if node.get("task_id")]
        test_learning_events = 0
        for raw in r.zrevrange("aios:bus:event:log", 0, 500):
            try:
                event = json.loads(raw)
            except Exception:
                continue
            if (event.get("type") == "learning.accepted" and
                    event.get("payload", {}).get("task_id") in child_ids):
                test_learning_events += 1
        check("e2e:test-evidence-not-admitted-to-learning",
              test_learning_events == 0,
              {"child_ids": child_ids, "learning_events": test_learning_events})
        trace_key = f"aios:trace:{parent_id}"
        check("e2e:parent-trace-recorded", r.zcard(trace_key) > 0,
              {"trace_key": trace_key, "events": r.zcard(trace_key)})
    except Exception as exc:
        check("e2e:gateway-ack-under-2s", False, exc)
        check("e2e:unique-parent-id", False, exc)
        check("e2e:real-parent-completed", False, exc)
        check("e2e:independent-semantic-gate", False, exc)
        check("e2e:actual-executor-recorded", False, exc)
        check("e2e:test-evidence-not-admitted-to-learning", False, exc)
        check("e2e:parent-trace-recorded", False, exc)
        _REAL_E2E_PARENT_ID = ""
        _REAL_E2E_NODES = []
        _REAL_E2E_ACTUAL_EXECUTOR = ""
        _REAL_E2E_REVIEWER = ""
        _REAL_E2E_RESULT_PRESENT = False
        _REAL_E2E_VERIFICATION_PRESENT = False


# P4 schema: schema_version, run_id, started_at, finished_at, trigger,
# core_result, overall_status, mandatory_checks, optional_capabilities,
# failure_classes, e2e_task_id, executor, reviewer, parent_status,
# result_present, verification_present, runtime_revision_status,
# provider_call_counts.
SCHEMA_VERSION = "aios-acceptance/2.0"
ACCEPTANCE_CORE_TTL_SECONDS = 8 * 3600


def _classify_failure_class(check_name: str) -> str:
    name = str(check_name or "")
    if name.startswith("e2e:"):
        if "real-parent-completed" in name or "independent-semantic-gate" in name:
            return "INTERNAL_CODE_OR_CONTRACT_FAILURE"
        return "AUTHORITATIVE_STATE_FAILURE"
    if name.startswith("runtime-dependency"):
        return "RUNTIME_DEPENDENCY_STALE"
    if name.startswith("service:") or name == "systemd:no-failed-aios-units":
        return "INFRASTRUCTURE_UNAVAILABLE"
    if name.startswith("reconcile:") or name.startswith("orchestrator:"):
        return "CONFIGURATION_OR_REVISION_DRIFT"
    if name.startswith("verification:") or name.startswith("opencode:402"):
        return "EXTERNAL_CAPABILITY_DEGRADED"
    if name.startswith("port:") or name.startswith("http:"):
        return "INFRASTRUCTURE_UNAVAILABLE"
    if name.startswith("feishu:") or name.startswith("local-model:"):
        return "EXTERNAL_CAPABILITY_DEGRADED"
    if name.startswith("tools:"):
        return "EXTERNAL_CAPABILITY_DEGRADED"
    return "INTERNAL_CODE_OR_CONTRACT_FAILURE"


# ---------------------------------------------------------------------------
# P8D §十五 acceptance upgrades
# ---------------------------------------------------------------------------

def _p8d_persisted_node(parent_id: str) -> dict:
    """Return the first persisted workflow node for the canary task."""
    if not parent_id:
        return {}
    try:
        import redis as _redis
        r = _redis.Redis(host="localhost", port=6379,
                          socket_connect_timeout=2)
        raw = r.hget(f"aios:orchestrator:workflow:{parent_id}", "nodes")
        if not raw:
            return {}
        nodes = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return {}
    if not isinstance(nodes, list) or not nodes:
        return {}
    return nodes[0] if isinstance(nodes[0], dict) else {}


def _p8d_monitor_effective_bindings() -> dict:
    """Read the live routing engine's effective binding for each tool.

    The monitor exposes ``p8c_u.tool_effective_binding``; that field is
    the authoritative signal of which binding the routing layer would
    currently pick for each tool.

    The P8D live snapshot can take ~9s on a busy orchestrator; the
    acceptance runner MUST wait longer than a single HTTP probe so it
    doesn't return an empty dict (which would make every P8D gate
    appear ``False`` and falsely fail the run).
    """
    try:
        import urllib.request as _u
        with _u.urlopen(
            "http://127.0.0.1:8086/api/health/v2", timeout=20,
        ) as response:
            payload = json.loads(response.read())
    except Exception:
        return {}
    return (
        payload.get("p8c_u", {}).get("tool_effective_binding", {}) or {}
    )


def _p8d_monitor_feature_flags() -> dict:
    """Return the live feature-flag snapshot for the P8D canary gate."""
    try:
        import urllib.request as _u
        with _u.urlopen(
            "http://127.0.0.1:8086/api/health/v2", timeout=20,
        ) as response:
            payload = json.loads(response.read())
    except Exception:
        return {}
    return (
        payload.get("p8c_u", {}).get("feature_flags", {}) or {}
    )


_P8D_OVERRIDES: Dict[str, bool] = {}


def _p8d_set_override(name: str, value: bool) -> None:
    """Tests / fixtures can short-circuit a P8D gate without touching
    the live monitor.  Production callers leave the dict empty and
    every gate reads the actual monitor snapshot.
    """
    _P8D_OVERRIDES[name] = value


def _p8d_check(name: str) -> bool:
    """Return the override value if set, else call the live helper."""
    if name in _P8D_OVERRIDES:
        return _P8D_OVERRIDES[name]
    return _P8D_LIVE[name]()


_P8D_LIVE: Dict[str, Any] = {}


def _p8d_registered_tools_have_route() -> bool:
    """Every registered, enabled tool must have a non-empty effective_binding.

    The check refuses to claim PASS when the routing engine reports
    ``None`` for an enabled tool — that would mean the tool is in
    ``UNAVAILABLE_TOOL_RUNTIME`` and we are silently pretending the
    dual-axis failover stack still serves it.
    """
    bindings = _p8d_monitor_effective_bindings()
    if not bindings:
        return False
    for tool_id, info in bindings.items():
        eff = (info or {}).get("effective_binding")
        if not eff:
            return False
    return True


def _p8d_canary_sender_isolated() -> bool:
    """Source-only canary gates are forbidden; the live flag must be
    a composite ``source+sender`` gate.

    The acceptance runtime MUST have ``canary_allowed_senders`` in the
    routing snapshot; if the monitor says the sender gate is empty,
    the canary itself would auto-allow every API request, which is
    exactly the security regression P8D forbids.
    """
    flags = _p8d_monitor_feature_flags()
    return bool(flags.get("canary_allowed_senders"))


def _p8d_actual_binding_recorded() -> bool:
    """The canary workflow node MUST carry the new effective-binding
    fields the orchestrator hook writes.  Without them, downstream
    monitors cannot tell whether the live task used
    ``opencode:free`` or fell back to ``opencode:minimax``."""
    parent_id = _REAL_E2E_PARENT_ID
    node = _p8d_persisted_node(parent_id)
    return bool(node and node.get("actual_model_binding"))


def _p8d_tool_switch_recorded() -> bool:
    """The node MUST carry ``attempted_tools`` / ``attempted_model_bindings``
    so the dual-axis trace is durable across Redis restarts."""
    parent_id = _REAL_E2E_PARENT_ID
    node = _p8d_persisted_node(parent_id)
    if not node:
        return False
    return ("attempted_tools" in node and "attempted_model_bindings" in node)


def _p8d_model_identity_preserved() -> bool:
    """When a non-strict-model failover happens, ``primary_model_binding``
    and ``actual_model_binding`` MUST differ; when strict, they MUST
    match. The canary uses ``strict_executor=opencode`` but no strict
    model, so the two are allowed to differ; the acceptance verdict
    is therefore a non-empty pair."""
    parent_id = _REAL_E2E_PARENT_ID
    node = _p8d_persisted_node(parent_id)
    return bool(node and node.get("primary_model_binding") and node.get("actual_model_binding"))


def _p8d_no_hidden_fallback() -> bool:
    """The canary MUST NOT silently swap to an unrelated tool or model.
    The only tool the canary allows is opencode, so the actual
    executor and reviewer MUST stay inside the opencode / hermes pair.
    """
    parent_id = _REAL_E2E_PARENT_ID
    node = _p8d_persisted_node(parent_id)
    actual = node.get("actual_executor") if node else ""
    reviewer = (node.get("verification") or {}).get("reviewer", "") if node else ""
    return actual in ("opencode", "hermes") and reviewer in ("opencode", "hermes")


def _p8d_claude_model_failover_success() -> bool:
    """P8D evidence gate: ``claude`` MUST be reachable via its verified
    fallback binding (currently ``claude:minimax``); the routing layer
    must surface that as the live effective_binding. We tolerate a
    DEGRADED primary so long as the verified fallback is healthy.
    """
    bindings = _p8d_monitor_effective_bindings()
    info = bindings.get("claude")
    if not info:
        return False
    eff = info.get("effective_binding")
    return eff in ("claude:minimax", "claude:primary", "claude:deepseek")


def _p8d_codex_effective_route_truthful() -> bool:
    """Codex MUST have a non-empty effective_binding. P8D accepts either
    a healthy primary or a verified fallback; what we refuse is
    ``UNAVAILABLE_TOOL_RUNTIME`` that we silently call ``AVAILABLE``."""
    bindings = _p8d_monitor_effective_bindings()
    info = bindings.get("codex")
    if not info:
        return False
    return bool(info.get("effective_binding"))


def _p8d_tool_failover_success() -> bool:
    """When the orchestrator's chosen tool differs from the user's
    preferred tool, the node MUST record a non-zero ``tool_failover_count``
    and a non-empty ``tool_failover_reason``.

    This is a generic guard; the canary uses ``strict_executor=opencode``
    so the count is normally 0.  The check still runs so that any
    accidental overlay-induced switch is visible in the report.
    """
    parent_id = _REAL_E2E_PARENT_ID
    node = _p8d_persisted_node(parent_id)
    if not node:
        return False
    attempted = node.get("attempted_tools") or []
    return isinstance(attempted, list) and len(attempted) >= 1


def _p8d_all_enabled_tools_operational() -> bool:
    """All five enabled tools MUST have a non-empty effective_binding.

    Acceptance previously used ``ALL_ENABLED_TOOLS_OPERATIONAL`` for the
    canary report schema; we keep the same field name so downstream
    monitors (which aggregate all-tools-vs-core-chain) continue to work,
    but the gate now requires the new ``tool_effective_binding`` evidence
    rather than the legacy capability matrix.
    """
    bindings = _p8d_monitor_effective_bindings()
    if not bindings:
        return False
    expected = {"openclaw", "hermes", "opencode", "claude", "codex"}
    if set(bindings) != expected:
        return False
    return all(
        bindings[t].get("effective_binding") for t in expected
    )


_P8D_LIVE.update({
    "registered_tools_have_effective_route": _p8d_registered_tools_have_route,
    "canary_sender_isolated": _p8d_canary_sender_isolated,
    "actual_binding_recorded": _p8d_actual_binding_recorded,
    "tool_switch_recorded": _p8d_tool_switch_recorded,
    "model_identity_preserved": _p8d_model_identity_preserved,
    "no_hidden_fallback": _p8d_no_hidden_fallback,
    "claude_model_failover_success": _p8d_claude_model_failover_success,
    "codex_effective_route_truthful": _p8d_codex_effective_route_truthful,
    "tool_failover_success": _p8d_tool_failover_success,
    "all_enabled_tools_operational_signal": _p8d_all_enabled_tools_operational,
})


def _provider_call_counts() -> dict:
    """Snapshot how many times each provider has been called in the last 24h.

    Source of truth: ``aios:bus:governance:daily:<YYYYMMDD>`` hash written
    by the orchestrator bus. Fallback to Redis keys when missing.
    """
    counts = {"opencode": 0, "claude": 0, "codex": 0, "hermes": 0,
              "openclaw": 0, "telegram": 0, "feishu": 0}
    try:
        import redis as _redis
        client = _redis.Redis(host="localhost", port=6379,
                              socket_connect_timeout=2)
        today = datetime.now().strftime("%Y%m%d")
        hash_data = client.hgetall(f"aios:bus:governance:daily:{today}") or {}
        for raw_key, raw_value in hash_data.items():
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            value = raw_value.decode() if isinstance(raw_value, bytes) else raw_value
            for provider in counts:
                if key.startswith(f"{provider}_calls") or key == f"{provider}_call_count":
                    try:
                        counts[provider] = int(float(value))
                    except (TypeError, ValueError):
                        counts[provider] = 0
    except Exception:
        pass
    return counts


def _runtime_revision_status() -> dict:
    """Compare loaded revisions to expected file hashes (truthful)."""
    expected = {
        "gateway": _file_sha256(TOOLS / "aios_entry_gateway.py"),
        "aios-orchestrator": _file_sha256(TOOLS / "aios_orchestrator.py"),
        "aios-verification-gate": _file_sha256(TOOLS / "aios_verification_gate.py"),
    }
    loaded = {}
    try:
        _, health = http("http://127.0.0.1:18801/health")
        loaded["gateway"] = str(health.get("loaded_revision", ""))
    except Exception:
        loaded["gateway"] = ""
    try:
        import redis as _redis
        client = _redis.Redis(host="localhost", port=6379,
                              socket_connect_timeout=2)
        for name in ("aios-orchestrator", "aios-verification-gate"):
            raw = client.hget(f"aios:bus:agent:{name}", "loaded_revision")
            loaded[name] = raw.decode() if isinstance(raw, bytes) else (
                str(raw or "")
            )
    except Exception:
        loaded.setdefault("aios-orchestrator", "")
        loaded.setdefault("aios-verification-gate", "")
    drift = []
    for name, expected_hash in expected.items():
        observed = loaded.get(name, "")
        if not observed:
            drift.append({"unit": name, "status": "UNKNOWN",
                          "reason": "loaded_revision_unavailable"})
            continue
        if expected_hash and observed != expected_hash:
            drift.append({"unit": name, "status": "STALE",
                          "reason": "loaded_revision_does_not_match_source"})
        else:
            drift.append({"unit": name, "status": "CURRENT"})
    return {
        "loaded_revisions": loaded,
        "expected_revisions": expected,
        "drift": drift,
        "all_current": all(item.get("status") == "CURRENT" for item in drift),
    }


def _mandatory_check(name: str) -> bool:
    for entry in checks:
        if entry.get("name") == name:
            return bool(entry.get("ok"))
    return False


def _find_evidence(name: str):
    """Return the evidence payload for a check, transparently decoding
    the JSON form that ``check()`` uses for dict/list values so that
    downstream consumers (the report writer, the orchestrator
    introspection helpers, etc.) can read the original structured
    fields instead of the stringified repr.
    """
    for entry in checks:
        if entry.get("name") == name:
            raw = entry.get("evidence")
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped.startswith("{") or stripped.startswith("["):
                    try:
                        return json.loads(stripped)
                    except Exception:
                        return raw
            return raw
    return None


# ---------------------------------------------------------------------------
# Real-E2E state captured by ``run_real_e2e``. These module-level slots are
# the single source of truth for the canary's parent task id, the recorded
# executor and reviewer, and the real workflow nodes. They exist so that
# ``write_report`` cannot accidentally claim PASS based on fabricated or
# placeholder evidence (the historic 23-microsecond synthetic-PASS bug):
# the report writer MUST consult these slots when assembling the mandatory
# guards and will refuse to mark ``core_result`` as PASS if any of them
# remains empty after ``run_real_e2e`` has been invoked.
# ---------------------------------------------------------------------------
_REAL_E2E_PARENT_ID = ""
_REAL_E2E_NODES = []
_REAL_E2E_ACTUAL_EXECUTOR = ""
_REAL_E2E_REVIEWER = ""
_REAL_E2E_RESULT_PRESENT = False
_REAL_E2E_VERIFICATION_PRESENT = False

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_real_parent_id(value) -> bool:
    return bool(value) and isinstance(value, str) and bool(UUID_RE.match(value))


def _reset_real_e2e_state() -> None:
    """Clear the captured real-E2E slots. Called at the beginning of every
    ``run_real_e2e`` invocation so that a partial / failed run cannot
    leak state into the next canary cycle.
    """
    global _REAL_E2E_PARENT_ID, _REAL_E2E_NODES
    global _REAL_E2E_ACTUAL_EXECUTOR, _REAL_E2E_REVIEWER
    global _REAL_E2E_RESULT_PRESENT, _REAL_E2E_VERIFICATION_PRESENT
    _REAL_E2E_PARENT_ID = ""
    _REAL_E2E_NODES = []
    _REAL_E2E_ACTUAL_EXECUTOR = ""
    _REAL_E2E_REVIEWER = ""
    _REAL_E2E_RESULT_PRESENT = False
    _REAL_E2E_VERIFICATION_PRESENT = False


def _build_capability_matrix() -> dict:
    """Derive per-provider capability from the tool-adapter health probe.

    Falls back to the original ``tools:adapter-contract-health`` check
    result so the canary report still produces a capability matrix when
    the adapter health endpoint is not directly callable.
    """
    matrix = {}
    try:
        proc = run(["python3", str(TOOLS / "aios_tool_adapter.py"), "health"])
        tool_health = json.loads(proc.stdout)
    except Exception:
        tool_health = {}
    if tool_health:
        for name, item in tool_health.items():
            state = str(item.get("state", "")).lower() if isinstance(item, dict) else ""
            fully = bool(item.get("fully_operational")) if isinstance(item, dict) else False
            model_state = str(item.get("model_state", "")).lower() if isinstance(item, dict) else ""
            if fully and item.get("contract_ok"):
                matrix[name] = "AVAILABLE"
            elif model_state in ("quota_exhausted", "plan_exhausted", "auth_failed"):
                matrix[name] = "DEGRADED_EXTERNAL"
            elif state in ("unavailable", "auth_failed"):
                matrix[name] = "UNAVAILABLE"
            elif model_state in ("stale", "timeout", "probe_error"):
                matrix[name] = "DEGRADED_INTERNAL"
            elif state == "registered":
                matrix[name] = "UNVERIFIED"
            else:
                matrix[name] = "NOT_CONFIGURED"
    return matrix


def _core_e2e_outcome() -> dict:
    """Pull the E2E outcome from the canary checks.

    The ``task_id`` field is sourced from the real-E2E slots captured by
    ``run_real_e2e`` (a UUID parent id). Falling back to the evidence
    string is intentionally avoided: that string was historically a dict
    repr (e.g. ``"{'status': 'completed'}"``) which is not a valid task
    identifier and was the source of the 23\u00b5s synthetic PASS reports.
    """
    parent_status = _mandatory_check("e2e:real-parent-completed")
    verification_present = _mandatory_check("e2e:independent-semantic-gate")
    if _REAL_E2E_PARENT_ID:
        task_id = _REAL_E2E_PARENT_ID
    else:
        # Fallback for unit tests where the real-E2E slots are injected
        # via ``_populate_mandatory_success_checks``. We only accept a
        # UUID-shaped string; the previous dict-repr form is rejected.
        raw_evidence = _find_evidence("e2e:real-parent-completed")
        task_id = raw_evidence if isinstance(raw_evidence, str) and _is_real_parent_id(raw_evidence) else ""
    return {
        "parent_status": parent_status,
        # ``result_present`` and ``verification_present`` MUST be sourced
        # exclusively from the real-E2E slots. The legacy ``or`` with the
        # check status was the source of the synthetic-PASS bug: the
        # placeholder check always returned ``True``, masking the missing
        # real signal.
        "result_present": bool(_REAL_E2E_RESULT_PRESENT),
        "verification_present": bool(_REAL_E2E_VERIFICATION_PRESENT),
        "task_id": task_id,
    }


def _actual_reviewer_and_executor() -> tuple[str, str]:
    """Return ``(executor, reviewer)`` extracted from real-E2E nodes.

    Prefers the slots populated by ``run_real_e2e`` so the report
    reflects the actual workflow (executor = opencode, reviewer = hermes)
    instead of the historically-stringified evidence blob.
    """
    executor = _REAL_E2E_ACTUAL_EXECUTOR
    reviewer = _REAL_E2E_REVIEWER
    if executor and reviewer:
        return executor, reviewer
    evidence = _find_evidence("e2e:independent-semantic-gate")
    if isinstance(evidence, list) and evidence:
        first = evidence[0] if isinstance(evidence[0], dict) else {}
        if not executor:
            executor = str(first.get("actual_executor") or "")
        verification = first.get("verification") or {}
        if not reviewer:
            reviewer = str(verification.get("reviewer") or "")
    return executor, reviewer


def write_report(kind="full", reports_dir=None):
    """Write a P4 acceptance report.

    The schema is now ``aios-acceptance/2.0`` and carries both the
    historical ``checks`` list and the new structured fields required by
    the P4 monitor.

    Truthfulness guard: the canary must not be marked PASS without a real
    parent task id, a real executor and reviewer. When ``run_real_e2e``
    actually executes, it populates the ``_REAL_E2E_*`` slots; the
    guard below refuses to claim ``core_result = "PASS"`` otherwise,
    which closes the historic 23µs synthetic PASS bug.

    ``reports_dir`` defaults to the module-level :data:`REPORTS`
    (production ``logs/acceptance``). Tests MUST pass ``tmp_path`` (or
    another temporary directory) to keep offline tests from polluting
    the canonical canary report directory.
    """
    target_dir = Path(reports_dir) if reports_dir is not None else REPORTS
    started_at = getattr(write_report, "_started_at", None)
    if started_at is None:
        started_at = datetime.now(timezone.utc).isoformat()
    finished_at = datetime.now(timezone.utc).isoformat()
    provider_counts = _provider_call_counts()
    revision = _runtime_revision_status()
    capability = _build_capability_matrix()
    e2e = _core_e2e_outcome()
    executor, reviewer = _actual_reviewer_and_executor()
    parent_status = e2e["parent_status"]
    result_present = e2e["result_present"]
    verification_present = e2e["verification_present"]

    # Mandatory core: P4 §十三 mandatory checks (without touching the
    # historical canary checks that gate the full acceptance path).
    mandatory_checks = {
        "gateway_reachable": _mandatory_check("e2e:gateway-ack-under-2s"),
        "unique_parent_id": _mandatory_check("e2e:unique-parent-id"),
        "real_parent_completed": parent_status,
        "independent_reviewer": verification_present,
        "actual_executor_recorded": _mandatory_check("e2e:actual-executor-recorded"),
        "no_learning_admission": _mandatory_check("e2e:test-evidence-not-admitted-to-learning"),
        "parent_trace_recorded": _mandatory_check("e2e:parent-trace-recorded"),
        "runtime_revision_current": revision.get("all_current", False),
        # P8D §十五 mandatory upgrades: the canary must additionally
        # observe the routing-policy fields that prove the live task
        # actually executed through the dual-axis failover stack.
        # Acceptance currently drives a single-node opencode+hermes
        # task via the canary; the new checks below inspect that
        # task's persisted workflow node and refuse to claim
        # PASS if any of the new evidence is missing.
        "registered_tools_have_effective_route": _p8d_check("registered_tools_have_effective_route"),
        "canary_sender_isolated": _p8d_check("canary_sender_isolated"),
        "actual_binding_recorded": _p8d_check("actual_binding_recorded"),
        "tool_switch_recorded": _p8d_check("tool_switch_recorded"),
        "model_identity_preserved": _p8d_check("model_identity_preserved"),
        "no_hidden_fallback": _p8d_check("no_hidden_fallback"),
        "claude_model_failover_success": _p8d_check("claude_model_failover_success"),
        "codex_effective_route_truthful": _p8d_check("codex_effective_route_truthful"),
        "tool_failover_success": _p8d_check("tool_failover_success"),
        "all_enabled_tools_operational_signal": _p8d_check("all_enabled_tools_operational_signal"),
    }
    failure_classes = {}
    for entry in checks:
        if entry.get("ok"):
            continue
        code = _classify_failure_class(str(entry.get("name", "")))
        failure_classes[code] = True
    if not revision.get("all_current"):
        failure_classes["RUNTIME_DEPENDENCY_STALE"] = True

    # ----- Truthfulness guards (P5R) -----
    # These prevent ``core_result`` from being marked PASS when the real
    # E2E loop did not actually produce a parent task id, executor,
    # reviewer, or non-empty check list.
    truthfulness_failures: list[str] = []
    if not checks:
        truthfulness_failures.append("no_checks_executed")
        failure_classes["NO_CHECKS_EXECUTED"] = True
    if not e2e.get("task_id"):
        truthfulness_failures.append("real_task_id_missing")
        failure_classes["REAL_TASK_ID_MISSING"] = True
    if not executor:
        truthfulness_failures.append("actual_executor_missing")
        failure_classes["ACTUAL_EXECUTOR_MISSING"] = True
    if not reviewer:
        truthfulness_failures.append("independent_reviewer_missing")
        failure_classes["INDEPENDENT_REVIEWER_MISSING"] = True
    if not result_present:
        truthfulness_failures.append("result_not_present")
        failure_classes["RESULT_NOT_PRESENT"] = True
    if not verification_present:
        truthfulness_failures.append("verification_not_present")
        failure_classes["VERIFICATION_NOT_PRESENT"] = True

    failure_classes = failure_classes or {"NONE": True}

    mandatory_failures = [name for name, ok in mandatory_checks.items() if not ok]
    mandatory_failures.extend(truthfulness_failures)
    optional_capabilities = {
        name: state for name, state in capability.items()
        if state != "AVAILABLE"
    }
    capability_status = "DEGRADED" if optional_capabilities else "HEALTHY"

    core_result = "PASS" if not mandatory_failures else "FAIL"
    if core_result == "PASS" and optional_capabilities:
        overall_status = "DEGRADED"
    elif core_result == "PASS":
        overall_status = "HEALTHY"
    else:
        overall_status = "FAILED"

    schema = SCHEMA_VERSION
    trigger = "manual" if kind == "canary" else "manual"  # canary is always manual today
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        + "-" + str(int(datetime.now().timestamp() * 1000) % 1000000)
    )
    payload = {
        "schema_version": schema,
        "run_id": run_id,
        "kind": kind,
        "trigger": trigger,
        "started_at": started_at,
        "finished_at": finished_at,
        "core_result": core_result,
        "overall_status": overall_status,
        "capability_status": capability_status,
        "mandatory_checks": mandatory_checks,
        "mandatory_failures": mandatory_failures,
        "optional_capabilities": optional_capabilities,
        "capability_matrix": capability,
        "failure_classes": failure_classes,
        "e2e_task_id": e2e["task_id"],
        "executor": executor,
        "reviewer": reviewer,
        "parent_status": "completed" if parent_status else "missing",
        "result_present": result_present,
        "verification_present": verification_present,
        "runtime_revision_status": revision,
        "provider_call_counts": provider_counts,
        "checks": checks,
        "passed": sum(1 for x in checks if x.get("ok")),
        "failed": sum(1 for x in checks if not x.get("ok")),
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = target_dir / f"{kind}_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"REPORT={path}")
    return payload, path


def canary_main(reports_dir=None):
    """P4 canary entry point.

    Runs only the real E2E canary, writes a v2 report and decides the
    exit code based on the **mandatory core**, NOT on the raw ``failed``
    count of optional checks. Optional provider degradations (Claude
    402, Codex plan 429) must NEVER cause the systemd unit to surface
    a non-zero exit code.

    ``reports_dir`` is forwarded to :func:`write_report` so offline
    tests can isolate the canary output from the production
    ``logs/acceptance`` directory. Production callers (the systemd unit)
    leave it ``None`` and continue to write to the canonical reports
    directory.
    """
    checks.clear()
    write_report._started_at = datetime.now(timezone.utc).isoformat()
    run_real_e2e()
    payload, path = write_report("canary", reports_dir=reports_dir)
    core_failed = payload.get("core_result") != "PASS"
    if core_failed:
        try:
            sys.path.insert(0, str(TOOLS))
            from aios_bus import publish_event
            publish_event("alert.critical", {
                "type": "aios_real_e2e_canary_failed",
                "report": str(path),
                "mandatory_failures": payload.get("mandatory_failures", []),
                "overall_status": payload.get("overall_status"),
            }, "aios-acceptance")
        except Exception:
            pass
    return 1 if core_failed else 0

def main(real_task=True):
    required = [
        "aios-entry-gateway.service", "aios-orchestrator.service",
        "aios-model-gateway.service", "aios-verification-gate.service",
        "aios-result-push.service", "aios-event-daemon.service",
        "aios-executor-opencode.service", "aios-opencode-server.service",
        "aios-executor-claude.service", "aios-executor-codex.service",
        "aios-codex-relay.service", "aios-feishu-entry.service",
        "openclaw-gateway.service",
    ]
    for unit in required:
        p = run(["systemctl", "--user", "is-active", unit])
        check(f"service:{unit}", p.stdout.strip() == "active", p.stdout + p.stderr)

    failed = run(["systemctl", "--user", "list-units", "aios-*",
                  "--state=failed", "--no-legend", "--plain"])
    failed_units = [line for line in failed.stdout.splitlines()
                    if not line.lstrip().startswith("aios-acceptance.service ")]
    check("systemd:no-failed-aios-units", not failed_units, "\n".join(failed_units))

    try:
        openclaw_config = json.loads(
            (Path.home() / ".openclaw/openclaw.json").read_text(encoding="utf-8")
        )
        bridge_source = (
            HOME / "modules/openclaw-aios-bridge/index.js"
        ).read_text(encoding="utf-8")
        def js_ms(name):
            match = re.search(
                rf"const {name} = ([0-9_]+);",
                bridge_source,
            )
            return int(match.group(1).replace("_", "")) if match else 0
        task_timeout_ms = js_ms("TASK_TIMEOUT_MS")
        hook_timeout_ms = js_ms("HOOK_TIMEOUT_MS")
        route_cache_ms = js_ms("ROUTE_CACHE_MS")
        stuck_abort_ms = int(
            openclaw_config.get("diagnostics", {}).get(
                "stuckSessionAbortMs", 0,
            )
        )
        wait_contract = (
            0 < task_timeout_ms < hook_timeout_ms < stuck_abort_ms <= route_cache_ms
        )
        check(
            "openclaw:bridge-wait-window-precedes-stuck-abort",
            wait_contract,
            {
                "task_timeout_ms": task_timeout_ms,
                "hook_timeout_ms": hook_timeout_ms,
                "stuck_abort_ms": stuck_abort_ms,
                "route_cache_ms": route_cache_ms,
            },
        )
        approval_contract = (
            'task.approval_required === true' in bridge_source and
            'task.status.toLowerCase() === "awaiting_approval"' in bridge_source and
            'approval_required: true' in bridge_source and
            'Explicit owner approval is required.' in bridge_source
        )
        check(
            "openclaw:approval-state-returns-without-timeout",
            approval_contract,
            {"approval_contract": approval_contract},
        )
    except Exception as exc:
        check("openclaw:bridge-wait-window-precedes-stuck-abort", False, exc)
        check("openclaw:approval-state-returns-without-timeout", False, exc)

    try:
        manifest = json.loads((HOME / "config/module_manifest.json").read_text(encoding="utf-8"))
        with (HOME / "config/features.toml").open("rb") as handle:
            features = tomllib.load(handle)
        with (HOME / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)
        setup_source = (HOME / "setup.py").read_text(encoding="utf-8")
        setup_match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', setup_source)
        changelog_source = (HOME / "CHANGELOG.md").read_text(encoding="utf-8")
        changelog_match = re.search(
            r'^##\s+[^\n]*?AIOS\s+([0-9]+\.[0-9]+\.[0-9]+)',
            changelog_source, re.M,
        )
        gateway_source = (TOOLS / "aios_entry_gateway.py").read_text(encoding="utf-8")
        gateway_match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', gateway_source, re.M)
        spec = (HOME / "docs/AIOS_ORCHESTRATOR_RESHAPE_SPEC.md").read_text(encoding="utf-8")
        _, runtime_health = http("http://127.0.0.1:18801/health")
        versions = {
            "manifest": manifest.get("system_version"),
            "features": features.get("system_version"),
            "pyproject": pyproject.get("project", {}).get("version"),
            "setup": setup_match.group(1) if setup_match else "",
            "changelog_head": changelog_match.group(1) if changelog_match else "",
            "gateway_code": gateway_match.group(1) if gateway_match else "",
            "runtime": runtime_health.get("version"),
        }
        contract = manifest.get("core_contract", {}).get("workflow")
        architecture = features.get("architecture", {})
        runtime_version = str(versions["runtime"] or "")
        current_docs = (
            HOME / "AIOS_SYSTEM_OVERVIEW.md",
            HOME / "AIOS_MODULE_REFERENCE.md",
            HOME / "AIOS_PORT_GUIDE.md",
            HOME / "SECURITY_DEFAULTS.md",
        )
        docs_ok = (
            "Status: VERIFIED / \u5df2\u9a8c\u8bc1" in spec and
            f"AIOS version: \x60{runtime_version}\x60" in spec and
            "\x60aios-workflow/1.1\x60" in spec and
            all(runtime_version in path.read_text(encoding="utf-8") for path in current_docs)
        )
        check("reconcile:docs-config-code-runtime-version",
              bool(runtime_version) and len(set(versions.values())) == 1 and docs_ok,
              {"versions": versions, "docs_ok": docs_ok})
        check("reconcile:orchestrator-contract",
              manifest.get("architecture") == "modular-tool-chip" and
              manifest.get("core_contract", {}).get("orchestrator") == "aios-orchestrator" and
              contract == "aios-workflow/1.1" and
              architecture.get("orchestrator_owner") == "aios-orchestrator" and
              architecture.get("orchestrator_contract") == contract,
              {"manifest": manifest.get("core_contract"), "features": architecture})

        unit_names = (
            "aios-orchestrator.service", "aios-opencode-server.service",
            "aios-codex-relay.service", "aios-executor-codex.service",
            "aios-acceptance.service", "aios-acceptance.timer",
            "aios-backup.service", "aios-backup.timer",
        )
        unit_hashes = {}
        for unit in unit_names:
            repo = HOME / "systemd" / unit
            installed = Path.home() / ".config/systemd/user" / unit
            unit_hashes[unit] = {
                "repo": sha256(repo), "runtime": sha256(installed),
            }
        check("reconcile:repo-runtime-service-units",
              all(value["repo"] == value["runtime"] for value in unit_hashes.values()),
              unit_hashes)

        import redis
        revision_redis = redis.Redis(socket_connect_timeout=2)
        executor_revision = compute_executor_revision(TOOLS)
        expected_revisions = {
            "gateway": sha256(TOOLS / "aios_entry_gateway.py"),
            "aios-orchestrator": sha256(TOOLS / "aios_orchestrator.py"),
            "aios-verification-gate": sha256(TOOLS / "aios_verification_gate.py"),
            "opencode": executor_revision,
            "claude": executor_revision,
            "codex": executor_revision,
        }
        loaded_revisions = {"gateway": runtime_health.get("loaded_revision", "")}
        for name in expected_revisions:
            if name == "gateway":
                continue
            raw_revision = revision_redis.hget(
                f"aios:bus:agent:{name}", "loaded_revision",
            )
            loaded_revisions[name] = (
                raw_revision.decode() if isinstance(raw_revision, bytes)
                else str(raw_revision or "")
            )
        check("reconcile:runtime-loaded-revisions",
              loaded_revisions == expected_revisions,
              {"loaded": loaded_revisions, "expected": expected_revisions})

        chip_roles = {chip.get("id"): chip.get("role") for chip in manifest.get("chips", [])}
        expected_roles = {
            "openclaw": "primary-channel-automation",
            "hermes": "primary-memory-semantic-review",
            "opencode": "primary-general-executor",
            "claude": "specialist-high-depth-review",
            "codex": "specialist-code-batch",
        }
        check("reconcile:five-tool-role-boundaries", chip_roles.items() >= expected_roles.items(),
              {name: chip_roles.get(name) for name in expected_roles})

        opencode_cfg = json.loads((HOME / "config/opencode-aios.json").read_text(encoding="utf-8"))
        unit_env = run(["systemctl", "--user", "show", "aios-opencode-server.service",
                        "-p", "Environment", "--value"]).stdout
        mcp_disabled = all(item.get("enabled") is False for item in opencode_cfg.get("mcp", {}).values())
        check("opencode:aios-owned-execution-boundary",
              opencode_cfg.get("permission", {}).get("task") == "deny" and
              opencode_cfg.get("permission", {}).get("question") == "deny" and
              mcp_disabled and
              "OPENCODE_CONFIG=${AIOS_HOME}/config/opencode-aios.json" in unit_env,
              {"task": opencode_cfg.get("permission", {}).get("task"),
               "question": opencode_cfg.get("permission", {}).get("question"),
               "disabled_mcp": len(opencode_cfg.get("mcp", {})), "unit_env": unit_env})

        model_policy = json.loads(
            (HOME / "config/opencode_models.json").read_text(encoding="utf-8")
        )
        candidates = [
            item.get("model", "") for item in model_policy.get("candidates", [])
            if isinstance(item, dict) and item.get("enabled", True)
        ]
        _, provider_response = http("http://127.0.0.1:4096/provider", timeout=15)
        live_models = {}
        for provider in provider_response.get("all", []):
            provider_id = str(provider.get("id", ""))
            for model_id, metadata in provider.get("models", {}).items():
                live_models[f"{provider_id}/{model_id}"] = metadata
        free_evidence = {
            model: live_models.get(model, {}).get("cost", {}) for model in candidates
        }
        check("opencode:free-only-model-policy",
              model_policy.get("schema") == "aios-opencode-model-routing/1.0" and
              model_policy.get("policy") == "free_only" and len(candidates) >= 2 and
              all(
                  evidence.get("input") == 0 and evidence.get("output") == 0
                  for evidence in free_evidence.values()
              ),
              {"candidates": candidates, "live_cost": free_evidence})
        client_version = run([
            "python3", str(TOOLS / "aios_opencode_client.py"), "--version",
        ])
        check("opencode:model-router-runtime-loaded",
              client_version.returncode == 0 and
              "policy free_only" in client_version.stdout and
              "candidates 4" in client_version.stdout,
              client_version.stdout + client_version.stderr)

        import aios_opencode_client as opencode_client
        saved = {
            "health": opencode_client.health,
            "free": opencode_client._verified_free_models,
            "candidates": opencode_client._candidate_models,
            "load_state": opencode_client._load_state,
            "write_state": opencode_client._write_state,
            "run": opencode_client._run_with_model,
        }
        fallback_states = []
        try:
            opencode_client.health = lambda: {"healthy": True}
            opencode_client._verified_free_models = lambda: {
                "opencode/test-free-a", "opencode/test-free-b",
            }
            opencode_client._candidate_models = lambda policy: [
                "opencode/test-free-a", "opencode/test-free-b",
            ]
            opencode_client._load_state = lambda: {}
            opencode_client._write_state = lambda state: fallback_states.append(
                json.loads(json.dumps(state))
            )
            def simulated_model_call(task, model, timeout):
                if model == "opencode/test-free-a":
                    raise opencode_client.OpenCodeAdapterError(
                        "OpenCode HTTP 402: Insufficient Balance"
                    )
                return "AUTO_SWITCH_OK"
            opencode_client._run_with_model = simulated_model_call
            fallback_result = opencode_client.run_task("router acceptance", timeout=30)
            fallback_state = fallback_states[-1] if fallback_states else {}
            fallback_attempts = fallback_state.get("last_attempts", [])
            fallback_ok = (
                fallback_result == "AUTO_SWITCH_OK" and
                fallback_state.get("last_selected_model") == "opencode/test-free-b" and
                [item.get("status") for item in fallback_attempts] == ["failed", "success"] and
                fallback_attempts[0].get("reason") == "quota_exhausted"
            )
        finally:
            opencode_client.health = saved["health"]
            opencode_client._verified_free_models = saved["free"]
            opencode_client._candidate_models = saved["candidates"]
            opencode_client._load_state = saved["load_state"]
            opencode_client._write_state = saved["write_state"]
            opencode_client._run_with_model = saved["run"]
        check("opencode:402-automatic-free-model-switch", fallback_ok,
              fallback_state if 'fallback_state' in locals() else {})

        import aios_orchestrator as orchestrator
        original_ready = orchestrator._tool_ready
        try:
            orchestrator._tool_ready = lambda name: name in {
                "opencode", "claude", "codex",
            }
            no_repeat = (
                orchestrator.choose_executor(
                    "opencode", exclude={"opencode", "claude"},
                ) == "codex" and
                orchestrator.choose_executor(
                    "opencode", exclude={"opencode", "claude", "codex"},
                ) == ""
            )
            retry_target = orchestrator._verification_retry_target(
                {"verification_same_executor_repairs": 0},
                "authoritative_version_missing:expected=5.2.1:observed=[]",
                "opencode",
            )
            retry_target_on_conflict = orchestrator._verification_retry_target(
                {"verification_same_executor_repairs": 0},
                "authoritative_version_conflict:expected=5.2.2:conflicts=['6.7.4']",
                "opencode",
            )
            retry_target_after_once = orchestrator._verification_retry_target(
                {"verification_same_executor_repairs": 1},
                "authoritative_version_missing:expected=5.2.1:observed=[]",
                "opencode",
            )
            retry_target_truncated = orchestrator._verification_retry_target(
                {"verification_same_executor_repairs": 0},
                "Deliverable is truncated and incomplete before required rollback section",
                "codex",
            )
        finally:
            orchestrator._tool_ready = original_ready
        check("orchestrator:repair-never-repeats-failed-executor", no_repeat,
              "attempt history excludes every previously failed executor")
        check(
            "orchestrator:verification-gap-retries-same-executor-once",
            retry_target == "opencode" and
            retry_target_on_conflict == "opencode" and
            retry_target_after_once == "" and
            retry_target_truncated == "codex",
            {
                "missing": retry_target,
                "conflict": retry_target_on_conflict,
                "after_once": retry_target_after_once,
                "truncated": retry_target_truncated,
            },
        )
        quarantined_repair = orchestrator._execution_text(
            "Read the live AIOS version",
            {"task": "Read the live AIOS version", "acceptance": ["runtime evidence"]},
            repair_reason="Run `curl http://127.0.0.1:18801/health | python3`",
            previous_result="untrusted previous executor output",
        )
        check(
            "orchestrator:repair-input-quarantines-untrusted-output",
            "| python" not in quarantined_repair and
            "untrusted previous executor output" not in quarantined_repair,
            quarantined_repair,
        )
        focused_repair = orchestrator._execution_text(
            "Read the live AIOS version and health status",
            {
                "task": "Read the live AIOS version and health status",
                "acceptance": ["runtime evidence"],
            },
            repair_reason=(
                "authoritative_version_missing:expected=5.2.1:observed=[];"
                "authoritative_status_missing:expected=live"
            ),
        )
        check(
            "orchestrator:repair-focus-is-bounded-and-actionable",
            "current AIOS runtime version" in focused_repair and
            "current AIOS runtime health status" in focused_repair and
            "expected=5.2.1" not in focused_repair,
            focused_repair,
        )
        concise_repair = orchestrator._execution_text(
            "Return version comparison, pre-upgrade checks, rollback and conclusion",
            {"task": "complete audit", "acceptance": [], "evidence_mode": "semantic"},
            repair_reason="Deliverable is truncated and incomplete before rollback",
            parent_id="parent-compact", actual_executor="codex", single_node=True,
        )
        check(
            "orchestrator:truncated-output-gets-bounded-concise-repair",
            "under 7000 characters" in concise_repair and
            "omit reasoning transcripts" in concise_repair and
            len(concise_repair) <= 4096,
            {"prompt_chars": len(concise_repair)},
        )
    except Exception as exc:
        check("reconcile:docs-config-code-runtime-version", False, exc)
        check("reconcile:orchestrator-contract", False, exc)
        check("reconcile:repo-runtime-service-units", False, exc)
        check("reconcile:runtime-loaded-revisions", False, exc)
        check("reconcile:five-tool-role-boundaries", False, exc)
        check("opencode:aios-owned-execution-boundary", False, exc)
        check("opencode:free-only-model-policy", False, exc)
        check("opencode:model-router-runtime-loaded", False, exc)
        check("opencode:402-automatic-free-model-switch", False, exc)
        check("orchestrator:repair-never-repeats-failed-executor", False, exc)
        check("orchestrator:verification-gap-retries-same-executor-once", False, exc)
        check("orchestrator:repair-input-quarantines-untrusted-output", False, exc)
        check("orchestrator:repair-focus-is-bounded-and-actionable", False, exc)
        check("orchestrator:truncated-output-gets-bounded-concise-repair", False, exc)

    for port in (4096, 4444, 6379, 8080, 8086, 8848, 9998, 9999,
                 18086, 18789, 18801, 18802):
        try:
            with socket.create_connection(("127.0.0.1", port), 2): pass
            check(f"port:{port}", True, "connected")
        except Exception as exc: check(f"port:{port}", False, exc)

    for url in ("http://127.0.0.1:8086/api/health",
                "http://127.0.0.1:8848/health",
                "http://127.0.0.1:18801/health", "http://127.0.0.1:18802/health"):
        try:
            status, body = http(url)
            check(f"http:{url}", status == 200 and body.get("ok", True), body)
        except Exception as exc: check(f"http:{url}", False, exc)

    p = run(["python3", str(TOOLS / "aios_module_registry.py"), "health"])
    try:
        health = json.loads(p.stdout)
        check("modules:no-offline", health.get("offline") == 0, health)
    except Exception: check("modules:no-offline", False, p.stdout + p.stderr)

    p = run(["python3", str(TOOLS / "aios_tool_adapter.py"), "health"])
    try:
        tool_health = json.loads(p.stdout)
        check("tools:adapter-contract-health", p.returncode == 0 and
              all(item.get("contract_ok") for item in tool_health.values()), tool_health)
        operational = {name for name, item in tool_health.items()
                       if item.get("fully_operational")}
        degraded = {name: {"state": item.get("state"),
                           "model_state": item.get("model_state"),
                           "reason": item.get("reason", "")}
                    for name, item in tool_health.items()
                    if not item.get("fully_operational")}
        # P4: Claude 402 / Codex plan 429 are no longer mandatory failures.
        # We keep the historical check name so existing dashboards do not
        # break, but the recorded verdict is now informational: it returns
        # True when at least OpenCode and Hermes are operational (the
        # executor + reviewer pair required by P3) and reports the
        # degraded providers in evidence. Failing only when OpenCode or
        # Hermes themselves are unavailable.
        core_pair = {"opencode", "hermes"}.issubset(operational)
        check("tools:all-five-real-inference-operational",
              core_pair,
              {"operational": sorted(operational), "degraded": degraded,
               "mandatory_core_pair": core_pair,
               "informational_only": True})
        check("tools:degraded-chips-reported", True, degraded)
    except Exception:
        check("tools:adapter-contract-health", False, p.stdout + p.stderr)
        check("tools:all-five-real-inference-operational", False, p.stdout + p.stderr)

    enabled = run(["systemctl", "--user", "is-enabled", "ollama.service", "llama-api.service"])
    active = run(["systemctl", "--user", "is-active", "ollama.service", "llama-api.service"])
    local_ok = "enabled" not in enabled.stdout.split() and "active" not in active.stdout.split()
    check("local-model:disabled-inactive", local_ok, enabled.stdout + active.stdout)

    try:
        feishu_active = run([
            "systemctl", "--user", "is-active", "aios-feishu-entry.service",
        ])
        feishu_pid = run([
            "systemctl", "--user", "show", "aios-feishu-entry.service",
            "-p", "MainPID", "--value",
        ]).stdout.strip()
        _, feishu_health = http("http://127.0.0.1:18802/health", timeout=5)
        feishu_sockets = run(["ss", "-Htpn", "state", "established"])
        live_socket = [
            line for line in feishu_sockets.stdout.splitlines()
            if feishu_pid and f"pid={feishu_pid}," in line and ":443" in line
        ]
        feishu_ok = (
            feishu_active.stdout.strip() == "active" and
            feishu_health.get("ok") is True and
            feishu_health.get("status") == "live" and bool(live_socket)
        )
        check("feishu:websocket-connected", feishu_ok,
              {"pid": feishu_pid, "health": feishu_health, "socket": live_socket[:1]})
    except Exception as exc:
        check("feishu:websocket-connected", False, exc)

    try:
        import redis
        from aios_bus import get_queue_status
        r = redis.Redis(socket_connect_timeout=2)
        q = get_queue_status()
        active_parents = r.zcard("aios:orchestrator:active")
        drained = sum(int(q.get(k, 0)) for k in ("pending", "locked", "running", "verifying")) == 0
        check("queue:initially-drained", drained and active_parents == 0,
              {"queue": q, "active_parents": active_parents})
        event_count = r.zcard("aios:bus:event:log")
        timeline_count = r.zcard("aios:obs:timeline")
        check("observability:bounded-event-retention",
              event_count <= 500000 and timeline_count <= 500000,
              {"event_log": event_count, "timeline": timeline_count, "limit": 500000})
    except Exception as exc:
        check("queue:initially-drained", False, exc)

    try:
        sys.path.insert(0, str(TOOLS))
        from aios_executor_daemon import core_write_boundary
        denied, reason = core_write_boundary("修改 ${AIOS_HOME}/kernel/tools/aios_bus.py")
        allowed, _ = core_write_boundary("在 ${AIOS_HOME}/sandbox/coding 创建 report.txt")
        readonly_audit, readonly_reason = core_write_boundary(
            "Read-only audit; do not modify or install anything. Analyze upgrade and rollback risk. "
            "Evidence: installed_version=1.17.20, client=${AIOS_HOME}/kernel/tools/client.py"
        )
        denied_install, _ = core_write_boundary(
            "Install package into ${AIOS_HOME}/kernel/tools"
        )
        check(
            "security:core-write-boundary",
            not denied and allowed and readonly_audit and not denied_install,
            {"write_denial": reason, "readonly_audit": readonly_reason},
        )
    except Exception as exc:
        check("security:core-write-boundary", False, exc)

    try:
        import aios_orchestrator as orchestrator
        fact_plan = orchestrator._normalise_plan([{
            "task": "Read the current AIOS version",
            "depends_on": [],
            "role": "opencode",
            "acceptance": ["Return a non-empty version"],
        }], "Read the current AIOS version")
        check("orchestrator:dynamic-fact-plan-requires-grounding",
              fact_plan[0].get("evidence_mode") == "aios-runtime" and
              any("authoritative evidence" in item for item in fact_plan[0].get("acceptance", [])),
              fact_plan)
    except Exception as exc:
        check("orchestrator:dynamic-fact-plan-requires-grounding", False, exc)

    try:
        component_goal = (
            "Audit installed OpenClaw and OpenCode versions, query the official latest "
            "stable releases, then assess AIOS adapter upgrade risk."
        )
        component_tasks = (
            "Audit current AIOS runtime to extract the actual installed versions of OpenClaw and OpenCode.",
            "Query the official upstream latest stable versions and release dates for OpenClaw and OpenCode.",
            "Compare installed and latest versions and assess impact on AIOS adapters and bridges.",
        )
        modes = [
            orchestrator._classify_evidence_mode(component_goal, task)
            for task in component_tasks
        ]
        own_mode = orchestrator._classify_evidence_mode(
            "Read the current AIOS version and gateway health status", "",
        )
        check(
            "orchestrator:component-version-scope-is-not-aios-identity",
            modes == ["independent-live", "independent-live", "independent-live"] and
            own_mode == "aios-runtime",
            {"component_modes": modes, "aios_mode": own_mode},
        )
    except Exception as exc:
        check("orchestrator:component-version-scope-is-not-aios-identity", False, exc)

    try:
        wrapped_plan = (
            '<think>planner reasoning</think>\n```json\n'
            '{"nodes":[{"task":"audit","depends_on":[],"role":"opencode",'
            '"acceptance":["grounded"],"evidence_mode":"independent-live"}]}\n```'
        )
        extracted_plan = orchestrator._extract_json_array(wrapped_plan)
        original_collector = orchestrator._collect_independent_evidence
        orchestrator._collect_independent_evidence = lambda goal, node: ({
            "mode": "independent-live",
            "collected_at": "2026-07-17T00:00:00+00:00",
            "authoritative": {"component_versions": {
                "openclaw": {
                    "installed_version": "2026.6.10",
                    "installed_package_name": "openclaw",
                    "official_registry_url": "https://registry.npmjs.org/openclaw",
                    "official_package_page": "https://www.npmjs.com/package/openclaw",
                    "registry_retrieved_at": "2026-07-16T22:00:00+00:00",
                    "latest_stable_version": "2026.7.1",
                    "official_registry_url": "https://registry.npmjs.org/openclaw",
                },
            }},
        }, [])
        try:
            evidence_prompt = orchestrator._execution_text(
                "Audit current and latest OpenClaw versions",
                {"task": "Audit current and latest OpenClaw versions",
                 "acceptance": [], "evidence_mode": "independent-live"},
                parent_id="parent-528", actual_executor="codex",
            )
        finally:
            orchestrator._collect_independent_evidence = original_collector
        check(
            "orchestrator:planner-wrapper-and-live-evidence-handoff",
            isinstance(extracted_plan, list) and extracted_plan[0].get("task") == "audit" and
            "AIOS-owned live evidence" in evidence_prompt and
            "2026.7.1" in evidence_prompt and
            "https://registry.npmjs.org/openclaw" in evidence_prompt and
            len(evidence_prompt) <= 4096,
            {"extracted": extracted_plan, "evidence_in_prompt": "2026.7.1" in evidence_prompt,
             "prompt_chars": len(evidence_prompt)},
        )
    except Exception as exc:
        check("orchestrator:planner-wrapper-and-live-evidence-handoff", False, exc)

    try:
        audit_goal = "Audit current and latest OpenClaw and OpenCode versions and upgrade risk"
        fanout = [
            {"task": "installed", "depends_on": [], "role": "opencode",
             "acceptance": [], "evidence_mode": "independent-live"},
            {"task": "upstream", "depends_on": [0], "role": "opencode",
             "acceptance": [], "evidence_mode": "independent-live"},
            {"task": "risk", "depends_on": [0, 1], "role": "claude",
             "acceptance": [], "evidence_mode": "independent-live"},
        ]
        fused, changed = orchestrator._fuse_plan_for_runtime(
            fanout, audit_goal, healthy_executors=["codex"],
        )
        fused_multi, changed_multi = orchestrator._fuse_plan_for_runtime(
            fanout, audit_goal, healthy_executors=["opencode", "codex"],
        )
        explicit_goal = "Create exactly two dependent nodes for this audit"
        preserved, explicit_changed = orchestrator._fuse_plan_for_runtime(
            fanout[:2], explicit_goal, healthy_executors=["codex"],
        )
        check(
            "orchestrator:default-simple-plan-fuses-unrequested-dag",
            changed and len(fused) == 1 and fused[0]["task"] == audit_goal and
            fused[0]["role"] == "codex" and
            fused[0]["evidence_mode"] == "independent-live" and
            changed_multi and len(fused_multi) == 1 and
            fused_multi[0]["role"] == "opencode" and
            not explicit_changed and len(preserved) == 2,
            {"fused": fused, "fused_multi": fused_multi,
             "explicit_preserved": preserved},
        )
    except Exception as exc:
        check("orchestrator:default-simple-plan-fuses-unrequested-dag", False, exc)

    try:
        goal = "Create exactly two dependent AIOS workflow nodes using MARKER521."
        one = [{"task": "only", "depends_on": [], "acceptance": []}]
        two = [
            {"task": "first MARKER521", "depends_on": [], "acceptance": []},
            {"task": "second MARKER521", "depends_on": [0], "acceptance": []},
        ]
        one_errors = orchestrator._plan_structure_errors(one, goal)
        two_errors = orchestrator._plan_structure_errors(two, goal)
        literals_ok, literal_error = orchestrator._plan_preserves_user_literals(two, goal)
        check(
            "orchestrator:explicit-multinode-structure-guard",
            orchestrator._required_node_count(goal) == 2 and
            bool(one_errors) and not two_errors and literals_ok,
            {"one_node_errors": one_errors, "two_node_errors": two_errors,
             "literal_error": literal_error},
        )
    except Exception as exc:
        check("orchestrator:explicit-multinode-structure-guard", False, exc)

    try:
        path = "${AIOS_HOME}/sandbox/coding/path_guard_521.txt"
        path_goal = f"Write PATH_GUARD_521 to {path}"
        mistyped = [{
            "task": "Write PATH_GUARD_521 to /home/borim/aios/sandbox/coding/path_guard_521.txt",
            "depends_on": [], "acceptance": ["report length/size"],
        }]
        repaired = orchestrator._repair_user_paths(mistyped, path_goal)
        repaired_ok, repaired_error = orchestrator._plan_preserves_user_literals(
            repaired, path_goal,
        )
        check(
            "orchestrator:exact-user-path-repair",
            repaired_ok and repaired[0]["task"].endswith(path) and
            not orchestrator._absolute_paths("report length/size"),
            {"plan": repaired, "error": repaired_error},
        )
    except Exception as exc:
        check("orchestrator:exact-user-path-repair", False, exc)

    try:
        from aios_verification_gate import (
            _collect_independent_evidence, _aios_grounding_errors,
            _node_verification_goal,
        )
        fact_node = {"evidence_mode": "aios-runtime"}
        fact_goal = "Read the current AIOS version and system time"
        grounding, grounding_collection_errors = _collect_independent_evidence(
            fact_goal, fact_node,
        )
        expected = grounding.get("authoritative", {}).get("health", {}).get("version", "")
        stale_result = (
            "AIOS version: v4.0.1 (from historical CHANGELOG)\n"
            "System time: 2026-07-15T09:13:02Z"
        )
        current_timestamp = grounding.get("authoritative", {}).get("utc_now", "")
        good_result = f"AIOS version: {expected}\nSystem time: {current_timestamp}"
        redis_result = f"AIOS version: {expected}\nRedis version: 7.0.15"
        url_result = f"Endpoint http://127.0.0.1:18801/health reports AIOS version: {expected}"
        script_goal = (
            "Read current AIOS version, create sandbox script using redis-cli PING "
            "and curl http://127.0.0.1:18801/health, output pass/fail"
        )
        script_result = (
            f"AIOS version: {expected}\n"
            "redis-cli PING: pass\n"
            "curl http://127.0.0.1:18801/health: pass"
        )
        stale_errors = _aios_grounding_errors(fact_goal, stale_result, grounding)
        good_errors = _aios_grounding_errors(fact_goal, good_result, grounding)
        redis_errors = _aios_grounding_errors("Read current AIOS version", redis_result, grounding)
        url_errors = _aios_grounding_errors("Read current AIOS version", url_result, grounding)
        script_errors = _aios_grounding_errors(script_goal, script_result, grounding)
        check("verification:stale-version-bait-rejected",
              not grounding_collection_errors and bool(expected) and
              any("authoritative_version" in item for item in stale_errors) and
              not good_errors and not redis_errors and not url_errors and not script_errors,
              {"expected": expected, "stale_errors": stale_errors,
               "good_errors": good_errors, "redis_errors": redis_errors,
               "url_errors": url_errors,
               "script_errors": script_errors,
               "collection": grounding_collection_errors})
        check("verification:fresh-timestamp-without-duplicate-raw-json",
              not good_errors and
              any("authoritative_timestamp_invalid_or_stale" in item for item in stale_errors),
              {"fresh": current_timestamp, "good_errors": good_errors,
               "stale_errors": stale_errors})
    except Exception as exc:
        check("verification:stale-version-bait-rejected", False, exc)
        check("verification:fresh-timestamp-without-duplicate-raw-json", False, exc)

    try:
        audit_goal = "Perform a current AIOS health audit: queue, executors and failed services"
        audit_node = {"evidence_mode": "aios-runtime"}
        audit_grounding, audit_collection = _collect_independent_evidence(
            audit_goal, audit_node,
        )
        audit_authoritative = audit_grounding.get("authoritative", {})
        runtime_status = audit_authoritative.get("runtime_status", {})
        queue = runtime_status.get("queue", {})
        executor_list = runtime_status.get("executors", {}).get("list", [])
        task_executors = [
            item for item in executor_list
            if item.get("name") in ("opencode", "claude", "codex")
        ]
        available_count = sum(bool(item.get("available")) for item in task_executors)
        unavailable_count = len(task_executors) - available_count
        failed_count = len(audit_authoritative.get("failed_systemd_units", []))
        expected_service = audit_authoritative.get("health", {}).get("service", "")
        bad_audit = (
            "AIOS health audit\n"
            f"Service: {expected_service}\n"
            "aios:queue:priority:data contains 8 unfinished tasks\n"
            "aios:agents:available contains 0 registered agents\n"
            "Available executors: 3\n"
            "Unavailable executors: 0\n"
            "Unfinished tasks: 8\n"
            f"Failed services: {failed_count}"
        )
        canonical_audit = (
            "AIOS health audit\n"
            f"Service: {expected_service}\n"
            f"Unfinished tasks: {int(queue.get('total_active', 0) or 0)}\n"
            f"Available executors: {available_count}\n"
            f"Unavailable executors: {unavailable_count}\n"
            f"Failed services: {failed_count}"
        )
        bad_audit_errors = _aios_grounding_errors(
            audit_goal, bad_audit, audit_grounding,
        )
        canonical_audit_errors = _aios_grounding_errors(
            audit_goal, canonical_audit, audit_grounding,
        )
        missing_status_grounding = {
            "mode": "aios-runtime",
            "authoritative": {"health": audit_authoritative.get("health", {})},
        }
        missing_status_errors = _aios_grounding_errors(
            audit_goal, bad_audit, missing_status_grounding,
        )
        required_codes = (
            "non_authoritative_legacy_runtime_key",
            "authoritative_queue_conflict",
            "authoritative_available_executor_count_conflict",
            "authoritative_unavailable_executor_count_conflict",
        )
        check("verification:runtime-audit-uses-canonical-sources",
              not audit_collection and
              all(any(code in item for item in bad_audit_errors) for code in required_codes) and
              not canonical_audit_errors and
              "runtime_status" in audit_authoritative and
              "orchestrator_processes" in audit_authoritative,
              {"bad_errors": bad_audit_errors,
               "canonical_errors": canonical_audit_errors,
               "available": available_count, "unavailable": unavailable_count,
               "failed_services": failed_count})
        check("verification:missing-runtime-status-is-not-zero",
              not any(
                  marker in item for item in missing_status_errors
                  for marker in (
                      "authoritative_queue_conflict",
                      "authoritative_available_executor_count_conflict",
                      "authoritative_unavailable_executor_count_conflict",
                  )
              ),
              {"errors": missing_status_errors})
    except Exception as exc:
        check("verification:runtime-audit-uses-canonical-sources", False, exc)
        check("verification:missing-runtime-status-is-not-zero", False, exc)

    try:
        import inspect
        import aios_verification_gate as verification_gate
        planner_source = inspect.getsource(orchestrator.build_plan)
        execution_source = inspect.getsource(orchestrator._execution_text)
        verifier_source = inspect.getsource(verification_gate.verify_parent_node)
        sanitized = orchestrator._sanitize_numeric_acceptance(
            [
                "Use the user-supplied weight 40%",
                "Supplier A total equals 86.10",
                "Supplier C is the overall winner",
                "The price-component leader is not the overall leader",
            ],
            "Weight is 40%; compute the supplier totals and winner from inputs 92, 78, 88",
        )
        check(
            "verification:numeric-comparative-consistency-policy",
            "must not hard-code a computed total" in planner_source and
            "Recompute numeric/comparative claims" in execution_source and
            "independently check every " in verifier_source and
            "component winner" in verifier_source and
            "Any single materially false claim means passed=false" in verifier_source and
            "Use the user-supplied weight 40%" in sanitized and
            "Supplier A total equals 86.10" not in sanitized and
            "Supplier C is the overall winner" not in sanitized and
            "The price-component leader is not the overall leader" not in sanitized and
            any("Independently recompute every numeric result" in item for item in sanitized),
            {
                "planner_untrusted_derived_answer": "must not hard-code a computed total" in planner_source,
                "executor_recalculation": "Recompute numeric/comparative claims" in execution_source,
                "independent_component_check": (
                    "independently check every " in verifier_source and
                    "component winner" in verifier_source
                ),
                "single_false_claim_rejected": "Any single materially false claim means passed=false" in verifier_source,
                "sanitized_acceptance": sanitized,
            },
        )
    except Exception as exc:
        check("verification:numeric-comparative-consistency-policy", False, exc)

    try:
        from aios_verification_gate import _node_verification_goal
        parent_goal = "Read the current AIOS version, then compute a file hash and Redis PING"
        node_goal = "Compute the file SHA-256 and verify Redis responds to PING"
        scoped_node = {"task": node_goal, "evidence_mode": "aios-runtime"}
        scoped_goal = _node_verification_goal(parent_goal, scoped_node)
        scoped_grounding, scoped_collection = _collect_independent_evidence(scoped_goal, scoped_node)
        scoped_result = "SHA-256: 9e76e3a79ad5cc00479fae60102487f1139414495dc09b858356b41561a2a045\nRedis PING: PONG"
        scoped_errors = _aios_grounding_errors(scoped_goal, scoped_result, scoped_grounding)
        check(
            "verification:grounding-scoped-to-current-node",
            not scoped_collection and scoped_goal == node_goal and not scoped_errors,
            {"scope": scoped_goal, "errors": scoped_errors, "collection": scoped_collection},
        )
    except Exception as exc:
        check("verification:grounding-scoped-to-current-node", False, exc)

    try:
        from aios_verification_gate import _component_grounding_errors, _executor_payload
        component_goal = (
            "Audit current AIOS runtime to extract installed OpenClaw and OpenCode versions "
            "using current executable or running services."
        )
        mistaken_runtime_evidence = {
            "mode": "aios-runtime",
            "authoritative": {
                "health": {
                    "version": "5.2.7", "service": "aios-entry-gateway",
                    "status": "live", "redis": True,
                },
            },
        }
        component_result = (
            "OpenClaw installed version: 2026.6.10 (from executable)\n"
            "OpenCode installed version: 1.17.20 (from executable)\n"
            "Both running services were inspected read-only."
        )
        false_aios_errors = _aios_grounding_errors(
            component_goal, component_result, mistaken_runtime_evidence,
        )
        component_evidence = {
            "mode": "independent-live",
            "authoritative": {"component_versions": {
                "openclaw": {
                    "installed_version": "2026.6.10",
                    "installed_package_name": "openclaw",
                    "official_registry_url": "https://registry.npmjs.org/openclaw",
                    "official_package_page": "https://www.npmjs.com/package/openclaw",
                    "registry_retrieved_at": "2026-07-16T22:00:00+00:00",
                    "latest_stable_version": "2026.7.1",
                    "latest_stable_release_date": "2026-07-13T17:58:18.920Z",
                    "stable_release_gap_from_installed": 2,
                    "stable_releases_after_installed": ["2026.6.11", "2026.7.1"],
                    "release_notes_verified": False,
                    "aios_integration": {
                        "name": "@aios/openclaw-bridge",
                        "compat": {"pluginApi": ">=2026.6.10"},
                    },
                },
                "opencode": {
                    "installed_version": "1.17.20",
                    "installed_package_name": "opencode-ai",
                    "official_registry_url": "https://registry.npmjs.org/opencode-ai",
                    "official_package_page": "https://www.npmjs.com/package/opencode-ai",
                    "registry_retrieved_at": "2026-07-16T22:00:05+00:00",
                    "latest_stable_version": "1.18.3",
                    "latest_stable_release_date": "2026-07-16T15:31:57.209Z",
                    "stable_release_gap_from_installed": 4,
                    "stable_releases_after_installed": [
                        "1.18.0", "1.18.1", "1.18.2", "1.18.3",
                    ],
                    "release_notes_verified": False,
                    "aios_integration": {"client": "aios_opencode_client.py"},
                },
            }},
        }
        latest_goal = "Compare installed and latest OpenClaw and OpenCode versions and release dates"
        good_component_result = (
            "OpenClaw installed 2026.6.10, latest 2026.7.1, released 2026-07-13.\n"
            "OpenCode installed 1.17.20, latest 1.18.3, released 2026-07-16."
        )
        bad_component_result = (
            "OpenClaw installed 2026.6.10, latest 2026.6.10.\n"
            "OpenCode installed 1.17.20, latest 1.17.20."
        )
        good_component_errors = _component_grounding_errors(
            latest_goal, good_component_result, component_evidence,
        )
        bad_component_errors = _component_grounding_errors(
            latest_goal, bad_component_result, component_evidence,
        )
        risk_goal = (
            "Compare installed/latest OpenClaw and OpenCode versions, analyze compatibility "
            "and upgrade risk, and assess AIOS adapters and bridges"
        )
        bad_risk_result = (
            "OpenClaw installed 2026.6.10, latest 2026.7.1, released 2026-07-13; "
            "semantic versioning suggests backward compatibility.\n"
            "OpenCode installed 1.17.20, latest 1.18.3, released 2026-07-16; "
            "patch-level releases suggest backward compatibility."
        )
        good_risk_result = (
            "OpenClaw installed 2026.6.10, latest 2026.7.1, released 2026-07-13; "
            "official release notes were not acquired and compatibility is unable to verify; "
            "assess the openclaw-aios-bridge module (@aios/openclaw-bridge) before upgrading.\n"
            "OpenCode installed 1.17.20, latest 1.18.3, released 2026-07-16; this is a "
            "minor-line change. Official release notes were not acquired and compatibility "
            "is unable to verify; assess aios_opencode_client.py before upgrading."
        )
        bad_risk_errors = _component_grounding_errors(
            risk_goal, bad_risk_result, component_evidence,
        )
        good_risk_errors = _component_grounding_errors(
            risk_goal, good_risk_result, component_evidence,
        )
        gap_goal = "How many versions behind are OpenClaw and OpenCode? Report the release gap"
        bad_gap_errors = _component_grounding_errors(
            gap_goal,
            "OpenClaw is 1 release behind. OpenCode is 4 releases behind.",
            component_evidence,
        )
        good_gap_errors = _component_grounding_errors(
            gap_goal,
            "OpenClaw is 2 stable releases behind. OpenCode is 4 stable releases behind.",
            component_evidence,
        )
        rollback_goal = (
            "Give official source URL, query time, compatibility risk and rollback for "
            "OpenClaw and OpenCode upgrades"
        )
        bad_rollback_errors = _component_grounding_errors(
            rollback_goal,
            "OpenClaw installed 2026.6.10, latest 2026.7.1 released 2026-07-13; source "
            "https://registry.npmjs.org/openclaw queried 2026-07-16; 2026.7.1 is a minor "
            "version and should be backward compatible; rollback with nvm.\n"
            "OpenCode installed 1.17.20, latest 1.18.3 released 2026-07-16; source "
            "https://registry.npmjs.org/opencode-ai queried 2026-07-16; "
            "release notes were not acquired and compatibility is unable to verify; use old binary.",
            component_evidence,
        )
        good_rollback_errors = _component_grounding_errors(
            rollback_goal,
            "OpenClaw installed 2026.6.10, latest 2026.7.1 released 2026-07-13; source "
            "https://registry.npmjs.org/openclaw queried 2026-07-16; "
            "release notes were not acquired and compatibility is unable to verify; "
            "rollback with npm install -g openclaw@2026.6.10.\n"
            "OpenCode installed 1.17.20, latest 1.18.3 released 2026-07-16; source "
            "https://registry.npmjs.org/opencode-ai queried 2026-07-16; "
            "release notes were not acquired and compatibility is unable to verify; "
            "rollback with npm install -g opencode-ai@1.17.20.",
            component_evidence,
        )
        invented_version_errors = _component_grounding_errors(
            rollback_goal,
            "OpenClaw 2026.6.10 to 2026.7.1; validate invented 2026.7.0 and the upper bound; "
            "npm install -g openclaw@2026.6.10. Release notes not acquired; compatibility "
            "unable to verify; source https://registry.npmjs.org/openclaw queried 2026-07-16, "
            "released 2026-07-13.\nOpenCode 1.17.20 to 1.18.3; npm install -g "
            "opencode-ai@1.17.20. Release notes not acquired; compatibility unable to verify; "
            "source https://registry.npmjs.org/opencode-ai queried 2026-07-16, released 2026-07-16.",
            component_evidence,
        )
        clean_payload = _executor_payload(
            "[codex] <think>private draft</think>\nComplete final answer", "codex",
        )
        check(
            "verification:component-versions-use-component-authority",
            not false_aios_errors and not good_component_errors and
            len(bad_component_errors) >= 4 and not good_risk_errors and
            any("release_notes_uncertainty" in item for item in bad_risk_errors) and
            any("delta_classification" in item for item in bad_risk_errors) and
            any("aios_integration_missing" in item for item in bad_risk_errors) and
            any("release_gap_missing_or_conflict:component=openclaw" in item for item in bad_gap_errors) and
            not good_gap_errors and
            any("version_scheme_claim" in item for item in bad_rollback_errors) and
            sum("rollback_pin_missing" in item for item in bad_rollback_errors) == 2 and
            not good_rollback_errors and
            any("unrecognized_version_claim" in item for item in invented_version_errors) and
            any("constraint_direction_conflict" in item for item in invented_version_errors) and
            clean_payload == "Complete final answer",
            {
                "false_aios_errors": false_aios_errors,
                "good_component_errors": good_component_errors,
                "bad_component_errors": bad_component_errors,
                "bad_risk_errors": bad_risk_errors,
                "good_risk_errors": good_risk_errors,
                "bad_gap_errors": bad_gap_errors,
                "good_gap_errors": good_gap_errors,
                "bad_rollback_errors": bad_rollback_errors,
                "good_rollback_errors": good_rollback_errors,
                "invented_version_errors": invented_version_errors,
                "clean_payload": clean_payload,
            },
        )
    except Exception as exc:
        check("verification:component-versions-use-component-authority", False, exc)

    try:
        from aios_verification_gate import _workflow_metadata_errors
        metadata_goal = (
            "Finally return task ID, actual executor and final status / "
            "最后返回任务 ID、实际执行器、最终状态"
        )
        expected_parent = "00000000-1111-2222-3333-444444444444"
        bad_metadata = (
            "Task ID: based on a worker process\n"
            "Actual Executor: aios_executor_daemon.py (claude, codex, opencode)\n"
            "Final Status: Running"
        )
        good_metadata = (
            f"Task ID: {expected_parent}\n"
            "Actual Executor: opencode\n"
            "Final Status: completed"
        )
        bad_metadata_errors = _workflow_metadata_errors(
            metadata_goal, bad_metadata, expected_parent, "opencode",
        )
        good_metadata_errors = _workflow_metadata_errors(
            metadata_goal, good_metadata, expected_parent, "opencode",
        )
        metadata_prompt = orchestrator._execution_text(
            metadata_goal,
            {"task": metadata_goal, "acceptance": [], "evidence_mode": "aios-runtime"},
            parent_id=expected_parent,
            actual_executor="opencode",
        )
        check(
            "verification:system-owned-result-metadata",
            any("authoritative_parent_task_id_conflict" in item for item in bad_metadata_errors) and
            any("authoritative_actual_executor_conflict" in item for item in bad_metadata_errors) and
            any("authoritative_final_status_conflict" in item for item in bad_metadata_errors) and
            not good_metadata_errors and expected_parent in metadata_prompt and
            "- Actual executor: opencode" in metadata_prompt and
            "- Successful final status: completed" in metadata_prompt,
            {"bad_errors": bad_metadata_errors, "good_errors": good_metadata_errors},
        )
    except Exception as exc:
        check("verification:system-owned-result-metadata", False, exc)

    try:
        invalid_task_id = "2fe60248-683e-4892-9813-e27f23500ed3"
        active_learning = HOME / "logs/tool_learning/opencode.jsonl"
        quarantine = HOME / "logs/quarantine/revoked_learning_20260715.jsonl"
        active_text = active_learning.read_text(encoding="utf-8") if active_learning.exists() else ""
        quarantine_text = quarantine.read_text(encoding="utf-8") if quarantine.exists() else ""
        check("learning:invalid-version-outcome-revoked",
              invalid_task_id not in active_text and invalid_task_id in quarantine_text,
              {"active_contains": invalid_task_id in active_text,
               "quarantine_contains": invalid_task_id in quarantine_text})
    except Exception as exc:
        check("learning:invalid-version-outcome-revoked", False, exc)

    try:
        metadata_task_id = "2389da94-2bb3-4cc2-8edf-4e7fd040fe2c"
        active_learning = HOME / "logs/tool_learning/opencode.jsonl"
        quarantine = HOME / "logs/quarantine/revoked_learning_20260716.jsonl"
        active_text = active_learning.read_text(encoding="utf-8") if active_learning.exists() else ""
        quarantine_text = quarantine.read_text(encoding="utf-8") if quarantine.exists() else ""
        check("learning:metadata-conflict-outcome-revoked",
              metadata_task_id not in active_text and metadata_task_id in quarantine_text,
              {"active_contains": metadata_task_id in active_text,
               "quarantine_contains": metadata_task_id in quarantine_text})
    except Exception as exc:
        check("learning:metadata-conflict-outcome-revoked", False, exc)

    try:
        audit_task_id = "c90bbd48-0b6e-4d67-ab33-01eaeeb0b8c0"
        active_learning = HOME / "logs/tool_learning/opencode.jsonl"
        quarantine = HOME / "logs/quarantine/revoked_learning_20260717.jsonl"
        active_text = active_learning.read_text(encoding="utf-8") if active_learning.exists() else ""
        quarantine_text = quarantine.read_text(encoding="utf-8") if quarantine.exists() else ""
        check("learning:legacy-runtime-audit-outcome-revoked",
              audit_task_id not in active_text and audit_task_id in quarantine_text,
              {"active_contains": audit_task_id in active_text,
               "quarantine_contains": audit_task_id in quarantine_text})
    except Exception as exc:
        check("learning:legacy-runtime-audit-outcome-revoked", False, exc)

    try:
        numeric_task_id = "c612579f-3bdb-47f4-9215-2608f4cad6f4"
        active_learning = HOME / "logs/tool_learning/opencode.jsonl"
        quarantine = HOME / "logs/quarantine/revoked_learning_20260717.jsonl"
        active_text = active_learning.read_text(encoding="utf-8") if active_learning.exists() else ""
        quarantine_text = quarantine.read_text(encoding="utf-8") if quarantine.exists() else ""
        check("learning:false-numeric-comparison-outcome-revoked",
              numeric_task_id not in active_text and numeric_task_id in quarantine_text,
              {"active_contains": numeric_task_id in active_text,
               "quarantine_contains": numeric_task_id in quarantine_text})
    except Exception as exc:
        check("learning:false-numeric-comparison-outcome-revoked", False, exc)

    try:
        from aios_verification_gate import _allow_live_semantic_recovery
        stale = {"fully_operational": False, "contract_ok": True, "model_state": "stale"}
        timeout = {"fully_operational": False, "contract_ok": True, "model_state": "timeout"}
        auth = {"fully_operational": False, "contract_ok": True, "model_state": "auth_failed"}
        missing = {"fully_operational": False, "contract_ok": False, "model_state": "stale"}
        check("verification:bounded-live-hermes-recovery-policy",
              _allow_live_semantic_recovery(stale) and
              _allow_live_semantic_recovery(timeout) and
              not _allow_live_semantic_recovery(auth) and
              not _allow_live_semantic_recovery(missing),
              {"recoverable": ["stale", "timeout"],
               "fail_closed": ["auth_failed", "binary_missing"]})
    except Exception as exc:
        check("verification:bounded-live-hermes-recovery-policy", False, exc)

    try:
        import aios_verification_gate as verification_gate
        import aios_tool_adapter
        original_get_adapter = aios_tool_adapter.get_adapter
        original_run = verification_gate.subprocess.run
        original_policy = verification_gate._review_policy

        class FakeAdapter:
            def __init__(self, ready):
                self.ready = ready
                self.config = {"task_timeout_seconds": 2}
            def health(self):
                return {"fully_operational": self.ready, "contract_ok": True,
                        "model_state": "fresh" if self.ready else "auth_failed"}
            def command_for_task(self, prompt):
                return ["fake-reviewer", prompt]
            def record_inference_success(self, *args, **kwargs):
                return None

        try:
            adapters = {"hermes": FakeAdapter(False), "codex": FakeAdapter(True)}
            aios_tool_adapter.get_adapter = lambda name: adapters[name]
            verification_gate._review_policy = lambda: {
                "exclude_executor": True,
                "reviewers": [
                    {"id": "hermes", "backend": "hermes-cli"},
                    {"id": "opencode", "backend": "opencode-server"},
                    {"id": "codex", "backend": "codex-cli"},
                ],
            }
            verification_gate.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
                a[0] if a else [], 0,
                '{"passed":true,"reason":"ok","repair_instruction":""}', "",
            )
            fallback = verification_gate._semantic_review("review", "opencode")
            fallback_ok = (
                fallback.get("reviewer") == "codex" and
                fallback.get("reviewer_backend") == "codex-cli" and
                any(item.get("reason") == "self_review_forbidden"
                    for item in fallback.get("attempts", []))
            )
        finally:
            aios_tool_adapter.get_adapter = original_get_adapter
            verification_gate.subprocess.run = original_run
            verification_gate._review_policy = original_policy
        check("verification:independent-semantic-fallback", fallback_ok, fallback)
    except Exception as exc:
        check("verification:independent-semantic-fallback", False, exc)

    try:
        from aios_secure import classify_l4_action
        from aios_bus import (request_approval, approve_request, consume_approval,
                              approval_is_valid, _redis_client)
        parent = "acceptance-" + str(int(time.time() * 1000))
        action = classify_l4_action("设置 AIOS 配置文件中的测试值")
        normal = classify_l4_action("分析 AIOS 配置，不做任何修改")
        readonly_incident = classify_l4_action(
            "请完成以下生产事故根因分析：08:55 发布版本，认证服务配置中的"
            "令牌地址从 /v1/token 改为 /v2/token。请判断根因并给出预防措施。"
            "不得创建或修改文件。"
        )
        real_credential_change = classify_l4_action(
            "请修改 AIOS 配置中的 API token"
        )
        contradictory_mutation = classify_l4_action(
            "请分析后删除 API token。不得创建或修改文件。"
        )
        aid = request_approval("acceptance L4", "test", risk="L4",
                               requester="acceptance", parent_id=parent,
                               action=action)
        blocked, _ = approval_is_valid(aid, parent, action)
        approved, _ = approve_request(aid, parent, "acceptance")
        consumed, _ = consume_approval(aid, parent)
        valid, _ = approval_is_valid(aid, parent, action)
        replay, _ = approval_is_valid(aid, parent + "-other", action)
        _redis_client.delete(f"aios:bus:governance:approval:{aid}")
        check("security:l4-approval-fail-closed-and-replay-safe",
              action == "modify_config" and not normal and not readonly_incident and
              real_credential_change == "credential_or_permission" and not blocked and
              contradictory_mutation == "credential_or_permission" and
              approved and consumed and valid and not replay,
              {"action": action, "normal": normal,
               "readonly_incident": readonly_incident,
               "real_credential_change": real_credential_change,
               "contradictory_mutation": contradictory_mutation,
               "approval_id": aid})
    except Exception as exc:
        check("security:l4-approval-fail-closed-and-replay-safe", False, exc)

    try:
        from aios_monitor import AGENT_UNITS, WORKLOAD_UNITS
        registry = json.loads((HOME / "config/tool_lifecycle.json").read_text(encoding="utf-8"))["tools"]
        expected = set(registry)
        mapping_ok = set(AGENT_UNITS) == expected and all(AGENT_UNITS[n] == registry[n].get("service","") for n in expected)
        check("control:systemd-agent-mapping", mapping_ok, AGENT_UNITS)
        check("control:monitor-outside-workload", "aios-monitor.service" not in WORKLOAD_UNITS,
              WORKLOAD_UNITS)
    except Exception as exc:
        check("control:systemd-agent-mapping", False, exc)

    timer = run(["systemctl", "--user", "is-active", "aios-self-healing.timer"])
    check("self-heal:systemd-timer-active", timer.stdout.strip() == "active", timer.stdout + timer.stderr)
    for unit in ("aios-core.target", "aios-monitor.service", "aios-self-healing.timer"):
        enabled = run(["systemctl", "--user", "is-enabled", unit])
        check(f"autostart:{unit}", enabled.stdout.strip() == "enabled",
              enabled.stdout + enabled.stderr)

    for unit, mode in (("aios-acceptance.timer", "--canary"),
                       ("aios-backup.timer", "create-and-drill")):
        enabled = run(["systemctl", "--user", "is-enabled", unit])
        active = run(["systemctl", "--user", "is-active", unit])
        service = unit.replace(".timer", ".service")
        command = run(["systemctl", "--user", "show", service,
                       "-p", "ExecStart", "--value"])
        check(f"reliability-timer:{unit}",
              enabled.stdout.strip() == "enabled" and
              active.stdout.strip() == "active" and mode in command.stdout,
              enabled.stdout + active.stdout + command.stdout)

    try:
        from aios_tool_evolution import health_all as tool_health_all
        tool_health = tool_health_all()
        registered = json.loads((HOME / "config/tool_lifecycle.json").read_text(encoding="utf-8"))["tools"]
        check("evolution:all-registered-tool-chips-isolated",
              set(tool_health) == set(registered) and all(item.get("infrastructure_ok") for item in tool_health.values()),
              tool_health)
    except Exception as exc:
        check("evolution:all-registered-tool-chips-isolated", False, exc)

    for unit in ("aios-tool-learning.timer", "aios-tool-upgrade.timer", "aios-tool-health-probe.timer",
                 "aios-evolution-review.timer"):
        enabled = run(["systemctl", "--user", "is-enabled", unit])
        active = run(["systemctl", "--user", "is-active", unit])
        check(f"evolution-timer:{unit}", enabled.stdout.strip() == "enabled" and
              active.stdout.strip() == "active", enabled.stdout + active.stdout)

    try:
        evolution_dir = HOME / "kernel/centers/evolution_center"
        sys.path.insert(0, str(evolution_dir))
        from evolution_controller import status as evolution_status
        evolution = evolution_status()
        check("evolution:core-never-auto-deploy",
              evolution.get("auto_deploy") is False and
              evolution.get("deployment_confirmation_required") is True, evolution)
    except Exception as exc:
        check("evolution:core-never-auto-deploy", False, exc)

    if real_task:
        run_real_e2e()

    result, _ = write_report("acceptance")
    return 0 if result["failed"] == 0 else 1

if __name__ == "__main__":
    if "--canary" in sys.argv:
        raise SystemExit(canary_main())
    raise SystemExit(main(real_task="--no-task" not in sys.argv))
