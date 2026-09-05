#!/usr/bin/env python3
"""
AIOS v4.0 Verification Gate (验证门禁)
======================================
自动验证已完成任务结果:
  1. 订阅总线事件，监控 task.completed
  2. 自动运行 verify.py 验证
  3. 更新任务状态 (verifying → completed/failed)
  4. 发布验证报告到 Redis

用法:
  python3 aios_verification_gate.py              # 持续监控
  python3 aios_verification_gate.py --once       # 单次扫描验证
  python3 aios_verification_gate.py --verify <task_id>  # 验证指定任务
"""

import sys, os, json, time, subprocess, traceback, hashlib, re, tomllib, urllib.request
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import (_is_available, _redis_client, publish_event,
                      transition_task_state, get_task_state,
                      get_queue_status, check_recent, generate_task_id,
                      heartbeat)
from aios_enforcer import post_exec_verification
from aios_error_classifier import classify_error, log_error

RECOVERABLE_REVIEWER_STATES = frozenset({
    "stale", "timeout", "network_error", "probe_error", "probe_failed", "unverified",
})
LOADED_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
REVIEW_POLICY_PATH = Path("${AIOS_HOME}/config/verification_policy.json")


def _allow_live_semantic_recovery(health: dict) -> bool:
    """Allow one bounded real semantic call to recover transient health-cache states."""
    state = str(health.get("model_state", health.get("state", "unknown")))
    return (
        not health.get("fully_operational")
        and bool(health.get("contract_ok"))
        and state in RECOVERABLE_REVIEWER_STATES
    )

CHECK_INTERVAL = 60  # 检查间隔秒数
VERIFY_TIMEOUT = 60  # 验证超时秒数


def verify_completed_tasks(limit: int = 10) -> list:
    """扫描总线中已完成但未经验证的任务，自动验证."""
    if not _is_available():
        return []

    verified = []

    # 获取最近 completed 状态的任务
    recent = check_recent(hours=24, limit=limit, status_filter="completed")
    for r in recent:
        task_id = r.get("task_id", "")
        if not task_id:
            continue

        # 查询完整状态
        state = get_task_state(task_id)
        current_status = state.get("status", "")

        # 跳过已验证或不需要验证的任务
        # Parent-owned nodes are verified only by verify_parent_node().
        # The legacy scanner must never override or retract that verdict.
        if state.get("parent_id") or state.get("system") == "aios-orchestrator":
            continue

        already_passed = str(state.get("verification_passed", "")).lower() in ("1", "true", "yes")
        if current_status == "verified" or already_passed:
            continue
        if current_status != "completed":
            continue

        # 跳过已经有过 verifying 标记的
        if state.get("ts_verifying"):
            continue

        # 执行验证
        result = _verify_single_task(task_id, state, r)
        verified.append(result)

    return verified


def _verify_single_task(task_id: str, state: dict, record: dict) -> dict:
    """对单个任务执行验证."""
    print(f"  🔍 Verifying {task_id[:12]}...")

    # 过渡到 verifying 状态
    original_executor = state.get("executor", record.get("executor", ""))
    transition_task_state(
        task_id, "verifying", executor=original_executor,
        metadata={"verifier": "verification_gate"},
    )

    task_name = state.get("task_name", record.get("task_name", ""))
    source = state.get("source", record.get("source", "unknown"))

    # 创建验证用的 task dict
    verify_task = {
        "task_id": task_id,
        "task_name": task_name,
        "logic_depth": state.get("logic_depth", "low"),
        "source": source,
        "verification_criteria": state.get("verification_criteria", []),
        "output_file": state.get("output_file", ""),
    }

    start_ts = time.time()

    try:
        # 使用 aios_enforcer 的 post_exec_verification
        ok, report = post_exec_verification(
            verify_task, result_summary=state.get("result_summary", ""))

        if ok:
            # 验证通过
            transition_task_state(
                task_id, "completed", executor=original_executor,
                metadata={"verification_passed": True,
                          "verification_report": report[:200],
                          "verifier": "verification_gate"},
            )
            publish_event("task.verified", {
                "task_id": task_id, "passed": True, "report": report[:200],
            }, "verification_gate")
            print(f"    ✅ 验证通过")
        else:
            # 验证失败 — fail closed; preserve the executor result in state.
            transition_task_state(
                task_id, "failed", executor=original_executor,
                metadata={"verification_failed": True, "error": report[:200],
                          "verifier": "verification_gate"},
            )
            # 记录错误
            err = classify_error(report)
            log_error(err["code"], "verification_gate", f"Task {task_id[:12]} verification failed: {report[:100]}")
            publish_event("task.verification_failed", {
                "task_id": task_id, "error": report[:200],
            }, "verification_gate")
            print(f"    ❌ 验证失败: {report[:100]}")

        elapsed_ms = int((time.time() - start_ts) * 1000)
        return {
            "task_id": task_id,
            "passed": ok,
            "report": report[:200],
            "elapsed_ms": elapsed_ms,
        }

    except Exception as e:
        # Verification infrastructure errors are failures, never successful
        # delivery. Preserve the real executor for traceability.
        transition_task_state(
            task_id, "failed", executor=original_executor,
            metadata={"verification_error": str(e)[:200],
                      "verifier": "verification_gate"},
        )
        print(f"    ⚠️ 验证异常: {e}")
        return {"task_id": task_id, "passed": False, "error": str(e)[:200]}


# P2 hardening: verdict JSON extraction, schema validation, and provider
# lifecycle classification. These helpers are deterministic, backward
# compatible, and never default an invalid verdict to pass.

VERDICT_EXTRACT_OK = "OK"
VERDICT_EXTRACT_EMPTY_OUTPUT = "EMPTY_OUTPUT"
VERDICT_EXTRACT_NO_JSON_OBJECT = "NO_JSON_OBJECT"
VERDICT_EXTRACT_MALFORMED_JSON = "MALFORMED_JSON"
VERDICT_EXTRACT_MULTIPLE_JSON_OBJECTS = "MULTIPLE_JSON_OBJECTS"
VERDICT_EXTRACT_NON_OBJECT_JSON = "NON_OBJECT_JSON"


def _strip_one_markdown_fence(s: str) -> str:
    """Strip a single outer ```json ... ``` or ``` ... ``` fence if present.

    Returns the original string when no balanced outer fence is detected.
    """
    s = (s or "").strip()
    if s.startswith("```json") and s.endswith("```") and len(s) >= len("```json```"):
        inner = s[len("```json"):-3].strip()
        return inner or s
    if s.startswith("```") and s.endswith("```") and len(s) >= 6:
        inner = s[3:-3].strip()
        return inner or s
    return s


def _extract_verdict_json(text):
    """Strictly extract a single JSON object from a model reply.

    P2 hardening contract:
      - Reject None and empty strings (EMPTY_OUTPUT).
      - Strip exactly one outer Markdown fence (```json ... ``` / ``` ... ```).
      - Strip the trailing portion of one <think>... block for
        backward compatibility with reasoning-capable models.
      - First try a direct json.loads on the normalized text.
      - On failure, use json.JSONDecoder().raw_decode to scan for the
        FIRST complete JSON object from left to right.
      - Reject MULTIPLE_JSON_OBJECTS (multiple top-level dict objects found).
      - Reject NON_OBJECT_JSON (top-level value is not a dict, e.g. array).
      - Reject MALFORMED_JSON only when no complete object can be found.

    Returns (category, value):
      VERDICT_EXTRACT_OK                       -> dict
      VERDICT_EXTRACT_EMPTY_OUTPUT             -> None
      VERDICT_EXTRACT_NO_JSON_OBJECT           -> None
      VERDICT_EXTRACT_MALFORMED_JSON           -> None
      VERDICT_EXTRACT_MULTIPLE_JSON_OBJECTS    -> list[dict]
      VERDICT_EXTRACT_NON_OBJECT_JSON          -> non-dict JSON value
    """
    if text is None:
        return VERDICT_EXTRACT_EMPTY_OUTPUT, None
    s = str(text).strip()
    if not s:
        return VERDICT_EXTRACT_EMPTY_OUTPUT, None
    s = _strip_one_markdown_fence(s)
    if not s:
        return VERDICT_EXTRACT_EMPTY_OUTPUT, None
    # Strip the trailing portion of one <think>... block for backward
    # compatibility with reasoning-capable models. The XML tag is built
    # from chr() so the literal angle-bracket sequence survives editor
    # transformations.
    _THINK_OPEN = chr(60) + "think" + chr(62)
    _THINK_CLOSE = chr(60) + "/think" + chr(62)
    if _THINK_CLOSE in s:
        s = s.rsplit(_THINK_CLOSE, 1)[-1].strip()
        if s.startswith(_THINK_OPEN):
            s = s[len(_THINK_OPEN):].lstrip()
    if not s:
        return VERDICT_EXTRACT_EMPTY_OUTPUT, None
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return VERDICT_EXTRACT_OK, v
        return VERDICT_EXTRACT_NON_OBJECT_JSON, v
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    candidates = []
    pos = 0
    while pos < len(s):
        idx = s.find("{", pos)
        if idx < 0:
            break
        try:
            obj, end = decoder.raw_decode(s[idx:])
        except json.JSONDecodeError:
            pos = idx + 1
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
        pos = idx + end if end > 0 else idx + 1
    if not candidates:
        return VERDICT_EXTRACT_NO_JSON_OBJECT, None
    if len(candidates) > 1:
        return VERDICT_EXTRACT_MULTIPLE_JSON_OBJECTS, candidates
    return VERDICT_EXTRACT_OK, candidates[0]


def _extract_json_object(text: str) -> dict:
    """Backward-compatible wrapper around _extract_verdict_json.

    Returns the parsed dict on success, else {}. Callers that need the
    strict failure category should call _extract_verdict_json directly.
    """
    category, value = _extract_verdict_json(text)
    if category == VERDICT_EXTRACT_OK and isinstance(value, dict):
        return value
    return {}


# P2 schema validation: pass only when the verdict object is structurally
# sound.  Missing 'passed' is a hard failure.  Other known fields are
# type-checked when present; unknown fields are kept but do not affect the
# verdict.

_VERDICT_SCHEMA_ERRORS = {
    "INVALID_VERDICT_NOT_OBJECT",
    "INVALID_VERDICT_MISSING_PASSED",
    "INVALID_VERDICT_PASSED_NOT_BOOL",
    "INVALID_VERDICT_REASON_NOT_STRING",
    "INVALID_VERDICT_REPAIR_INSTRUCTION_NOT_STRING",
    "INVALID_VERDICT_EVIDENCE_CHECKED_NOT_BOOL",
    "INVALID_VERDICT_EVIDENCE_SOURCES_NOT_LIST",
    "INVALID_VERDICT_EVIDENCE_SOURCES_CONTAINS_NON_STRING",
}


def validate_verdict_schema(parsed):
    """Validate a parsed verdict against the AIOS semantic-review schema.

    Returns (ok, error_code, normalized_dict). When ok is False, error_code
    is one of the constants in _VERDICT_SCHEMA_ERRORS and normalized_dict is
    an empty dict. When ok is True, normalized_dict contains at minimum
    {'passed': bool} plus any well-formed optional fields with length caps
    applied.
    """
    if not isinstance(parsed, dict):
        return False, "INVALID_VERDICT_NOT_OBJECT", {}
    if "passed" not in parsed:
        return False, "INVALID_VERDICT_MISSING_PASSED", {}
    if not isinstance(parsed["passed"], bool):
        return False, "INVALID_VERDICT_PASSED_NOT_BOOL", {}
    normalized = {"passed": parsed["passed"]}
    if "reason" in parsed:
        if not isinstance(parsed["reason"], str):
            return False, "INVALID_VERDICT_REASON_NOT_STRING", {}
        normalized["reason"] = parsed["reason"][:1000]
    if "repair_instruction" in parsed:
        if not isinstance(parsed["repair_instruction"], str):
            return False, "INVALID_VERDICT_REPAIR_INSTRUCTION_NOT_STRING", {}
        normalized["repair_instruction"] = parsed["repair_instruction"][:1000]
    if "evidence_checked" in parsed:
        if not isinstance(parsed["evidence_checked"], bool):
            return False, "INVALID_VERDICT_EVIDENCE_CHECKED_NOT_BOOL", {}
        normalized["evidence_checked"] = parsed["evidence_checked"]
    if "evidence_sources" in parsed:
        sources = parsed["evidence_sources"]
        if not isinstance(sources, list):
            return False, "INVALID_VERDICT_EVIDENCE_SOURCES_NOT_LIST", {}
        if not all(isinstance(x, str) for x in sources):
            return False, "INVALID_VERDICT_EVIDENCE_SOURCES_CONTAINS_NON_STRING", {}
        normalized["evidence_sources"] = sources[:10]
    return True, None, normalized


_EVIDENCE_BLOCK_RE = re.compile(
    r"<!--\s*AIOS_EVIDENCE\s*-->\s*(\{.*?\})\s*<!--\s*/AIOS_EVIDENCE\s*-->",
    re.DOTALL,
)
_EVIDENCE_BLOCK_TAG_RE = re.compile(
    r"<!--\s*(?:AIOS_EVIDENCE|/AIOS_EVIDENCE)\s*-->",
)


def _extract_evidence_block(text: str) -> tuple[str, list[dict]]:
    """Pull the trailing AIOS_EVIDENCE JSON block out of an executor payload.

    Production Evidence Contract (P2):
      Executors may append a fenced JSON evidence block at the end of their
      deliverable to surface structured, authoritative-feeling observations
      (systemd unit / journal / git / queue / provider / health / etc.) that
      are otherwise hard to recover from the natural-language summary.

    The block is purely a transport contract; it does NOT replace the
    independent evidence the verifier itself acquires. The returned list
    flows back into ``verify_parent_node`` so the deterministic grounding
    checks can correlate claims against real, structured sources.

    Returns ``(clean_text, evidence_list)``.
    """
    if not text:
        return "", []
    match = _EVIDENCE_BLOCK_RE.search(text)
    if not match:
        return text, []
    raw = match.group(1)
    evidence_list: list[dict] = []
    parsed_ok = True
    try:
        parsed = json.loads(raw)
        items = parsed.get("evidence") if isinstance(parsed, dict) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    evidence_list.append(item)
    except (TypeError, ValueError):
        # Malformed JSON block: drop the entire block (including the
        # inner payload) so the verifier never sees arbitrary text
        # masquerading as authoritative evidence.
        parsed_ok = False
        evidence_list = []
    if not parsed_ok:
        clean = text[:match.start()].rstrip() + text[match.end():]
        clean = _EVIDENCE_BLOCK_TAG_RE.sub("", clean).strip()
        return clean, []
    clean = (_EVIDENCE_BLOCK_TAG_RE.sub("", text)).strip()
    return clean, evidence_list


def _executor_payload(result_text: str, executor: str) -> str:
    """Separate transport labels from the user-facing executor payload."""
    text = str(result_text or "").strip()
    prefixes = {
        "opencode": ("[opencode] ",),
        "codex": ("[codex] ",),
        "claude": ("Claude Code: ", "[claude] "),
    }
    for prefix in prefixes.get(str(executor or "").lower(), ()):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
            break
    return text


COMPONENT_VERSION_SOURCES = {
    "openclaw": {
        "tokens": ("openclaw",),
        "binary": "${HOME}/.n/bin/openclaw",
        "package_json": "${HOME}/.n/lib/node_modules/openclaw/package.json",
        "registry_package": "openclaw",
    },
    "opencode": {
        "tokens": ("opencode", "open code"),
        "binary": "${HOME}/.n/bin/opencode",
        "package_json": "${HOME}/.n/lib/node_modules/opencode-ai/package.json",
        "registry_package": "opencode-ai",
    },
}


def _component_version_evidence(objective: str) -> dict:
    """Read installed component versions and, when requested, official npm data."""
    lowered = str(objective or "").lower()
    wants_upstream = any(token in lowered for token in (
        "latest", "upstream", "official", "release date", "upgrade",
        "最新", "上游", "官方", "发布日期", "升级",
    ))
    records = {}
    for component, spec in COMPONENT_VERSION_SOURCES.items():
        if not any(token in lowered for token in spec["tokens"]):
            continue
        record = {
            "component": component,
            "installed_source": spec["binary"] + " --version",
            "package_manifest": spec["package_json"],
        }
        try:
            package_data = json.loads(Path(spec["package_json"]).read_text(encoding="utf-8"))
            installed_version = str(package_data.get("version", ""))
            runtime_env = os.environ.copy()
            runtime_env["PATH"] = "${HOME}/.n/bin:" + runtime_env.get("PATH", "")
            proc = subprocess.run(
                [spec["binary"], "--version"], capture_output=True, text=True,
                timeout=10, shell=False, env=runtime_env,
            )
            output = (proc.stdout or proc.stderr or "").strip()
            if proc.returncode != 0 or not output:
                raise RuntimeError(f"exit_{proc.returncode}:{output[-300:]}")
            if not installed_version or installed_version not in output:
                record["installed_evidence_conflict"] = {
                    "executable_output": output,
                    "package_version": installed_version,
                }
            record.update({
                "installed_executable_output": output,
                "installed_version": installed_version,
                "installed_package_name": str(package_data.get("name", "")),
            })
        except Exception as exc:
            record["installed_evidence_error"] = f"{type(exc).__name__}:{str(exc)[:300]}"

        try:
            if component == "openclaw":
                bridge_path = Path(
                    "${AIOS_HOME}/modules/openclaw-aios-bridge/package.json"
                )
                bridge = json.loads(bridge_path.read_text(encoding="utf-8"))
                record["aios_integration"] = {
                    "name": str(bridge.get("name", "")),
                    "path": str(bridge_path),
                    "compat": bridge.get("openclaw", {}).get("compat", {}),
                }
            elif component == "opencode":
                adapters = json.loads(Path(
                    "${AIOS_HOME}/config/tool_adapters.json"
                ).read_text(encoding="utf-8"))
                adapter = adapters.get("tools", {}).get("opencode", {})
                record["aios_integration"] = {
                    "client": str(adapter.get("executable", "")),
                    "server_service": str(adapter.get("server_service", "")),
                    "server_url": "http://127.0.0.1:4096",
                    "role": str(adapter.get("role", "")),
                }
        except Exception as exc:
            record["aios_integration_error"] = f"{type(exc).__name__}:{str(exc)[:300]}"

        if wants_upstream:
            package = spec["registry_package"]
            registry_url = f"https://registry.npmjs.org/{package}"
            record["official_registry_url"] = registry_url
            record["official_package_page"] = f"https://www.npmjs.com/package/{package}"
            record["registry_retrieved_at"] = datetime.now(timezone.utc).isoformat()
            try:
                request = urllib.request.Request(
                    registry_url, headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    registry = json.loads(response.read())
                latest = str(registry.get("dist-tags", {}).get("latest", ""))
                if not latest or "-" in latest:
                    raise ValueError(f"latest_tag_is_not_stable:{latest}")
                times = registry.get("time", {})
                versions = registry.get("versions", {})
                stable_versions = [
                    version for version in versions
                    if "-" not in version and times.get(version)
                ]
                stable_versions.sort(key=lambda value: str(times.get(value, "")))
                installed = str(record.get("installed_version", ""))
                release_gap = None
                releases_after = []
                if installed in stable_versions and latest in stable_versions:
                    installed_index = stable_versions.index(installed)
                    latest_index = stable_versions.index(latest)
                    releases_after = stable_versions[installed_index + 1:latest_index + 1]
                    release_gap = max(0, latest_index - installed_index)
                record.update({
                    "latest_stable_version": latest,
                    "latest_stable_release_date": str(times.get(latest, "")),
                    "stable_release_gap_from_installed": release_gap,
                    "stable_releases_after_installed": releases_after[-30:],
                    "pre_releases_excluded": True,
                    "release_notes_verified": False,
                    "release_notes_status": (
                        "Official registry metadata verifies versions and dates, but no "
                        "official release-note/changelog content was acquired."
                    ),
                })
            except Exception as exc:
                record["official_registry_error"] = f"{type(exc).__name__}:{str(exc)[:300]}"
        records[component] = record
    return records


def _collect_independent_evidence(goal: str, node: dict) -> tuple[dict, list[str]]:
    """Acquire authoritative evidence independently of the executor result."""
    mode = str(node.get("evidence_mode", "semantic") or "semantic")
    evidence = {
        "mode": mode,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "authoritative": {},
    }
    errors = []
    # Host Read-Only Evidence Boundary (final-production 2026-08-11):
    # always surface the workflow-level host evidence here so any
    # caller — including the Orchestrator's correction-retry prompt —
    # sees the same authoritative baseline without re-running the host
    # probe.  The actual decode + merge lives in :func:`verify_parent_node`
    # so the legacy single-task verification path (no parent workflow)
    # still works.
    try:
        parent_id = ""
        if isinstance(node, dict):
            parent_id = str(node.get("parent_id", "") or "")
        if parent_id:
            from aios_orchestrator_host_evidence_injection import (
                load_workflow_host_evidence,
            )
            host_evidence = load_workflow_host_evidence(parent_id)
            if host_evidence:
                evidence["authoritative"]["host_evidence"] = host_evidence
                evidence["authoritative"]["host_evidence_used"] = bool(
                    host_evidence.get("items")
                )
    except Exception:
        pass
    if mode == "semantic":
        return evidence, errors
    if mode == "aios-runtime":
        try:
            with urllib.request.urlopen("http://127.0.0.1:18801/health", timeout=5) as response:
                health = json.loads(response.read())
            runtime_status = {}
            try:
                with urllib.request.urlopen("http://127.0.0.1:18801/status", timeout=15) as response:
                    runtime_status = json.loads(response.read())
            except Exception as exc:
                errors.append(
                    "independent_aios_status_evidence_unavailable:"
                    f"{type(exc).__name__}:{str(exc)[:300]}"
                )
            manifest = json.loads(
                Path("${AIOS_HOME}/config/module_manifest.json").read_text(encoding="utf-8")
            )
            with Path("${AIOS_HOME}/config/features.toml").open("rb") as handle:
                features = tomllib.load(handle)
            versions = {
                "runtime_health": str(health.get("version", "")),
                "module_manifest": str(manifest.get("system_version", "")),
                "features": str(features.get("system_version", "")),
            }
            if not all(versions.values()) or len(set(versions.values())) != 1:
                errors.append("authoritative_version_sources_disagree:" + json.dumps(versions))
            failed_units_result = subprocess.run(
                ["systemctl", "--user", "--failed", "--no-legend", "--plain"],
                capture_output=True, text=True, timeout=5, shell=False,
            )
            failed_units = []
            if failed_units_result.returncode == 0:
                failed_units = [
                    line.split()[0]
                    for line in failed_units_result.stdout.splitlines()
                    if line.strip() and line.split()
                ]
            else:
                errors.append(
                    "authoritative_systemd_failed_units_unavailable:"
                    + failed_units_result.stderr.strip()[:300]
                )
            orchestrator_processes = []
            orchestrator_script = "${AIOS_HOME}/kernel/tools/aios_orchestrator.py"
            for proc_dir in Path("/proc").iterdir():
                if not proc_dir.name.isdigit():
                    continue
                try:
                    cmdline = (proc_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                        "utf-8", errors="replace",
                    ).strip()
                except (OSError, PermissionError):
                    continue
                if orchestrator_script in cmdline and "--daemon" in cmdline:
                    orchestrator_processes.append({
                        "pid": int(proc_dir.name), "cmdline": cmdline,
                    })
            evidence["authoritative"] = {
                "health": health,
                "runtime_status": runtime_status,
                "versions": versions,
                "utc_now": datetime.now(timezone.utc).isoformat(),
                "version_source": "GET http://127.0.0.1:18801/health + config/module_manifest.json + config/features.toml",
                "runtime_source": "GET http://127.0.0.1:18801/status",
                "failed_systemd_units": failed_units,
                "orchestrator_processes": orchestrator_processes,
                "canonical_runtime_sources": {
                    "queue": "GET /status queue (backed by aios:bus:queue:pending/locked/running/verifying and aios:orchestrator:active)",
                    "tool_availability": "GET /status executors.list[].available and inference_ready",
                    "failed_services": "systemctl --user --failed",
                    "orchestrator_processes": "direct /proc/*/cmdline exact daemon match",
                },
                "non_authoritative_legacy_keys": [
                    "aios:queue:priority:data", "aios:agents:available",
                ],
            }
        except Exception as exc:
            errors.append(f"independent_aios_evidence_unavailable:{type(exc).__name__}:{str(exc)[:300]}")
    elif mode == "independent-live":
        objective = f"{goal}\n{node.get('task', '')}"
        component_versions = _component_version_evidence(objective)
        if component_versions:
            evidence["authoritative"]["component_versions"] = component_versions
            evidence["authoritative"]["component_version_source"] = (
                "AIOS Verification Gate direct executable/package read and official npm registry"
            )
        raw_paths = {
            item.rstrip(".,;:!?")
            for item in re.findall(r"/[^\s'\"\x60,;:()\[\]{}]+", objective)
            if item.count("/") >= 2
        }
        safe_roots = (
            Path("${AIOS_HOME}/sandbox/coding").resolve(),
            Path("/tmp").resolve(),
        )
        files = {}
        for raw_path in sorted(raw_paths):
            candidate = Path(raw_path)
            try:
                resolved = candidate.resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            if not any(resolved == root or root in resolved.parents for root in safe_roots):
                continue
            record = {"path": str(resolved), "exists": resolved.exists(),
                      "is_file": resolved.is_file()}
            if resolved.is_file():
                stat = resolved.stat()
                digest = hashlib.sha256()
                with resolved.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                record.update({"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                               "sha256": digest.hexdigest()})
                if stat.st_size <= 4096:
                    record["utf8_content"] = resolved.read_bytes().decode(
                        "utf-8", errors="replace",
                    )
            files[str(resolved)] = record
        if files:
            evidence["authoritative"]["files"] = files
            evidence["authoritative"]["source"] = (
                "AIOS Verification Gate direct stat/read/SHA-256"
            )
            lowered = str(node.get("task", "")).lower()
            presence_required = any(term in lowered for term in (
                "write", "read", "report", "checksum", "hash", "content", "exists",
            ))
            deletion = any(term in lowered for term in ("delete", "remove", "unlink"))
            if presence_required and not deletion:
                for file_record in files.values():
                    if not file_record.get("is_file"):
                        errors.append("authoritative_file_missing:" + file_record["path"])
        elif not component_versions:
            evidence["reviewer_must_acquire_live_sources"] = True
    else:
        evidence["reviewer_must_acquire_live_sources"] = True
    return evidence, errors


# ---------------------------------------------------------------------------
# Reviewer payload bounded construction (2026-08-10 production availability)
# ---------------------------------------------------------------------------
#
# Production reality: the full ``grounding`` block returned by
# :func:`_collect_independent_evidence` routinely contains 60-70 KB of JSON
# (large ``runtime_status.recent_tasks`` history, full ``executors.list``
# records, redis dump, etc.).  Truncating it at 8000 characters inside the
# reviewer prompt produced several real production defects:
#   * The truncation cut a JSON object mid-string, so the Hermes reviewer
#     saw a malformed payload and triggered a repair attempt — first attempt
#     30-50 s, repair 30-50 s, totalling 60-100 s, which could overflow the
#     120 s subprocess bound under variance.
#   * The 8000-character slice fell inside ``runtime_status.recent_tasks``
#     (the 60 KB history list), so the reviewer never saw the
#     queue / executor summary fields it actually needs to validate.
#
# ``_build_minimal_review_evidence`` distils the authoritative block into
# only the fields the reviewer must see to make a fact-grounded verdict:
#   * health.ok, health.version, health.timestamp
#   * runtime_status.queue (pending/locked/running/completed/failed)
#   * runtime_status.executors_summary (name + available + inference_ready)
#   * runtime_status.recent_tasks_summary (24 h aggregate only)
#   * runtime_status.services (systemd health)
#   * failed_systemd_units, orchestrator_processes
#   * versions, canonical_runtime_sources, non_authoritative_legacy_keys
#   * workflow, systemd_units, corroborated_evidence (max 3), executor_evidence_summary
#   * utc_now, version_source, runtime_source
# The full ``recent_tasks`` history is intentionally **not** inlined — it
# lives in the deterministic gate (which still consumes the full block) and
# is not part of the Reviewer's independent reasoning surface.
#
# ``_build_minimal_review_prompt`` rebuilds the Reviewer prompt around the
# five blocks the spec requires:
#   1. REVIEW INSTRUCTIONS (~750 chars)
#   2. TASK REQUIREMENTS (goal + node_objective + acceptance)
#   3. FINAL DELIVERABLE (executor result)
#   4. RELEVANT AUTHORITATIVE EVIDENCE (minimal evidence, ≤2 500 chars)
#   5. PREVIOUS VERIFICATION FAILURE (only on correction retry, ≤1 800 chars)
# It also returns ``payload_bytes`` for audit so the orchestrator can record
# ``review_payload_before_bytes`` and ``review_payload_after_bytes`` on the
# verdict without the truncation ever cutting an open JSON object.
REVIEWER_PROMPT_FIELD_CAPS = {
    "goal": 1500,
    "node_objective": 1500,
    "acceptance": 1500,
    "deliverable": 6000,
    "evidence": 2500,
    "previous_failure": 1800,
    "verifier_intro": 750,
}


def _build_minimal_review_evidence(grounding: dict) -> dict:
    """Distil the full authoritative grounding into the reviewer-visible surface.

    The full grounding block is preserved unchanged for the deterministic
    gate; only the slice surfaced to the Reviewer subprocess is condensed.
    The shape mirrors the canonical AIOS runtime surface so the Reviewer
    can still cite ``queue.pending`` / ``executors_summary.available`` /
    ``failed_systemd_units`` / ``versions`` etc. when issuing a verdict.
    """
    if not isinstance(grounding, dict):
        return {"mode": "semantic", "authoritative": {}}
    out = {
        "mode": str(grounding.get("mode", "semantic") or "semantic"),
        "collected_at": str(grounding.get("collected_at", "") or ""),
        "authoritative": {},
    }
    auth = grounding.get("authoritative") or {}
    if not isinstance(auth, dict):
        return out
    a = out["authoritative"]
    # 1. health block — small and authoritative for runtime audits.
    health = auth.get("health")
    if isinstance(health, dict):
        a["health"] = {
            "ok": health.get("ok"),
            "service": health.get("service"),
            "version": health.get("version"),
            "timestamp": health.get("timestamp"),
            "redis": health.get("redis"),
            "status": health.get("status"),
        }
    # 2. runtime_status — keep queue / services / executors_summary /
    #    recent_tasks_summary only.  Drop the full recent_tasks history
    #    (60 KB+) and the verbose executors.list (expertise + today_stats
    #    + process_alive detail).
    rs = auth.get("runtime_status") or {}
    if isinstance(rs, dict):
        rs_min = {
            "ok": rs.get("ok"),
            "service": rs.get("service"),
            "version": rs.get("version"),
            "timestamp": rs.get("timestamp"),
            "queue": rs.get("queue") if isinstance(rs.get("queue"), dict) else {},
        }
        executors = rs.get("executors") or {}
        if isinstance(executors, dict):
            rs_min["executors_summary"] = {
                "count": executors.get("count"),
                "available": [
                    {
                        "name": e.get("name"),
                        "available": e.get("available"),
                        "runtime_status": e.get("runtime_status"),
                        "inference_ready": e.get("inference_ready"),
                    }
                    for e in (executors.get("list") or [])
                    if isinstance(e, dict)
                ],
            }
        rts = rs.get("recent_tasks_summary")
        if isinstance(rts, dict):
            rs_min["recent_tasks_summary"] = rts
        services = rs.get("services")
        if isinstance(services, dict):
            rs_min["services"] = services
        a["runtime_status"] = rs_min
    # 3. Versions + sources.
    for key in ("versions", "utc_now", "version_source", "runtime_source",
                "failed_systemd_units", "orchestrator_processes",
                "canonical_runtime_sources", "non_authoritative_legacy_keys"):
        if key in auth:
            a[key] = auth[key]
    # 4. Workflow + corroboration.
    if isinstance(auth.get("workflow"), dict):
        a["workflow"] = auth["workflow"]
    if isinstance(auth.get("systemd_units"), list):
        a["systemd_units"] = auth["systemd_units"]
    ce = auth.get("corroborated_evidence")
    if isinstance(ce, list) and ce:
        a["corroborated_evidence"] = ce[:3]
    ee = auth.get("executor_evidence")
    if isinstance(ee, list) and ee:
        a["executor_evidence_summary"] = {
            "count": auth.get("executor_evidence_count", len(ee)),
            "items": [
                {k: v for k, v in (item or {}).items()
                 if k in ("source_type", "source", "summary", "authoritative")}
                for item in ee[:3]
                if isinstance(item, dict)
            ],
        }
    # 5. Host Read-Only Evidence Boundary (final-production 2026-08-11).
    # The orchestrator collected this evidence on submit and persisted
    # it on the workflow hash.  Re-read it here so the Reviewer has
    # the SAME authoritative baseline the executor was grounded on.
    he = auth.get("host_evidence")
    if isinstance(he, dict):
        he_items = list(he.get("items") or [])
        a["host_evidence"] = {
            "profile": he.get("profile", ""),
            "generated_at": he.get("generated_at", ""),
            "summary": he.get("summary") or {},
            "items": he_items[:10],
        }
    elif isinstance(he, (bytes, bytearray, str)) and he:
        import json as _json
        try:
            parsed = _json.loads(he)
            if isinstance(parsed, dict):
                a["host_evidence"] = {
                    "profile": parsed.get("profile", ""),
                    "generated_at": parsed.get("generated_at", ""),
                    "summary": parsed.get("summary") or {},
                    "items": list(parsed.get("items") or [])[:10],
                }
        except Exception:
            pass
    return out


def _build_minimal_review_prompt(goal: str, node: dict, executor: str,
                                 deliverable: str, evidence_mode: str,
                                 grounding: dict,
                                 previous_failure: str = "") -> tuple[str, dict]:
    """Build the bounded Reviewer prompt (5-block shape) and audit metadata.

    Returns ``(prompt, meta)`` where ``meta`` carries the per-block sizes
    so the caller can record ``review_payload_before_bytes`` and
    ``review_payload_after_bytes`` on the verdict / audit ledger.
    """
    caps = REVIEWER_PROMPT_FIELD_CAPS
    g = str(goal or "")
    task = str(node.get("task", "") if isinstance(node, dict) else "")
    acceptance = node.get("acceptance", []) if isinstance(node, dict) else []
    evidence_min = _build_minimal_review_evidence(grounding)
    evidence_json = json.dumps(evidence_min, ensure_ascii=False)
    # Apply caps.
    g_c = g[:caps["goal"]]
    task_c = task[:caps["node_objective"]]
    acc_c = json.dumps(acceptance, ensure_ascii=False)[:caps["acceptance"]]
    deliverable_c = str(deliverable or "")[:caps["deliverable"]]
    evidence_c = evidence_json[:caps["evidence"]]
    prev_c = ""
    if previous_failure:
        prev_c = str(previous_failure)[:caps["previous_failure"]]
    intro = (
        "You are the independent AIOS result verifier. Judge the executor "
        "result against the goal, node objective and acceptance list. "
        "Verify only the current NODE_OBJECTIVE; do not require dependent "
        "or future nodes. Reject unsupported claims, contradictions, vague "
        "completion claims, and false draft statements. Reject any single "
        "materially false claim. For aios-runtime mode, compare every "
        "factual claim with AUTHORITATIVE_EVIDENCE; the /status response, "
        "systemd failed-unit list, and exact process snapshot are "
        "authoritative. Accept a timestamp field when it parses as "
        "ISO-8601, is not later than the verifier snapshot, and is no more "
        "than 300 seconds older. Return one JSON object only with keys "
        "passed(boolean), reason(string), repair_instruction(string), "
        "evidence_checked(boolean), and evidence_sources(array of strings)."
    )[:caps["verifier_intro"]]
    blocks = [
        "REVIEW INSTRUCTIONS:\n" + intro,
        "TASK REQUIREMENTS:\n"
        f"  goal: {g_c}\n"
        f"  node_objective: {task_c}\n"
        f"  acceptance: {acc_c}",
        "FINAL DELIVERABLE:\n" + deliverable_c,
        "RELEVANT AUTHORITATIVE EVIDENCE:\n"
        f"  mode: {evidence_mode}\n"
        f"  payload: {evidence_c}",
    ]
    if prev_c:
        blocks.append(
            "PREVIOUS VERIFICATION FAILURE "
            "(truncated to 1800 chars for correction):\n" + prev_c
        )
    # Host Read-Only Evidence Boundary (final-production 2026-08-11):
    # surface a compact, deterministic host-evidence section AFTER the
    # AUTHORITATIVE_EVIDENCE block so the reviewer can compare claims
    # against the structured summary (capability / ok / truncated) the
    # same way the executor saw it.  The raw bodies are deliberately
    # omitted here — the bounded evidence cap and the section's own
    # cap keep the reviewer prompt tight.
    try:
        from aios_orchestrator_host_evidence_injection import (
            build_host_evidence_reviewer_section,
            load_workflow_host_evidence,
        )
        parent_id = ""
        if isinstance(node, dict):
            parent_id = str(node.get("parent_id", "") or "")
        if not parent_id:
            # ``verify_parent_node`` always passes ``parent_id`` as a
            # separate argument; fall back to grounding's workflow
            # parent_task_id when the node does not carry it directly.
            wf = (grounding.get("authoritative") or {}).get("workflow") or {}
            parent_id = str(wf.get("parent_task_id", "") or "")
        host_evidence = load_workflow_host_evidence(parent_id)
        if host_evidence:
            he_section = build_host_evidence_reviewer_section(
                host_evidence,
                max_chars=4000,
                max_items=10,
                include_bodies=True,
            )
            if he_section:
                blocks.append(he_section)
    except Exception:
        pass
    prompt = "\n\n".join(blocks)
    meta = {
        "review_payload_after_bytes": len(prompt.encode("utf-8")),
        "review_payload_after_chars": len(prompt),
        "field_bytes": {
            "verifier_intro": len(intro.encode("utf-8")),
            "goal": len(g_c.encode("utf-8")),
            "node_objective": len(task_c.encode("utf-8")),
            "acceptance": len(acc_c.encode("utf-8")),
            "deliverable": len(deliverable_c.encode("utf-8")),
            "evidence": len(evidence_c.encode("utf-8")),
            "previous_failure": len(prev_c.encode("utf-8")),
        },
        "field_caps": dict(caps),
    }
    return prompt, meta


def _explicit_aios_fact_request(goal: str, fact: str) -> bool:
    """Match system-owned AIOS fields without capturing hosted-tool fields."""
    lowered = str(goal or "").lower()
    aliases = {
        "version": ("version", "版本"),
        "status": ("status", "health status", "health state", "状态", "健康状态"),
        "service": ("service", "服务"),
    }
    values = aliases[fact]
    english = "|".join(re.escape(item) for item in values if item.isascii())
    if english and re.search(
        rf"\baios(?:\s+(?:system|runtime|gateway|entry\s+gateway))?\s+(?:current\s+)?(?:{english})\b",
        lowered,
    ):
        return True
    if english and re.search(
        rf"\b(?:{english})\s+(?:of\s+)?(?:the\s+)?(?:aios|aios\s+gateway|entry\s+gateway)\b",
        lowered,
    ):
        return True
    chinese = "|".join(re.escape(item) for item in values if not item.isascii())
    return bool(chinese and re.search(
        rf"(?:aios|入口网关|系统网关)(?:系统)?(?:当前|运行)?(?:{chinese})",
        lowered,
    ))


def _component_grounding_errors(goal: str, deliverable: str, evidence: dict) -> list[str]:
    """Reject component-version claims that disagree with direct official evidence."""
    if evidence.get("mode") != "independent-live":
        return []
    records = evidence.get("authoritative", {}).get("component_versions", {})
    if not isinstance(records, dict) or not records:
        return []
    lowered_goal = str(goal or "").lower()
    text = str(deliverable or "")
    lowered_text = text.lower()
    errors = []
    wants_installed = any(token in lowered_goal for token in (
        "installed", "runtime", "local version", "actual version", "current version",
        "安装版本", "运行版本", "本地版本", "实际版本", "当前版本",
    ))
    wants_latest = any(token in lowered_goal for token in (
        "latest", "upstream", "release date", "upgrade",
        "最新", "上游", "发布日期", "升级",
    ))
    asks_compatibility = any(token in lowered_goal for token in (
        "compatibility", "upgrade risk", "risk analysis", "兼容", "升级风险", "风险分析",
    ))
    asks_aios_integration = any(token in lowered_goal for token in (
        "adapter", "bridge", "适配器", "桥接器",
    ))
    asks_release_gap = any(token in lowered_goal for token in (
        "gap", "behind", "how many versions", "相差多少", "落后多少", "版本差距",
    ))
    asks_official_source = any(token in lowered_goal for token in (
        "official source", "source url", "official url", "官方来源", "来源url",
    ))
    asks_query_time = any(token in lowered_goal for token in (
        "query time", "retrieved at", "retrieval time", "查询时间", "获取时间",
    ))
    asks_rollback = any(token in lowered_goal for token in (
        "rollback", "roll back", "回滚", "回退",
    ))
    release_note_uncertainty = (
        any(token in lowered_text for token in ("release notes", "changelog", "发布说明", "更新日志")) and
        any(token in lowered_text for token in (
            "not verified", "unverified", "unable to verify", "not acquired",
            "unavailable", "could not verify", "未验证", "无法验证", "未获取", "不可用",
        ))
    )
    # Aggregate every component-specific Markdown/table section. Reports may
    # legitimately centralize sources, comparisons and rollback plans instead
    # of using one rigid layout.
    heading_marks = []
    offset = 0
    for line in text.splitlines(keepends=True):
        normalized = re.sub(r"^[\s#>*|`-]+", "", line).replace("**", "").lower()
        present = [name for name in records if re.search(rf"\b{re.escape(name)}\b", normalized)]
        if len(present) == 1 and re.match(
            rf"^(?:[a-z0-9]+[.)]\s*)?{re.escape(present[0])}\b", normalized,
        ):
            heading_marks.append((offset, present[0]))
        offset += len(line)
    component_sections = {name: [] for name in records}
    for index, (start, component) in enumerate(heading_marks):
        end = heading_marks[index + 1][0] if index + 1 < len(heading_marks) else len(text)
        component_sections[component].append(text[start:end])

    for component, record in records.items():
        component_text = "\n".join(component_sections.get(component) or [text])
        component_lower = component_text.lower()
        unsupported_compatibility_claim = any(token in component_lower for token in (
            "semantic versioning suggests backward compatibility",
            "semver suggests backward compatibility",
            "should be backward compatible",
            "is backward compatible",
            "upgrade is compatible",
            "compatible with",
            "向后兼容性得到保证",
            "应当向后兼容",
            "确认兼容",
            "升级兼容",
        ))
        installed = str(record.get("installed_version", ""))
        latest = str(record.get("latest_stable_version", ""))
        release_date = str(record.get("latest_stable_release_date", ""))[:10]
        registry_url = str(record.get("official_registry_url", ""))
        package_page = str(record.get("official_package_page", ""))
        retrieved_date = str(record.get("registry_retrieved_at", ""))[:10]
        package_name = str(record.get("installed_package_name", ""))
        other_components = [name for name in records if name != component]
        stop_pattern = "|".join(re.escape(name) for name in other_components) or r"$^"
        inline_segments = re.findall(
            rf"\b{re.escape(component)}\b(?:(?!\b(?:{stop_pattern})\b).){{0,500}}",
            text, flags=re.IGNORECASE,
        )
        inline_claim_text = "\n".join(inline_segments)
        component_claim_text = component_text + "\n" + inline_claim_text
        allowed_versions = {
            value for value in (
                installed, latest,
                *[str(value) for value in record.get("stable_releases_after_installed", [])],
            ) if value
        }
        observed_versions = set(re.findall(
            r"(?<![0-9.])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9.])",
            inline_claim_text,
        ))
        unknown_versions = sorted(observed_versions - allowed_versions)
        if unknown_versions:
            errors.append(
                "authoritative_component_unrecognized_version_claim:"
                f"component={component}:observed={unknown_versions}:allowed={sorted(allowed_versions)}"
            )
        if wants_installed and installed and installed not in text:
            errors.append(
                f"authoritative_component_installed_version_missing:component={component}:"
                f"expected={installed}"
            )
        if wants_latest and latest and latest not in text:
            errors.append(
                f"authoritative_component_latest_version_missing:component={component}:"
                f"expected={latest}"
            )
        if wants_latest and release_date and release_date not in text:
            errors.append(
                f"authoritative_component_release_date_missing:component={component}:"
                f"expected={release_date}"
            )
        if wants_latest and asks_official_source and registry_url:
            if registry_url not in text and package_page not in text:
                errors.append(
                    "authoritative_component_official_source_missing:"
                    f"component={component}:expected={registry_url}"
                )
        if wants_latest and asks_query_time and retrieved_date:
            if retrieved_date not in text:
                errors.append(
                    "authoritative_component_query_time_missing:"
                    f"component={component}:expected_date={retrieved_date}"
                )
        release_gap = record.get("stable_release_gap_from_installed")
        if asks_release_gap and isinstance(release_gap, int):
            gap_patterns = (
                rf"(?:gap|behind|落后|相差)[^\n]{{0,80}}\b{release_gap}\b",
                rf"\b{release_gap}\s*(?:stable\s+)?(?:versions?|releases?|个(?:稳定)?版本|个(?:稳定)?发布)",
            )
            component_lines = "\n".join(
                line for line in text.splitlines() if component in line.lower()
            ).lower()
            gap_candidates = [
                re.sub(r"[*_`|]", " ", candidate)
                for candidate in (component_lower, component_lines)
            ]
            if not any(
                re.search(pattern, candidate)
                for pattern in gap_patterns
                for candidate in gap_candidates
            ):
                errors.append(
                    "authoritative_component_release_gap_missing_or_conflict:"
                    f"component={component}:expected={release_gap}"
                )
        if (wants_latest and asks_compatibility and
                record.get("release_notes_verified") is False):
            if not release_note_uncertainty:
                errors.append(
                    "authoritative_component_release_notes_uncertainty_missing:"
                    f"component={component}"
                )
            if unsupported_compatibility_claim:
                errors.append(
                    "unsupported_component_compatibility_claim_without_release_notes:"
                    f"component={component}"
                )
            if component == "openclaw" and any(token in component_lower for token in (
                "semantic version", "semver", "patch version", "minor version",
                "补丁版本", "次版本", "通常向后兼容", "一般向后兼容",
            )):
                errors.append(
                    "unsupported_component_version_scheme_claim_without_release_notes:"
                    "component=openclaw"
                )
        if asks_rollback and package_name and installed:
            exact_pin = f"{package_name}@{installed}".lower()
            if exact_pin not in lowered_text:
                errors.append(
                    "authoritative_component_rollback_pin_missing:"
                    f"component={component}:expected={package_name}@{installed}"
                )
            elif not re.search(
                rf"\bnpm\s+(?:i|install)\b[^\n]{{0,240}}{re.escape(exact_pin)}",
                lowered_text,
            ):
                errors.append(
                    "authoritative_component_rollback_command_missing:"
                    f"component={component}:expected=npm_install_{package_name}@{installed}"
                )
        integration = record.get("aios_integration", {})
        compat = integration.get("compat", {}) if isinstance(integration, dict) else {}
        if component == "openclaw" and compat and all(
            str(value).strip().startswith(">=") for value in compat.values()
        ) and any(token in component_claim_text.lower() for token in (
            "upper bound", "maximum version", "上界", "最高版本",
        )):
            errors.append(
                "authoritative_component_constraint_direction_conflict:"
                "component=openclaw:expected=minimum_lower_bound"
            )
        if component == "opencode" and installed and latest:
            installed_parts = installed.split(".")
            latest_parts = latest.split(".")
            minor_changed = (
                len(installed_parts) >= 2 and len(latest_parts) >= 2 and
                installed_parts[0] == latest_parts[0] and
                installed_parts[1] != latest_parts[1]
            )
            if minor_changed and any(token in component_lower for token in (
                "patch-level releases", "patch level releases", "patch version gap",
                "single patch", "仅补丁版本", "补丁级升级", "单个补丁",
            )):
                errors.append(
                    "authoritative_component_delta_classification_conflict:"
                    f"component=opencode:installed={installed}:latest={latest}:expected=minor_change"
                )
        if asks_aios_integration and record.get("aios_integration"):
            if component == "openclaw" and "openclaw-aios-bridge" not in lowered_text:
                errors.append(
                    "authoritative_aios_integration_missing:component=openclaw:"
                    "expected=openclaw-aios-bridge"
                )
            if component == "opencode" and not any(token in lowered_text for token in (
                "aios_opencode_client.py", "aios-opencode-server.service",
            )):
                errors.append(
                    "authoritative_aios_integration_missing:component=opencode:"
                    "expected=aios_opencode_client.py_or_aios-opencode-server.service"
                )
    return errors


def _aios_grounding_errors(goal: str, deliverable: str, evidence: dict) -> list[str]:
    """Reject AIOS runtime facts that conflict with the independently read runtime."""
    if evidence.get("mode") != "aios-runtime":
        return []
    authoritative = evidence.get("authoritative", {})
    health = authoritative.get("health", {})
    expected_version = str(health.get("version", ""))
    text = str(deliverable or "")
    lowered_goal = str(goal or "").lower()
    errors = []

    asks_version = _explicit_aios_fact_request(lowered_goal, "version")
    if asks_version and expected_version:
        observed = []
        for line in text.splitlines():
            lowered_line = line.lower()
            if "version" not in lowered_line and "\u7248\u672c" not in line:
                continue
            values = re.findall(
                r"(?<![0-9.])v?([0-9]+\.[0-9]+\.[0-9]+)(?![0-9.])", line,
            )
            if "redis" in lowered_line and not any(
                label in lowered_line for label in ("aios", "gateway", "service")
            ):
                continue
            observed.extend(values)
        if expected_version not in observed:
            errors.append(f"authoritative_version_missing:expected={expected_version}:observed={observed}")
        conflicts = sorted({value for value in observed if value != expected_version})
        if conflicts:
            errors.append(
                f"authoritative_version_conflict:expected={expected_version}:conflicts={conflicts}"
            )

    asks_status = _explicit_aios_fact_request(lowered_goal, "status")
    expected_status = str(health.get("status", ""))
    if asks_status and expected_status and expected_status.lower() not in text.lower():
        errors.append(f"authoritative_status_missing:expected={expected_status}")

    asks_redis = any(token in lowered_goal for token in (
        "redis state", "redis status", "redis_state",
        "redis \u72b6\u6001", "redis\u72b6\u6001",
    ))
    expected_redis = str(health.get("redis", ""))
    if asks_redis and expected_redis.lower() not in text.lower():
        errors.append(f"authoritative_redis_missing:expected={expected_redis}")

    asks_service = _explicit_aios_fact_request(lowered_goal, "service")
    expected_service = str(health.get("service", ""))
    if asks_service and expected_service and expected_service.lower() not in text.lower():
        # P2 Production Evidence Contract (2026-08-10):
        # When the deliverable names at least one real AIOS systemd unit
        # (any *.service literal that names an authoritative service, AND
        # which the verifier independently observes in the AIOS unit set),
        # the asks_service question is already answered. We do NOT require
        # the deliverable to also mention the web health-service name
        # (e.g. aios-entry-gateway). systemd unit audit IS the requested
        # "service" fact; the original T1/T2 OPS failure was caused by
        # an over-strict literal match against the web-service name when
        # the goal was actually about a different systemd unit.
        systemd_unit_re = re.compile(r"\b[a-z][a-z0-9_-]*\.service\b", re.IGNORECASE)
        named_units = set(systemd_unit_re.findall(text))
        # Verifier-side gate: a stray *.service string in the deliverable
        # MUST correspond to a real, observed AIOS unit. Otherwise the
        # bypass would let any made-up *.service literal satisfy the
        # check. ``authoritative_systemd_units`` (when present in the
        # ground truth, e.g. from a unit-list snapshot) anchors this.
        observed_units = set(
            str(unit) for unit in
            authoritative.get("failed_systemd_units", [])
        ) | set(str(unit) for unit in authoritative.get("systemd_units", []))
        evidence_units = set()
        for item in authoritative.get("executor_evidence", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("source_type") or "").lower() == "systemd":
                src = str(item.get("source") or "")
                if src.endswith(".service"):
                    evidence_units.add(src)
        evidence_pass = bool(named_units & (observed_units | evidence_units))
        ai_service_pass = any(
            unit.lower().startswith("aios-") for unit in named_units
        ) and (bool(evidence_units & named_units) or bool(named_units & observed_units))
        if evidence_pass or ai_service_pass:
            # service fact is covered by structured systemd evidence; do
            # NOT raise authoritative_service_missing.
            pass
        else:
            errors.append(f"authoritative_service_missing:expected={expected_service}")

    asks_timestamp = any(token in lowered_goal for token in (
        "timestamp", "system time", "current time", "\u65f6\u95f4\u6233",
        "\u7cfb\u7edf\u65f6\u95f4", "\u5f53\u524d\u65f6\u95f4",
    ))
    if asks_timestamp:
        snapshot_raw = str(authoritative.get("utc_now", ""))
        try:
            snapshot = datetime.fromisoformat(snapshot_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            errors.append("authoritative_timestamp_snapshot_unavailable")
        else:
            timestamp_values = re.findall(
                r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b",
                text,
            )
            valid_fresh = []
            for value in timestamp_values:
                try:
                    observed_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    age = (snapshot - observed_time).total_seconds()
                except (TypeError, ValueError):
                    continue
                if 0 <= age <= 300:
                    valid_fresh.append(value)
            if not timestamp_values:
                errors.append("authoritative_timestamp_missing")
            elif not valid_fresh:
                errors.append(
                    "authoritative_timestamp_invalid_or_stale:"
                    f"snapshot={snapshot_raw}:observed={timestamp_values[:5]}"
                )

    runtime_status = authoritative.get("runtime_status", {})
    audit_scope = any(token in lowered_goal for token in (
        "audit", "health check", "health inspection", "queue", "executor",
        "\u5ba1\u8ba1", "\u5de1\u68c0", "\u961f\u5217", "\u6267\u884c\u5668",
    ))
    if audit_scope and isinstance(runtime_status, dict) and runtime_status:
        legacy_labels = ("legacy", "obsolete", "deprecated", "non-authoritative",
                         "\u5e9f\u5f03", "\u65e7\u952e", "\u975e\u6743\u5a01", "\u4e0d\u4f5c\u4e3a")
        for key in authoritative.get("non_authoritative_legacy_keys", []):
            for line in text.splitlines():
                if key in line and not any(label in line.lower() for label in legacy_labels):
                    errors.append(f"non_authoritative_legacy_runtime_key:{key}")
                    break

        def labeled_integer(labels: tuple[str, ...], exclusions: tuple[str, ...] = ()):
            for line in text.splitlines():
                lowered_line = line.lower()
                if any(label in lowered_line for label in labels) and not any(
                    exclusion in lowered_line for exclusion in exclusions
                ):
                    values = re.findall(r"(?<![\w.])\d+(?![\w.])", line)
                    if values:
                        return int(values[0]), line.strip()
            return None, ""

        queue = runtime_status.get("queue", {})
        expected_active = int(queue.get("total_active", 0) or 0) if isinstance(queue, dict) else 0
        observed_active, active_line = labeled_integer((
            "unfinished tasks", "pending tasks", "active tasks", "\u672a\u5b8c\u6210\u4efb\u52a1",
            "\u5f85\u5904\u7406\u4efb\u52a1", "\u6d3b\u8dc3\u4efb\u52a1",
        ))
        if observed_active is not None and observed_active != expected_active:
            errors.append(
                f"authoritative_queue_conflict:expected_active={expected_active}:"
                f"observed={observed_active}:line={active_line[:200]}"
            )

        executor_block = runtime_status.get("executors", {})
        executor_list = executor_block.get("list", []) if isinstance(executor_block, dict) else []
        task_executors = [
            item for item in executor_list
            if isinstance(item, dict) and item.get("name") in ("opencode", "claude", "codex")
        ]
        expected_available = sum(bool(item.get("available")) for item in task_executors)
        expected_unavailable = len(task_executors) - expected_available
        observed_available, available_line = labeled_integer(
            ("available executors", "available executor", "\u53ef\u7528\u6267\u884c\u5668"),
            ("unavailable", "\u4e0d\u53ef\u7528"),
        )
        if observed_available is not None and observed_available != expected_available:
            errors.append(
                "authoritative_available_executor_count_conflict:"
                f"expected={expected_available}:observed={observed_available}:"
                f"line={available_line[:200]}"
            )
        observed_unavailable, unavailable_line = labeled_integer((
            "unavailable executors", "unavailable executor", "\u4e0d\u53ef\u7528\u6267\u884c\u5668",
        ))
        if observed_unavailable is not None and observed_unavailable != expected_unavailable:
            errors.append(
                "authoritative_unavailable_executor_count_conflict:"
                f"expected={expected_unavailable}:observed={observed_unavailable}:"
                f"line={unavailable_line[:200]}"
            )

        failed_units = authoritative.get("failed_systemd_units", [])
        observed_failed, failed_line = labeled_integer((
            "failed services", "failed service", "\u5931\u8d25\u670d\u52a1",
        ))
        if observed_failed is not None and observed_failed != len(failed_units):
            errors.append(
                "authoritative_failed_service_count_conflict:"
                f"expected={len(failed_units)}:observed={observed_failed}:"
                f"line={failed_line[:200]}"
            )
    return errors


def _workflow_metadata_errors(goal: str, deliverable: str, parent_id: str,
                              executor: str) -> list[str]:
    """Validate requested system-owned metadata before semantic review."""
    text = str(deliverable or "")
    compact_goal = re.sub(r"[\s_\-]+", "", str(goal or "").lower())
    asks_task_id = "taskid" in compact_goal or "任务id" in compact_goal or "任务编号" in compact_goal
    asks_executor = "actualexecutor" in compact_goal or "实际执行器" in compact_goal
    asks_final_status = "finalstatus" in compact_goal or "最终状态" in compact_goal
    errors = []

    def labeled_line(labels: tuple[str, ...]) -> str:
        for raw_line in text.splitlines():
            compact_line = re.sub(r"[\s*_`:#]+", "", raw_line.lower())
            if any(label in compact_line for label in labels):
                return raw_line.strip()
        return ""

    if asks_task_id:
        task_line = labeled_line(("taskid", "任务id", "任务编号"))
        if not task_line:
            errors.append(
                f"authoritative_parent_task_id_missing:expected={parent_id}"
            )
        elif parent_id not in task_line:
            errors.append(
                "authoritative_parent_task_id_conflict:"
                f"expected={parent_id}:observed={task_line[:300]}"
            )

    if asks_executor:
        executor_line = labeled_line(("actualexecutor", "实际执行器"))
        lowered_line = executor_line.lower()
        foreign = [
            name for name in ("opencode", "claude", "codex", "hermes", "openclaw")
            if name != executor.lower() and re.search(rf"\b{re.escape(name)}\b", lowered_line)
        ]
        if not executor_line:
            errors.append(
                f"authoritative_actual_executor_missing:expected={executor}"
            )
        elif (not re.search(rf"\b{re.escape(executor.lower())}\b", lowered_line)
              or foreign or "aios_executor_daemon" in lowered_line):
            errors.append(
                "authoritative_actual_executor_conflict:"
                f"expected={executor}:observed={executor_line[:300]}"
            )

    if asks_final_status:
        status_line = labeled_line(("finalstatus", "最终状态"))
        lowered_line = status_line.lower()
        if not status_line:
            errors.append("authoritative_final_status_missing:expected=completed")
        elif "completed" not in lowered_line and "已完成" not in status_line:
            errors.append(
                "authoritative_final_status_conflict:"
                f"expected=completed:observed={status_line[:300]}"
            )
    return errors


def _review_policy() -> dict:
    defaults = {
        "fail_closed": True,
        "exclude_executor": True,
        "reviewers": [
            {"id": "hermes", "backend": "hermes-cli"},
            {"id": "claude", "backend": "claude-cli"},
            {"id": "codex", "backend": "codex-cli"},
            {"id": "opencode", "backend": "opencode-server"},
        ],
    }
    try:
        loaded = json.loads(REVIEW_POLICY_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            defaults.update(loaded)
    except Exception:
        pass
    return defaults


# P2 provider-lifecycle classification. Distinct from the existing
# _classify_failure probe-side helper: this classifies what a verifier
# invocation actually returned, including transport outcomes and verdict
# validity. is_fatal=True means the current verification cycle MUST NOT
# retry the same reviewer for the same Task/Candidate.

PROVIDER_LIFECYCLE_KINDS = frozenset({
    "AUTH_FAILED",
    "QUOTA_EXHAUSTED",
    "PLAN_EXHAUSTED",
    "RATE_LIMITED_TRANSIENT",
    "TIMEOUT",
    "CONNECTION_FAILED",
    "NONZERO_EXIT",
    "EMPTY_OUTPUT",
    "MALFORMED_RESPONSE",
    "INVALID_VERDICT",
    "AVAILABLE",
    "UNKNOWN",
})

_PLAN_429_TOKENS = (
    "token plan", "plan exhausted", "plan_exhausted", "plan limit",
    "subscription limit", "subscription exhausted", "usage limit",
    "套餐", "套餐耗尽",
)


def _classify_provider_failure(stderr_text, returncode, parsed_dict=None,
                                extract_category=None):
    """Classify a reviewer invocation into a Provider lifecycle kind.

    The function first inspects stderr/returncode for transport failures
    (auth, quota, plan, rate, timeout, connection), then falls back to the
    verdict extraction category. Returns (kind, description, is_fatal).
    """
    txt = str(stderr_text or "").lower()
    # transport / status-code patterns first
    if "timed out" in txt or "timeout" in txt or "timed_out" in txt:
        return "TIMEOUT", "reviewer timed out", True
    if any(token in txt for token in ("connection", "network", "dns", "unreachable", "refused")):
        return "CONNECTION_FAILED", "reviewer connection failed", False
    # Claude quota: 402 + insufficient balance / quota exhaustion
    if "402" in txt or "insufficient balance" in txt or "余额不足" in txt:
        return "QUOTA_EXHAUSTED", "reviewer quota exhausted", True
    # Auth: 401 / 403
    if "401" in txt or "403" in txt or any(t in txt for t in (
        "unauthorized", "invalid api key", "authentication failed",
    )):
        return "AUTH_FAILED", "reviewer authentication failed", True
    # Rate limit: distinguish plan-based vs transient
    if "429" in txt or "rate limit" in txt or "too many requests" in txt:
        for token in _PLAN_429_TOKENS:
            if token in txt:
                return "PLAN_EXHAUSTED", "reviewer plan exhausted", True
        return "RATE_LIMITED_TRANSIENT", "reviewer transient rate limit", False
    # Non-zero exit without a recognized status code
    if returncode != 0:
        return "NONZERO_EXIT", f"reviewer non-zero exit rc={returncode}", False
    # Verdict-shape errors BEFORE empty/UNKNOWN fallback so a classified
    # extract category always wins.
    if extract_category in (
        VERDICT_EXTRACT_NO_JSON_OBJECT,
        VERDICT_EXTRACT_MALFORMED_JSON,
        VERDICT_EXTRACT_NON_OBJECT_JSON,
        VERDICT_EXTRACT_MULTIPLE_JSON_OBJECTS,
    ):
        return "MALFORMED_RESPONSE", f"reviewer verdict {extract_category.lower()}", False
    if extract_category and extract_category.startswith("INVALID_VERDICT"):
        return "INVALID_VERDICT", f"reviewer verdict {extract_category.lower()}", False
    if extract_category == VERDICT_EXTRACT_OK and isinstance(parsed_dict, dict):
        return "AVAILABLE", "reviewer verdict accepted", False
    # Empty output (extraction classified it as such OR no stderr/stdout)
    if extract_category == VERDICT_EXTRACT_EMPTY_OUTPUT or (
        parsed_dict is None and not txt and returncode == 0
    ):
        return "EMPTY_OUTPUT", "reviewer empty output", False
    return "UNKNOWN", "reviewer unknown failure", False


def _build_repair_prompt(original_prompt, content_text, schema_error):
    """Build a minimal repair prompt that requests reformat only.

    The repair MUST NOT change the Task/Candidate and MUST NOT add new
    evaluation criteria. The model is asked to return only the same
    verdict in a strict JSON object without Markdown fences.
    """
    excerpt = str(content_text or "").strip()
    if len(excerpt) > 2000:
        excerpt = excerpt[:2000] + "..."
    return (
        "Your previous reply could not be parsed as a single valid JSON "
        "verdict object. Re-emit ONLY one JSON object, no Markdown fence, "
        "no commentary, no extra text. Keys must be exactly: "
        "passed(boolean), reason(string), repair_instruction(string), "
        "evidence_checked(boolean), evidence_sources(array of strings). "
        "Do not change your judgement. Do not change the original Task "
        "or Candidate. Schema error: " + str(schema_error) +
        ". Original output: " + excerpt
    )


def _call_reviewer_via_minimax_adapter(prompt, reviewer, binding_id):
    """Run a Claude-style reviewer via the MiniMax parallel adapter.

    P9D-R secondary-role closure: the ``claude:minimax`` binding
    resolves through :class:`ClaudeMiniMaxAdapter` rather than the
    Claude binary's default DeepSeek endpoint, so the Reviewer path
    actually honours the selected binding_id / provider / endpoint /
    model / resource_id tuple instead of falling back to whatever the
    Claude binary was last configured with.  Returns the same dict
    shape as :func:`_call_reviewer_once` so the rest of the
    verification gate stays unchanged.
    """
    try:
        from minimax_official_client import (
            chat, MiniMaxOfficialError, MiniMaxDisabledError,
        )
    except Exception as exc:
        return {
            "reviewer": reviewer,
            "returncode": 0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stderr_text": f"minimax_official_import_failed:{type(exc).__name__}:{exc}"[-500:],
            "stdout_text": "",
            "latency_ms": 0,
            "live_recovery": False,
            "extract_category": VERDICT_EXTRACT_EMPTY_OUTPUT,
            "parsed_value": None,
            "provider_kind": "INVALID_LOCAL_CONFIGURATION",
            "provider_description": (
                "minimax_official_client import failed: " f"{type(exc).__name__}"
            ),
            "provider_fatal": True,
            "binding_id": binding_id,
        }
    messages = [{"role": "user", "content": str(prompt or "")[:16000]}]
    started = time.monotonic()
    try:
        r = chat(messages, max_tokens=1024, temperature=0.0,
                 purpose="reviewer", timeout=60)
    except MiniMaxDisabledError as exc:
        return {
            "reviewer": reviewer,
            "returncode": 0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stderr_text": f"minimax_disabled:{exc}"[-500:],
            "stdout_text": "",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "live_recovery": False,
            "extract_category": VERDICT_EXTRACT_EMPTY_OUTPUT,
            "parsed_value": None,
            "provider_kind": "INVALID_LOCAL_CONFIGURATION",
            "provider_description": f"Billing guard blocked: {exc}"[:300],
            "provider_fatal": True,
            "binding_id": binding_id,
        }
    except MiniMaxOfficialError as exc:
        return {
            "reviewer": reviewer,
            "returncode": 0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stderr_text": f"minimax_provider_error:{exc}"[-500:],
            "stdout_text": "",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "live_recovery": False,
            "extract_category": VERDICT_EXTRACT_EMPTY_OUTPUT,
            "parsed_value": None,
            "provider_kind": "CONNECTION_FAILED",
            "provider_description": f"MiniMax provider error: {exc}"[:300],
            "provider_fatal": False,
            "binding_id": binding_id,
        }
    except Exception as exc:
        return {
            "reviewer": reviewer,
            "returncode": 0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stderr_text": (
                f"adapter_chat_failed:{type(exc).__name__}:{exc}"
            )[-500:],
            "stdout_text": "",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "live_recovery": False,
            "extract_category": VERDICT_EXTRACT_EMPTY_OUTPUT,
            "parsed_value": None,
            "provider_kind": "CONNECTION_FAILED",
            "provider_description": (
                f"minimax_official_client chat raised "
                f"{type(exc).__name__}: {exc}"
            )[:300],
            "provider_fatal": False,
            "binding_id": binding_id,
        }
    latency_ms = int((time.monotonic() - started) * 1000)
    content = (r.get("content") or "").strip()
    extract_category, parsed_value = _extract_verdict_json(content)
    return {
        "reviewer": reviewer,
        "returncode": 0,
        "stdout_bytes": len(content),
        "stderr_bytes": 0,
        "stderr_text": (
            f"binding={binding_id} provider=minimax-official model="
            f"{r.get('model') or 'MiniMax-M3'}"
        )[-500:],
        "stdout_text": content,
        "latency_ms": latency_ms,
        "live_recovery": False,
        "extract_category": extract_category,
        "parsed_value": parsed_value,
        "provider_kind": "AVAILABLE",
        "provider_description": (
            f"minimax-official adapter success via "
            f"{r.get('model') or 'MiniMax-M3'}"
        ),
        "provider_fatal": False,
        "binding_id": binding_id,
    }


def _call_reviewer_once(prompt, reviewer, attempts, binding_id=""):
    """Run one reviewer subprocess. Returns dict with outcome details or None.

    On success: returns dict with extracted (category, value) and stdout/stderr
    sizes. On failure: appends an attempt entry and returns None.

    P9D-R secondary-role closure: when ``binding_id`` resolves to the
    Claude-typed MiniMax parallel binding (``claude:minimax``), the
    call is routed through :class:`ClaudeMiniMaxAdapter` instead of
    the Claude binary, so the verifier honours the selected
    binding / provider / endpoint / model / resource_id tuple.
    """
    if binding_id == "claude:minimax" and reviewer == "claude":
        return _call_reviewer_via_minimax_adapter(
            prompt, reviewer, binding_id,
        )
    from aios_tool_adapter import get_adapter
    try:
        adapter = get_adapter(reviewer)
        health = adapter.health()
        live_recovery = _allow_live_semantic_recovery(health)
        if not health.get("fully_operational") and not live_recovery:
            raise RuntimeError(
                f"not_operational:{health.get('model_state', health.get('state', 'unknown'))}"
            )
        env = dict(os.environ)
        env["PATH"] = (
            "${HOME}/.hermes/hermes-agent/venv/bin:"
            "${HOME}/.n/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
        )
        started = time.monotonic()
        proc = subprocess.run(
            adapter.command_for_task(prompt), capture_output=True, text=True,
            timeout=int(adapter.config.get("task_timeout_seconds", 120)),
            shell=False, env=env, cwd="${AIOS_HOME}/sandbox",
        )
        content = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        latency_ms = int((time.monotonic() - started) * 1000)
        extract_category, parsed_value = _extract_verdict_json(content)
        kind, description, fatal = _classify_provider_failure(
            stderr, proc.returncode,
            parsed_dict=parsed_value if isinstance(parsed_value, dict) else None,
            extract_category=extract_category,
        )
        return {
            "reviewer": reviewer,
            "returncode": proc.returncode,
            "stdout_bytes": len(proc.stdout or ""),
            "stderr_bytes": len(proc.stderr or ""),
            "stderr_text": stderr[-500:],
            "stdout_text": content,
            "latency_ms": latency_ms,
            "live_recovery": live_recovery,
            "extract_category": extract_category,
            "parsed_value": parsed_value,
            "provider_kind": kind,
            "provider_description": description,
            "provider_fatal": fatal,
            "adapter": adapter,
            "binding_id": binding_id,
        }
    except subprocess.TimeoutExpired:
        attempts.append({
            "reviewer": reviewer,
            "reason": "TIMEOUT:reviewer subprocess timed out",
            "provider_kind": "TIMEOUT",
            "provider_fatal": True,
        })
    except Exception as exc:
        attempts.append({
            "reviewer": reviewer,
            "reason": f"{type(exc).__name__}:{str(exc)[:300]}",
        })
    return None


def _semantic_review(prompt: str, executor: str, task_policy: dict = None,
                    child_state: dict = None) -> dict:
    """Run the first healthy independent reviewer; never self-approve.

    P2 hardening:
      - exclude_executor is enforced first.
      - Strict JSON extraction via _extract_verdict_json.
      - Strict schema validation via validate_verdict_schema.
      - One bounded format-repair attempt per reviewer. Repair is allowed
        ONLY for MALFORMED_RESPONSE / INVALID_VERDICT, NEVER for transport
        failures (auth, quota, plan, rate, timeout, connection).
      - Provider-lifecycle kind is recorded on every attempt.
      - All-reviewers-unavailable is a structured failure (NOT pass).

    P9D-R role-closure: when ``task_policy`` is supplied, reviewer
    selection honours ``preferred_reviewer`` /
    ``blocked_reviewer_tools`` / ``allow_reviewer_fallback`` via the
    same registry surface as the planner / executor paths.  When
    ``task_policy`` is missing, the legacy default reviewer set
    (``hermes`` first, then ``claude`` / ``opencode`` / ``openclaw``)
    is preserved so existing tests / canaries stay green.
    """
    tp = task_policy or {}
    preferred_reviewer = str(tp.get("preferred_reviewer") or "")
    blocked_reviewers = tuple(
        str(x) for x in (tp.get("blocked_reviewer_tools") or ())
        if str(x)
    )
    allow_reviewer_fallback = bool(
        tp.get("allow_reviewer_fallback", True)
    )
    # ---- P9D-R: route via the unified registry selection ---------
    # choose_reviewer is the single truth source for Reviewer
    # ordering: preferred first, then independent candidates, blocked
    # excluded, executor excluded.  Its ``candidates`` list is the
    # iteration order consumed by the loop below.
    try:
        from aios_orchestrator import choose_reviewer
        selection = choose_reviewer(
            task_id=str((child_state or {}).get("task_id", "")) or "verification-gate",
            preferred_reviewer=preferred_reviewer,
            blocked_reviewer_tools=blocked_reviewers,
            allow_reviewer_fallback=allow_reviewer_fallback,
            exclude_executor=executor or "",
        )
    except Exception as exc:
        selection = {
            "reviewer": "",
            "candidates": [],
            "excluded": [f"selection_unavailable:{type(exc).__name__}"],
            "fallback_count": 0,
            "failure_scope": "",
            "binding": "",
        }
    # Decide the iteration source: policy-driven selection takes
    # precedence; the static _review_policy() list is the fallback
    # for callers that did not pass a task_policy.
    policy = _review_policy()
    use_policy_driven = bool(tp) and bool(selection.get("candidates"))
    if use_policy_driven:
        iteration_order = list(selection.get("candidates") or [])
        iteration_excluded = list(selection.get("excluded") or [])
    else:
        iteration_order = [
            str(entry.get("id", ""))
            for entry in (policy.get("reviewers") or [])
            if isinstance(entry, dict) and entry.get("id")
        ]
        iteration_excluded = []
    attempts = []
    attempted_reviewers: list = []
    excluded_reviewers: list = [
        {"reviewer": str(ex[0]) if isinstance(ex, tuple) else str(ex),
         "reason": str(ex[1]) if isinstance(ex, tuple) and len(ex) > 1
                   else "policy_excluded"}
        for ex in iteration_excluded
    ]
    # Enforce strict no-fallback contract: if allow_reviewer_fallback
    # is False, the loop MUST NOT switch to a secondary reviewer.
    # P9D-R-Reviewer-Strict-Closure: the previous close-out short-circuited
    # to ``VERIFICATION_BLOCKED:strict_no_fallback`` whenever
    # ``compute_tool_status(preferred_reviewer)`` reported
    # ``UNAVAILABLE_*``, even if the reviewer was actually reachable
    # and only had a transient failure event in the bounded TTL.  The
    # correct semantic is: ALWAYS attempt the preferred reviewer
    # through the normal call path (the recovery probe clears any
    # transient failure event inline), and ONLY surface
    # ``VERIFICATION_BLOCKED:strict_no_fallback`` when the preferred
    # reviewer genuinely failed to produce a verdict.  See
    # docs/AIOS_P9DR_REVIEWER_STRICT_VERIFICATION_CLOSURE_20260804.md
    # for the full contract.
    strict_no_fallback = bool(tp) and not allow_reviewer_fallback
    if strict_no_fallback and preferred_reviewer:
        # P9D-R-Reviewer-Strict-Closure: clear any stale failure event
        # for the preferred reviewer so the inline recovery path can
        # re-evaluate its true health.  This is the same
        # service-active + endpoint-reachable + adapter-probe-ok
        # contract used by the executor recovery path; it is safe to
        # run on every semantic-review call because the three legs
        # are bounded and cheap.
        try:
            from aios_orchestrator import (
                _attempt_tool_recovery as _attempt_recovery,
            )
            _attempt_recovery(str(preferred_reviewer))
        except Exception:
            # Best-effort: the recovery path is a positive probe; if
            # the orchestrator module does not expose the helper (e.g.
            # a future refactor) we fall through to the legacy block
            # surface and let the loop attempt the call.
            pass
    # Build the loop body.  Each iteration is wrapped so we can record
    # attempted/excluded reviewers around the legacy call shape.
    legacy_index_to_reviewer = {
        str(entry.get("id", "")): str(entry.get("backend", entry.get("id", "")))
        for entry in (policy.get("reviewers") or [])
        if isinstance(entry, dict) and entry.get("id")
    }
    legacy_backend_for = lambda rid: legacy_index_to_reviewer.get(rid, rid)
    for reviewer in iteration_order:
        if not reviewer:
            continue
        backend = legacy_backend_for(reviewer)
        # P2: exclude_executor enforced first
        if policy.get("exclude_executor", True) and reviewer == executor:
            attempts.append({
                "reviewer": reviewer,
                "reason": "self_review_forbidden",
                "provider_kind": "EXCLUDED",
            })
            excluded_reviewers.append({
                "reviewer": reviewer,
                "reason": "self_review_forbidden",
            })
            continue
        # Track the call attempt in the audit field.
        attempted_reviewers.append(reviewer)
        # P9D-R: blocked_reviewer_tools is enforced both by
        # choose_reviewer (selection-time) and again here (call-time)
        # so any caller that bypasses the selection helper still
        # honours the policy.
        if reviewer in set(blocked_reviewers):
            attempts.append({
                "reviewer": reviewer,
                "reason": "blocked_reviewer_tool",
                "provider_kind": "BLOCKED",
            })
            excluded_reviewers.append({
                "reviewer": reviewer,
                "reason": "blocked_reviewer_tool",
            })
            continue
        # P9D-R role-closure: each iteration passes the per-reviewer
        # binding the registry actually selected (or the candidate
        # binding when the legacy path is used) so the verifier
        # honours the binding / provider / endpoint / model tuple.
        # ``selection`` is keyed by ``tool_id``; for legacy fallback
        # iterations we look up the per-tool binding via the failover
        # engine.  When no binding is available the call falls through
        # to the legacy subprocess path, preserving the old behaviour.
        reviewer_binding = str(selection.get("binding") or "")
        if not reviewer_binding:
            try:
                from aios_tool_failover import (
                    get_default_tool_engine as _dg,
                )
                _eng = _dg()
                _status = _eng.compute_tool_status(reviewer)
                if _status is not None:
                    reviewer_binding = str(
                        getattr(_status, "effective_binding", "") or ""
                    )
            except Exception:
                reviewer_binding = ""
        # First attempt
        outcome = _call_reviewer_once(
            prompt, reviewer, attempts, binding_id=reviewer_binding,
        )
        if outcome is None:
            continue
        # Record transport-level failure for audit
        if outcome["extract_category"] != VERDICT_EXTRACT_OK:
            # P2: do NOT trigger repair for transport/permission failures.
            if outcome["provider_kind"] in (
                "AUTH_FAILED", "QUOTA_EXHAUSTED", "PLAN_EXHAUSTED",
                "TIMEOUT", "CONNECTION_FAILED", "NONZERO_EXIT", "EMPTY_OUTPUT",
            ):
                attempts.append({
                    "reviewer": reviewer,
                    "reason": f"{outcome['provider_kind']}:{outcome['provider_description']}",
                    "provider_kind": outcome["provider_kind"],
                    "provider_fatal": outcome["provider_fatal"],
                    "returncode": outcome["returncode"],
                    "extract_category": outcome["extract_category"],
                    "stdout_bytes": outcome["stdout_bytes"],
                    "stderr_bytes": outcome["stderr_bytes"],
                    "binding_id": outcome.get("binding_id", reviewer_binding),
                })
                continue
            # Verdict-shape or unknown: ONE repair attempt allowed.
            repair_prompt = _build_repair_prompt(
                prompt, outcome["stdout_text"], outcome["extract_category"],
            )
            repair = _call_reviewer_once(
                repair_prompt, reviewer, attempts,
                binding_id=reviewer_binding,
            )
            repair_attempted = True
            if repair is None:
                attempts.append({
                    "reviewer": reviewer,
                    "reason": (
                        f"REPAIR_INFRASTRUCTURE_FAILED:"
                        f"{outcome['provider_kind']}:{outcome['provider_description']}"
                    ),
                    "provider_kind": outcome["provider_kind"],
                    "repair_attempted": True,
                    "repair_success": False,
                    "initial_error_kind": outcome["provider_kind"],
                    "final_error_kind": outcome["provider_kind"],
                })
                continue
            attempts.append({
                "reviewer": reviewer,
                "reason": (
                    f"REPAIR:initial={outcome['extract_category']}:"
                    f"repair={repair['extract_category']}"
                ),
                "repair_attempted": True,
                "initial_error_kind": outcome["provider_kind"],
            })
            outcome = repair  # use repair result for schema validation
        # Validate schema on the final outcome
        schema_ok, schema_err, normalized = validate_verdict_schema(outcome["parsed_value"])
        if not schema_ok:
            attempts.append({
                "reviewer": reviewer,
                "reason": f"INVALID_VERDICT:{schema_err}",
                "provider_kind": "INVALID_VERDICT",
                "provider_fatal": False,
                "extract_category": outcome["extract_category"],
                "schema_error": schema_err,
                "repair_attempted": True,
                "repair_success": False,
                "initial_error_kind": (
                    "MALFORMED_RESPONSE" if outcome["extract_category"]
                    not in (VERDICT_EXTRACT_OK,) else "INVALID_VERDICT"
                ),
                "final_error_kind": "INVALID_VERDICT",
            })
            continue
        # Record successful verdict
        if outcome.get("live_recovery"):
            try:
                outcome["adapter"].record_inference_success(
                    f"{reviewer} returned a valid semantic-review JSON verdict",
                    latency_ms=outcome["latency_ms"],
                )
            except Exception:
                pass
        # Determine if a repair was actually triggered (for the caller)
        was_repair = any(
            a.get("repair_attempted") and a.get("reviewer") == reviewer
            for a in attempts
        )
        attempts.append({
            "reviewer": reviewer,
            "reason": "verdict_accepted",
            "provider_kind": "AVAILABLE",
            "extract_category": outcome["extract_category"],
            "schema_ok": True,
            "repair_attempted": was_repair,
            # P4 truthfulness: a verdict that was never repaired must NOT
            # claim repair_success=True. The previous version always wrote
            # True here, which contradicted the upstream repair_attempted
            # field and made "first-try success" indistinguishable from
            # "success after a repair attempt". Now repair_success mirrors
            # repair_attempted: a repair actually happened iff was_repair.
            "repair_success": was_repair,
        })
        return {
            "parsed": normalized,
            "reviewer": reviewer,
            "reviewer_backend": backend,
            "reviewer_health_recovered": outcome.get("live_recovery", False),
            "repair_attempted": was_repair,
            "repair_success": was_repair,
            "attempts": attempts,
            # P9D-R role-closure: persist the unified routing audit so
            # the verdict surface records which Reviewer was actually
            # called and which were blocked / excluded.  These fields
            # are required for any workflow that wants to verify the
            # secondary Reviewer capability baseline.
            "attempted_reviewers": attempted_reviewers,
            "excluded_reviewers": excluded_reviewers,
            "preferred_reviewer": preferred_reviewer,
            "blocked_reviewer_tools": list(blocked_reviewers),
            "allow_reviewer_fallback": allow_reviewer_fallback,
            "actual_reviewer": reviewer,
            "reviewer_binding": str(selection.get("binding") or ""),
            "reviewer_fallback_count": len(attempted_reviewers) - 1,
            "reviewer_bypass": False,
            "policy_driven": use_policy_driven,
            "selection_candidates": list(iteration_order),
        }
    # P9D-R-Reviewer-Strict-Closure: when strict_no_fallback is in
    # effect and the preferred reviewer genuinely failed, surface a
    # ``VERIFICATION_BLOCKED:strict_no_fallback`` signal so the parent
    # workflow can record the exact reason.  The legacy
    # ``all_independent_reviewers_unavailable`` reason is kept for the
    # non-strict path so the existing canary / alert surface keeps
    # working.
    if strict_no_fallback and preferred_reviewer:
        attempts.append({
            "reviewer": preferred_reviewer,
            "reason": "VERIFICATION_BLOCKED:strict_no_fallback",
            "provider_kind": "BLOCKED",
        })
        raise RuntimeError(
            "VERIFICATION_BLOCKED:strict_no_fallback:" +
            json.dumps(attempts, ensure_ascii=False)[:1200]
        )
    raise RuntimeError("all_independent_reviewers_unavailable:" +
                       json.dumps(attempts, ensure_ascii=False)[:1200])


def _node_verification_goal(parent_goal: str, node: dict) -> str:
    node_goal = str(node.get("task", "") or "").strip()
    return node_goal or str(parent_goal or "")


def verify_parent_node(parent_id: str, goal: str, node: dict,
                       child_state: dict,
                       task_policy: dict = None) -> dict:
    """Canonical parent-node verification owned by Verification Gate.

    Deterministic checks run first. The Verification Gate owns an ordered set
    of replaceable independent reviewers and never lets an executor approve
    its own result.

    P9D-R role-closure: when ``task_policy`` is supplied, the Reviewer
    selection honours ``preferred_reviewer`` /
    ``blocked_reviewer_tools`` / ``allow_reviewer_fallback`` (see
    :func:`_semantic_review`).  ``task_policy`` is the canonical
    TaskRoutingPolicy view the Orchestrator derives from the workflow
    hash; it is the SAME source of truth that drives the planner /
    executor paths.  When ``task_policy`` is missing, the legacy
    ``_review_policy()`` static list drives the iteration so existing
    canaries / fallbacks stay green.
    """
    result_text = str(child_state.get("result_summary", "") or "").strip()
    executor = str(child_state.get("executor", "") or "unknown")
    raw_deliverable = _executor_payload(result_text, executor)
    # P2 Production Evidence Contract (2026-08-10):
    # Pull the structured AIOS_EVIDENCE JSON block out of the raw
    # deliverable. The block is a transport contract that lets executors
    # surface authoritative evidence (systemd unit, journal tail,
    # git HEAD, queue snapshot, provider health, ...) without having to
    # quote raw command output in the natural-language summary.
    deliverable, executor_evidence = _extract_evidence_block(raw_deliverable)
    # Host-evidence lock (2026-08-11): REPLACE any material fact
    # line in the LLM deliverable that conflicts with an
    # authoritative HF-* anchor derived from the workflow's
    # host_evidence.  This runs BEFORE the deliverable is handed
    # to the Reviewer so the Reviewer never sees a hallucinated
    # value.  Conflicts are recorded on the audit event stream.
    # Recommendations / risk prose survive untouched.
    try:
        from aios_orchestrator_host_evidence_injection import (
            load_workflow_host_evidence,
        )
        from aios_host_evidence_lock import (
            extract_anchor_facts,
            filter_fact_conflicts,
            render_verified_facts_block,
        )
        he = load_workflow_host_evidence(parent_id) if parent_id else None
        if he:
            anchors = extract_anchor_facts(he)
            if anchors:
                cleaned, conflicts = filter_fact_conflicts(
                    deliverable, anchors,
                )
                verified = render_verified_facts_block(anchors)
                deliverable = (
                    (cleaned.rstrip() + "\n\n") if cleaned.strip() else ""
                ) + verified
                if conflicts:
                    try:
                        publish_event(
                            "host_evidence.lock.filtered",
                            {
                                "parent_id": parent_id,
                                "child_id": child_id,
                                "anchor_count": len(anchors),
                                "conflict_count": len(conflicts),
                                "conflicts": conflicts,
                            },
                            "aios-verification-gate",
                        )
                    except Exception:
                        pass
    except Exception:
        # Evidence lock is best-effort: a failure here MUST NOT
        # block the verification chain.
        pass
    child_id = str(child_state.get("task_id", "") or "")
    deterministic_errors = []

    if child_state.get("status") != "completed":
        deterministic_errors.append("child_not_completed")
    if len(deliverable) < 2:
        deterministic_errors.append("empty_result")
    if child_state.get("execution_error"):
        deterministic_errors.append("execution_error_present")
    if deterministic_errors:
        verdict = {
            "passed": False,
            "stage": "deterministic",
            "reviewer": "aios-verification-gate",
            "reason": ",".join(deterministic_errors),
            "repair_instruction": "Produce a concrete result and execution evidence.",
        }
        publish_event("task.parent_verification_failed", {
            "parent_id": parent_id, "task_id": child_id,
            "reason": verdict["reason"],
        }, "verification_gate")
        return verdict

    verification_goal = _node_verification_goal(goal, node)
    grounding, grounding_errors = _collect_independent_evidence(verification_goal, node)
    grounding.setdefault("authoritative", {})["workflow"] = {
        "parent_task_id": parent_id,
        "actual_executor": executor,
        "successful_final_status": "completed",
    }
    # Host Read-Only Evidence Boundary (final-production 2026-08-11):
    # splice the workflow-level host evidence the Orchestrator
    # collected on submit.  The deterministic grounding can now
    # compare claims against this baseline; the Reviewer surface
    # (built by ``_build_minimal_review_evidence`` above) also sees
    # the same baseline through ``grounding.authoritative``.
    try:
        from aios_orchestrator_host_evidence_injection import (
            load_workflow_host_evidence,
        )
        host_evidence = load_workflow_host_evidence(parent_id)
        if host_evidence:
            grounding["authoritative"]["host_evidence"] = host_evidence
            grounding["authoritative"]["host_evidence_used"] = bool(
                host_evidence.get("items")
            )
    except Exception:
        pass
    # Surface the structured executor evidence so deterministic
    # grounding (systemd unit / queue / health) can correlate claims
    # against authoritative ground truth even when the natural-language
    # summary has been compressed. The block is one of many inputs; the
    # verifier STILL independently re-reads /health, /status, and
    # systemctl before publishing a verdict.
    if executor_evidence:
        grounding["authoritative"]["executor_evidence"] = executor_evidence
        grounding["authoritative"]["executor_evidence_count"] = len(executor_evidence)
        # Promote any authoritative=true item to a top-level
        # ``corroborated_evidence`` field used by downstream Reviewers
        # when assembling their prompt.
        corroborated = [
            dict(item) for item in executor_evidence
            if isinstance(item, dict) and item.get("authoritative") is True
        ]
        if corroborated:
            grounding["authoritative"]["corroborated_evidence"] = corroborated
        # Surface any systemd unit the executor claims to have observed.
        systemd_units = {
            str(item.get("source") or "")
            for item in executor_evidence
            if isinstance(item, dict)
            and str(item.get("source_type") or "").lower() == "systemd"
            and str(item.get("source") or "").endswith(".service")
        }
        if systemd_units:
            grounding["authoritative"]["systemd_units"] = sorted(systemd_units)
    grounding_errors.extend(
        _workflow_metadata_errors(verification_goal, deliverable, parent_id, executor)
    )
    grounding_errors.extend(_aios_grounding_errors(verification_goal, deliverable, grounding))
    grounding_errors.extend(
        _component_grounding_errors(verification_goal, deliverable, grounding)
    )
    if grounding_errors:
        verdict = {
            "passed": False,
            "stage": "deterministic_grounding",
            "reviewer": "aios-verification-gate",
            "executor": executor,
            "reason": ";".join(grounding_errors)[:1000],
            "repair_instruction": (
                "Re-read the current authoritative source and replace every conflicting "
                "dynamic fact. Historical files and executor self-claims are not authoritative."
            ),
            "verification_strength": "grounding-rejected",
            "learning_eligible": False,
            "independent_evidence": grounding,
        }
        publish_event("task.parent_verification_failed", {
            "parent_id": parent_id, "task_id": child_id,
            "reason": verdict["reason"],
        }, "verification_gate")
        return verdict

    acceptance = node.get("acceptance", []) or []
    evidence_mode = str(grounding.get("mode", "semantic"))
    # P9D-R Reviewer production availability (2026-08-10): use the bounded
    # 5-block minimal-reviewer prompt shape so the Reviewer subprocess
    # does not time out on the old 60-70 KB truncation-prone prompt.
    # The full grounding block is preserved unchanged for the
    # deterministic gate (which still consumes it above); only the
    # Reviewer-visible surface is condensed.
    previous_failure = ""
    if isinstance(node, dict):
        previous_failure = str(node.get("previous_failure") or "")
    prompt, prompt_meta = _build_minimal_review_prompt(
        goal=goal, node=node, executor=executor,
        deliverable=deliverable, evidence_mode=evidence_mode,
        grounding=grounding, previous_failure=previous_failure,
    )
    # Record the bounded payload size against the OLD prompt shape so
    # the orchestrator can audit ``review_payload_before_bytes`` /
    # ``review_payload_after_bytes`` on the verdict.
    legacy_prompt_bytes = sum((
        4000,  # ORIGINAL_GOAL cap
        3000,  # NODE_OBJECTIVE cap
        3000,  # ACCEPTANCE cap
        8000,  # AUTHORITATIVE_EVIDENCE cap
        16000,  # RESULT cap
        2869,  # verifier_intro (was unbounded)
        200,   # template/separators
    ))
    prompt_meta["review_payload_before_bytes"] = legacy_prompt_bytes
    # P9D-R role-closure: prefer the task policy's reviewer selection.
    # The workflow hash already carries ``preferred_reviewer`` /
    # ``blocked_reviewer_tools`` / ``allow_reviewer_fallback``;
    # re-derive the view here so the Verification Gate honours the
    # same task policy as the planner / executor.
    #
    # ``task_policy`` may be a plain dict (legacy callers) or a
    # :class:`aios_task_routing_policy.TaskRoutingPolicy` object
    # (the canonical shape produced by ``from_workflow_dict``).
    # Both shapes are unwrapped transparently so the downstream
    # ``_semantic_review`` always receives a plain dict.
    if task_policy is None:
        task_policy = {}
    elif not isinstance(task_policy, dict):
        task_policy = {
            "preferred_reviewer": getattr(task_policy, "preferred_reviewer", "") or "",
            "blocked_reviewer_tools": list(
                getattr(task_policy, "blocked_reviewer_tools", []) or []
            ),
            "allow_reviewer_fallback": bool(
                getattr(task_policy, "allow_reviewer_fallback", True)
            ),
        }
    if isinstance(node, dict):
        workflow_like = {
            "preferred_reviewer": node.get("preferred_reviewer", ""),
            "blocked_reviewer_tools": node.get("blocked_reviewer_tools", []),
            "allow_reviewer_fallback": node.get("allow_reviewer_fallback", True),
        }
        # Merge explicit top-level fields the orchestrator may also
        # forward (e.g. parent_id-keyed overrides) when present.
        for source_key in ("parent_id", "workflow_policy"):
            sub = node.get(source_key) if isinstance(node.get(source_key), dict) else None
            if isinstance(sub, dict):
                workflow_like.update({k: v for k, v in sub.items() if v not in (None, "", [])})
        node_policy = {k: v for k, v in workflow_like.items()
                       if v not in (None, "", [], {})}
        # Explicit task_policy wins over node-derived fields.
        task_policy = {**node_policy, **task_policy}
    try:
        review = _semantic_review(
            prompt, executor,
            task_policy=task_policy or None,
            child_state=child_state,
        )
        parsed = review["parsed"]
        sources = parsed.get("evidence_sources", [])
        if not isinstance(sources, list):
            sources = []
        evidence_checked = bool(parsed.get("evidence_checked", False))
        if evidence_mode == "aios-runtime":
            evidence_checked = bool(grounding.get("authoritative"))
            if not sources:
                sources = [grounding.get("authoritative", {}).get("version_source", "")]
                sources = [item for item in sources if item]
        # 2026-08-11 Host Read-Only Evidence Boundary closure: when
        # ``evidence_mode == "independent-live"`` and the workflow
        # already carries a real host-side authoritative evidence block
        # (collected by ``aios_host_readonly_evidence.collect_host_evidence``
        # on submit, persisted on the workflow hash, and re-loaded into
        # ``grounding.authoritative.host_evidence``), the verifier MUST
        # treat that block as independently acquired evidence.  We
        # therefore set ``evidence_checked`` to True and seed
        # ``sources`` from the host_evidence profile/items.  This is
        # symmetric with the ``aios-runtime`` branch above and does NOT
        # bypass the independent-live requirement — empty / error /
        # self-claimed evidence still fails.
        if evidence_mode == "independent-live":
            auth_he = (
                grounding.get("authoritative", {}).get("host_evidence") or {}
            )
            he_items = auth_he.get("items") or []
            if isinstance(he_items, list):
                he_real = [
                    item for item in he_items
                    if isinstance(item, dict)
                    and not item.get("error")
                    and str(item.get("capability") or "").strip()
                ]
            else:
                he_real = []
            if he_real:
                evidence_checked = True
                if not sources:
                    profile = str(auth_he.get("profile", "") or "")
                    caps = sorted({
                        str(item.get("capability") or "").strip()
                        for item in he_real
                        if str(item.get("capability") or "").strip()
                    })
                    if profile and caps:
                        sources = [
                            f"host_evidence:{profile}:{cap}"
                            for cap in caps
                        ]
        grounded = (
            evidence_mode == "semantic" or
            (evidence_mode == "aios-runtime" and evidence_checked) or
            (evidence_mode == "independent-live" and evidence_checked and bool(sources))
        )
        passed = bool(parsed["passed"]) and grounded
        reason = str(parsed.get("reason", ""))
        if parsed["passed"] and not grounded:
            reason = "independent_live_evidence_not_acquired"
        verdict = {
            "passed": passed,
            "stage": "semantic_grounded" if evidence_mode != "semantic" else "semantic",
            "reviewer": review["reviewer"],
            "reviewer_backend": review["reviewer_backend"],
            "executor": executor,
            "reason": reason[:1000],
            "repair_instruction": str(parsed.get("repair_instruction", ""))[:1000],
            "deliverable": deliverable[:32768],
            "reviewer_health_recovered": review["reviewer_health_recovered"],
            "reviewer_attempts": review["attempts"],
            "evidence_mode": evidence_mode,
            "evidence_checked": evidence_checked,
            "evidence_sources": sources[:10],
            "independent_evidence": grounding,
            "verification_strength": (
                "grounded-runtime+semantic" if evidence_mode == "aios-runtime"
                else "grounded-independent-live+semantic" if evidence_mode == "independent-live"
                else "independent-semantic"
            ),
            "learning_eligible": passed,
            # P9D-R role-closure: persist the unified reviewer routing
            # audit so downstream persistence (gateway task, parent
            # hash, workflow hash, child state, verification record,
            # trace, Result Push) all observe the same fields.  These
            # were returned by ``_semantic_review`` but never copied
            # into the verdict; downstream readers therefore could not
            # tell which Reviewer was actually invoked.
            "preferred_reviewer": review.get("preferred_reviewer", ""),
            "blocked_reviewer_tools": review.get("blocked_reviewer_tools", []),
            "allow_reviewer_fallback": review.get("allow_reviewer_fallback", True),
            "attempted_reviewers": review.get("attempted_reviewers", []),
            "excluded_reviewers": review.get("excluded_reviewers", []),
            "actual_reviewer": review.get("actual_reviewer", ""),
            "reviewer_binding": review.get("reviewer_binding", ""),
            "reviewer_failure_scope": review.get("failure_scope", ""),
            "reviewer_fallback_count": review.get("reviewer_fallback_count", 0),
            "reviewer_bypass": review.get("reviewer_bypass", False),
            "reviewer_policy_driven": review.get("policy_driven", False),
            "reviewer_selection_candidates": review.get("selection_candidates", []),
            # P9D-R Reviewer production availability (2026-08-10): record
            # the bounded payload audit so downstream readers can confirm
            # the Reviewer subprocess received a minimal-reviewer prompt
            # instead of the legacy 60-70 KB truncation-prone shape.
            "review_payload_before_bytes": int(
                prompt_meta.get("review_payload_before_bytes", 0)
            ),
            "review_payload_after_bytes": int(
                prompt_meta.get("review_payload_after_bytes", 0)
            ),
            "review_payload_after_chars": int(
                prompt_meta.get("review_payload_after_chars", 0)
            ),
            "review_payload_field_bytes": dict(
                prompt_meta.get("field_bytes", {})
            ),
            "review_payload_field_caps": dict(
                prompt_meta.get("field_caps", {})
            ),
        }
    except Exception as exc:
        verdict = {
            "passed": False,
            "stage": "semantic_infrastructure",
            "reviewer": "aios-verification-gate",
            "executor": executor,
            "reason": f"semantic_verifier_unavailable: {str(exc)[:500]}",
            "repair_instruction": "Do not deliver; restore an independent reviewer and recheck.",
            "verification_strength": "unverified",
            "learning_eligible": False,
            "independent_evidence": grounding,
        }

    publish_event(
        "task.parent_verified" if verdict["passed"] else "task.parent_verification_failed",
        {"parent_id": parent_id, "task_id": child_id,
         "reviewer": verdict["reviewer"], "reason": verdict["reason"][:300]},
        "verification_gate",
    )
    return verdict


def run_once():
    """单次运行验证扫描."""
    print("=" * 50)
    print(f"  AIOS Verification Gate — {datetime.now().isoformat()}")
    print("=" * 50)

    # 验证已完成的任务
    print("\n[1/1] 扫描并验证已完成任务...")
    results = verify_completed_tasks(limit=20)

    passed = sum(1 for r in results if r.get("passed"))
    failed = sum(1 for r in results if not r.get("passed"))

    print(f"\n  验证结果: {len(results)} 个任务")
    print(f"    通过: {passed}")
    print(f"    失败: {failed}")

    return {"total": len(results), "passed": passed, "failed": failed}


def verify_specific(task_id: str) -> dict:
    """验证指定任务."""
    state = get_task_state(task_id)
    if state.get("status") == "unknown":
        print(f"❌ 任务 {task_id[:12]} 未找到")
        return {"error": "not_found"}

    recent = check_recent(hours=72, limit=50)
    record = next((r for r in recent if r.get("task_id") == task_id), {})

    result = _verify_single_task(task_id, state, record)
    icon = "✅" if result.get("passed") else "❌"
    print(f"\n{icon} 验证完成: {result.get('report', result.get('error', '?'))}")
    return result


def run_daemon():
    """持续运行验证监控."""
    print(f"\n{'='*50}")
    print(f"  AIOS Verification Gate Daemon")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print(f"{'='*50}\n")

    last_check = time.time()
    while True:
        now = time.time()
        heartbeat("aios-verification-gate", revision=LOADED_REVISION)
        if now - last_check >= CHECK_INTERVAL:
            ts = datetime.now().strftime("%H:%M:%S")
            try:
                results = verify_completed_tasks(limit=20)
                if results:
                    passed = sum(1 for r in results if r.get("passed"))
                    print(f"  [{ts}] ✅ {passed}/{len(results)} passed")
                else:
                    print(f"  [{ts}] .")  # no new tasks to verify
            except Exception as e:
                print(f"  [{ts}] ❌ Error: {e}")
            last_check = now

        time.sleep(10)  # sleep short, check loop


def add_verify_endpoint_to_entry_gateway():
    """
    Verification Gate 入口网关扩展说明:
    在 aios_entry_gateway.py 中添加:
      POST /verify          {"task_id": "..."}  — 验证指定任务
      GET  /verify/status                       — 验证状态总览
    当前通过 CLI 方式调用: python3 aios_verification_gate.py --verify <task_id>
    """
    pass


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    elif "--verify" in sys.argv:
        idx = sys.argv.index("--verify")
        tid = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else ""
        if tid:
            verify_specific(tid)
        else:
            print("用法: aios_verification_gate.py --verify <task_id>")
    elif "--help" in sys.argv or "-h" in sys.argv:
        print("AIOS Verification Gate")
        print("  (no args)     Continuous verification daemon")
        print("  --once        Single scan and verify")
        print("  --verify <id> Verify specific task")
        print("  --help        This help")
    else:
        run_daemon()
