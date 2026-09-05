#!/usr/bin/env python3
"""
AIOS v4.0 安全工具集 (aioS Secure Utilities)
==============================================
集中所有安全相关的工具函数, 被 AIOS 各模块统一引用。
原分散在 aios_enforcer / aios_firewall / 各 entry 模块的零散安全代码
统一收敛到这里, 避免漂移和遗漏。

包含:
  - 网络绑定安全 (默认 127.0.0.1; 仅在显式 opt-in 时允许 0.0.0.0)
  - 输入校验 (消息长度, 危险命令, 协议层)
  - 认证 token (随机的 AIOS_AUTH_TOKEN, 持久化到 ~/.aios_auth_token)
  - 启动防火墙 (默认 enforce 模式, 而非 warn)
  - 安全的 shell 转义 (拒绝 shell=True 风格的注入)
"""

from __future__ import annotations
import os
import re
import sys
import json
import hmac
import hashlib
import secrets
import socket
import logging
import unicodedata
from pathlib import Path
from typing import Any, Optional, Tuple, List

AIOS_HOME = Path(os.environ.get("AIOS_HOME", "${AIOS_HOME}"))
_AUTH_TOKEN_PATH = Path(os.environ.get("HOME", str(Path.home()))) / ".aios_auth_token"
_LOG = logging.getLogger("aios_secure")

# ════════════════════════════════════════════════════════════════
#  1. 绑定安全: 默认仅 127.0.0.1
# ════════════════════════════════════════════════════════════════

def safe_bind_host(preferred: str = "") -> str:
    """
    解析环境/配置, 给出本次进程应该绑定的 IP.

    规则:
      - 默认绑 127.0.0.1 (loopback only, 局域网/公网不可达)
      - 仅当 AIOS_ALLOW_PUBLIC_BIND=1 (或 AIOS_BIND_HOST 显式指定) 时才允许非 loopback
      - 防止 AIOS 服务被无意暴露在 0.0.0.0
    """
    env_bind = os.environ.get("AIOS_BIND_HOST", "").strip()
    if env_bind:
        # 显式指定则尊重, 但如果是 0.0.0.0 必须有 opt-in 标记
        if env_bind in ("0.0.0.0", "::"):
            if os.environ.get("AIOS_ALLOW_PUBLIC_BIND") != "1":
                _LOG.warning(
                    "AIOS_BIND_HOST=%s 被忽略, 改为 127.0.0.1; "
                    "如需对外暴露请设置 AIOS_ALLOW_PUBLIC_BIND=1", env_bind,
                )
                return "127.0.0.1"
        return env_bind
    if preferred and preferred not in ("0.0.0.0", "::"):
        return preferred
    return "127.0.0.1"


# ════════════════════════════════════════════════════════════════
#  2. 认证 Token (持久化到文件, 由 entry gateway 等使用)
# ════════════════════════════════════════════════════════════════

def get_or_create_auth_token() -> str:
    """获取或生成 AIOS 认证 token. 32 字节 URL-safe 随机串."""
    if _AUTH_TOKEN_PATH.exists():
        tok = _AUTH_TOKEN_PATH.read_text().strip()
        if tok and len(tok) >= 16:
            return tok
    tok = secrets.token_urlsafe(32)
    _AUTH_TOKEN_PATH.write_text(tok)
    _AUTH_TOKEN_PATH.chmod(0o600)
    return tok


def verify_auth_token(provided: Optional[str], required: Optional[str] = None) -> bool:
    """
    校验 HTTP 请求头 X-AIOS-Token 或 query ?token=...

    如果 AIOS_AUTH_REQUIRED != 1, 直接返回 True (方便本地开发).
    否则要求 provided 与 required (默认 get_or_create_auth_token()) 相等.
    """
    if os.environ.get("AIOS_AUTH_REQUIRED", "0") != "1":
        return True
    expected = required or get_or_create_auth_token()
    if not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


# ════════════════════════════════════════════════════════════════
#  3. 输入校验: 消息长度 + 危险命令检测
# ════════════════════════════════════════════════════════════════

# 最大输入字符数 (经过 unicode 标准化后)
MAX_MESSAGE_CHARS = 4096

# 危险模式. 比 aios_enforcer.DANGEROUS_PATTERNS 更激进, 加入
# python 内置函数 import/base64 反引号等绕过手段.
_DANGEROUS_RES: List[re.Pattern] = [
    # 文件/磁盘破坏
    re.compile(r"rm\s+-[rR][fF]\s+/", re.I),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.I),  # fork bomb
    re.compile(r"mkfs(\.|\s)", re.I),
    re.compile(r"dd\s+if=/dev/(zero|urandom|random)", re.I),
    re.compile(r">\s*/dev/sd[a-z]", re.I),
    re.compile(r"chmod\s+(-R\s+)?(777|666)\s+/", re.I),
    re.compile(r"chown\s+-R\s+", re.I),

    # SQL 注入痕迹
    re.compile(r"\bdrop\s+table\b", re.I),
    re.compile(r"\bdrop\s+database\b", re.I),
    re.compile(r"\btruncate\s+table\b", re.I),

    # 反向 shell / 远程下载
    re.compile(r"\bnc\s+-l", re.I),
    re.compile(r"/dev/tcp/", re.I),
    re.compile(r"curl\s+[^|;]*\|\s*(ba)?sh", re.I),
    re.compile(r"wget\s+[^|;]*\|\s*(ba)?sh", re.I),
    re.compile(r"base64\s+-d.*\|\s*(ba)?sh", re.I),
    # 任意 "管道到 sh/python": 用户文本里出现 “| sh” / “|bash” / “|python” / “|perl” 直接拒
    re.compile(r"\|\s*(ba)?sh\b", re.I),
    re.compile(r"\|\s*python\d?\b", re.I),
    re.compile(r"\|\s*perl\d?\b", re.I),
    re.compile(r"\|\s*ruby\d?\b", re.I),
    # curl 指向明确黑名单主机 (常见 C2 / poison)
    re.compile(r"curl\s+https?://[^/]*\b(evil|malicious|phish|attacker)\b", re.I),
    re.compile(r"\bcurl\s+https?://(?!(?:127\.|localhost)\b)[0-9.]+\b", re.I),  # curl to non-loopback IP



    # 系统控制
    re.compile(r"\bshutdown\s+-h\b", re.I),
    re.compile(r"\breboot\s+-f\b", re.I),
    re.compile(r"\bpoweroff\b", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bmount\b", re.I),
    re.compile(r"\bsudo\s+", re.I),
    re.compile(r"\bsu\s+-\s*", re.I),

    # 敏感文件 / 凭据
    re.compile(r"/etc/passwd", re.I),
    re.compile(r"/etc/shadow", re.I),
    re.compile(r"${HOME}/", re.I),
    re.compile(r"~/.ssh", re.I),
    re.compile(r"~/.aws", re.I),
    re.compile(r"~/.gnupg", re.I),
    re.compile(r"\bcat\s+/etc/", re.I),

    # 路径穿越
    re.compile(r"\.\./"),
    re.compile(r"\.\.\\"),

    # Python/Node 内置 eval (用于阻止 __import__('os').system('rm -rf /'))
    re.compile(r"\b__import__\s*\(", re.I),
    re.compile(r"\beval\s*\(", re.I),
    re.compile(r"\bexec\s*\(", re.I),
    re.compile(r"\bcompile\s*\(", re.I),
    re.compile(r"\bcompile_command\b", re.I),
    re.compile(r"\bsubprocess\b\.", re.I),
    re.compile(r"\bos\.system\s*\(", re.I),
    re.compile(r"\bos\.popen\s*\(", re.I),
    re.compile(r"\bos\.remove\s*\(", re.I),
    re.compile(r"\bos\.unlink\s*\(", re.I),
    re.compile(r"\bshutil\.rmtree\s*\(", re.I),
]

_WHITESPACE_NORMALIZER = re.compile(r"\s+")


def normalize_message(text: str) -> str:
    """unicode NFKC + 折叠全角字符 + 压缩空白 — 防止空格分隔绕过."""
    if not isinstance(text, str):
        text = str(text)
    text = unicodedata.normalize("NFKC", text)
    # 将零宽字符 / 全角空格 / 换页等折叠
    text = _WHITESPACE_NORMALIZER.sub(" ", text)
    return text.strip()


def check_input_safety(message: str, *, allow_short_circuit: bool = True) -> Tuple[bool, str]:
    """
    统一输入校验.

    返回 (passed, reason).
    先做长度检查再做模式匹配.
    """
    if message is None or not str(message).strip():
        return False, "空消息被拦截"

    text = str(message)
    if len(text) > MAX_MESSAGE_CHARS:
        return False, (
            f"消息过长 ({len(text)} > {MAX_MESSAGE_CHARS}); "
            "请上传文件并提供 UUID 引用"
        )

    norm = normalize_message(text)
    if not norm:
        return False, "空消息被拦截"

    for pat in _DANGEROUS_RES:
        if pat.search(norm):
            return False, f"危险指令被拦截 (pattern: {pat.pattern[:40]})"

    return True, "ok"


# 检查是否是真正可执行的 shell 命令 (强白名单: 只能是用空白分隔的 [a-z0-9 _/.+-]+)
_SAFE_SHELL_TOKEN = re.compile(r"^[a-zA-Z0-9_/.+=:@\-]+$")


def looks_like_shell_command(text: str) -> bool:
    """
    严格判断一段文本是否是 *单纯* 的 shell 命令.
    用于 _extract_shell_cmd 类函数的安全版本 — 任何包含管道/重定向/
    子 shell 的字符串都不再被视为可执行命令.
    """
    if not text or not isinstance(text, str):
        return False
    t = text.strip()
    if not t or len(t) > 200:
        return False
    # 不允许任何 shell metacharacter
    for ch in ("&", "|", ";", "$", "`", "(", ")", "<", ">",
               "{", "}", "[", "]", "?", "*", "\\", "\""):
        if ch in t:
            return False
    # 不允许换行
    if "\n" in t or "\r" in t:
        return False
    # 必须由合法 token + 空白组成
    parts = t.split()
    if not parts:
        return False
    for p in parts:
        if not _SAFE_SHELL_TOKEN.match(p):
            return False
    return True


def safe_join_command(args: List[str]) -> List[str]:
    """
    校验 args 列表中每一项都是合法 token (无 shell metachar).
    任何失败都抛 ValueError 阻止后续执行.
    调用方使用 subprocess.run(args, shell=False) 执行.
    """
    if not args:
        raise ValueError("empty args")
    for a in args:
        if not isinstance(a, str):
            raise ValueError(f"non-string arg: {a!r}")
        if not a or len(a) > 4096:
            raise ValueError(f"bad arg length: {len(a) if a else 0}")
        for ch in ("&", "|", ";", "$", "`", "(", ")", "<", ">",
                   "{", "}", "[", "]", "?", "*", "\\", "\""):
            if ch in a:
                raise ValueError(f"arg contains shell metachar: {a!r}")
    return args


# ════════════════════════════════════════════════════════════════
#  4. 启动防火墙默认模式
# ════════════════════════════════════════════════════════════════

def harden_firewall_default() -> None:
    """
    把 AIOS_FIREWALL_MODE 默认设为 enforce.
    """
    os.environ.setdefault("AIOS_FIREWALL_MODE", "enforce")


# ════════════════════════════════════════════════════════════════
#  5. 安全的 JSON 错误响应 (不泄漏 traceback)
# ════════════════════════════════════════════════════════════════

def safe_error_response(exc: BaseException, code: int = 500,
                        public_detail: str = "internal_error") -> dict:
    """
    生成对客户端安全的错误响应体.

    默认不暴露 traceback, 仅输出通用消息. 开发环境可设
    AIOS_VERBOSE_ERRORS=1 让响应带上异常类名.
    """
    detail = public_detail
    if os.environ.get("AIOS_VERBOSE_ERRORS") == "1":
        detail = f"{type(exc).__name__}: {exc}"
    return {"ok": False, "error": detail, "code": code}


# ════════════════════════════════════════════════════════════════
#  6. 工具: 安全 print (避免 traceback.print_exc 泄漏路径)
# ════════════════════════════════════════════════════════════════

def safe_log_exception(prefix: str = "error", exc: Optional[BaseException] = None) -> None:
    """把异常安全地输出到 stderr (不打印完整 traceback 到 stdout)."""
    import traceback
    if exc is None:
        exc = sys.exc_info()[1]
    if exc is None:
        return
    msg = f"{prefix}: {type(exc).__name__}: {exc}"
    sys.stderr.write(msg + "\n")
    if os.environ.get("AIOS_VERBOSE_ERRORS") == "1":
        traceback.print_exc(file=sys.stderr)


# ════════════════════════════════════════════════════════════════
#  7. CORS helper
# ════════════════════════════════════════════════════════════════

def cors_origin() -> str:
    """
    返回当前应当使用的 CORS 源.
    默认返回 * 仅在 AIOS_CORS_ALLOW_ALL=1; 否则为空(浏览器将拒绝跨域).
    """
    if os.environ.get("AIOS_CORS_ALLOW_ALL") == "1":
        return "*"
    return ""


# ════════════════════════════════════════════════════════════════
#  8. 安全配置: 启动时一次性调用
# ════════════════════════════════════════════════════════════════

def boot_security_defaults() -> None:
    """进程启动时调用一次: 收紧所有默认安全设置."""
    harden_firewall_default()
    # 默认禁止遍历 paths: 已实现见 runtime_server


# 自动设置默认值
boot_security_defaults()


SECURITY_GATE_CONTRACT = "aios-security-gate/1.0"


def classify_l4_action(message: str) -> str:
    """Return the governed L4 action class, or an empty string for normal work.

    This classifier deliberately requires both an action verb and a protected
    target.  Mentioning a risky topic in an audit or report is not approval-
    gated; asking AIOS to change that target is.
    """
    text = normalize_message(message).lower()
    if not text:
        return ""
    readonly_markers = (
        "不做任何修改", "不要修改", "不得修改", "禁止修改", "只读分析",
        "不得创建或修改文件", "不要创建或修改文件", "禁止创建或修改文件",
        "do not create or modify files", "do not modify", "do not change",
        "without changing", "read-only",
    )
    explicit_readonly = any(marker in text for marker in readonly_markers)
    for negated in readonly_markers:
        text = text.replace(negated, " ")

    mutation = (
        "修改", "更改", "设置", "调整", "启用", "禁用", "写入", "替换",
        "删除", "清空", "重启", "停止", "关闭", "发送", "发布", "覆盖",
        "modify", "change", "set ", "update", "enable", "disable", "write",
        "replace", "delete", "remove", "restart", "stop", "shutdown",
        "send", "publish", "drop", "truncate", "override",
    )
    if not any(word in text for word in mutation):
        return ""

    # Read-only incident/audit prompts often contain historical words such as
    # "published", "changed" and "token" in their evidence.  Do not combine
    # those unrelated spans into a requested mutation.  A real imperative
    # mutation still wins over a contradictory read-only suffix and remains L4.
    analysis_terms = (
        "分析", "判断", "解释", "审计", "总结", "报告", "计算",
        "analysis", "analyze", "explain", "audit", "summarize", "report",
    )
    if explicit_readonly and any(term in text for term in analysis_terms):
        verbs = "|".join(re.escape(word.strip()) for word in mutation)
        directive = re.compile(
            rf"(?:请|帮我|需要你|要求你|立即|现在|please|need you to|must)"
            rf"(?P<gap>.{{0,120}}?)(?:{verbs})",
            re.I,
        )
        requested_mutation = False
        for match in directive.finditer(text):
            gap = match.group("gap")
            historical_evidence = (
                any(term in gap for term in analysis_terms) and
                bool(re.search(
                    r"[:：].{0,80}(?:\d{1,2}:\d{2}|昨天|此前|日志|时间线|timeline|log)",
                    gap,
                    re.I,
                ))
            )
            if not historical_evidence:
                requested_mutation = True
                break
        if not requested_mutation:
            return ""

    rules = (
        ("database_destructive", (
            "drop table", "drop database", "truncate table", "清空数据库",
            "删除数据库", "删除数据表",
        )),
        ("system_control", (
            "shutdown", "poweroff", "reboot", "systemctl", "重启服务",
            "停止服务", "关闭系统", "重启系统", "停止 aios", "重启 aios",
        )),
        ("external_message", (
            "发送邮件", "发送消息", "发飞书", "发 telegram", "publish externally",
            "send email", "send message", "post externally",
        )),
        ("credential_or_permission", (
            "authorized_keys", "ssh key", "api key", "token", "密钥", "凭据",
            "权限", "permission", "chmod", "chown",
        )),
        ("critical_delete", (
            "删除核心", "删除系统", "delete critical", "remove critical",
            "${AIOS_HOME}/kernel", "${AIOS_HOME}/config",
        )),
        ("modify_config", (
            "配置文件", "系统配置", "aios 配置", "aios配置", "config file",
            "system config", "redis config", "systemd", "firewall", "防火墙",
            "maxmemory", "服务配置",
        )),
        ("token_budget_override", (
            "token budget", "token limit", "令牌预算", "token上限", "token 上限",
        )),
    )
    for action, markers in rules:
        if any(marker in text for marker in markers):
            return action
    return ""


def security_gate_decide(decision_type: str, payload: Any,
                         actor: str = "unknown") -> dict:
    """Single AIOS-owned decision contract for security enforcement."""
    if decision_type == "input":
        allowed, reason = check_input_safety(str(payload))
    elif decision_type == "bus_write":
        if actor not in {"aios-orchestrator", "aios-verification-gate",
                         "minimax-official"}:
            allowed, reason = False, f"unknown bus actor: {actor}"
        elif not isinstance(payload, dict) or "key" not in payload:
            allowed, reason = False, "invalid bus_write payload"
        else:
            from aios_firewall import check_bus
            allowed, reason = check_bus(actor, str(payload["key"]))
    elif decision_type == "l4_risk":
        allowed, reason = not bool(action), action or "normal_risk"
    else:
        allowed, reason = False, f"unsupported security decision: {decision_type}"
    return {
        "contract": SECURITY_GATE_CONTRACT,
        "decision_type": decision_type,
        "actor": actor,
        "allowed": bool(allowed),
        "reason": str(reason),
    }


def _read_json_report(path: Path) -> tuple[Optional[dict], Optional[str]]:
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def security_backend_status() -> dict:
    """Return backend evidence without granting backends system ownership."""
    trivy_path = AIOS_HOME / "logs/capabilities/trivy/aios-security.json"
    trivy_db = AIOS_HOME / "cache/capabilities/trivy/db/trivy.db"
    semgrep_path = AIOS_HOME / "logs/capabilities/semgrep/aios.json"
    trivy, trivy_error = _read_json_report(trivy_path)
    semgrep, semgrep_error = _read_json_report(semgrep_path)

    trivy_findings = 0
    if trivy:
        for result in trivy.get("Results", []):
            for field in ("Vulnerabilities", "Misconfigurations", "Secrets", "Licenses"):
                trivy_findings += len(result.get(field) or [])

    semgrep_findings = len((semgrep or {}).get("results", []))
    semgrep_errors = len((semgrep or {}).get("errors", []))
    agentshield_version = AIOS_HOME / "extensions/agentshield/VERSION"
    agentshield_path = AIOS_HOME / "logs/capabilities/agentshield/claude-report.json"
    agentshield, agentshield_error = _read_json_report(agentshield_path)

    return {
        "trivy": {
            "state": "operational" if trivy and not trivy_error and trivy_db.is_file() else "degraded",
            "report": str(trivy_path),
            "findings": trivy_findings,
            "coverage": ["vulnerability", "secret", "misconfig"],
            "vulnerability_db": trivy_db.is_file(),
            "vulnerability_db_bytes": trivy_db.stat().st_size if trivy_db.is_file() else 0,
            "error": trivy_error,
        },
        "semgrep": {
            "state": "operational" if semgrep and not semgrep_error and semgrep_errors == 0 else "degraded",
            "report": str(semgrep_path),
            "findings": semgrep_findings,
            "scan_errors": semgrep_errors,
            "error": semgrep_error,
        },
        "agentshield": {
            "state": (
                "operational_findings"
                if agentshield and not agentshield_error
                else ("installed_not_connected" if agentshield_version.is_file() else "missing")
            ),
            "version": agentshield_version.read_text(encoding="utf-8").strip()
                       if agentshield_version.is_file() else None,
            "connected": bool(agentshield and not agentshield_error),
            "report": str(agentshield_path),
            "findings": (agentshield or {}).get("summary", {}).get("totalFindings", 0),
            "critical": (agentshield or {}).get("summary", {}).get("critical", 0),
            "high": (agentshield or {}).get("summary", {}).get("high", 0),
            "files_scanned": (agentshield or {}).get("summary", {}).get("filesScanned", 0),
            "error": agentshield_error,
        },
    }



def run_agentshield_scan() -> dict:
    """Run the pinned AgentShield backend and persist a validated private report."""
    import subprocess
    import tempfile

    executable = "${HOME}/.n/bin/npx"
    cwd = AIOS_HOME / "extensions/agentshield"
    target_dir = Path("${HOME}/.claude")
    report = AIOS_HOME / "logs/capabilities/agentshield/claude-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)

    command = [executable, "ecc-agentshield", "scan",
               "-p", str(target_dir), "-f", "json"]
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w+", encoding="utf-8", delete=False,
                prefix="agentshield-", suffix=".json") as stream:
            temp_name = stream.name
            proc = subprocess.run(
                command, cwd=str(cwd), stdout=stream,
                stderr=subprocess.PIPE, text=True, timeout=300,
                shell=False,
            )
        raw = Path(temp_name).read_text(encoding="utf-8").lstrip()
        parsed, _ = json.JSONDecoder().raw_decode(raw)
        summary = parsed.get("summary")
        if not isinstance(summary, dict):
            raise ValueError("AgentShield report has no summary")
        parsed["_aios_backend"] = {
            "contract": SECURITY_GATE_CONTRACT,
            "scanner_exit": proc.returncode,
            "target": str(target_dir),
        }
        temporary = report.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, report)
        return {
            "ok": True,
            "report": str(report),
            "scanner_exit": proc.returncode,
            "files_scanned": summary.get("filesScanned", 0),
            "findings": summary.get("totalFindings", 0),
            "critical": summary.get("critical", 0),
            "high": summary.get("high", 0),
        }
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def security_gate_status() -> dict:
    backends = security_backend_status()
    return {
        "contract": SECURITY_GATE_CONTRACT,
        "owner": "aios_secure",
        "state": "operational_with_findings",
        "hot_paths": {
            "entry_input": True,
            "executor_input": True,
            "bus_write": True,
        },
        "backends": backends,
        "unclosed": [
            "Semgrep findings require triage",
            "AgentShield findings require triage",
        ],
    }


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "agentshield-scan":
    print(json.dumps(run_agentshield_scan(), ensure_ascii=False, indent=2))
    raise SystemExit(0)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "gate-status":
    print(json.dumps(security_gate_status(), ensure_ascii=False, indent=2))
    raise SystemExit(0)


if __name__ == "__main__":
    print("AIOS Secure Utilities")
    print(f"  bind host:      {safe_bind_host()}")
    print(f"  auth token:     {get_or_create_auth_token()[:8]}...")
    print(f"  CORS origin:    '{cors_origin() or '(none, default deny)'}'")
    print(f"  firewall mode:  {os.environ.get('AIOS_FIREWALL_MODE', 'enforce')}")
    # 演示输入检查
    for t in [
        "ls -la",
        "rm -rf /",
        "cat /etc/passwd",
        "__import__('os').system('rm -rf /')",
        "kubectl delete",
    ]:
        ok, why = check_input_safety(t)
        print(f"  {'✅' if ok else '🚫'} {t!r}: {why}")
