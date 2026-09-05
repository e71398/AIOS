#!/usr/bin/env python3
"""Regression test for P1-MON-001 — monitor alert filter no longer
hides critical events from no-op sources.

Closure (2026-08-17): the previous filter unconditionally dropped
``agent.offline`` / ``agent.online`` events AND filtered by source
prefix (``lingying_*``, ``star_*``, ``agent_mesh``).  Real
``alert.critical`` payloads from those sources were lost.

This test exercises the exported ``_filter_observability_events``
helper that the live monitor loop now uses.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, TOOLS)
sys.path.insert(0, "${AIOS_HOME}")


def _synthetic_obs():
    return {
        "timeline": [
            # Real critical alert from a real source — must surface
            {"ts": "2026-08-17T01:00:00Z", "source": "aios-orchestrator",
             "type": "alert.critical", "payload": {"task": "planner_timeout"}},
            # Lifecycle noise from a real source — kept in timeline
            {"ts": "2026-08-17T01:00:00Z", "source": "aios-orchestrator",
             "type": "agent.offline", "payload": {}},
            # Critical alert from a no-op source — must STILL surface
            {"ts": "2026-08-17T01:00:00Z", "source": "lingying_yi",
             "type": "alert.critical", "payload": {"task": "missing_field"}},
            # Lifecycle noise from a no-op source — must be filtered
            {"ts": "2026-08-17T01:00:00Z", "source": "lingying_yi",
             "type": "agent.online", "payload": {}},
            # Lifecycle noise from agent_mesh — must be filtered
            {"ts": "2026-08-17T01:00:00Z", "source": "agent_mesh",
             "type": "agent.online", "payload": {}},
            # task.completed from a real source — kept
            {"ts": "2026-08-17T01:00:00Z", "source": "aios-executor-codex",
             "type": "task.completed", "payload": {"task": "ping"}},
        ]
    }


class MonitorFilterTest(unittest.TestCase):
    def test_critical_alert_from_real_source_surfaces(self):
        from aios_monitor import _filter_observability_events
        kept = _filter_observability_events(_synthetic_obs())
        sources = {e["system"] for e in kept if e["type"].startswith("alert.")}
        self.assertIn("aios-orchestrator", sources)

    def test_lifecycle_noise_from_noop_filtered(self):
        from aios_monitor import _filter_observability_events
        kept = _filter_observability_events(_synthetic_obs())
        for ev in kept:
            if ev["type"] in ("agent.offline", "agent.online"):
                self.assertNotEqual(ev["system"], "lingying_yi")
                self.assertNotEqual(ev["system"], "agent_mesh")

    def test_critical_alert_from_noop_source_surfaces(self):
        from aios_monitor import _filter_observability_events
        kept = _filter_observability_events(_synthetic_obs())
        alert_sources = {
            e["system"] for e in kept
            if e["type"].startswith("alert.")
        }
        self.assertIn("lingying_yi", alert_sources,
                      "alert.critical from lingying_yi must surface")

    def test_two_critical_alerts_kept(self):
        from aios_monitor import _filter_observability_events
        kept = _filter_observability_events(_synthetic_obs())
        critical = [e for e in kept if e["type"] == "alert.critical"]
        self.assertEqual(len(critical), 2)

    def test_real_executor_lifecycle_kept_in_timeline(self):
        """``agent.offline`` from aios-orchestrator is kept (in timeline)."""
        from aios_monitor import _filter_observability_events
        kept = _filter_observability_events(_synthetic_obs())
        real_offline = [e for e in kept
                        if e["type"] == "agent.offline"
                        and e["system"] == "aios-orchestrator"]
        self.assertEqual(len(real_offline), 1)


if __name__ == "__main__":
    unittest.main()
