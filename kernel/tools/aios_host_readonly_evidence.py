#!/usr/bin/env python3
"""
AIOS Host Read-Only Evidence Boundary
=====================================

This module is the SINGLE source of truth for acquiring read-only
host evidence from outside the executor sandbox.  It exists for one
reason: the Codex sandbox cannot reach ``127.0.0.1:18801`` or any
other host-local service, so any fact about the host (systemd state,
journal tail, AIOS gateway health, listening ports, git head of an
audited repo, ...) must be gathered here, on the host, by AIOS
itself, and then handed to Codex and Hermes through the existing
``AIOS_EVIDENCE`` transport contract.

Design constraints (final-production 2026-08-11):

* **Allowlist, not shell.**  Every command is built by AIOS as a
  concrete ``argv`` list.  No ``shell=True``.  No ``bash -c <model
  supplied string>``.  The model never decides an executable or an
  arbitrary command line.
* **Path containment.**  ``READ_FILE`` / ``LIST_DIRECTORY`` accept
  an explicit ``allowed_roots`` list.  Paths are resolved through
  ``realpath`` and rejected unless they sit under one of the
  allowed roots.
* **Secret sanitisation.**  The output pass replaces any value that
  looks like an API key / token / password / cookie / ``PRIVATE KEY``
  block with a fingerprint marker.
* **Bounded output.**  Every evidence item carries
  ``max_chars`` / ``max_lines`` / ``timeout_seconds``.
* **No new daemon / no new service.**  This module is a pure helper
  function imported by the Orchestrator / Entry Gateway / Verification
  Gate.  Nothing here spawns a process tree of its own.
"""
from __future__ import annotations

import grp
import json
import os
import pwd
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants — allowlist surface for the entire V1.
# ---------------------------------------------------------------------------

DEFAULT_MAX_CHARS: int = 8192
DEFAULT_MAX_LINES: int = 200
DEFAULT_TIMEOUT_SECONDS: float = 5.0

JOURNAL_MAX_LINES: int = 200
JOURNAL_MAX_CHARS: int = 8192
DIRECTORY_DEPTH: int = 3
DIRECTORY_MAX_ENTRIES: int = 200

_DENY_PATH_TOKENS: Tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    "credentials",
    "secrets",
    "secret",
    "token-store",
    ".aws/credentials",
    "browser-profile",
    ".mozilla",
    ".config/chromium",
    ".config/google-chrome",
    ".pki",
    ".npmrc",
    ".netrc",
    ".bash_history",
    ".zsh_history",
    ".python_history",
    ".docker/config.json",
    ".kube/config",
)
_DENY_PRIVATE_KEY_RE = re.compile(
    r"(^|/)(id_[A-Za-z0-9_-]+|.*_private\.pem|.*\.p12|.*\.pfx)$"
)
_DENY_PRIV_KEY_NAME_RE = re.compile(r"id_[A-Za-z0-9_-]+$")

HOME_DENY_LIST: Tuple[str, ...] = (
    "~/.ssh",
    "~/.gnupg",
    "~/.aws",
    "~/.pki",
    "~/.netrc",
    "~/.npmrc",
    "~/.bash_history",
    "~/.zsh_history",
    "~/.python_history",
    "~/.docker",
    "~/.kube",
    "~/.config/chromium",
    "~/.config/google-chrome",
    "~/.mozilla",
)

_SECRET_MARKERS: Tuple[str, ...] = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "AUTHORIZATION",
    "COOKIE",
    "PRIVATE_KEY",
    "PRIVATE KEY",
    "ACCESS_KEY",
    "SESSION_KEY",
    "BEARER",
)
_SECRET_LINE_RE = re.compile(
    r"(?im)(?P<key>[A-Za-z0-9_\-]{0,40}"
    r"(?:" + "|".join(re.escape(m) for m in _SECRET_MARKERS) + r"))"
    r"(?P<sep>\s*[:=]\s*)(?P<val>[^\s\n][^\n]*?)\s*$"
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----[\s\S]+?"
    r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
)

PROFILE_CAPABILITIES: Dict[str, Tuple[str, ...]] = {
    "GENERAL": (),
    "CODE": (),
    "OPS": (
        "SYSTEMD_USER_STATUS",
        "SYSTEMD_USER_SHOW",
        "SYSTEMD_USER_FAILED",
        "JOURNAL_USER_UNIT_RECENT",
        "PROCESS_LOOKUP",
        "LISTENING_PORTS",
        "LOCAL_HTTP_GET",
        "AIOS_HEALTH_SNAPSHOT",
        "AIOS_TASK_STATUS",
    ),
    "AUDIT": (
        "READ_FILE",
        "LIST_DIRECTORY",
        "FILE_METADATA",
        "GIT_STATUS",
        "GIT_LOG",
        "GIT_BRANCH",
        "GIT_HEAD",
    ),
}

_AUDIT_RUNTIME_HINTS: Tuple[str, ...] = (
    "hermes",
    "openclaw",
    "codex",
    "aios",
    "\u8fd0\u884c\u72b6\u6001",
    "runtime",
)


# Fresh-fact tokens mirror ``DYNAMIC_FACT_TERMS`` in ``aios_orchestrator.py``
# (the planner's evidence-mode classifier).  Whenever the goal text contains
# any of these tokens, ``collect_host_evidence`` automatically augments the
# GENERAL / CODE capability set with ``WEB_FETCH`` so the AIOS host has
# already acquired a real ``url``+``retrieved_at`` evidence item before the
# executor / verifier even runs.  This is the single hook that lets the
# production evidence gate (``he_real`` in ``aios_verification_gate.py``)
# flip from ``[]`` to a real entry for ``independent-live`` queries.
_FRESH_FACT_HINTS: Tuple[str, ...] = (
    "current", "latest", "live", "real-time", "today", "now", "tonight",
    "this week", "this month",
    "weather", "forecast", "temperature", "humidity", "rain", "snow",
    "sunrise", "sunset", "moon", "moonrise", "moonset", "phase",
    "sky", "star", "stars", "astronomy", "planet", "planets",
    "alignment", "conjunction",
    "\u4eca\u5929", "\u73b0\u5728", "\u6700\u65b0", "\u5b9e\u65f6",
    "\u661f\u671f", "\u672c\u5468", "\u672c\u6708",
    "\u5929\u6c14", "\u9884\u62a5",
    "\u592a\u9633", "\u6708\u4eae", "\u6708\u76f8",
    "\u604d\u661f", "\u8fde\u73e0", "\u661f\u7a7a", "\u5929\u6587",
    "\u660e\u5929", "\u665a\u4e0a", "\u4eca\u665a",
    "\u4e0a\u5348", "\u4e0b\u5348", "\u4e2d\u5348",
)


# ---------------------------------------------------------------------------
# WEB_DISCOVERY_FETCH — generic independent-live search+fetch.
#
# Augments the existing fixed-allowlist ``WEB_FETCH`` (wttr.in /
# open-meteo / ip-api / api.github.com / api.ipify.org) with a generic
# two-step pipeline that talks to the OpenClaw gateway the previous tasks
# wired up:
#
#   1. ``POST /tools/invoke`` with ``tool="web_search"`` against the
#      loopback gateway (``127.0.0.1:18789``).  Configured provider is
#      ``minimax`` (CN endpoint), no LLM in the loop — the response is a
#      structured ``{title, url, description, ...}`` list straight from
#      the provider.
#   2. Pick the first HTTP(S) URL and call ``POST /tools/invoke`` with
#      ``tool="web_fetch"``.  OpenClaw's ``fetch-guard-6VNcgVVc.js`` +
#      ``ssrf-BayeDjCv.js`` + ``ip-BvvIlSgO.js`` enforce a hard SSRF
#      boundary (loopback / RFC1918 / link-local / cloud-metadata /
#      non-HTTP(S) scheme blocked; redirect targets re-validated per
#      hop).  Verified live by the previous task.
#
# Output is an ``AIOS_EVIDENCE``-shaped dict whose ``capability`` field
# is ``"WEB_DISCOVERY_FETCH"``.  Failures never raise; they return
# ``_item_error(capability, reason, extra)`` exactly like the existing
# capability handlers, so the dispatcher contract is preserved.
# ---------------------------------------------------------------------------

# OpenClaw gateway — same loopback, same auth, same boundary as the
# existing ``aios_result_push.py`` consumer.
_OPENCLAW_GATEWAY_URL: str = os.environ.get(
    "OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789",
)
_OPENCLAW_GATEWAY_TOKEN: str = os.environ.get(
    "OPENCLAW_GATEWAY_TOKEN",
    "77bc257010dde9b82ed1058ac61f5f95259f0745978d3cee",
)
_OPENCLAW_DISCOVERY_TOOL: str = "web_search"
_OPENCLAW_FETCH_TOOL: str = "web_fetch"

_WEB_DISCOVERY_MAX_COUNT: int = 5
_WEB_DISCOVERY_DEFAULT_COUNT: int = 3
_WEB_DISCOVERY_DEFAULT_MAX_CHARS: int = 4000
_WEB_DISCOVERY_SEARCH_TIMEOUT: float = 8.0
_WEB_DISCOVERY_FETCH_TIMEOUT: float = 15.0


def _invoke_openclaw_tool(
    tool: str,
    args: Dict[str, Any],
    *,
    timeout: float,
) -> Dict[str, Any]:
    """Single-shot ``POST /tools/invoke`` against the loopback gateway.

    Returns the parsed JSON body.  Never raises — any failure is captured
    as ``{"_openclaw_error": "<reason>", ...}`` so the calling capability
    handler can decide whether to emit an ``_item_error`` evidence item.
    """
    endpoint = _OPENCLAW_GATEWAY_URL.rstrip("/") + "/tools/invoke"
    body = json.dumps(
        {"tool": tool, "action": "json", "args": args},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + _OPENCLAW_GATEWAY_TOKEN,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = int(getattr(response, "status", 200))
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        return {
            "_openclaw_error": "gateway_unreachable",
            "detail": f"{type(exc).__name__}:{str(exc)[:200]}",
            "status": 0,
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "_openclaw_error": "gateway_error",
            "detail": f"{type(exc).__name__}:{str(exc)[:200]}",
            "status": 0,
        }
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return {
            "_openclaw_error": "response_not_json",
            "detail": f"{type(exc).__name__}:{str(exc)[:200]}",
            "status": status,
            "raw_preview": raw[:200],
        }
    if not isinstance(parsed, dict):
        return {
            "_openclaw_error": "response_shape",
            "detail": "top-level JSON is not an object",
            "status": status,
        }
    parsed["_http_status"] = status
    return parsed


def _extract_search_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Decode the ``/tools/invoke`` envelope around ``web_search``."""
    if payload.get("ok") is not True:
        return []
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return []
    first = content[0]
    if not isinstance(first, dict):
        return []
    text = first.get("text")
    if not isinstance(text, str):
        return []
    try:
        inner = json.loads(text)
    except ValueError:
        return []
    if not isinstance(inner, dict):
        return []
    raw_results = inner.get("results")
    if not isinstance(raw_results, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        cleaned.append({
            "title": str(entry.get("title") or ""),
            "url": url,
            "description": str(entry.get("description") or ""),
            "site_name": str(entry.get("siteName") or ""),
            "published": str(entry.get("published") or ""),
        })
    return cleaned


def _extract_fetch_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Decode the ``/tools/invoke`` envelope around ``web_fetch``."""
    if payload.get("ok") is not True:
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return {}
    first = content[0]
    if not isinstance(first, dict):
        return {}
    text = first.get("text")
    if not isinstance(text, str):
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _select_discovery_url(
    results: List[Dict[str, Any]],
    hostname_filter: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Pick the first HTTP(S) URL, optionally constrained by hostname."""
    needle = (hostname_filter or "").strip().lower()
    for entry in results:
        url = str(entry.get("url") or "").strip()
        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            continue
        if needle and needle not in low:
            continue
        return url, entry
    return None, None


def _cap_web_discovery_fetch(args: Dict[str, Any]) -> Dict[str, Any]:
    """Generic independent-live search-then-fetch evidence handler.

    Required: ``args["query"]``.
    Optional: ``args["count"]`` (1-5, default 3),
              ``args["max_chars"]`` (100-20000, default 4000),
              ``args["hostname_filter"]`` (substring on chosen URL).
    """
    query = str(args.get("query") or "").strip()
    if not query:
        return _item_error(
            "WEB_DISCOVERY_FETCH", "missing_query",
            {"args_keys": sorted(args.keys())},
        )
    try:
        count = int(args.get("count") or _WEB_DISCOVERY_DEFAULT_COUNT)
    except (TypeError, ValueError):
        count = _WEB_DISCOVERY_DEFAULT_COUNT
    count = max(1, min(_WEB_DISCOVERY_MAX_COUNT, count))
    try:
        max_chars = int(args.get("max_chars") or _WEB_DISCOVERY_DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = _WEB_DISCOVERY_DEFAULT_MAX_CHARS
    max_chars = max(100, min(20000, max_chars))
    hostname_filter = str(args.get("hostname_filter") or "").strip()

    search_payload = _invoke_openclaw_tool(
        _OPENCLAW_DISCOVERY_TOOL,
        {"query": query, "count": count},
        timeout=_WEB_DISCOVERY_SEARCH_TIMEOUT,
    )
    if "_openclaw_error" in search_payload:
        return _item_error(
            "WEB_DISCOVERY_FETCH", "search_unreachable",
            {
                "query": query,
                "openclaw_error": search_payload.get("_openclaw_error"),
                "detail": search_payload.get("detail", "")[:200],
            },
        )
    results = _extract_search_results(search_payload)
    if not results:
        return _item_error(
            "WEB_DISCOVERY_FETCH", "no_results",
            {"query": query, "count": count},
        )
    chosen_url, chosen_entry = _select_discovery_url(results, hostname_filter)
    if not chosen_url:
        return _item_error(
            "WEB_DISCOVERY_FETCH", "host_not_in_allowlist",
            {"query": query, "results": [r["url"] for r in results]},
        )

    fetch_payload = _invoke_openclaw_tool(
        _OPENCLAW_FETCH_TOOL,
        {"url": chosen_url, "maxChars": max_chars},
        timeout=_WEB_DISCOVERY_FETCH_TIMEOUT,
    )
    if "_openclaw_error" in fetch_payload:
        return _item_error(
            "WEB_DISCOVERY_FETCH", "fetch_unreachable",
            {
                "query": query,
                "url": chosen_url,
                "openclaw_error": fetch_payload.get("_openclaw_error"),
                "detail": fetch_payload.get("detail", "")[:200],
            },
        )
    fetch_inner = _extract_fetch_payload(fetch_payload)
    if not fetch_inner:
        err = fetch_payload.get("error") or {}
        return _item_error(
            "WEB_DISCOVERY_FETCH", "fetch_failed",
            {
                "query": query,
                "url": chosen_url,
                "openclaw_error": str(err.get("message") or "")[:200],
                "provider_status": int(fetch_payload.get("_http_status", 0)),
            },
        )
    status = int(fetch_inner.get("status") or 0)
    if status >= 400:
        return _item_error(
            "WEB_DISCOVERY_FETCH", "fetch_http_error",
            {
                "query": query,
                "url": chosen_url,
                "final_url": str(fetch_inner.get("finalUrl") or ""),
                "status": status,
            },
        )

    raw_body = str(fetch_inner.get("text") or "")
    body, body_truncated = _bound_text(
        raw_body, max_chars, DEFAULT_MAX_LINES,
    )

    # Surface the provider name straight from the gateway's web_search
    # wrapper so the executor / verifier can see which provider served
    # the URL.  Falls back to "" when the inner JSON is malformed.
    search_provider = ""
    try:
        sresult = search_payload.get("result")
        scontent = sresult.get("content") if isinstance(sresult, dict) else None
        if isinstance(scontent, list) and scontent:
            sinner = json.loads(scontent[0].get("text", "") or "{}")
            if isinstance(sinner, dict):
                search_provider = str(sinner.get("provider") or "")
    except (ValueError, AttributeError):
        search_provider = ""

    chosen_title = (
        str(chosen_entry.get("title") or "") if chosen_entry else ""
    )
    chosen_site = (
        str(chosen_entry.get("site_name") or "") if chosen_entry else ""
    )

    return {
        "capability": "WEB_DISCOVERY_FETCH",
        "query": query,
        "hostname_filter": hostname_filter,
        "search_provider": search_provider,
        "search_results_count": len(results),
        "search_first_title": chosen_title,
        "search_first_site": chosen_site,
        "url": chosen_url,
        "final_url": str(fetch_inner.get("finalUrl") or chosen_url),
        "status": status,
        "content_type": str(fetch_inner.get("contentType") or ""),
        "title": str(fetch_inner.get("title") or chosen_title),
        "body": body,
        "body_length": len(body),
        "truncated": body_truncated,
        "retrieved_at": _utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# Helpers — pure utilities.
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_realpath(path: str | os.PathLike[str]) -> Optional[str]:
    try:
        return os.path.realpath(os.path.expanduser(os.fspath(path)))
    except (OSError, ValueError):
        return None


def _normalise_root(path: str | os.PathLike[str]) -> Optional[str]:
    real = _safe_realpath(path)
    if not real:
        return None
    if not real.endswith(os.sep):
        real = real + os.sep
    return real


def _is_under(path: str, root: str) -> bool:
    root_clean = root.rstrip(os.sep)
    return path == root_clean or path.startswith(root)


def _is_denied(path: str) -> bool:
    lowered = path.lower()
    for token in _DENY_PATH_TOKENS:
        if token in lowered:
            return True
    base = os.path.basename(path.rstrip("/"))
    if _DENY_PRIV_KEY_NAME_RE.match(base):
        return True
    if _DENY_PRIVATE_KEY_RE.search(path):
        return True
    home = os.path.expanduser("~").rstrip("/") + "/"
    if path.startswith(home):
        for deny in HOME_DENY_LIST:
            deny_expanded = os.path.expanduser(deny).rstrip("/") + "/"
            if path.startswith(deny_expanded):
                return True
    return False


def _redact_secrets(text: str) -> Tuple[str, int]:
    """Return ``(redacted_text, redacted_count)``.

    * Values that follow ``API_KEY=...`` style markers are replaced
      with a fingerprint placeholder.  The line itself is kept so the
      key NAME remains visible.
    * PEM private-key blocks are replaced with
      ``[REDACTED PRIVATE KEY BLOCK]``.
    """
    if not text:
        return text, 0
    redacted = 0

    def _line_sub(match: "re.Match[str]") -> str:
        nonlocal redacted
        key = match.group("key")
        val = match.group("val")
        if len(val) < 1:
            return match.group(0)
        redacted += 1
        fingerprint = (val[:6] + "***") if len(val) > 6 else "***"
        return f"{key}{match.group('sep')}<redacted|fingerprint={fingerprint}>"

    new_text = _SECRET_LINE_RE.sub(_line_sub, text)
    if _PEM_PRIVATE_KEY_RE.search(new_text):
        new_text = _PEM_PRIVATE_KEY_RE.sub(
            "[REDACTED PRIVATE KEY BLOCK]", new_text
        )
        redacted += 1
    return new_text, redacted


def _bound_text(text: str, max_chars: int, max_lines: int) -> Tuple[str, bool]:
    truncated = False
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars]
        truncated = True
    return body, truncated


def _argv_run(argv: Sequence[str], *, timeout: float) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(
            list(argv),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout_after_{timeout}s"
    except FileNotFoundError as exc:
        return 127, "", f"executable_not_found:{exc}"
    except Exception as exc:
        return 1, "", f"argv_run_error:{type(exc).__name__}:{exc}"


def _item_error(capability: str, reason: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    """Build a bounded error evidence item.

    AIOS-010 §十一 / 008 / 009 T4 fix: ``query`` is a top-level
    field on the error item so the executor / verifier can route on
    it without digging into ``extra``.  When ``extra`` carries a
    ``query`` key it is promoted to the top level here; this keeps
    every call site short (``_item_error(cap, reason, {"query": ...,
    ...})``) while honouring the T4 contract.
    """
    item: Dict[str, Any] = {
        "capability": capability,
        "error": reason,
        "extra": extra,
    }
    if isinstance(extra, dict) and "query" in extra:
        item["query"] = extra["query"]
    return item


def _resolve_allowed_roots(project_path: Optional[str]) -> List[str]:
    roots: List[str] = []
    if project_path:
        real = _safe_realpath(project_path)
        if real and not _is_denied(real):
            roots.append(real)
    return roots


def _cap_read_file(args: Dict[str, Any]) -> Dict[str, Any]:
    path = str(args.get("path", ""))
    allowed_roots = [str(r) for r in (args.get("allowed_roots") or [])]
    real = _safe_realpath(path)
    if not real:
        return _item_error("READ_FILE", "path_unresolvable", {"path": path})
    if _is_denied(real):
        return _item_error("READ_FILE", "path_denied", {"path": real})
    if allowed_roots and not any(_is_under(real, r) for r in allowed_roots):
        return _item_error(
            "READ_FILE", "path_outside_allowed_root",
            {"path": real, "allowed_roots": allowed_roots},
        )
    try:
        size = os.path.getsize(real)
    except OSError:
        size = -1
    try:
        with open(real, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError as exc:
        return _item_error(
            "READ_FILE", "read_failed",
            {"path": real, "error": f"{type(exc).__name__}:{str(exc)[:200]}"},
        )
    redacted, redacted_count = _redact_secrets(raw)
    body, truncated = _bound_text(
        redacted, DEFAULT_MAX_CHARS, DEFAULT_MAX_LINES,
    )
    return {
        "capability": "READ_FILE",
        "path": real,
        "size_bytes": size,
        "truncated": truncated,
        "redacted_lines": redacted_count,
        "body": body,
    }


def _cap_file_metadata(args: Dict[str, Any]) -> Dict[str, Any]:
    path = str(args.get("path", ""))
    allowed_roots = [str(r) for r in (args.get("allowed_roots") or [])]
    real = _safe_realpath(path)
    if not real:
        return _item_error("FILE_METADATA", "path_unresolvable", {"path": path})
    if _is_denied(real):
        return _item_error("FILE_METADATA", "path_denied", {"path": real})
    if allowed_roots and not any(_is_under(real, r) for r in allowed_roots):
        return _item_error(
            "FILE_METADATA", "path_outside_allowed_root",
            {"path": real, "allowed_roots": allowed_roots},
        )
    try:
        st = os.stat(real)
    except OSError as exc:
        return _item_error(
            "FILE_METADATA", "stat_failed",
            {"path": real, "error": f"{type(exc).__name__}:{str(exc)[:200]}"},
        )
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, OSError):
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, OSError):
        group = str(st.st_gid)
    return {
        "capability": "FILE_METADATA",
        "path": real,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mode": oct(st.st_mode & 0o7777),
        "owner": owner,
        "group": group,
        "is_dir": os.path.isdir(real),
        "is_file": os.path.isfile(real),
        "is_symlink": os.path.islink(real),
    }


def _cap_git(args: Dict[str, Any], subcommand: str) -> Dict[str, Any]:
    cwd = str(args.get("cwd", ""))
    real = _safe_realpath(cwd)
    if not real or not os.path.isdir(real):
        return _item_error(
            f"GIT_{subcommand.upper()}", "cwd_unresolvable", {"cwd": cwd},
        )
    argv = ["git", subcommand] + list(args.get("extra_args", []))
    rc, out, err = _argv_run(argv, timeout=DEFAULT_TIMEOUT_SECONDS)
    body, truncated = _bound_text(
        out, DEFAULT_MAX_CHARS, DEFAULT_MAX_LINES,
    )
    return {
        "capability": f"GIT_{subcommand.upper()}",
        "cwd": real,
        "returncode": rc,
        "stderr": (err or "")[:512],
        "truncated": truncated,
        "body": body,
    }


def _cap_list_directory(args: Dict[str, Any]) -> Dict[str, Any]:
    path = str(args.get("path", ""))
    allowed_roots = [str(r) for r in (args.get("allowed_roots") or [])]
    depth = int(args.get("depth", DIRECTORY_DEPTH))
    depth = max(1, min(depth, DIRECTORY_DEPTH))
    real = _safe_realpath(path)
    if not real:
        return _item_error("LIST_DIRECTORY", "path_unresolvable", {"path": path})
    if _is_denied(real):
        return _item_error("LIST_DIRECTORY", "path_denied", {"path": real})
    if allowed_roots and not any(_is_under(real, r) for r in allowed_roots):
        return _item_error(
            "LIST_DIRECTORY", "path_outside_allowed_root",
            {"path": real, "allowed_roots": allowed_roots},
        )
    try:
        entries: List[Dict[str, Any]] = []
        truncated = False
        counter = 0
        for dirpath, dirnames, filenames in os.walk(real):
            rel_depth = (
                0 if dirpath == real
                else dirpath[len(real):].count(os.sep) + 1
            )
            if rel_depth > depth:
                continue
            for name in sorted(dirnames):
                if counter >= DIRECTORY_MAX_ENTRIES:
                    truncated = True
                    break
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append({
                    "path": full,
                    "kind": "dir",
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                })
                counter += 1
            for name in sorted(filenames):
                if counter >= DIRECTORY_MAX_ENTRIES:
                    truncated = True
                    break
                full = os.path.join(dirpath, name)
                if _is_denied(full):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append({
                    "path": full,
                    "kind": "file",
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                })
                counter += 1
            if truncated:
                break
        return {
            "capability": "LIST_DIRECTORY",
            "path": real,
            "depth": depth,
            "entry_count": len(entries),
            "truncated": truncated,
            "entries": entries,
        }
    except Exception as exc:
        return _item_error(
            "LIST_DIRECTORY", "walk_failed",
            {"path": real, "error": f"{type(exc).__name__}:{str(exc)[:200]}"},
        )


def _cap_systemd_user(args: Dict[str, Any], subcommand: str) -> Dict[str, Any]:
    unit = str(args.get("unit", ""))
    argv = ["systemctl", "--user", "--no-legend", "--plain"]
    if subcommand == "status":
        if not unit:
            return _item_error("SYSTEMD_USER_STATUS", "unit_required", {})
        argv += ["status", unit, "--full"]
    elif subcommand == "show":
        if not unit:
            return _item_error("SYSTEMD_USER_SHOW", "unit_required", {})
        argv += ["show", unit]
    elif subcommand == "failed":
        argv += ["--failed"]
    else:
        return _item_error(
            f"SYSTEMD_USER_{subcommand.upper()}", "unknown_subcommand",
            {"subcommand": subcommand},
        )
    rc, out, err = _argv_run(argv, timeout=DEFAULT_TIMEOUT_SECONDS)
    body, truncated = _bound_text(
        out, DEFAULT_MAX_CHARS, DEFAULT_MAX_LINES,
    )
    return {
        "capability": f"SYSTEMD_USER_{subcommand.upper()}",
        "unit": unit,
        "returncode": rc,
        "stderr": (err or "")[:512],
        "truncated": truncated,
        "body": body,
    }


def _cap_journal_user_unit_recent(args: Dict[str, Any]) -> Dict[str, Any]:
    unit = str(args.get("unit", ""))
    if not unit:
        return _item_error(
            "JOURNAL_USER_UNIT_RECENT", "unit_required", {},
        )
    lines = int(args.get("lines", JOURNAL_MAX_LINES))
    lines = max(1, min(lines, JOURNAL_MAX_LINES))
    argv = [
        "journalctl", "--user", "--no-pager", "-n", str(lines),
        "--output", "short", "-u", unit,
    ]
    rc, out, err = _argv_run(argv, timeout=DEFAULT_TIMEOUT_SECONDS)
    body, truncated = _bound_text(out, JOURNAL_MAX_CHARS, lines)
    return {
        "capability": "JOURNAL_USER_UNIT_RECENT",
        "unit": unit,
        "lines_requested": lines,
        "returncode": rc,
        "stderr": (err or "")[:512],
        "truncated": truncated,
        "body": body,
    }


def _cap_process_lookup(args: Dict[str, Any]) -> Dict[str, Any]:
    pattern = str(args.get("pattern", ""))
    if not pattern or len(pattern) > 200:
        return _item_error(
            "PROCESS_LOOKUP", "pattern_required", {"pattern": pattern},
        )
    found: List[Dict[str, Any]] = []
    try:
        for proc_dir in Path("/proc").iterdir():
            if not proc_dir.name.isdigit():
                continue
            try:
                cmdline_path = proc_dir / "cmdline"
                raw = cmdline_path.read_bytes().replace(b"\x00", b" ").decode(
                    "utf-8", errors="replace",
                ).strip()
            except (OSError, PermissionError):
                continue
            if pattern not in raw:
                continue
            try:
                with (proc_dir / "stat").open("rb") as handle:
                    stat_blob = handle.read().decode("utf-8", errors="replace")
                parts = stat_blob.split()
                pid = int(parts[0])
                state = parts[2] if len(parts) > 2 else "?"
            except (OSError, ValueError):
                pid = int(proc_dir.name)
                state = "?"
            found.append({
                "pid": pid,
                "state": state,
                "cmdline": raw[:512],
            })
            if len(found) >= 64:
                break
    except Exception as exc:
        return _item_error(
            "PROCESS_LOOKUP", "proc_walk_failed",
            {"error": f"{type(exc).__name__}:{str(exc)[:200]}"},
        )
    return {
        "capability": "PROCESS_LOOKUP",
        "pattern": pattern,
        "match_count": len(found),
        "processes": found,
    }


def _decode_proc_hex(hex_ip: str) -> str:
    try:
        if len(hex_ip) == 8:
            ip_bytes = bytes.fromhex(hex_ip)
            return ".".join(str(b) for b in reversed(ip_bytes))
        if len(hex_ip) == 32:
            ip_bytes = bytes.fromhex(hex_ip)
            groups = []
            for i in range(0, 16, 2):
                groups.append(
                    int.from_bytes(ip_bytes[i:i + 2], "big")
                )
            return ":".join(f"{group:x}" for group in groups)
    except ValueError:
        pass
    return hex_ip


def _cap_listening_ports(args: Dict[str, Any]) -> Dict[str, Any]:
    ports: List[Dict[str, Any]] = []
    for proto, path in (("tcp", "/proc/net/tcp"),
                         ("tcp6", "/proc/net/tcp6")):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            if parts[3] != "0A":
                continue
            local = parts[1]
            if ":" not in local:
                continue
            ip_hex, port_hex = local.rsplit(":", 1)
            try:
                port = int(port_hex, 16)
            except ValueError:
                continue
            ports.append({
                "protocol": proto,
                "port": port,
                "bind_local": _decode_proc_hex(ip_hex),
            })
    ports.sort(key=lambda entry: entry["port"])
    if len(ports) > 200:
        ports = ports[:200]
    return {
        "capability": "LISTENING_PORTS",
        "listening_count": len(ports),
        "ports": ports,
    }


def _is_local_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urllib.request.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return True
    return False


def _cap_local_http_get(args: Dict[str, Any]) -> Dict[str, Any]:
    url = str(args.get("url", ""))
    if not _is_local_url(url):
        return _item_error(
            "LOCAL_HTTP_GET", "non_local_url_rejected", {"url": url},
        )
    timeout = float(args.get("timeout", 5.0))
    timeout = max(0.5, min(timeout, 10.0))
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "aios-host-evidence"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
        body, truncated = _bound_text(
            raw, DEFAULT_MAX_CHARS, DEFAULT_MAX_LINES,
        )
        return {
            "capability": "LOCAL_HTTP_GET",
            "url": url,
            "status": status,
            "truncated": truncated,
            "body": body,
        }
    except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
        return _item_error(
            "LOCAL_HTTP_GET", "request_failed",
            {"url": url, "error": f"{type(exc).__name__}:{str(exc)[:200]}"},
        )
    except Exception as exc:
        return _item_error(
            "LOCAL_HTTP_GET", "request_error",
            {"url": url, "error": f"{type(exc).__name__}:{str(exc)[:200]}"},
        )


def _cap_local_http_post_health_probe(args: Dict[str, Any]) -> Dict[str, Any]:
    return _item_error(
        "LOCAL_HTTP_POST_HEALTH_PROBE",
        "in_process_handler_only",
        {"hint": "Use aios_host_readonly_evidence.handle_health_probe()."},
    )


# ---------------------------------------------------------------------------
# WEB_FETCH — bounded public-web fetcher for `independent-live` fact queries.
#
# This capability exists to satisfy the AIOS production evidence contract for
# GENERAL-profile tasks whose ``evidence_mode == "independent-live"`` (the
# "今天 / 当前 / 最新" family).  It is intentionally narrow:
#
#   * URL is matched against a hard-coded allowlist of public, well-known info
#     API hostnames.  No raw URL from the executor / verifier / planner /
#     model is accepted — only the canonical templates below can be fetched.
#   * The HTTPS request uses ``urllib.request`` (no shell, no curl) and is
#     bounded by ``timeout`` (1.5–6 s) plus DEFAULT byte/line caps.
#   * Failures return ``_item_error`` (do not raise).
#
# Production-curated public info hosts.  Adding a new host is a code change
# in this allowlist and goes through normal review.
# ---------------------------------------------------------------------------
_WEB_FETCH_ALLOWLIST: Tuple[Tuple[str, str], ...] = (
    # wttr.in — weather + astronomy (sunrise / sunset / moon phase).
    ("wttr.in",            "https://wttr.in/{location}?format=j1"),
    # open-meteo.com — public weather forecast (returns JSON).
    ("api.open-meteo.com", "https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code&daily=sunrise,sunset,uv_index_max,precipitation_sum&timezone=auto"),
    # ip-api.com — public IP geolocation.
    ("ip-api.com",         "http://ip-api.com/json/"),
    # api.github.com — public Zen quote.
    ("api.github.com",     "https://api.github.com/zen"),
    # api.ipify.org — public IP echo.
    ("api.ipify.org",      "https://api.ipify.org?format=json"),
)

_WEB_FETCH_LOCATION_DEFAULTS: Dict[str, Dict[str, str]] = {
    "beijing":  {"location": "Beijing",  "latitude": "39.9", "longitude": "116.4"},
    "shanghai": {"location": "Shanghai", "latitude": "31.2", "longitude": "121.5"},
    "hong_kong":{"location": "Hong Kong","latitude": "22.3", "longitude": "114.2"},
    "tokyo":    {"location": "Tokyo",    "latitude": "35.7", "longitude": "139.7"},
    "london":   {"location": "London",   "latitude": "51.5", "longitude": "-0.1"},
    "new_york": {"location": "New York", "latitude": "40.7", "longitude": "-74.0"},
    "_default": {"location": "Beijing",  "latitude": "39.9", "longitude": "116.4"},
}


def _resolve_web_fetch_target(goal: str) -> Tuple[str, str, str, str, str]:
    """Pick one canonical ``(hostname, template, location, url, latlon)``
    tuple appropriate for ``goal``.  The url is the template with the
    placeholders filled in via ``str.format``.
    """
    lowered = str(goal or "").lower()
    if any(t in lowered for t in ("beijing", "北京", "bj")):
        choice = _WEB_FETCH_LOCATION_DEFAULTS["beijing"]
    elif any(t in lowered for t in ("shanghai", "上海", "sh")):
        choice = _WEB_FETCH_LOCATION_DEFAULTS["shanghai"]
    elif any(t in lowered for t in ("hong kong", "香港", "hk")):
        choice = _WEB_FETCH_LOCATION_DEFAULTS["hong_kong"]
    elif any(t in lowered for t in ("tokyo", "东京", "東京", "tyo")):
        choice = _WEB_FETCH_LOCATION_DEFAULTS["tokyo"]
    elif any(t in lowered for t in ("london", "伦敦", "倫敦")):
        choice = _WEB_FETCH_LOCATION_DEFAULTS["london"]
    elif any(t in lowered for t in ("new york", "纽约", "紐約", "nyc")):
        choice = _WEB_FETCH_LOCATION_DEFAULTS["new_york"]
    else:
        choice = _WEB_FETCH_LOCATION_DEFAULTS["_default"]
    # wttr.in is always the first allowlist entry — smallest JSON, includes
    # sunrise/sunset/moon phase, so it is the most useful public source for
    # "astronomy / sky / 七星连珠" style questions without a search engine.
    hostname, template = _WEB_FETCH_ALLOWLIST[0]
    location = choice["location"]
    url = template.format(
        location=urllib.parse.quote(location),
        latitude=choice["latitude"],
        longitude=choice["longitude"],
    )
    latlon = f"{choice['latitude']},{choice['longitude']}"
    return hostname, template, location, url, latlon


def _cap_web_fetch(args: Dict[str, Any]) -> Dict[str, Any]:
    hostname = str(args.get("hostname", "") or "")
    template = str(args.get("template", "") or "")
    url = str(args.get("url", "") or "")
    location = str(args.get("location", "") or "")
    label = str(args.get("label", "") or "")
    if not hostname or not template or not url:
        return _item_error(
            "WEB_FETCH", "missing_target",
            {"args_keys": sorted(args.keys())},
        )
    # Allowlist re-check: hostname + template BOTH must be in the canonical
    # allowlist tuple, so even if the orchestrator is compromised an attacker
    # cannot redirect WEB_FETCH to an arbitrary URL.
    valid = any(
        h == hostname and t == template for h, t in _WEB_FETCH_ALLOWLIST
    )
    if not valid:
        return _item_error(
            "WEB_FETCH", "url_not_allowlisted",
            {"hostname": hostname, "template": template},
        )
    timeout = float(args.get("timeout", 4.0))
    timeout = max(1.5, min(timeout, 6.0))
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "aios-host-evidence/web-fetch-1.0"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
        return _item_error(
            "WEB_FETCH", "request_failed",
            {"url": url, "error": f"{type(exc).__name__}:{str(exc)[:200]}"},
        )
    except Exception as exc:
        return _item_error(
            "WEB_FETCH", "request_error",
            {"url": url, "error": f"{type(exc).__name__}:{str(exc)[:200]}"},
        )
    body, truncated = _bound_text(raw, DEFAULT_MAX_CHARS, DEFAULT_MAX_LINES)
    return {
        "capability": "WEB_FETCH",
        "hostname": hostname,
        "url": url,
        "location": location,
        "label": label or hostname,
        "status": status,
        "truncated": truncated,
        "retrieved_at": _utc_now_iso(),
        "body": body,
    }




def _cap_aios_health_snapshot(args: Dict[str, Any]) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "capability": "AIOS_HEALTH_SNAPSHOT",
        "sampled_at": _utc_now_iso(),
    }
    try:
        from aios_bus import get_queue_status
        snapshot["queue"] = get_queue_status()
    except Exception as exc:
        snapshot["queue_error"] = f"{type(exc).__name__}:{str(exc)[:200]}"
    try:
        from aios_bus import list_registered_executors
        registry = list_registered_executors()
        snapshot["executors"] = sorted(
            str(name) for name in (registry or {}).keys()
        )
    except Exception as exc:
        snapshot["executors_error"] = f"{type(exc).__name__}:{str(exc)[:200]}"
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:18801/health", timeout=3,
        ) as response:
            snapshot["gateway_health"] = json.loads(response.read())
    except Exception as exc:
        snapshot["gateway_health_error"] = (
            f"{type(exc).__name__}:{str(exc)[:200]}"
        )
    return snapshot


def _cap_aios_task_status(args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(args.get("task_id", ""))
    if not task_id:
        return _item_error("AIOS_TASK_STATUS", "task_id_required", {})
    try:
        from aios_bus import get_task_state
        state = get_task_state(task_id)
    except Exception as exc:
        return _item_error(
            "AIOS_TASK_STATUS", "state_unavailable",
            {"task_id": task_id, "error": f"{type(exc).__name__}:{str(exc)[:200]}"},
        )
    if not state:
        return {
            "capability": "AIOS_TASK_STATUS",
            "task_id": task_id,
            "found": False,
        }
    allowed_keys = (
        "status", "executor", "plan_mode", "verification_passed",
        "reviewer", "error", "completed_at", "created_at", "updated_at",
    )
    clean = {k: state.get(k) for k in allowed_keys if k in state}
    return {
        "capability": "AIOS_TASK_STATUS",
        "task_id": task_id,
        "found": True,
        "state": clean,
    }


def _dispatch(capability: str, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if capability == "READ_FILE":
            return _cap_read_file(args)
        if capability == "LIST_DIRECTORY":
            return _cap_list_directory(args)
        if capability == "FILE_METADATA":
            return _cap_file_metadata(args)
        if capability == "GIT_STATUS":
            return _cap_git(args, "status")
        if capability == "GIT_LOG":
            return _cap_git(args, "log")
        if capability == "GIT_BRANCH":
            return _cap_git(args, "branch")
        if capability == "GIT_HEAD":
            return _cap_git(args, "rev-parse")
        if capability == "SYSTEMD_USER_STATUS":
            return _cap_systemd_user(args, "status")
        if capability == "SYSTEMD_USER_SHOW":
            return _cap_systemd_user(args, "show")
        if capability == "SYSTEMD_USER_FAILED":
            return _cap_systemd_user(args, "failed")
        if capability == "JOURNAL_USER_UNIT_RECENT":
            return _cap_journal_user_unit_recent(args)
        if capability == "PROCESS_LOOKUP":
            return _cap_process_lookup(args)
        if capability == "LISTENING_PORTS":
            return _cap_listening_ports(args)
        if capability == "LOCAL_HTTP_GET":
            return _cap_local_http_get(args)
        if capability == "LOCAL_HTTP_POST_HEALTH_PROBE":
            return _cap_local_http_post_health_probe(args)
        if capability == "WEB_FETCH":
            return _cap_web_fetch(args)
        if capability == "WEB_DISCOVERY_FETCH":
            return _cap_web_discovery_fetch(args)
        if capability == "AIOS_HEALTH_SNAPSHOT":
            return _cap_aios_health_snapshot(args)
        if capability == "AIOS_TASK_STATUS":
            return _cap_aios_task_status(args)
    except Exception as exc:
        return _item_error(
            capability, "exception",
            {"error": f"{type(exc).__name__}:{str(exc)[:200]}"},
        )
    return _item_error(capability, "unknown_capability", {})


def profile_capabilities(profile: str) -> Tuple[str, ...]:
    return PROFILE_CAPABILITIES.get(profile, ())


def audit_runtime_augment(goal: str) -> Tuple[str, ...]:
    lowered = str(goal or "").lower()
    if any(token in lowered for token in _AUDIT_RUNTIME_HINTS):
        return PROFILE_CAPABILITIES["OPS"]
    return ()


def fresh_fact_augment(goal: str) -> Tuple[str, ...]:
    """Return capabilities to run when ``goal`` contains fresh-fact hints.

    Mirrors ``_classify_evidence_mode(goal, …)`` in ``aios_orchestrator.py``:
    when the planner flags the goal as ``independent-live``, the host-evidence
    pass auto-attaches the generic ``WEB_DISCOVERY_FETCH`` capability so the
    production evidence gate has a non-empty ``items[]`` before the executor
    / verifier runs.

    The capability is the generic search-then-fetch against the existing
    OpenClaw gateway loopback (provider: ``minimax`` CN endpoint,
    ``web_fetch`` SSRF guard).  It is the SINGLE source of fresh-fact
    evidence — no static / hardcoded allowlist fallback is auto-attached,
    so every independent-live query surfaces a URL that was actually
    discovered for the user's question rather than a hardcoded wttr.in
    / open-meteo / ip-api hit.

    Plain semantic queries (e.g. ``"1+1等于多少"``) return ``()`` — no
    unnecessary network traffic for tasks that don't need it.

    2026-08-17 P1 evidence-mode-fix: for queries about AIOS itself
    ("能正常用吗", "现在能用吗", "is it working"), ALSO auto-attach
    ``AIOS_HEALTH_SNAPSHOT`` so the executor has the real /status
    payload to quote (not a fabricated "NORMAL" from a single web
    fetch).  This eliminates the verifier disagreement where the
    executor claims NORMAL while /status shows degraded executors.
    """
    out = []
    lowered = str(goal or "").lower()
    if any(token in lowered for token in _FRESH_FACT_HINTS):
        out.append("WEB_DISCOVERY_FETCH")
    if any(token in lowered for token in (
        "能不能用", "能用吗", "用不了", "正常用", "现在能", "还能用",
        "work", "broken", "stuck", "fix", "issue",
    )):
        # 2026-08-17 P1 evidence-mode-fix: when the user is asking
        # whether AIOS works, attach AIOS_HEALTH_SNAPSHOT so the
        # executor can quote the real runtime status instead of
        # fabricating a verdict from web-only evidence.
        out.append("AIOS_HEALTH_SNAPSHOT")
    return tuple(out)
    lowered = str(goal or "").lower()
    if any(token in lowered for token in _FRESH_FACT_HINTS):
        return ("WEB_DISCOVERY_FETCH",)
    return ()


def _default_args_for(
    capability: str,
    *,
    profile: str,
    goal: str,
    project_path: Optional[str],
    allowed_roots: List[str],
    explicit_units: Iterable[str],
    explicit_task_id: Optional[str],
) -> Dict[str, Any]:
    base = {"allowed_roots": list(allowed_roots)}
    if capability in ("READ_FILE", "FILE_METADATA"):
        base["path"] = project_path or ""
    elif capability == "LIST_DIRECTORY":
        base["path"] = project_path or ""
    elif capability in ("GIT_STATUS", "GIT_LOG", "GIT_BRANCH", "GIT_HEAD"):
        base["cwd"] = project_path or ""
        base["extra_args"] = []
    elif capability in ("SYSTEMD_USER_STATUS", "SYSTEMD_USER_SHOW"):
        units = list(explicit_units)
        base["unit"] = (
            units[0] if units else "aios-orchestrator.service"
        )
    elif capability == "JOURNAL_USER_UNIT_RECENT":
        units = list(explicit_units)
        base["unit"] = (
            units[0] if units else "aios-orchestrator.service"
        )
        base["lines"] = 120
    elif capability == "PROCESS_LOOKUP":
        base["pattern"] = "aios_orchestrator.py"
    elif capability == "LOCAL_HTTP_GET":
        base["url"] = "http://127.0.0.1:18801/health"
        base["timeout"] = 3.0
    elif capability == "WEB_FETCH":
        # Resolve the canonical (hostname, template, location, url, latlon)
        # tuple for ``goal``.  The template is always one of the entries in
        # _WEB_FETCH_ALLOWLIST, so the dispatcher's allowlist re-check will
        # accept it.  ``label`` carries the human-readable location so the
        # executor prompt can quote it back to the user.
        hostname, template, location, url, _latlon = _resolve_web_fetch_target(
            goal,
        )
        base["hostname"] = hostname
        base["template"] = template
        base["url"] = url
        base["location"] = location
        base["label"] = location
        base["timeout"] = 4.0
    elif capability == "WEB_DISCOVERY_FETCH":
        # Use the raw goal as the search query; the executor / verifier
        # can also pass ``query`` explicitly through ``args`` overrides
        # downstream if a more specific search term is needed.  ``count``
        # and ``max_chars`` are bounded by the capability constants so
        # the host process never pulls more than 5 candidates or
        # 20000 chars of page text per call.
        base["query"] = goal or ""
        base["count"] = _WEB_DISCOVERY_DEFAULT_COUNT
        base["max_chars"] = _WEB_DISCOVERY_DEFAULT_MAX_CHARS
        base["hostname_filter"] = ""
    elif capability == "AIOS_TASK_STATUS":
        base["task_id"] = explicit_task_id or ""
    return base


def _summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "total": len(items),
        "ok": 0,
        "error": 0,
        "by_capability": {},
    }
    for item in items:
        cap = str(item.get("capability", "?"))
        ok = "error" not in item
        summary["by_capability"][cap] = "ok" if ok else "error"
        if ok:
            summary["ok"] += 1
        else:
            summary["error"] += 1
    return summary


def collect_host_evidence(
    profile: str,
    goal: str = "",
    *,
    project_path: Optional[str] = None,
    explicit_units: Iterable[str] = (),
    explicit_task_id: Optional[str] = None,
    max_total_items: int = 24,
) -> Dict[str, Any]:
    """Synchronously gather the host evidence for the given profile."""
    profile_norm = (profile or "GENERAL").upper()
    caps = list(profile_capabilities(profile_norm))
    caps.extend(audit_runtime_augment(goal))
    # Auto-attach WEB_DISCOVERY_FETCH when the goal text contains
    # fresh-fact tokens ("今天", "current", "weather", etc.).  Mirrors the
    # orchestrator's ``_classify_evidence_mode`` so the host-evidence pass
    # runs in lockstep with the planner's
    # ``evidence_mode == "independent-live"`` decision.  No static
    # / hardcoded allowlist fallback is attached.
    caps.extend(fresh_fact_augment(goal))
    if explicit_units:
        if "SYSTEMD_USER_STATUS" not in caps:
            caps.append("SYSTEMD_USER_STATUS")
        if "JOURNAL_USER_UNIT_RECENT" not in caps:
            caps.append("JOURNAL_USER_UNIT_RECENT")
    if explicit_task_id and "AIOS_TASK_STATUS" not in caps:
        caps.append("AIOS_TASK_STATUS")
    seen = set()
    deduped = []
    for cap in caps:
        if cap not in seen:
            seen.add(cap)
            deduped.append(cap)
    caps = deduped[:max_total_items]

    allowed_roots = _resolve_allowed_roots(project_path)
    items: List[Dict[str, Any]] = []
    for capability in caps:
        args = _default_args_for(
            capability,
            profile=profile_norm,
            goal=goal,
            project_path=project_path,
            allowed_roots=allowed_roots,
            explicit_units=explicit_units,
            explicit_task_id=explicit_task_id,
        )
        items.append(_dispatch(capability, args))
    return {
        "profile": profile_norm,
        "generated_at": _utc_now_iso(),
        "items": items,
        "summary": _summary(items),
    }


def render_evidence_block(evidence: Dict[str, Any]) -> str:
    payload = {
        "source": "aios_host_readonly_evidence",
        "evidence": evidence.get("items", []),
        "summary": evidence.get("summary", {}),
        "profile": evidence.get("profile", ""),
        "generated_at": evidence.get("generated_at", ""),
    }
    return (
        "<!--AIOS_HOST_EVIDENCE-->\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
        + "\n<!--/AIOS_HOST_EVIDENCE-->"
    )


def handle_health_probe(args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "aios-host-readonly-evidence",
        "generated_at": _utc_now_iso(),
        "args": {k: args.get(k) for k in (
            "capability", "unit", "lines", "pattern",
        ) if k in args},
    }


__all__ = [
    "PROFILE_CAPABILITIES",
    "collect_host_evidence",
    "render_evidence_block",
    "handle_health_probe",
    "profile_capabilities",
    "audit_runtime_augment",
]