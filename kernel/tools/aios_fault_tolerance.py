#!/usr/bin/env python3
"""Retired fault-tolerance compatibility command.

Execution failover, bounded repair and timeout recovery are owned exclusively
by aios_orchestrator.  This module is intentionally read-only and cannot
requeue tasks or alter executor lifecycle state.
"""
import json
import sys

from aios_bus import get_queue_status


def health():
    return {
        "ok": True,
        "status": "retired-compatibility",
        "owner": "aios-orchestrator",
        "capability": "bounded repair and executor failover",
        "queue": get_queue_status(),
    }


if __name__ == "__main__":
    if len(sys.argv) == 1 or sys.argv[1] in ("health", "--status"):
        print(json.dumps(health(), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    print("Refused: recovery mutations are owned by aios-orchestrator", file=sys.stderr)
    raise SystemExit(2)
