"""Offline test for the Demo Provider. No external calls.

This test exercises the Demo Provider Adapter contract end-to-end
without contacting any real Provider.
"""
from __future__ import annotations

import os
import sys

# Allow running this file directly: `python test_demo_provider.py`
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from provider import AdapterRequest, AdapterResponse, DemoProvider


def test_disabled_by_default():
    p = DemoProvider()
    assert p.enabled is False
    resp = p.invoke(AdapterRequest(prompt="x"))
    assert resp.status == "error"
    assert "disabled" in (resp.error or "")


def test_invoke_when_enabled_is_ok():
    p = DemoProvider()
    p.enabled = True
    resp = p.invoke(AdapterRequest(prompt="hello world"))
    assert isinstance(resp, AdapterResponse)
    assert resp.status == "ok"
    assert resp.text
    assert resp.input_tokens > 0
    assert resp.output_tokens > 0


def test_health():
    p = DemoProvider()
    h = p.health()
    assert h["status"] == "ok"
    assert h["provider"] == "demo_provider"
    assert h["enabled"] is False


def test_shutdown_is_noop():
    p = DemoProvider()
    assert p.shutdown() is None


if __name__ == "__main__":
    test_disabled_by_default()
    test_invoke_when_enabled_is_ok()
    test_health()
    test_shutdown_is_noop()
    print("demo_provider tests: 4 passed")