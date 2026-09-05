#!/usr/bin/env python3
"""AIOS P8B-R MiniMax Shared Resource Usage Ledger.

The P8B-R design separates **account-scoped** usage (one
``minimax.shared`` quota pool shared by all bindings to that
account) from **tool binding-scoped** usage (per-tool ledger
kept by each adapter). The ledger module owns the account-
scoped side and exposes helpers for both readers.

Why this exists:

* P8A's earlier audits conflated Hermes and OpenClaw token
  counts, which produced incorrect account-level rollups.
  This module starts from an explicit zero baseline and only
  counts events that flow through the P8B-R adapters.
* Historical ``aggregate_minimax_tokens`` scanned OpenClaw
  session files; that historical file is *not* mixed in here.
  The historical file is only consulted for the
  ``MINIMAX_HISTORICAL_BASELINE`` field so monitors can show
  the previous-day total without polluting the current-run
  counter.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from aios_claude_minimax_adapter import ClaudeBindingUsageSummary
from aios_codex_minimax_adapter import CodexBindingUsageSummary
from aios_hermes_minimax_verifier import HermesBindingUsageSummary
from aios_openclaw_planner_verifier import OpenClawPlannerUsageSummary


LEDGER_RESOURCE_ID = "minimax.shared"
HISTORICAL_BASELINE_PATH = (
    "${AIOS_HOME}/cache/tool_health/openclaw.json"
)


@dataclass
class MiniMaxAccountUsage:
    """Account-scoped rollup.

    ``historical_baseline`` is the previous-day MiniMax token
    total scanned from the legacy OpenClaw session files.
    It is reported for context but NEVER mixed with the
    current-run counters; the P8B-R design rule is that
    historical totals are an observation, not active quota.
    """

    resource_id: str = LEDGER_RESOURCE_ID
    current_run_calls: int = 0
    current_run_successes: int = 0
    current_run_failures: int = 0
    current_run_input_tokens: int = 0
    current_run_output_tokens: int = 0
    current_run_total_tokens: int = 0
    current_run_estimated_cost: float = 0.0
    last_call_at: Optional[str] = None
    by_binding_calls: Dict[str, int] = field(default_factory=dict)
    by_binding_tokens: Dict[str, int] = field(default_factory=dict)
    historical_baseline_total: int = 0
    historical_baseline_source: str = ""
    historical_baseline_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MiniMaxAccountLedger:
    """Thread-safe account-scoped ledger.

    A single instance is shared by all four MiniMax adapters
    so the account total is the sum of the per-binding
    counters. The instance owns its lock; it never mutates
    module-level state.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._usage = MiniMaxAccountUsage()
        self._historical_baseline_loaded = False

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def ingest_claude(self, summary: ClaudeBindingUsageSummary) -> None:
        self._ingest("claude:minimax", summary.calls, summary.total_tokens,
                     summary.estimated_cost,
                     summary.successes, summary.failures,
                     last_call_at=summary.last_call_at)

    def ingest_codex(self, summary: CodexBindingUsageSummary) -> None:
        self._ingest("codex:minimax", summary.calls, summary.total_tokens,
                     summary.estimated_cost,
                     summary.successes, summary.failures,
                     last_call_at=summary.last_call_at)

    def ingest_hermes(self, summary: HermesBindingUsageSummary) -> None:
        self._ingest("hermes:minimax", summary.calls, summary.total_tokens,
                     summary.estimated_cost,
                     summary.successes, summary.failures,
                     last_call_at=summary.last_call_at)

    def ingest_openclaw(self, summary: OpenClawPlannerUsageSummary) -> None:
        self._ingest("openclaw:minimax", summary.calls, summary.total_tokens,
                     summary.estimated_cost,
                     summary.successes, summary.failures,
                     last_call_at=summary.last_call_at)

    def _ingest(self, binding_id: str, calls: int, tokens: int,
                cost: float, successes: int, failures: int,
                *, last_call_at: Optional[str]) -> None:
        with self._lock:
            self._usage.current_run_calls += int(calls)
            self._usage.current_run_successes += int(successes)
            self._usage.current_run_failures += int(failures)
            self._usage.current_run_total_tokens += int(tokens)
            self._usage.current_run_estimated_cost += float(cost)
            self._usage.by_binding_calls[binding_id] = (
                self._usage.by_binding_calls.get(binding_id, 0) + int(calls))
            self._usage.by_binding_tokens[binding_id] = (
                self._usage.by_binding_tokens.get(binding_id, 0) + int(tokens))
            if last_call_at and (not self._usage.last_call_at
                                  or last_call_at > self._usage.last_call_at):
                self._usage.last_call_at = last_call_at

    # ------------------------------------------------------------------
    # Historical baseline (separate channel)
    # ------------------------------------------------------------------

    def load_historical_baseline(self) -> Tuple[int, str, Optional[str]]:
        """Scan the legacy OpenClaw cache and store the prior
        day's MiniMax token total.

        The baseline is an OBSERVATION; the current-run counter
        MUST NOT include it. The two channels are kept separate
        so the user can see "previous day totals" without
        polluting the current-run gate.
        """
        with self._lock:
            if self._historical_baseline_loaded:
                return (self._usage.historical_baseline_total,
                        self._usage.historical_baseline_source,
                        self._usage.historical_baseline_at)
            total = 0
            source = ""
            ts = None
            if os.path.exists(HISTORICAL_BASELINE_PATH):
                try:
                    payload = json.loads(
                        open(HISTORICAL_BASELINE_PATH, encoding="utf-8").read())
                    total = int(payload.get("latency_ms", 0) or 0)
                    source = HISTORICAL_BASELINE_PATH
                    ts = str(payload.get("checked_at", "")) or None
                except Exception:
                    total = 0
            self._usage.historical_baseline_total = total
            self._usage.historical_baseline_source = source
            self._usage.historical_baseline_at = ts
            self._historical_baseline_loaded = True
            return (total, source, ts)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> MiniMaxAccountUsage:
        with self._lock:
            return MiniMaxAccountUsage(
                resource_id=self._usage.resource_id,
                current_run_calls=self._usage.current_run_calls,
                current_run_successes=self._usage.current_run_successes,
                current_run_failures=self._usage.current_run_failures,
                current_run_input_tokens=self._usage.current_run_input_tokens,
                current_run_output_tokens=self._usage.current_run_output_tokens,
                current_run_total_tokens=self._usage.current_run_total_tokens,
                current_run_estimated_cost=self._usage.current_run_estimated_cost,
                last_call_at=self._usage.last_call_at,
                by_binding_calls=dict(self._usage.by_binding_calls),
                by_binding_tokens=dict(self._usage.by_binding_tokens),
                historical_baseline_total=self._usage.historical_baseline_total,
                historical_baseline_source=self._usage.historical_baseline_source,
                historical_baseline_at=self._usage.historical_baseline_at,
            )


def ledger_to_tsv(snapshot: MiniMaxAccountUsage) -> str:
    lines = [
        "\t".join(["resource_id", "current_run_calls",
                    "current_run_successes", "current_run_failures",
                    "current_run_total_tokens",
                    "current_run_estimated_cost",
                    "historical_baseline_total",
                    "historical_baseline_source",
                    "historical_baseline_at", "last_call_at"])
    ]
    lines.append("\t".join([
        snapshot.resource_id,
        str(snapshot.current_run_calls),
        str(snapshot.current_run_successes),
        str(snapshot.current_run_failures),
        str(snapshot.current_run_total_tokens),
        f"{snapshot.current_run_estimated_cost:.6f}",
        str(snapshot.historical_baseline_total),
        snapshot.historical_baseline_source,
        snapshot.historical_baseline_at or "",
        snapshot.last_call_at or "",
    ]))
    for binding_id in sorted(snapshot.by_binding_calls):
        lines.append("\t".join([
            "binding=" + binding_id,
            str(snapshot.by_binding_calls[binding_id]),
            "", "",
            str(snapshot.by_binding_tokens.get(binding_id, 0)),
            "", "", "", "", "",
        ]))
    return "\n".join(lines) + "\n"


__all__ = [
    "LEDGER_RESOURCE_ID", "HISTORICAL_BASELINE_PATH",
    "MiniMaxAccountUsage", "MiniMaxAccountLedger", "ledger_to_tsv",
]