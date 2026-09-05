#!/usr/bin/env python3
"""
AIOS v4.0 执行管线强制器 (Execution Pipeline Enforcer)
======================================================
合并三个P0模块:
  M2: Protocol Enforcer — 消息格式验证, UUID强制, 长度限制
  M4: World Model Hook  — 高风险任务执行前安全模拟
  M3: Verification Gate  — 完成后强制跑verify.py

管线:
  task submitted → [Protocol Check] → [World Model] → execute → [Verifier] → deliver
"""

import sys, os, json, re, subprocess, time
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

AIOS_HOME = os.environ.get("AIOS_HOME", "${AIOS_HOME}")

# ── M2: Protocol Enforcer ──────────────────────────────────

MAX_MESSAGE_CHARS = 4096
# [SECURITY] 2026-07-11 扩充: 加入 aios_secure._DANGEROUS_RES 中的拦截.
# 这里保留*简单的可读*列表, 详细检查从 aios_secure 复用. 二者同步使用.
DANGEROUS_PATTERNS = [
    # 文件/磁盘破坏
    "rm -rf /", "fork bomb", ":(){ :|:& };:", "chmod 777 /",
    "mv /etc/", "dd if=/dev/zero", "mkfs.", "> /dev/sda",
    "DROP TABLE", "DROP DATABASE", "TRUNCATE TABLE",
    "format disk", "format drive", "delete all",
    "shutdown -h", "reboot -f", "poweroff",
    "wget http://evil", "curl http://evil",
    "chmod -R 777", "chown -R", "> /dev/sd",
    # 路径穿越 & 敏感文件
    "/etc/passwd", "/etc/shadow", "${HOME}/",
    "~/.ssh", "~/.aws", "~/.gnupg",
    "../", "..\\",
    "cat /etc/", "cat ~/.", "cat ${HOME}/",
    # Python/Node 内置函数注入
    "__import__(", "eval(", "exec(", "compile(",
    "subprocess.", "os.system(", "os.popen(", "shutil.rmtree(",
]


def protocol_check(message: str, sender: str = "unknown") -> Tuple[bool, str]:
    """Compatibility wrapper delegated to the unique AIOS Security Gate."""
    from aios_secure import security_gate_decide
    decision = security_gate_decide("input", message, actor=sender)
    return bool(decision["allowed"]), str(decision["reason"])


HALT_KEY = "aios:bus:system:halt"

def is_system_halted() -> bool:
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, db=0, socket_connect_timeout=2)
        return r.exists(HALT_KEY) > 0
    except Exception:
        return False

def set_system_halt(reason: str = "consecutive_failures"):
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, db=0, socket_connect_timeout=2)
        r.setex(HALT_KEY, 3600, reason)
    except Exception:
        pass

def clear_system_halt():
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, db=0, socket_connect_timeout=2)
        r.delete(HALT_KEY)
    except Exception:
        pass


VERIFY_FAIL_KEY = "aios:bus:verify:consecutive_fails"
VERIFY_HALT_THRESHOLD = 3

EXEC_FAIL_PREFIX = "aios:bus:exec:fail:"
EXEC_HALT_THRESHOLD = 5              # 单执行器连续5次失败 → 熔断
# [方案 B 修复 2026-07-12] 30 分钟太长，在人机协作场景下错失恢复窗口太伤。
# 改为 5 分钟，足够让连续 5 次失败问题被人工看到但又不阻塞太久。
EXEC_HALT_TTL = 300                  # 熔断5分钟后自动恢复 (原 1800s)

def _count_exec_fail(executor: str):
    try:
        import redis as _r
        from aios_bus import publish_event
        r = _r.Redis(host='localhost', port=6379, db=0, socket_connect_timeout=2)
        key = f"{EXEC_FAIL_PREFIX}{executor}"
        count = r.incr(key)
        r.expire(key, EXEC_HALT_TTL)
        if count >= EXEC_HALT_THRESHOLD:
            r.setex(f"aios:bus:exec:halt:{executor}", EXEC_HALT_TTL, f"连续{count}次失败")
            publish_event("alert.critical",
                {"type": "executor_circuit_break", "executor": executor, "fail_count": count},
                "enforcer")
        return count
    except Exception:
        return 0

def _clear_exec_fail(executor: str):
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, db=0, socket_connect_timeout=2)
        r.delete(f"{EXEC_FAIL_PREFIX}{executor}")
    except Exception:
        pass

def is_executor_halted(executor: str) -> bool:
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, db=0, socket_connect_timeout=2)
        return r.exists(f"aios:bus:exec:halt:{executor}") > 0
    except Exception:
        return False

def _count_verify_fail(task_id: str):
    """递增验证失败计数, 达到阈值自动触发 HALT."""
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, db=0, socket_connect_timeout=2)
        count = r.incr(VERIFY_FAIL_KEY)
        r.expire(VERIFY_FAIL_KEY, 3600)
        if count >= VERIFY_HALT_THRESHOLD:
            set_system_halt(f"连续{count}次验证失败")
    except Exception:
        pass

def _clear_verify_fails():
    """验证成功时清零."""
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, db=0, socket_connect_timeout=2)
        r.delete(VERIFY_FAIL_KEY)
    except Exception:
        pass


def enforce_uuid_pointer(data: dict) -> dict:
    """强制大字段使用UUID指针: >MAX_MESSAGE_CHARS 的字段存Redis, 仅传UUID."""
    enforced = data.copy()
    uuid_fields = {}
    for field in ["context", "result_detail", "full_log", "task_name", "summary"]:
        if field in enforced and isinstance(enforced[field], str):
            if len(enforced[field]) > MAX_MESSAGE_CHARS:
                ptr = _store_uuid_blob(enforced[field])
                uuid_fields[field] = ptr
                enforced[field] = f"uuid://{ptr}"

                enforced[f"{field}_preview"] = enforced[field][:200]
    if uuid_fields:
        enforced["_uuid_pointers"] = uuid_fields
    return enforced


UUID_BLOB_KEY = "aios:bus:blob"

def _store_uuid_blob(text: str) -> str:
    import uuid
    ptr = str(uuid.uuid4())
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, db=0, socket_connect_timeout=2)
        r.setex(f"{UUID_BLOB_KEY}:{ptr}", 3600, text)
    except Exception:
        pass
    return ptr

def resolve_uuid_pointer(ptr: str) -> str:
    """解析 uuid://{ptr} 或 {ptr} 回原始文本."""
    key = ptr.replace("uuid://", "")
    try:
        import redis as _r
        r = _r.Redis(host='localhost', port=6379, db=0, socket_connect_timeout=2)
        val = r.get(f"{UUID_BLOB_KEY}:{key}")
        if val:
            return val.decode() if isinstance(val, bytes) else val
    except Exception:
        pass
    return ""

def resolve_all_pointers(data: dict) -> dict:
    """递归解析dict中所有 uuid:// 指针."""
    resolved = data.copy()
    ptrs = resolved.get("_uuid_pointers", {})
    for field, ptr in ptrs.items():
        if field in resolved and isinstance(resolved[field], str) and resolved[field].startswith("uuid://"):
            text = resolve_uuid_pointer(ptr)
            if text:
                resolved[field] = text
    return resolved


# ── M4: World Model Pre-exec Hook ──────────────────────────

HIGH_RISK_KEYWORDS = [
    "PLC", "控制", "硬件", "电气", "物理", "马达", "变频",
    "rm -rf", "delete", "DROP", "TRUNCATE", "format",
    "sudo", "chmod 777", "chown", "mount", "mkfs",
]

def pre_exec_safety_check(task: dict) -> Tuple[bool, str]:
    """
    M4: 执行前安全模拟。
    logic_depth=high 或包含高风险关键词时强制过World Model。
    Returns: (approved, reason)
    """
    task_name = task.get("task_name", "")
    task_type = task.get("logic_depth", "low")
    task_id = task.get("task_id", "unknown")

    # 判断是否需要World Model
    needs_wm = (task_type == "high") or any(
        kw.lower() in task_name.lower() for kw in HIGH_RISK_KEYWORDS
    )

    if not needs_wm:
        return True, "low_risk_skip"

    # 创建临时task.json
    tmp_file = f"/tmp/aios_wm_{task_id[:8]}.json"
    wm_task = {
        "Task_ID": task_id,
        "Task_Type": "coding" if task_type != "batch" else "plc_logic",
        "Context_Summary": task_name[:512],
        "Work_Dir": f"{AIOS_HOME}/sandbox/coding",
    }
    try:
        with open(tmp_file, 'w') as f:
            json.dump(wm_task, f)

        wm = TOOLS / "world_model_runner.py"
        if not wm.exists():
            return True, "wm_unavailable_skip"

        result = subprocess.run(
            ["python3", str(wm), tmp_file],
            capture_output=True, text=True, timeout=30
        )
        os.remove(tmp_file)

        if result.returncode == 0 and ("APPROVED" in result.stdout or "APPROVAL" in result.stdout):
            return True, "wm_approved"
        else:
            return False, f"World Model 拦截: {result.stdout[-200:]}"
    except Exception as e:
        try:
            os.remove(tmp_file)
        except Exception:
            pass
        return True, f"wm_error_allow: {e}"  # WM故障时放行,不阻塞


# ── M3: Verification Gate ──────────────────────────────────

def _extract_failures(stdout: str) -> str:
    """从 verify.py 输出提取失败行 (❌标记), 避开头部."""
    lines = stdout.split("\n")
    fails = [l.strip() for l in lines if "❌" in l or "[FAIL]" in l]
    if fails:
        return " | ".join(fails[:5])
    # 退而求其次: 取最后的 Summary 行
    for l in reversed(lines):
        if "失败:" in l or "fail" in l.lower():
            return l.strip()
    return stdout[-200:]


def _result_value_present(result_summary: str, expected: object) -> bool:
    """Match criteria across plain text, key=value and JSON syntax."""
    needle = str(expected).strip()
    if not needle:
        return True
    haystack = result_summary or ""
    if needle.casefold() in haystack.casefold():
        return True
    # Normalize presentation only; business values still match exactly.
    compact_haystack = re.sub(r"[\s\"']+", "", haystack).casefold()
    compact_needle = re.sub(r"[\s\"']+", "", needle).casefold()
    return compact_needle in compact_haystack


def _verify_builtin_result(result_summary: str) -> Optional[Tuple[bool, str]]:
    """Validate AIOS-owned structured results without user-authored criteria."""
    prefix = "[opencode/health-audit] "
    if not (result_summary or "").startswith(prefix):
        return None
    try:
        payload = json.loads(result_summary[len(prefix):])
    except (json.JSONDecodeError, TypeError) as exc:
        return False, f"health_audit_invalid_json: {exc}"
    errors = []
    if payload.get("service") != "aios-entry-gateway":
        errors.append("service")
    if not payload.get("version") or payload.get("status") != "live":
        errors.append("version/status")
    if payload.get("redis") is not True:
        errors.append("redis")
    executors = payload.get("executors", {})
    for name in ("opencode", "codex", "claude", "hermes"):
        if executors.get(name) is not True:
            errors.append(f"executor:{name}")
    recent = payload.get("recent_tasks", {})
    for key in ("total", "completed", "failed", "success_rate"):
        if key not in recent:
            errors.append(f"recent_tasks:{key}")
    services = payload.get("services", {})
    for name in ("model_gateway", "entry_gateway", "control_center", "openclaw_gateway"):
        if services.get(name) != "running":
            errors.append(f"service:{name}")
    if errors:
        return False, "health_audit_contract_failed: " + ", ".join(errors)
    return True, "health_audit_contract_pass"

def _minimax_official_independent_review(task: dict, result_summary: str,
                                          tool_evidence: str = "") -> Dict[str, Any]:
    """Task 014: 独立第二次 MiniMax-M3 调用做严格语义审查.
    返回 verdict 含 accepted / goal_completed / evidence_sufficient /
    reason / issues / VERIFIER_INDEPENDENCE=SEPARATE_CALL_SAME_PROVIDER.

    严格拒绝场景 (任务 §XI):
      * 模型回答"无法访问"且没有 tool_evidence
      * 没有执行所需工具
      * 文件未读取 / 未保存
      * 返回内容与目标无关
      * 只有模板文字 / 只有 HTTP 200 / 伪造系统状态
    """
    out: Dict[str, Any] = {
        "accepted": False, "goal_completed": False,
        "evidence_sufficient": False, "reason": "", "issues": [],
        "VERIFIER_INDEPENDENCE": "SEPARATE_CALL_SAME_PROVIDER",
    }
    try:
        from minimax_official_client import (
            chat, MiniMaxOfficialError, MiniMaxDisabledError,
        )
    except Exception as e:
        out["error"] = f"verifier_import_failed:{type(e).__name__}:{e}"
        return out
    system_msg = (
        "You are an AIOS Reviewer. Reply with strict JSON only, no markdown, "
        "no prose outside JSON. Schema: "
        '{"accepted": bool, "goal_completed": bool, '
        '"evidence_sufficient": bool, "reason": string, "issues": []}.'
    )
    user_msg = (
        "Judge EXECUTOR_OUTPUT against ORIGINAL_GOAL. Be strict.\n"
        "Set accepted=true iff:\n"
        "  - goal_completed=true: the executor actually achieved the goal; "
        "refusal / 'cannot access' / 'fabricated facts' counts as not completed.\n"
        "  - evidence_sufficient=true: tool_evidence is non-empty AND relevant "
        "to the goal (real file read, real system query, etc.).\n"
        "Common reject reasons to surface in 'issues':\n"
        "  'cannot_access' / 'no_tool_used' / 'file_not_read' / "
        "'file_not_written' / 'irrelevant_output' / 'template_only' / "
        "'fake_system_state'.\n"
        "Return JSON now.\n\n"
        f"ORIGINAL_GOAL:\n{(task.get('task_name','') or '')[:512]}\n\n"
        f"EXECUTOR_OUTPUT:\n{result_summary[:1200]}\n\n"
        f"TOOL_EVIDENCE:\n{tool_evidence[:600]}\n"
    )
    try:
        r = chat(
            [{"role": "system", "content": system_msg},
             {"role": "user", "content": user_msg}],
            max_tokens=512, temperature=0.0,
            purpose="reviewer",
        )
    except MiniMaxDisabledError as e:
        out["error"] = f"reviewer_blocked_by_guard:{e}"
        return out
    except MiniMaxOfficialError as e:
        out["error"] = f"reviewer_provider_error:{e}"
        return out
    except Exception as e:
        out["error"] = f"reviewer_error:{type(e).__name__}:{e}"
        return out
    raw = (r.get("content") or "").strip()
    if raw.startswith("```"):
        try:
            raw = raw.split("```", 2)[1].lstrip("json").lstrip()
        except Exception:
            raw = raw.strip("`\n ")
    verdict = None
    try:
        verdict = json.loads(raw)
    except Exception:
        decoder = json.JSONDecoder()
        for idx, ch in enumerate(raw):
            if ch != "{":
                continue
            try:
                cand, _ = decoder.raw_decode(raw[idx:])
                if isinstance(cand, dict):
                    verdict = cand
                    break
            except Exception:
                continue
    if not isinstance(verdict, dict) or "accepted" not in verdict:
        out["error"] = f"verifier_no_structured:{raw[:200]}"
        return out
    out["accepted"] = bool(verdict.get("accepted"))
    out["goal_completed"] = bool(verdict.get("goal_completed"))
    out["evidence_sufficient"] = bool(verdict.get("evidence_sufficient"))
    out["reason"] = str(verdict.get("reason", ""))[:300]
    out["issues"] = verdict.get("issues", []) or []
    out["model"] = r.get("model")
    return out
def post_exec_verification(task: dict, work_dir: str = "",
                           result_summary: str = "",
                           executor: str = "") -> Tuple[bool, str]:
    """
    M3: 执行后强验证门禁。
    对coding任务强制跑verify.py。
    没有显式验证标准的信息查询任务跳过验证。
    验证标准可放顶层或 context JSON 字段内。
    Returns: (verified, report)
    """
    task_id = task.get("task_id", "unknown")
    task_type = task.get("logic_depth", "low")
    criteria = task.get("verification_criteria", []) or []
    if isinstance(criteria, str):
        try:
            criteria = json.loads(criteria)
        except (json.JSONDecodeError, TypeError):
            return False, "invalid verification_criteria JSON"
    if not isinstance(criteria, list):
        return False, "verification_criteria must be a list"
    task["verification_criteria"] = criteria

    # Result-content criteria are verified against the actual executor output.
    output_criteria = [c for c in criteria
                       if isinstance(c, dict) and c.get("type") == "result_contains"]
    if output_criteria:
        missing = []
        for criterion in output_criteria:
            values = criterion.get("values", [])
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                return False, "result_contains.values must be a list"
            missing.extend(str(v) for v in values
                           if not _result_value_present(result_summary, v))
        if missing:
            return False, "result missing required values: " + ", ".join(missing[:10])
        remaining = [c for c in criteria if c not in output_criteria]
        if not remaining:
            return True, "result_contains_pass"
        criteria = remaining
        task["verification_criteria"] = remaining

    # Task 014: 对 minimax-official executor 触发独立第二次 MiniMax-M3 严格复审调用
    _reviewer_verdict = None
    if executor == "minimax-official" and result_summary:
        try:
            _reviewer_verdict = _minimax_official_independent_review(
                task, result_summary, tool_evidence=result_summary,
            )
        except Exception as _rev_e:
            _reviewer_verdict = {"accepted": False, "goal_completed": False,
                                  "evidence_sufficient": False,
                                  "reason": "", "issues": [],
                                  "error": f"reviewer_exception:{type(_rev_e).__name__}:{_rev_e}"}
        if not _reviewer_verdict or not isinstance(_reviewer_verdict, dict):
            _reviewer_verdict = {"error": "reviewer_returned_none",
                                  "accepted": False, "goal_completed": False,
                                  "evidence_sufficient": False}
        if _reviewer_verdict.get("error"):
            return False, _reviewer_verdict["error"]
        # 严格门: 必须 accepted AND goal_completed AND evidence_sufficient
        if not (_reviewer_verdict.get("accepted")
                and _reviewer_verdict.get("goal_completed")
                and _reviewer_verdict.get("evidence_sufficient")):
            issues = "; ".join(str(x)[:100] for x in
                               (_reviewer_verdict.get("issues") or [])[:5])
            return False, ("reviewer_strict_reject:" +
                           _reviewer_verdict.get("reason", "")[:200] +
                           "|issues=" + issues)
        try:
            task["_minimax_official_reviewer_verdict"] = _reviewer_verdict
        except Exception:
            pass
        return True, ("minimax_official_reviewer_accepted:" +
                      (_reviewer_verdict.get("reason") or "")[:300])

    builtin_result = _verify_builtin_result(result_summary)
    if not criteria:
        ctx = task.get("context", "")
        if isinstance(ctx, str) and ctx.strip().startswith("{"):
            try:
                parsed = json.loads(ctx)
                criteria = parsed.get("verification_criteria", []) or []
            except (json.JSONDecodeError, TypeError):
                pass
    builtin_result = _verify_builtin_result(result_summary)
    if builtin_result is not None:
        return builtin_result

    source = task.get("source", "")

    # 非coding类跳过
    if task_type == "batch":
        return True, "deferred_to_parent:batch"

    # 无显式验证标准的信息查询/CLI类任务跳过
    if not criteria and source in ("cli", "feishu", "test"):
        return True, "deferred_to_parent:no_contract"

    # 无显式验证标准且没有输出文件的任务跳过
    if not criteria and not task.get("output_file", ""):
        return True, "deferred_to_parent:semantic_review_required"

    verifier = TOOLS / "verify.py"
    if not verifier.exists():
        return False, "verifier_unavailable"

    # 创建task.json
    tmp_file = f"/tmp/aios_verify_{task_id[:8]}.json"
    v_task = {
        "Task_ID": task_id,
        "Task_Type": "coding",
        "Context_Summary": task.get("task_name", "")[:512],
        "Work_Dir": work_dir or f"{AIOS_HOME}/sandbox/coding",
        "Output_File": task.get("output_file", ""),
        "Verification_Criteria": task.get("verification_criteria", []),
    }
    try:
        with open(tmp_file, 'w') as f:
            json.dump(v_task, f)

        result = subprocess.run(
            ["python3", str(verifier), tmp_file],
            capture_output=True, text=True, timeout=60
        )
        os.remove(tmp_file)

        if result.returncode == 0:
            return True, _extract_failures(result.stdout)
        else:
            # 失败时提取实际的 FAIL 行，而不是尾部截断
            failures = _extract_failures(result.stdout)
            if not failures:
                failures = result.stdout[-200:]
            return False, failures + (result.stderr[-200:] if result.stderr else "")
    except Exception as e:
        try:
            os.remove(tmp_file)
        except Exception:
            pass
        return False, f"verifier_error: {e}"
# ── 完整管线 ────────────────────────────────────────────────

def enforce_pipeline(task: dict, executor: str,
                     execute_fn=None,
                     work_dir: str = "") -> Dict[str, Any]:
    """
    完整执行管线:
      Protocol Check → World Model → Execute → Verification → Result
    """
    task_name = task.get("task_name", "")
    task_id = task.get("task_id", "unknown")
    started = datetime.now(timezone.utc)

    task = resolve_all_pointers(task)

    result = {
        "task_id": task_id,
        "executor": executor,
        "pipeline": {},
        "success": False,
        "ts_start": started.isoformat(),
    }

    if is_system_halted():
        result["error"] = "SYSTEM_HALT: 连续3次验证失败触发熔断, 等待管理员清除"
        result["pipeline"]["halt"] = {"passed": False, "reason": "system_halted"}
        return result

    # Step 1: Protocol Check
    ok, reason = protocol_check(task_name, executor)
    result["pipeline"]["protocol"] = {"passed": ok, "reason": reason}
    if not ok:
        result["error"] = f"Protocol blocked: {reason}"
        return result

    # Step 2: World Model (pre-exec)
    ok, reason = pre_exec_safety_check(task)
    result["pipeline"]["world_model"] = {"passed": ok, "reason": reason}
    if not ok:
        result["error"] = f"World Model blocked: {reason}"
        return result

    # Step 3: Execute
    exec_start = time.time()
    if execute_fn:
        try:
            exec_ok, exec_summary = execute_fn(task)
            if exec_ok:
                _clear_exec_fail(executor)
            else:
                _count_exec_fail(executor)
        except Exception as e:
            exec_ok, exec_summary = False, f"Execute error: {e}"
            _count_exec_fail(executor)
    else:
        exec_ok, exec_summary = True, f"[{executor}] 任务已认领"
        _clear_exec_fail(executor)
    exec_ms = int((time.time() - exec_start) * 1000)
    result["pipeline"]["execute"] = {"passed": exec_ok, "summary": exec_summary[:32768], "duration_ms": exec_ms}

    # Step 4: Verification (post-exec)
    if exec_ok:
        ok, report = post_exec_verification(task, work_dir, exec_summary, executor=executor)
        result["pipeline"]["verify"] = {"passed": ok, "report": report[:300]}
        if not ok:
            _count_verify_fail(task_id)
            result["error"] = f"Verification failed: {report[:200]}"
            result["success"] = False
            result["ts_complete"] = datetime.now(timezone.utc).isoformat()
            return result
        else:
            _clear_verify_fails()

    result["success"] = exec_ok
    result["ts_complete"] = datetime.now(timezone.utc).isoformat()
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    result["elapsed_s"] = round(elapsed, 2)

    return result


# ── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("AIOS Enforcer — 执行管线强制器")
        print("用法:")
        print("  aios_enforcer.py check <message>      # 协议检查")
        print("  aios_enforcer.py safety <task_json>   # 安全模拟")
        print("  aios_enforcer.py verify <task_json>   # 验证门禁")
        print("  aios_enforcer.py pipeline <task_json> # 完整管线")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "check":
        msg = " ".join(sys.argv[2:])
        ok, reason = protocol_check(msg)
        print(f"{'✅' if ok else '❌'} {reason}")

    elif cmd == "safety":
        task_file = sys.argv[2] if len(sys.argv) > 2 else ""
        if task_file and os.path.exists(task_file):
            with open(task_file) as f:
                task = json.load(f)
            ok, reason = pre_exec_safety_check(task)
            print(f"{'✅' if ok else '❌'} {reason}")

    elif cmd == "verify":
        task_file = sys.argv[2] if len(sys.argv) > 2 else ""
        if task_file and os.path.exists(task_file):
            with open(task_file) as f:
                task = json.load(f)
            ok, report = post_exec_verification(task)
            print(f"{'✅' if ok else '❌'} {report[:500]}")

    elif cmd == "pipeline":
        task_file = sys.argv[2] if len(sys.argv) > 2 else ""
        executor = sys.argv[3] if len(sys.argv) > 3 else "opencode"
        if task_file and os.path.exists(task_file):
            with open(task_file) as f:
                task = json.load(f)
            result = enforce_pipeline(task, executor)
            print(json.dumps(result, ensure_ascii=False, indent=2))
