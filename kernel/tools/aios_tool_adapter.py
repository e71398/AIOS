#!/usr/bin/env python3
"""Stable, configuration-driven health contract for replaceable AI tool chips."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HOME = Path(os.getenv("AIOS_HOME", "${AIOS_HOME}")).resolve()
CONFIG = HOME / "config/tool_adapters.json"
CACHE = HOME / "cache/tool_health"
SUPPORTED_CONTRACTS = {"1.0", "1.1"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _safe_text(value: str, limit: int = 500) -> str:
    value = re.sub(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+", r"\1=[redacted]", value)
    return value.strip()[-limit:]


def _classify_failure(text: str, returncode: int) -> tuple[str, str]:
    lower = text.lower()
    if any(x in lower for x in ("token plan", "quota", "insufficient", "usage limit", "用量上限", "余额不足")):
        return "quota_exhausted", "模型额度已用尽"
    if "429" in lower or "rate limit" in lower or "too many requests" in lower:
        return "rate_limited", "模型供应商限流"
    if any(x in lower for x in ("unauthorized", "invalid api key", "authentication", "401", "403")):
        return "auth_failed", "模型凭证无效或无权限"
    if any(x in lower for x in ("timed out", "timeout", "connection", "network", "dns")):
        return "network_error", "模型网络不可用"
    return "probe_failed", f"推理探针失败(rc={returncode})"


@dataclass(frozen=True)
class ToolAdapter:
    name: str
    config: dict[str, Any]

    @property
    def executable(self) -> str:
        return str(self.config["executable"])

    @property
    def cache_file(self) -> Path:
        return CACHE / f"{self.name}.json"

    def command_for_task(self, task: str) -> list[str]:
        args = [task if item == "{task}" else str(item)
                for item in self.config.get("task_args", ["{task}"])]
        return [self.executable, *args]

    def _binary_health(self, timeout: int = 30) -> dict[str, Any]:
        if not self.config.get("enabled", False):
            return {"binary_ok": False, "binary_state": "disabled", "tool": self.name}
        if not Path(self.executable).is_file():
            return {"binary_ok": False, "binary_state": "missing", "tool": self.name}
        try:
            env = dict(os.environ)
            tool_dir = str(Path(self.executable).parent)
            env["PATH"] = os.pathsep.join(
                [tool_dir, "${HOME}/.n/bin", "${HOME}/.local/bin",
                 "/usr/local/bin", "/usr/bin", "/bin"]
            )
            result = subprocess.run(
                [self.executable, *self.config.get("version_args", ["--version"])],
                capture_output=True, text=True, timeout=timeout, shell=False, env=env,
            )
            lines = (result.stdout or result.stderr).strip().splitlines()
            return {"binary_ok": result.returncode == 0,
                    "binary_state": "ready" if result.returncode == 0 else "error",
                    "tool": self.name, "version": lines[0] if lines else "unknown"}
        except Exception as exc:
            return {"binary_ok": False, "binary_state": "error", "tool": self.name,
                    "error": f"{type(exc).__name__}: {exc}"}

    def cached_probe(self) -> dict[str, Any]:
        if not self.config.get("inference_required", True):
            data = {"model_state": "not_required", "model_available": True,
                    "probe_required": False}
        elif not self.cache_file.is_file():
            data = {"model_state": "unverified", "model_available": False,
                    "probe_required": True, "reason": "尚未执行真实推理探针"}
        else:
            try:
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            except Exception:
                data = {"model_state": "unverified", "model_available": False,
                        "probe_required": True, "reason": "探针缓存损坏"}
            else:
                marker = str(self.config.get("probe_success_marker", "AIOS_OK"))
                evidence = str(data.get("evidence", ""))
                marker_seen = data.get("success_marker_seen")
                if marker_seen is None:
                    marker_seen = marker in evidence
                fatal_error_seen = data.get("fatal_error_seen")
                if fatal_error_seen is None:
                    fatal_error_seen = any(
                        x in evidence.lower()
                        for x in ("api call failed", "authentication failed", "quota exceeded")
                    )
                if data.get("model_available") and (not marker_seen or fatal_error_seen):
                    state, reason = _classify_failure(evidence, int(data.get("returncode", 1)))
                    data.update({"model_available": False, "model_state": state,
                                 "reason": reason, "corrected_from_false_positive": True})
                checked = _parse_ts(data.get("checked_at"))
                max_age = int(self.config.get("probe_max_age_seconds", 7200))
                data["stale"] = not checked or datetime.now(timezone.utc) - checked > timedelta(seconds=max_age)
                if data["stale"]:
                    data["model_available"] = False
                    # P7F errata: preserve the original failure ownership
                    # bucket when the cache is stale. The original
                    # ``model_state`` (e.g. ``quota_exhausted``) reflects the
                    # real reason the tool was last observed as degraded;
                    # collapsing it into a generic ``stale`` would silently
                    # attribute an external provider failure to local
                    # staleness. ``evidence_freshness`` carries the staleness
                    # independently.
                    data["evidence_freshness"] = "STALE"
                    if not data.get("model_state") or data.get("model_state") == "available":
                        data["model_state"] = "stale"
                    data["reason"] = "真实推理结果已过期"
                data["probe_required"] = True
        # P9B: merge lightweight health (separate freshness dim).
        # The lightweight record is written by aios_health_publisher
        # independently of the (expensive) inference probe; both paths
        # share the same cache file as the single source of truth.
        if self.cache_file.is_file():
            try:
                _lw = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(_lw, dict):
                    for k in (
                        "lightweight_observed_at",
                        "lightweight_checked_at",
                        "lightweight_last_success_at",
                        "lightweight_expires_at",
                        "lightweight_fresh",
                        "lightweight_reachable",
                        "lightweight_protocol_ready",
                        "lightweight_failure_scope",
                        "lightweight_reason",
                        "lightweight_kind",
                        "lightweight_latency_ms",
                    ):
                        if k in _lw:
                            data[k] = _lw[k]
            except Exception:
                pass
        # Expose P9B semantic aliases at the top level.
        data["reachable"] = data.get("lightweight_reachable")
        data["protocol_ready"] = data.get("lightweight_protocol_ready")
        data["lightweight_fresh"] = bool(data.get("lightweight_fresh"))
        data["lightweight_checked_at"] = data.get("lightweight_checked_at")
        data["observed_at"] = data.get("lightweight_observed_at")
        # ``inference_verified`` reflects the *inference* path, not the
        # lightweight one; map ``model_available AND not stale AND
        # success_marker_seen`` to True.
        model_avail = bool(data.get("model_available"))
        stale = bool(data.get("stale"))
        success_marker_seen = bool(data.get("success_marker_seen"))
        if data.get("model_state") == "not_required":
            data["inference_verified"] = True
        else:
            data["inference_verified"] = model_avail and (not stale) and success_marker_seen
        # Failure scope priority: lightweight first (service), then
        # inference (provider class).  The model_state field is the
        # authoritative inference-side ownership bucket; we map it to
        # the canonical ``provider_*`` enum so the orchestrator /
        # monitoring does not misclassify a Provider-side failure as a
        # local Tool Process failure.  ``tool_process`` is the *last*
        # default only when no lighter classification applies.
        inference_scope_map = {
            "auth_failed": "provider_auth",
            "quota_exhausted": "provider_quota",
            "rate_limited": "provider_quota",
            "network_error": "provider_network",
            "model_error": "provider_model",
            "probe_failed": "provider_model",
        }
        model_state = str(data.get("model_state", "") or "")
        inference_scope = inference_scope_map.get(model_state)
        if data.get("stale"):
            inference_scope = "health_stale"
        if model_avail and not inference_scope:
            inference_scope = "unknown"
        if not model_avail and not inference_scope:
            inference_scope = "tool_process"
        lightweight_scope = data.get("lightweight_failure_scope")
        if lightweight_scope and lightweight_scope not in ("unknown",):
            data["failure_scope"] = lightweight_scope
        else:
            data["failure_scope"] = inference_scope
        return data

    def probe(self, force: bool = False) -> dict[str, Any]:
        if not self.config.get("inference_required", True):
            return self.cached_probe()
        args = self.config.get("probe_args")
        if not isinstance(args, list) or not args:
            result = {"checked_at": now(), "model_state": "unverified",
                      "model_available": False, "reason": "未配置真实推理探针",
                      "probe_required": True}
            self._write_probe(result)
            return result
        cached = self.cached_probe()
        retry_after = _parse_ts(cached.get("retry_after"))
        if not force and retry_after and retry_after > datetime.now(timezone.utc):
            cached["skipped"] = "backoff"
            return cached
        prompt = str(self.config.get("probe_prompt", "仅回复 AIOS_OK"))
        command = [self.executable, *[prompt if str(x) == "{prompt}" else str(x) for x in args]]
        env = dict(os.environ)
        env["PATH"] = "${HOME}/.n/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
        timeout = int(self.config.get("probe_timeout_seconds", 90))
        started = datetime.now(timezone.utc)
        try:
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, shell=False, env=env, cwd=str(HOME / "sandbox"),
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                raise
            output = (stdout or "") + "\n" + (stderr or "")
            marker = str(self.config.get("probe_success_marker", "AIOS_OK"))
            success_marker_seen = marker in output
            fatal_error_seen = any(x in output.lower() for x in
                ("api call failed", "authentication failed", "quota exceeded"))
            success = proc.returncode == 0 and success_marker_seen and not fatal_error_seen
            if success:
                state, reason = "available", "真实推理成功"
            else:
                state, reason = _classify_failure(output, proc.returncode)
            result = {"checked_at": now(), "latency_ms": int((datetime.now(timezone.utc)-started).total_seconds()*1000),
                      "model_state": state, "model_available": success, "reason": reason,
                      "returncode": proc.returncode, "evidence": _safe_text(output),
                      "success_marker_seen": success_marker_seen,
                      "fatal_error_seen": fatal_error_seen,
                      "probe_required": True}
        except subprocess.TimeoutExpired as exc:
            result = {"checked_at": now(), "latency_ms": timeout * 1000,
                      "model_state": "timeout", "model_available": False,
                      "reason": "真实推理探针超时", "evidence": _safe_text(str(exc)),
                      "probe_required": True}
        except Exception as exc:
            result = {"checked_at": now(), "model_state": "probe_error",
                      "model_available": False, "reason": f"{type(exc).__name__}: {exc}",
                      "probe_required": True}
        if not result["model_available"]:
            backoff = int(self.config.get("probe_failure_backoff_seconds", 21600))
            result["retry_after"] = (datetime.now(timezone.utc)+timedelta(seconds=backoff)).isoformat()
        self._write_probe(result)
        return result

    def _write_probe(self, result: dict[str, Any]) -> None:
        # P9B: preserve the lightweight-health fields written by
        # :mod:`aios_health_publisher` so the publisher's frequent
        # ``lightweight_*`` observations are not blown away by the
        # slower ``probe()`` / ``record_inference_*`` paths.  The
        # cache file is the single source of truth and both paths
        # write into it; the inference path owns its own slice.
        CACHE.mkdir(parents=True, exist_ok=True)
        preserved: dict[str, Any] = {}
        if self.cache_file.is_file():
            try:
                existing = json.loads(
                    self.cache_file.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    for k, v in existing.items():
                        if k.startswith("lightweight_") or k in (
                            "observed_at", "inference_verified",
                            "reachable", "protocol_ready", "failure_scope",
                        ):
                            preserved[k] = v
            except Exception:
                pass
        merged: dict[str, Any] = {**preserved, **result}
        temp = self.cache_file.with_suffix(".tmp")
        temp.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.replace(temp, self.cache_file)

    def record_inference_success(self, evidence: str, latency_ms: int = 0) -> dict[str, Any]:
        """Record a successful real task as fresh inference-health evidence."""
        result = {
            "checked_at": now(),
            "latency_ms": max(0, int(latency_ms)),
            "model_state": "available",
            "model_available": True,
            "reason": "真实任务推理成功",
            "returncode": 0,
            "evidence": _safe_text(evidence),
            "success_marker_seen": True,
            "fatal_error_seen": False,
            "probe_required": True,
            "evidence_source": "real_task",
        }
        self._write_probe(result)
        return result

    def record_inference_failure(self, evidence: str, returncode: int = 1,
                                 latency_ms: int = 0) -> dict[str, Any]:
        """Immediately remove a tool from dispatch after a real task failure."""
        state, reason = _classify_failure(evidence, returncode)
        backoff = int(self.config.get("probe_failure_backoff_seconds", 21600))
        result = {
            "checked_at": now(),
            "latency_ms": max(0, int(latency_ms)),
            "model_state": state,
            "model_available": False,
            "reason": reason,
            "returncode": int(returncode),
            "evidence": _safe_text(evidence),
            "success_marker_seen": False,
            "fatal_error_seen": True,
            "probe_required": True,
            "evidence_source": "real_task",
            "retry_after": (
                datetime.now(timezone.utc) + timedelta(seconds=backoff)
            ).isoformat(),
        }
        self._write_probe(result)
        return result

    def health(self, timeout: int = 30) -> dict[str, Any]:
        binary = self._binary_health(timeout)
        model = self.cached_probe()
        contract_ok = bool(binary.get("binary_ok"))
        # P9B semantic: a tool is production eligible only when the
        # *lightweight* probe says the service is reachable + protocol
        # ready *and* the *inference* canary either is fresh or is not
        # required.  This splits the freshness dim from the inference
        # verification dim and never hard-codes ``fully_operational``.
        inference_required = bool(self.config.get("inference_required", True))
        lightweight_ok = bool(model.get("lightweight_reachable")) and bool(
            model.get("lightweight_protocol_ready"))
        inference_verified = bool(model.get("inference_verified"))
        inference_path_ok = (not inference_required) or inference_verified
        fully_operational = contract_ok and lightweight_ok and inference_path_ok
        if fully_operational:
            state = "operational"
        elif contract_ok and (lightweight_ok or inference_path_ok):
            state = "degraded"
        elif contract_ok:
            state = "stale"
        else:
            state = binary.get("binary_state", "error") or "error"
        # ``fresh`` = the lightweight record is fresh and (if required)
        # the inference record is also fresh.
        fresh = bool(model.get("lightweight_fresh")) and (
            (not inference_required) or (not bool(model.get("stale"))))
        # ``infrastructure_ok`` keeps its historical meaning (binary +
        # no maintenance marker); the orchestrator uses it for the
        # legacy "did the service start?" check.
        infrastructure_ok = contract_ok
        return {**binary, **model,
                "contract_ok": contract_ok,
                "infrastructure_ok": infrastructure_ok,
                "fully_operational": fully_operational,
                "ok": fully_operational,
                "fresh": fresh,
                "reachable": bool(model.get("lightweight_reachable")),
                "protocol_ready": bool(model.get("lightweight_protocol_ready")),
                "inference_verified": inference_verified,
                "state": state,
                "label": self.config.get("label", self.name),
                "model": self.config.get("model", "unknown"),
                "provider": self.config.get("provider", "unknown")}


def registered_adapter_names(path: Path = CONFIG) -> frozenset[str]:
    """Return the set of registered adapter names without constructing them.

    SEC-PROBE-01 hardening: this function must be a pure read of the JSON
    registry (no ToolAdapter construction, no secret read, no subprocess,
    no cache write). Callers can use it to gate probe execution by name
    before paying for adapter construction or any per-adapter side effect.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if str(data.get("contract_version")) not in SUPPORTED_CONTRACTS:
        raise ValueError("unsupported tool adapter contract")
    tools = data.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise ValueError("tool adapter registry is empty")
    return frozenset(tools.keys())


def load_adapters(path: Path = CONFIG) -> dict[str, ToolAdapter]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if str(data.get("contract_version")) not in SUPPORTED_CONTRACTS:
        raise ValueError("unsupported tool adapter contract")
    tools = data.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise ValueError("tool adapter registry is empty")
    return {name: ToolAdapter(name, cfg) for name, cfg in tools.items()}


def get_adapter(name: str) -> ToolAdapter:
    adapters = load_adapters()
    if name not in adapters:
        raise KeyError(f"unregistered tool adapter: {name}")
    return adapters[name]


def health_all() -> dict[str, dict[str, Any]]:
    return {name: adapter.health() for name, adapter in load_adapters().items()}


def probe_all(force: bool = False) -> dict[str, dict[str, Any]]:
    result = {}
    for name, adapter in load_adapters().items():
        result[name] = adapter.probe(force=force)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    probe = sub.add_parser("probe")
    probe.add_argument("tool", nargs="?", default="all")
    probe.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.cmd == "health":
        result = health_all()
    elif args.tool == "all":
        result = probe_all(force=args.force)
    else:
        result = {args.tool: get_adapter(args.tool).probe(force=args.force)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
