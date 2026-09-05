"""Pytest configuration that puts kernel/tools on sys.path so tests can import
the production modules directly.

This conftest also installs three autouse fixtures that protect the
rest of the suite from cross-test pollution:

* ``_reset_tool_runtime_failures`` clears every per-tool
  :class:`ToolFailoverEngine` failure event after each test.  Without
  this, tests that exercise :func:`record_tool_runtime_failure` (e.g.
  ``test_p9dr_runtime_health_freshness``) leave a 120 s TTL event on
  the in-memory default engine, which then corrupts later tests
  (notably ``test_reviewer_production_availability`` whose
  ``choose_reviewer`` would otherwise see ``claude`` as unavailable
  for the rest of the pytest session).

* ``_reset_in_process_caches`` invalidates the
  :func:`_tool_process_health` and executor model cache so tests that
  exercised them do not leave stale entries behind.

* ``_reset_tool_failover_engine_state`` resets the per-process
  ``ToolFailoverEngine`` default singleton (clear adapter cache
  overrides, alive map, failure events, then ``reset_default_tool_engine``
  to detach any custom engine that earlier tests such as
  ``test_p8c_u_dual_axis_failover`` installed via
  ``tf.set_default_tool_engine``).  This is pure in-memory state
  reset; it does NOT touch the on-disk ``cache/tool_health/*.json``
  probe cache.

All fixtures are deliberately idempotent and best-effort so a test
that intentionally manipulates state can still produce deterministic
results.
"""
import os
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

# AIOS-010 §六: redirect ``AIOS_HOME`` to the iso-copy root so
# tests do not read formal-disk state.  The production modules
# resolve their cache / config roots from this env var (with a
# default of ``${AIOS_HOME}``).  Walking up two parents from
_ISO_REPO = Path(__file__).resolve().parents[3]
# AIOS-010 §六: force the env var to point at the iso copy so the
# production modules resolve their cache / config roots from the
# iso copy, NOT the formal disk.  This MUST run before any
# ``aios_capability`` / ``aios_orchestrator`` import.
os.environ["AIOS_HOME"] = str(_ISO_REPO)
os.environ["AIOS_CACHE_DIR"] = str(_ISO_REPO / "cache")

import pytest


@pytest.fixture(autouse=True)
def _reset_tool_runtime_failures(request):
    """Clear every in-memory tool-runtime failure event after each test.

    Production code keeps the failure events in an in-memory
    :data:`aios_tool_failover.ToolFailoverEngine._tool_runtime_failure_events`
    dict (see ``record_tool_runtime_failure``).  The dict has a 120 s
    bounded TTL so real production traffic recovers automatically; in
    tests, however, that TTL is long enough to leak across the entire
    pytest session and silently break tests that expect a clean
    health surface.  Resetting the dict after each test keeps the
    tests deterministic without touching the production recovery
    semantics.
    """
    yield
    try:
        from aios_tool_failover import get_default_tool_engine
        engine = get_default_tool_engine()
        # Clear failure events (the primary surface).
        failures = list(getattr(engine, "_tool_runtime_failure_events", {}).keys())
        for tool_id in failures:
            try:
                engine.clear_tool_runtime_failure(tool_id)
            except Exception:
                pass
        # Also clear the alive/availability flag cache so a tool
        # marked NOT alive by a prior test reverts to the default
        # "alive until probed" assumption on the next test.
        try:
            alive_map = getattr(engine, "_tool_runtime_alive", None)
            if isinstance(alive_map, dict):
                alive_map.clear()
        except Exception:
            pass
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_in_process_caches(request):
    """Invalidate the orchestrator's in-process process-health cache
    after each test so each test sees the actual underlying state."""
    yield
    try:
        from aios_orchestrator import _invalidate_tool_process_cache
        _invalidate_tool_process_cache()
    except Exception:
        pass






@pytest.fixture(autouse=True)
def _stub_orchestrator_globals_for_failover(request, monkeypatch):
    """AIOS-010 §七: only the Failover test scope may need the
    orchestrator helpers to be stubbed to True so the 4-leg
    eligibility gate does not silently exclude every tool.
    Tests that verify the production behaviour of these helpers
    (e.g. test_production_runtime_executor_health_gate) need the
    real implementation. This fixture activates only for the
    failover test scope (5 files) and is a no-op otherwise.
    """
    fspath = str(request.fspath)
    if not any(
        name in fspath
        for name in (
            "test_p8c_u_dual_axis_failover.py",
            "test_p8d_all_tools_effective_routes.py",
            "test_p9dr_executor_process_fault_revalidation.py",
            "test_p9dr_runtime_health_freshness.py",
            "test_production_executor_live_failover.py",
        )
    ):
        return
    try:
        monkeypatch.setattr(
            "aios_orchestrator._executor_model_available",
            lambda name: True,
        )
        monkeypatch.setattr(
            "aios_orchestrator._capability_available",
            lambda name, **kw: True,
        )
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_tool_failover_engine_state(request):
    """Reset the per-process ``ToolFailoverEngine`` default singleton.

    Tests such as ``test_p8c_u_dual_axis_failover`` install a custom
    engine via ``tf.set_default_tool_engine``; without this fixture
    the next test that calls ``get_default_tool_engine()`` would
    inherit that custom engine along with its adapter cache
    overrides and ``claude:primary`` shape, breaking
    ``test_reviewer_production_availability``'s assertion that
    production routes ``claude`` to ``claude:minimax``.

    The reset is purely in-memory — no on-disk probe cache files are
    touched.  After teardown, the next ``get_default_tool_engine()``
    call lazily constructs a fresh singleton that re-reads the
    on-disk probe cache the same way production does.
    """
    yield
    try:
        from aios_tool_failover import (
            get_default_tool_engine,
            reset_default_tool_engine,
        )
    except Exception:
        return
    try:
        engine = get_default_tool_engine()
        try:
            engine.reset_state()
        except Exception:
            pass
        try:
            engine.clear_adapter_cache_overrides()
        except Exception:
            pass
        try:
            alive_map = getattr(engine, "_tool_runtime_alive", None)
            if isinstance(alive_map, dict):
                alive_map.clear()
        except Exception:
            pass
        try:
            failures = list(
                getattr(engine, "_tool_runtime_failure_events", {}).keys()
            )
            for tool_id in failures:
                try:
                    engine.clear_tool_runtime_failure(tool_id)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass
    try:
        reset_default_tool_engine()
    except Exception:
        pass