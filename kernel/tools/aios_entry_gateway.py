#!/usr/bin/env python3
"""
AIOS v4.0 Multi-Entry Unification Gateway
==========================================
统一入口：CLI / Feishu / Cron 等所有入口通过此网关
提交任务，经 Dispatcher 分解后通过 Capability Registry 路由到最佳执行器。

用法:
  python3 aios_entry_gateway.py               # 启动 HTTP 服务 (默认 :18801)
  python3 aios_entry_gateway.py "任务描述"     # CLI 模式直接提交
  python3 aios_entry_gateway.py --status       # 查看系统状态

HTTP API:
  POST /task            {"input":"...","source":"feishu|cli|cron|openclaw","sender":"..."}
                        → {"ok":true,"task_ids":[...],"count":N}
  GET  /task/<id>       查询任务状态
                         → {"ok":true,"task":{"task_id":"...","status":"..."}}
  POST /task/aggregate  {"task_ids":[...],"timeout":60}
                        → {"ok":true,"summary":{...}}
  GET  /health          健康检查 → {"ok":true,"redis":true,"status":"live"}
  GET  /status          系统状态总览 → {"queue":{...},"executors":[...],"recent":[...]}
"""

import sys
import os
import json
import hashlib
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from threading import Thread

from aios_http_server import BoundedThreadingHTTPServer, make_bounded_server

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import (call_pin, get_task_state, list_registered_executors,
                      get_queue_status, init_registry, publish_event,
                      check_recent, generate_task_id, get_system_status)
from aios_enforcer import protocol_check
from aios_contract_enforcer import check_all_rules, enforce as contract_enforce
from aios_verification_gate import verify_specific, verify_completed_tasks
from aios_tool_adapter import get_adapter
from aios_secure import (
    safe_bind_host, get_or_create_auth_token, verify_auth_token,
    safe_error_response, safe_log_exception, cors_origin,
    check_input_safety,
)
from aios_queue_admission import evaluate as _admission_evaluate

PORT = 18801
VERSION = "5.2.8"
LOADED_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# ============================================================================
#  [SECURITY] 2026-07-11 重构:
#    * 默认绑 127.0.0.1 (AIOS_ALLOW_PUBLIC_BIND=1 才允许公开)
#    * 可选认证 token (AIOS_AUTH_REQUIRED=1 才会强制要求 Header: X-AIOS-Token)
#    * CORS 不再默认 *; 需 AIOS_CORS_ALLOW_ALL=1
#    * 错误响应不再返回 traceback
# ============================================================================


def _json_response(handler, code: int, data: dict):
    """发送 JSON 响应 (安全版)."""
    body = json.dumps(data, ensure_ascii=False, default=str).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    co = cors_origin()
    if co:
        handler.send_header("Access-Control-Allow-Origin", co)
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-AIOS-Token")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _auth_gate(handler) -> bool:
    """如果开启了 AIOS_AUTH_REQUIRED, 校验请求 header 或 query 中的 token.

    失败返回 False (已写响应). 成功返回 True.
    """
    if os.environ.get("AIOS_AUTH_REQUIRED", "0") != "1":
        return True
    provided = handler.headers.get("X-AIOS-Token") or parse_qs(
        urlparse(handler.path).query).get("token", [None])[0]
    if not verify_auth_token(provided):
        _json_response(handler, 401, {"ok": False, "error": "unauthorized"})
        return False
    return True


def _read_body(handler) -> dict:
    """读取并解析请求体 JSON."""
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw.decode("utf-8", errors="replace")}


def _format_task_summary(task_state: dict) -> dict:
    """格式化任务状态为摘要."""
    if not task_state or task_state.get("status") == "unknown":
        return None
    return {
        "task_id": task_state.get("task_id", ""),
        "status": task_state.get("status", "unknown"),
        "executor": task_state.get("executor", ""),
        "source": task_state.get("source", ""),
        "priority": task_state.get("priority", ""),
        "logic_depth": task_state.get("logic_depth", ""),
        "result_summary": task_state.get("result_summary", "")[:32768],
        "ts_created": task_state.get("ts_created", ""),
        "ts_completed": task_state.get("ts_completed", ""),
    }

def _hydrate_recent_task(snapshot: dict) -> dict:
    """Combine history index fields with authoritative runtime task state."""
    merged = dict(snapshot or {})
    task_id = merged.get("task_id") or merged.get("_id")
    if task_id:
        state = get_task_state(task_id)
        if state and state.get("status") != "unknown":
            merged.update({
                key: value for key, value in state.items()
                if value not in (None, "")
            })
    # Keep historical fields useful after runtime-state expiry.
    merged.setdefault("result_summary", merged.get("summary", ""))
    merged.setdefault("ts_created", merged.get("ts_start", ""))
    merged.setdefault("ts_completed", merged.get("ts_complete", ""))
    return merged




def _collect_local_model_policy() -> dict:
    """Build the ``local_model_policy`` surface for the gateway /status
    payload (P9 close-out 20260725 §三 / §十二).

    Reports the operator-side guard state.  ``local_model_running`` is
    read off the OS without invoking any AIOS-internal ollama
    lifecycle; it tracks the AIOS-spawned ollama (we look for our own
    process description) so the operator's own ollama is not confused
    with the one AIOS used to spawn in earlier tasks.
    """
    import os as _os
    import subprocess as _sp
    user_approved = bool(_os.environ.get("AIOS_OLLAMA_USER_APPROVED_AT"))
    allowed = bool(int(_os.environ.get("AIOS_LOCAL_MODEL_INFERENCE_ALLOWED",
                                       "0") or "0") == 1)
    aios_internal_running = False
    try:
        out = _sp.check_output(
            ["pgrep", "-af", "ollama serve"], timeout=1.0, text=True,
        )
        for line in out.splitlines():
            if "AIOS_HOME=" in line:
                aios_internal_running = True
                break
    except Exception:
        aios_internal_running = False
    return {
        "activation_mode": "MANUAL_USER_APPROVAL_ONLY",
        "inference_allowed": bool(user_approved and allowed),
        "local_model_running": aios_internal_running,
        "calls_current": 0,
        "blocked_reason": "USER_APPROVAL_REQUIRED",
        "user_approval_recorded_at": _os.environ.get(
            "AIOS_OLLAMA_USER_APPROVED_AT", ""),
        "policy_allows_flag": _os.environ.get(
            "AIOS_LOCAL_MODEL_INFERENCE_ALLOWED", "0"),
        "task_scoped_approval_required": True,
        "routing_eligible": False,
        "fallback_allowed": False,
        "recovery_manager_allowed": False,
        "canary_allowed": False,
        "manual_task_only": True,
        "max_concurrency": 1,
    }



class EntryGatewayHandler(BaseHTTPRequestHandler):
    """Unified Entry Gateway HTTP Handler."""

    def log_message(self, fmt, *args):
        """结构化日志."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {fmt % args}")

    # --- CORS preflight ---
    def do_OPTIONS(self):
        self.send_response(204)
        co = cors_origin()
        if co:
            self.send_header("Access-Control-Allow-Origin", co)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-AIOS-Token")
            self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # --- GET ---
    def do_GET(self):
        if not _auth_gate(self):
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            if path == "/health":
                self._handle_health()
            elif path == "/status":
                self._handle_status()
            elif path.startswith("/task/"):
                task_id = path[len("/task/"):]
                self._handle_task_status(task_id)
            else:
                _json_response(self, 404, {"ok": False, "error": "not_found",
                                           "available": ["/health", "/status", "/task/<id>"]})
        except Exception as e:
            _json_response(self, 500, safe_error_response(e))

    # --- POST ---
    def do_POST(self):
        if not _auth_gate(self):
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            if path == "/task":
                self._handle_create_task()
            elif path == "/task/aggregate":
                self._handle_aggregate()
            elif path == "/approval":
                self._handle_approval()
            elif path == "/verify":
                self._handle_verify()
            else:
                _json_response(self, 404, {"ok": False, "error": "not_found"})
        except Exception as e:
            _json_response(self, 500, safe_error_response(e))

    # ==================== Handlers ====================

    def _handle_health(self):
        """健康检查: 检测 Redis 连通性和网关自身状态."""
        redis_ok = False
        try:
            import redis
            r = redis.Redis(host='localhost', port=6379, socket_connect_timeout=2)
            r.ping()
            redis_ok = True
        except Exception:
            pass

        _json_response(self, 200, {
            "ok": True,
            "service": "aios-entry-gateway",
            "version": VERSION,
            "status": "live",
            "redis": redis_ok,
            "loaded_revision": LOADED_REVISION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _handle_status(self):
        """系统状态总览: 队列 + 执行器 + 最近任务 + 服务."""
        queue_status = get_queue_status()
        executors = list_registered_executors()
        recent = [_hydrate_recent_task(item)
                  for item in check_recent(hours=24, limit=50)]

        # 检查网关服务状态
        services = {}
        for name, port, desc in [
            ("model-gateway", 9998, "LLM 代理网关"),
            ("entry-gateway", 18801, "统一入口网关"),
            ("control-center", 8080, "控制中心"),
            ("hermes-gateway", None, "Hermes 消息网关"),
            ("openclaw-gateway", 18789, "OpenClaw 任务网关"),
        ]:
            if port:
                try:
                    import socket
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1)
                    result = s.connect_ex(("127.0.0.1", port))
                    s.close()
                    services[desc] = "running" if result == 0 else "down"
                except Exception:
                    services[desc] = "unknown"
            else:
                # hermes-gateway — check by PID file
                pid_path = Path(os.environ.get("HOME", str(Path.home()))) / ".hermes" / "gateway.pid"
                services[desc] = "running" if pid_path.exists() else "unknown"

        # 注册的执行器摘要 (executors = Dict[name, capabilities])
        runtime_status = get_system_status()
        executor_summary = []
        for name, caps in executors.items():
            alive = bool(runtime_status.get(name, {}).get("alive", False))
            inference_ready = False
            if name == "minimax-official":
                # Task 014: HTTP-only provider — derive inference_ready
                # from the Redis-backed usage counter (cross-process) AND
                # the daemon's heartbeat. Healthy iff:
                #   - guard has been enabled (env var set in daemon process)
                #   - daemon heartbeated within last 5 min
                #   - at least one successful call recorded in Redis
                #   - call limit not yet hit
                try:
                    import redis as _r
                    _rcli = _r.Redis(host='127.0.0.1', port=6379, db=0,
                                     socket_connect_timeout=2)
                    _h = _rcli.hgetall("aios:minimax_official:hash") or {}
                    _d = {k.decode() if isinstance(k, bytes) else k:
                          int(v.decode() if isinstance(v, bytes) else v)
                          for k, v in (_h.items() if _h else [])}
                    _calls = _d.get("calls_used", 0)
                    _hb = _rcli.get("aios:bus:system:minimax-official:heartbeat")
                    inference_ready = bool(
                        _calls > 0
                        and _calls < 30
                        and _hb is not None
                    )
                except Exception:
                    inference_ready = False
            else:
                try:
                    inference_ready = bool(
                        get_adapter(name).health().get("fully_operational", False)
                    )
                except Exception:
                    inference_ready = False

            available = alive and inference_ready
            if available:
                effective_status = "running"
            elif alive:
                effective_status = "degraded"
            else:
                effective_status = "offline"
            executor_summary.append({
                "name": name,
                "available": available,
                "runtime_status": effective_status,
                "process_alive": alive,
                "inference_ready": inference_ready,
                "today_stats": runtime_status.get(name, {}).get("today_stats", {}),
                "expertise": caps.get("expertise", []),
                "is_orchestrator": caps.get("is_orchestrator", False),
                "max_concurrent": caps.get("max_concurrent", 0),
                "depth_levels": caps.get("depth_levels", []),
            })

        recent_terminal = [t for t in recent if t.get("status") in ("completed", "failed")]
        recent_completed = sum(1 for t in recent_terminal if t.get("status") == "completed")
        recent_failed = sum(1 for t in recent_terminal if t.get("status") == "failed")
        recent_rate = round(100.0 * recent_completed / len(recent_terminal), 1) if recent_terminal else None

        _json_response(self, 200, {
            "ok": True,
            "service": "aios-entry-gateway",
            "version": VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "queue": {
                "pending": queue_status.get("pending", 0),
                "locked": queue_status.get("locked", 0),
                "running": queue_status.get("running", 0),
                "completed": queue_status.get("completed", 0),
                "failed": queue_status.get("failed", 0),
                "total_active": sum(queue_status.get(k, 0) for k in
                                     ["pending", "locked", "running", "verifying"]),
            },
            "executors": {
                "count": len(executor_summary),
                "list": executor_summary,
            },
            "recent_tasks": [_format_task_summary(t) for t in recent if _format_task_summary(t)],
            "local_model_policy": _collect_local_model_policy(),
            "recent_tasks_summary": {"window_hours": 24, "total": len(recent_terminal), "completed": recent_completed,
                                     "failed": recent_failed, "success_rate": recent_rate},
            "services": services,
        })

    def _handle_task_status(self, task_id: str):
        """查询指定任务的状态."""
        # 限制 task_id 长度, 避免巨大输入拖累 server
        if not task_id or len(task_id) > 64 or not re.match(r'^[a-f0-9\-]{32,64}$', task_id):
            _json_response(self, 400, {"ok": False, "error": "invalid_task_id"})
            return

        state = get_task_state(task_id)
        summary = _format_task_summary(state)
        if summary is None:
            ok, workflow = call_pin("orchestrator.status", task_id)
            if ok and isinstance(workflow, dict) and workflow.get("status") != "unknown":
                summary = workflow

        if summary is None:
            _json_response(self, 404, {"ok": False, "error": "task_not_found", "task_id": task_id})
        else:
            _json_response(self, 200, {"ok": True, "task": summary})

    def _handle_create_task(self):
        """创建新任务: 接收输入 → 调度器分解 → 入队."""
        body = _read_body(self)

        user_input = body.get("input") or body.get("text") or body.get("message") or body.get("_raw")
        if not user_input or not user_input.strip():
            _json_response(self, 400, {"ok": False, "error": "missing_input",
                                       "hint": "Send JSON {\"input\": \"task description\"}"})
            return

        source = body.get("source", "cli")
        sender = body.get("sender") or body.get("sender_id") or "gateway"
        session_key = body.get("session_key", "")
        preferred_executor = body.get("preferred_executor", "")
        # Close-out 20260727: ``preferred_tool`` is the canonical
        # task-policy field name (see ``aios_task_routing_policy``).
        # Accept either spelling so the existing executor-flavoured
        # callers keep working alongside the policy-flavoured ones.
        if not preferred_executor:
            preferred_executor = body.get("preferred_tool", "")
        preferred_model_binding = body.get("preferred_model_binding", "")
        preferred_planner = body.get("preferred_planner", "")
        preferred_reviewer = body.get("preferred_reviewer", "")
        allow_planner_fallback = body.get("allow_planner_fallback", True)
        allow_reviewer_fallback = body.get("allow_reviewer_fallback", True)
        strict_tool = bool(body.get("strict_tool", False))
        strict_model = bool(body.get("strict_model", False))
        # If ``strict_tool`` is requested AND a preferred executor /
        # tool is pinned, push that pin down as ``strict_executor``
        # so the existing orchestrator strict-mode contract lights
        # up.  This is the only place where the boolean knob is
        # translated into a hard tool_id pin because
        # ``Orchestrator._enqueue_node`` only honours a string.
        if strict_tool and preferred_executor and not body.get("strict_executor", ""):
            body_for_submit = body
            body_for_submit["strict_executor"] = preferred_executor
        blocked_tools = body.get("blocked_tools", [])
        blocked_model_bindings = body.get("blocked_model_bindings", [])
        blocked_resources = body.get("blocked_resources", [])
        blocked_reviewer_tools = body.get("blocked_reviewer_tools", [])
        blocked_planner_tools = body.get("blocked_planner_tools", [])
        strict_executor = body.get("strict_executor", "")
        allow_executor_fallback = body.get("allow_executor_fallback", True)
        wait = body.get("wait", False)
        user_priority = body.get("priority")
        user_logic_depth = body.get("logic_depth")
        verification_criteria = body.get("verification_criteria", [])
        if not isinstance(verification_criteria, list) or len(verification_criteria) > 20:
            _json_response(self, 400, {"ok": False, "error": "invalid_verification_criteria"})
            return

        # Host Read-Only Evidence Boundary (final-production 2026-08-11).
        # The CLI passes a canonical profile (GENERAL/AUDIT/OPS/CODE) via
        # ``aios_stable_v1_policy.build_cli_payload``.  The Gateway
        # forwards the profile plus the optional ``--project`` path to
        # the Orchestrator so the host-evidence boundary is gathered
        # BEFORE the Codex sandbox dispatches.
        profile = str(body.get("profile", "") or "").strip().upper()
        if profile not in ("GENERAL", "AUDIT", "OPS", "CODE"):
            profile = "GENERAL"
        project_path = str(body.get("project_path", "") or "").strip()
        host_evidence_profile = str(
            body.get("host_evidence_profile", "") or profile
        ).strip().upper() or profile
        if host_evidence_profile not in ("GENERAL", "AUDIT", "OPS", "CODE"):
            host_evidence_profile = profile

        # 校验 source 合法性
        # P7F errata: the source whitelist is a production contract.
        # P7A temporarily introduced ``p7a-local`` as an audit token for
        # local-only end-to-end identity / session / result boundary
        # tests; that audit source must NOT remain in the production
        # valid_sources tuple because (a) it was never produced by any
        # external platform and (b) extending the production contract
        # for a single test set creates a privilege surface that
        # survives code review cycles. Real local audits MUST use one of
        # the existing official sources (``web`` or ``api``) below.
        # The whitelist is the entry layer's first line of defence and
        # must remain in sync with the ``valid_sources`` mirror list
        # below.
        # P9D-R final signoff (20260804) adds the bespoke source name
        # ``p9dr-final-signoff`` to the canonical whitelist so the
        # three documented end-to-end sign-off tasks (Primary,
        # Fallback, Recovery) can be submitted through the normal
        # HTTP path with a stable, auditable source string.
        valid_sources = ("feishu", "cli", "cron", "telegram", "web",
                         "api", "system", "test", "openclaw",
                         "acceptance", "p9dr-final-signoff")
        if source not in valid_sources:
            _json_response(self, 400, {"ok": False, "error": f"invalid_source: {source}",
                                       "valid_sources": list(valid_sources)})
            return

        # Close-out 20260727-§三: source-level Pending-queue admission
        # guard.  This is the *soft* gate that prevents acceptance /
        # test / pytest floods from piling up in the queue while real
        # user / API tasks flow through unchanged.  The guard returns a
        # 429 with the audit fields (source, current_pending,
        # configured_limit, decision) so the close-out reports can
        # cite the exact reason.  Real user / API / web / cron / etc.
        # tasks are never affected by this check.
        try:
            _admission = _admission_evaluate(source)
        except Exception as _e:
            _admission = {"ok": True, "decision": "ADMIT",
                          "reason": f"ADMISSION_GUARD_ERROR:{_e}",
                          "http_status": 200, "source": source,
                          "current_pending": 0, "test_pending": 0,
                          "configured_limit": 0, "timestamp": ""}
        if not _admission.get("ok", True):
            try:
                publish_event("queue.admission_rejected", {
                    "source": _admission.get("source"),
                    "current_pending": _admission.get("current_pending"),
                    "test_pending": _admission.get("test_pending"),
                    "configured_limit": _admission.get("configured_limit"),
                    "decision": _admission.get("decision"),
                    "reason": _admission.get("reason"),
                    "sender": sender,
                }, "entry_gateway")
            except Exception:
                pass
            _json_response(self, int(_admission.get("http_status") or 429), {
                "ok": False,
                "error": _admission.get("reason") or "TEST_BACKLOG_LIMIT_REACHED",
                "decision": _admission.get("decision"),
                "source": _admission.get("source"),
                "current_pending": _admission.get("current_pending"),
                "test_pending": _admission.get("test_pending"),
                "configured_limit": _admission.get("configured_limit"),
                "hint": "Real user / API / web / cron / feishu / telegram / openclaw "
                        "tasks are not subject to this limit. Drain the test-source "
                        "backlog (or raise AIOS_TEST_PENDING_LIMIT) before retrying.",
            })
            return

        # 初始化注册中心
        try:
            init_registry()
        except Exception:
            pass

        # Protocol Enforcer: 检查消息合法性
        ok, reason = protocol_check(user_input.strip(), sender)
        if not ok:
            _json_response(self, 403, {"ok": False, "error": f"protocol_violation: {reason}"})
            return

        # Contract Enforcer: 检查系统级规则 (如Token超限、连续失败熔断)
        try:
            violations = contract_enforce(check_all_rules())
            if violations.get("halted", 0) > 0:
                _json_response(self, 503, {"ok": False, "error": "system_halt",
                                           "detail": violations.get("actions", [])})
                return
        except Exception:
            pass  # 规则检查失败不阻塞任务

        # P8D audit-overlay: optional task-scoped capability overlay.
        # ``AIOS_ROUTING_AUDIT_OVERLAY_ENABLED=1`` keeps the legacy
        # sender-gated form (``source=api`` + ``sender in {p8d-audit,
        # p8c-f-audit}``).  Outside audit, callers can still pass a
        # ``blocked_model_bindings`` overlay for *controlled* tests
        # (acceptance / canary); the gateway treats that key as
        # inherently safe because it cannot mutate global state and
        # cannot promote the task beyond the routing-engine defaults.
        capability_overlay = None
        raw_overlay = body.get("capability_overlay")
        if isinstance(raw_overlay, dict):
            allowed_senders = {"p8d-audit", "p8c-f-audit",
                               "aios-canary"}
            audit_only = os.environ.get(
                "AIOS_ROUTING_AUDIT_OVERLAY_ENABLED", "0") == "1"
            if not audit_only and "blocked_model_bindings" in raw_overlay:
                # Blocked-binding overlays are safe to accept from
                # any controlled source because the routing engine
                # treats them as a per-task candidate filter, not a
                # registry mutation.
                blocked_raw = raw_overlay.get("blocked_model_bindings")
                if isinstance(blocked_raw, (list, tuple, set)):
                    blocked_clean = [str(x) for x in blocked_raw if str(x)]
                elif isinstance(blocked_raw, str):
                    blocked_clean = [item.strip() for item
                                      in blocked_raw.split(",")
                                      if item.strip()]
                else:
                    blocked_clean = []
                capability_overlay = {"blocked_model_bindings": blocked_clean}
            elif str(source) == "api" and str(sender) in allowed_senders:
                capability_overlay = {
                    str(k): str(v) for k, v in raw_overlay.items()
                    if isinstance(v, str)
                }

        # 调度（通过 Pin 调用 OpenClaw，不直接 import）
        try:
            _kwargs = {"source": source, "sender_id": sender, "session_key": session_key,
                       "verification_criteria": verification_criteria,
                       "profile": profile,
                       "project_path": project_path,
                       "host_evidence_profile": host_evidence_profile}
            if preferred_executor:
                _kwargs["preferred_executor"] = preferred_executor
            # Forward strict_executor / allow_executor_fallback /
            # allow_tool_fallback / allow_model_fallback so the
            # orchestrator can decide between hard-pin vs. soft-prefer.
            if strict_executor:
                _kwargs["strict_executor"] = strict_executor
            if isinstance(allow_executor_fallback, bool):
                _kwargs["allow_executor_fallback"] = allow_executor_fallback
            if user_priority is not None:
                _kwargs["user_priority"] = user_priority
            if user_logic_depth:
                _kwargs["user_logic_depth"] = user_logic_depth
            if capability_overlay:
                _kwargs["capability_overlay"] = capability_overlay
            if preferred_model_binding:
                _kwargs["preferred_model_binding"] = preferred_model_binding
            if preferred_planner:
                _kwargs["preferred_planner"] = preferred_planner
            if preferred_reviewer:
                _kwargs["preferred_reviewer"] = preferred_reviewer
            for key, value in (("allow_planner_fallback", allow_planner_fallback),
                                ("allow_reviewer_fallback", allow_reviewer_fallback)):
                if isinstance(value, bool):
                    _kwargs[key] = value
            if strict_tool:
                _kwargs["strict_tool"] = True
            if strict_model:
                _kwargs["strict_model"] = True
            for key, value in (("blocked_tools", blocked_tools),
                                ("blocked_model_bindings", blocked_model_bindings),
                                ("blocked_resources", blocked_resources),
                                ("blocked_reviewer_tools", blocked_reviewer_tools),
                                ("blocked_planner_tools", blocked_planner_tools)):
                if isinstance(value, list) and value:
                    _kwargs[key] = list(value)
            ok, result = call_pin("orchestrator.submit", user_input.strip(), **_kwargs)
            if not ok:
                _json_response(self, 500, {"ok": False, "error": result,
                                           "hint": "AIOS Orchestrator submission failed."})
                return
            task_ids = result if isinstance(result, list) else result.get("task_ids", [])
            parent_id = result.get("parent_id", "") if isinstance(result, dict) else ""
            approval_required = bool(result.get("approval_required")) if isinstance(result, dict) else False
            approval_id = str(result.get("approval_id", "")) if isinstance(result, dict) else ""
            risk_action = str(result.get("risk_action", "")) if isinstance(result, dict) else ""
            if not task_ids:
                _json_response(self, 500, {"ok": False, "error": "dispatch_failed",
                                           "hint": "Orchestrator returned no executable nodes."})
                return

            # wait=true: 同步等待所有子任务完成
            if wait and parent_id and not approval_required:
                try:
                    ok2, _summary = call_pin("orchestrator.wait", parent_id,
                                             timeout_seconds=int(body.get("timeout", 300)))
                    _json_response(self, 200, {"ok": True, "task_ids": task_ids, "parent_id": parent_id,
                                               "wait_result": _summary})
                    return
                except Exception as _e:
                    _json_response(self, 200, {"ok": True, "task_ids": task_ids, "parent_id": parent_id,
                                               "wait_error": str(_e)})
                    return

            try:
                publish_event("task.created", {
                    "count": len(task_ids),
                    "source": source,
                    "sender": sender,
                    "task_ids": task_ids,
                }, "entry_gateway")
            except Exception:
                pass

            # 同步等待模式: wait=true → 自动汇聚结果
            wait = body.get("wait", False)
            if wait and not approval_required:
                timeout = int(body.get("timeout", 120))
                ok2, summary = call_pin("openclaw.aggregate", task_ids, timeout_seconds=timeout, parent_id=parent_id)
                _json_response(self, 200, {
                    "ok": True,
                    "task_ids": task_ids,
                    "parent_id": parent_id,
                    "summary": summary if ok2 else {"error": str(summary)},
                    "mode": "sync",
                })
                return

            _json_response(self, 200, {
                "ok": True,
                "task_ids": task_ids,
                "count": len(task_ids),
                "source": source,
                "parent_id": parent_id or (task_ids[0] if len(task_ids) == 1 else None),
                "status": "awaiting_approval" if approval_required else "planning",
                "approval_required": approval_required,
                "approval_id": approval_id,
                "risk_action": risk_action,
                "hint": f"Use GET /task/<id> to check status, "
                        f"POST /task/aggregate to collect results",
            })
        except Exception as e:
            _json_response(self, 500, safe_error_response(e, public_detail="dispatch_error"))

    def _handle_approval(self):
        """Approve and resume one authenticated L4 parent workflow."""
        body = _read_body(self)
        parent_id = str(body.get("parent_id", "") or "")
        approval_id = str(body.get("approval_id", "") or "")
        approver = str(body.get("approver", "") or "")
        note = str(body.get("note", "") or "")
        uuid_re = r"^[a-f0-9-]{36}$"
        if not re.match(uuid_re, parent_id) or not re.match(uuid_re, approval_id):
            _json_response(self, 400, {"ok": False, "error": "invalid_approval_reference"})
            return
        if not approver.strip():
            _json_response(self, 400, {"ok": False, "error": "approver_required"})
            return
        ok, result = call_pin(
            "orchestrator.approve", parent_id, approval_id,
            approver=approver, note=note,
        )
        if not ok:
            _json_response(self, 500, {"ok": False, "error": str(result)})
            return
        code = 200 if result.get("ok") else 409
        _json_response(self, code, result)

    def _handle_aggregate(self):
        """汇聚子任务结果."""
        body = _read_body(self)
        task_ids = body.get("task_ids", [])
        timeout = body.get("timeout", 60)

        if not task_ids or not isinstance(task_ids, list):
            _json_response(self, 400, {"ok": False, "error": "missing_task_ids",
                                       "hint": "Send JSON {\"task_ids\": [\"id1\", \"id2\"]}"})
            return
        if len(task_ids) > 200:
            _json_response(self, 400, {"ok": False, "error": "too_many_task_ids",
                                       "limit": 200})
            return
        # 拒绝任何非 UUID 风格 ID, 减少注入面
        for tid in task_ids:
            if not isinstance(tid, str) or not re.match(r'^[a-f0-9\-]{32,64}$', tid):
                _json_response(self, 400, {"ok": False, "error": "invalid_task_id_in_list"})
                return

        try:
            ok, result = call_pin("openclaw.aggregate", task_ids, timeout_seconds=int(timeout))
            summary = result if ok else {"error": str(result)}
            _json_response(self, 200, {"ok": True, "summary": summary})
        except Exception as e:
            _json_response(self, 500, safe_error_response(e, public_detail="aggregate_error"))

    def _handle_verify(self):
        """验证门禁: 验证指定任务或扫描未经验证的任务."""
        body = _read_body(self)
        task_id = body.get("task_id", "")

        if task_id:
            # 验证指定任务
            result = verify_specific(task_id)
            _json_response(self, 200, {"ok": True, "verify": result})
        else:
            # 扫描未经验证的任务
            results = verify_completed_tasks(limit=20)
            passed = sum(1 for r in results if r.get("passed"))
            _json_response(self, 200, {
                "ok": True, "verify": {
                    "total": len(results),
                    "passed": passed,
                    "failed": len(results) - passed,
                    "results": results,
                }
            })


def start_server(host=None, port=PORT, daemon=False):
    """启动 Entry Gateway HTTP 服务 (默认绑 127.0.0.1).

    如需公开接受连接, 请设置环境变量 AIOS_ALLOW_PUBLIC_BIND=1 或传 host='0.0.0.0'.

    P9A fix: the underlying socket uses a BoundedThreadingHTTPServer so
    that one slow handler (e.g. ``_handle_aggregate`` waiting for a
    parent workflow, ``_handle_status`` reading redis snapshots, or
    ``_handle_verify`` running the verifier) cannot pin the entire
    gateway and starve ``/health`` / parallel POSTs.  The default
    cap is 32 in-flight handlers; it can be tuned via
    ``AIOS_GATEWAY_MAX_HANDLERS``.
    """
    if host is None:
        host = safe_bind_host()
    server = BoundedThreadingHTTPServer((host, port), EntryGatewayHandler)
    if os.environ.get("AIOS_AUTH_REQUIRED") == "1":
        print("  🔒 认证要求: ON. Token 已生成在 ~/.aios_auth_token "
              "(同进程启动一次; 调用方需 Header: X-AIOS-Token: <token>)")
    print(f"  🔐 CORS: {'* (允许所有)' if cors_origin() == '*' else '默认拒绝 (需要 AIOS_CORS_ALLOW_ALL=1)'}")

    if daemon:
        t = Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server, t

    print(f"\n{'='*50}")
    print(f"  AIOS Entry Gateway v{VERSION}")
    print(f"  Listen: http://{host}:{port}")
    print(f"  API:")
    print(f"    POST /task           创建任务")
    print(f"    GET  /task/<id>      查询状态")
    print(f"    POST /task/aggregate 汇聚结果")
    print(f"    GET  /health         健康检查")
    print(f"    GET  /status         系统状态")
    print(f"{'='*50}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


def cli_mode():
    """CLI 模式: 直接提交任务并输出结果."""
    args = sys.argv[1:]

    if not args or args[0] in ("--help", "-h"):
        print("AIOS Entry Gateway v" + VERSION)
        print()
        print("用法:")
        print("  aios-entry-gateway                    启动 HTTP 服务")
        print("  aios-entry-gateway \"任务描述\"          提交任务 (CLI 模式)")
        print("  aios-entry-gateway --status            查看系统状态")
        print("  aios-entry-gateway --health            健康检查")
        print("  aios-entry-gateway --help              帮助")
        print()
        print("来源参数:")
        print("  aios-entry-gateway --source=feishu \"任务\"  指定来源")
        print("  aios-entry-gateway --sender=user123 \"任务\" 指定发送者")
        return

    if args[0] == "--status":
        # 显示系统状态
        try:
            init_registry()
        except Exception:
            pass

        print()
        print("=" * 50)
        print("  AIOS 系统状态")
        print("=" * 50)

        # 队列
        qs = get_queue_status()
        print(f"\n  📊 任务队列:")
        for k, v in qs.items():
            print(f"    {k}: {v}")

        # 执行器 (list_registered_executors returns Dict[name, capabilities])
        executors = list_registered_executors()
        print(f"\n  🤖 注册执行器 ({len(executors)}):")
        for name, caps in executors.items():
            expertise = caps.get("expertise", [])
            orch = " 🎯 Orchestrator" if caps.get("is_orchestrator") else ""
            print(f"    • {name}{orch}")
            if expertise:
                print(f"      专长: {', '.join(expertise[:3])}")

        # 最近任务
        recent = check_recent(hours=24, limit=50)
        if recent:
            print(f"\n  📋 最近任务 ({len(recent)}):")
            for t in recent:
                status_icon = {"completed": "✅", "failed": "❌", "pending": "⏳"}.get(
                    t.get("status", ""), "❓")
                print(f"    {status_icon} {t.get('task_name','?')[:50]} → {t.get('status','?')}")
        return

    if args[0] == "--health":
        try:
            import redis as _r
            r = _r.Redis(host='localhost', port=6379, socket_connect_timeout=2)
            r.ping()
            redis_ok = True
        except Exception:
            redis_ok = False
        print(json.dumps({
            "ok": True,
            "service": "aios-entry-gateway",
            "version": VERSION,
            "redis": redis_ok,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2))
        return

    # 提任务模式
    user_input = " ".join(args)
    source = "cli"
    sender = "local"

    for arg in args:
        if arg.startswith("--source="):
            source = arg.split("=", 1)[1]
        if arg.startswith("--sender="):
            sender = arg.split("=", 1)[1]

    # Strip the --flags from the input
    clean_input = " ".join(a for a in args if not a.startswith("--"))

    try:
        init_registry()
        ok, result = call_pin("openclaw.dispatch", clean_input, source=source, sender_id=sender)
        task_ids = result if isinstance(result, list) else (result.get("task_ids", []) if ok else [])
        parent_id = result.get("parent_id", "") if isinstance(result, dict) else ""
        if task_ids:
            resp = {"ok": True, "task_ids": task_ids, "count": len(task_ids)}
            if parent_id:
                resp["parent_id"] = parent_id
            print(json.dumps(resp, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"ok": False, "error": "dispatch_failed"}, ensure_ascii=False, indent=2))
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and not sys.argv[1].startswith("--"):
        cli_mode()
    elif len(sys.argv) >= 2 and sys.argv[1] in ("--status", "--health", "--help", "-h"):
        cli_mode()
    else:
        start_server()
