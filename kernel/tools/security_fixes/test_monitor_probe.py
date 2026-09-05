#!/usr/bin/env python3
"""Deterministic tests for SEC-PROBE-01 hardening of /api/tools/probe/<name>.

Strict rules:
  * No real network or subprocess probe.
  * No real monitor key.
  * No writes to tool_profile.json or tool_lifecycle.json.
  * Uses in-process fake adapter registry injected via monkey-patch.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import types
import unittest
from collections import defaultdict
from unittest import mock

REPO_ROOT = "${AIOS_HOME}"
MONITOR_PATH = f"{REPO_ROOT}/kernel/tools/aios_monitor.py"
FAKE_KEY = "test-key-must-be-long-enough-0123456789abcdef"


def _load_monitor_module():
    spec = importlib.util.spec_from_file_location(
        "aios_monitor_under_test", MONITOR_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.__name__ != "__main__"
    return module


def _make_handler(module, headers, path="/api/tools/probe/x"):
    """Build a Handler-like object without invoking BaseHTTPRequestHandler.__init__."""
    handler_cls = module.Handler
    instance = handler_cls.__new__(handler_cls)
    instance.headers = headers
    instance.path = path
    return instance


class FakeAdapter:
    def __init__(self, name, *, behavior="ok", probe_sleep=0.0,
                 raise_exc=False, fail_message="boom"):
        self.name = name
        self.behavior = behavior
        self.probe_sleep = probe_sleep
        self.raise_exc = raise_exc
        self.fail_message = fail_message
        self.probe_calls = []
        self._lock = threading.Lock()

    def probe(self, force=False):
        with self._lock:
            self.probe_calls.append({"force": force})
        if self.probe_sleep > 0:
            time.sleep(self.probe_sleep)
        if self.raise_exc:
            raise RuntimeError(f"synthetic-error-with-secret: {FAKE_KEY} path=/etc/aios.key")
        if self.behavior == "ok":
            return {
                "checked_at": "2026-01-01T00:00:00+00:00",
                "model_state": "available",
                "model_available": True,
                "reason": "ok",
                "latency_ms": 10,
                "evidence": "AIOS_OK",
                "returncode": 0,
                "secret_field": FAKE_KEY,
                "internal_path": "${HOME}/.config/aios/secrets/aios-monitor.env",
                "stderr_blob": "stderr garbage",
            }
        return {
            "checked_at": "2026-01-01T00:00:00+00:00",
            "model_state": "probe_failed",
            "model_available": False,
            "reason": "synthetic failure",
            "latency_ms": 5,
        }


def _install_fake_registry(module, names, *, raise_exc_on_lookup=False,
                           raise_class=None, raise_message="",
                           probe_sleep=0.0, raise_exc=False):
    """Replace aios_tool_adapter.get_adapter / load_adapters inside monitor's namespace.

    Returns the mapping of name -> FakeAdapter.
    """
    registry = {n: FakeAdapter(n, probe_sleep=probe_sleep, raise_exc=raise_exc) for n in names}

    def _fake_get_adapter(name, _registry=registry):
        if raise_exc_on_lookup:
            err = (raise_class or KeyError)(raise_message or f"unregistered: {name}")
            raise err
        if name not in _registry:
            raise KeyError(f"unregistered tool adapter: {name}")
        return _registry[name]

    def _fake_load_adapters(_registry=registry):
        return dict(_registry)

    def _fake_registered_adapter_names(_registry=registry):
        return frozenset(_registry.keys())

    # Use plain types.ModuleType, not mock.MagicMock, to avoid surprising
    # __getattr__ side effects on attribute lookups.
    fake_adapter_module = types.ModuleType("aios_tool_adapter")
    fake_adapter_module.get_adapter = _fake_get_adapter
    fake_adapter_module.load_adapters = _fake_load_adapters
    fake_adapter_module.registered_adapter_names = _fake_registered_adapter_names
    sys.modules["aios_tool_adapter"] = fake_adapter_module
    return registry


def _with_auth_env(module):
    return mock.patch.dict(os.environ, {
        "AIOS_AUTH_REQUIRED": "1",
        "AIOS_MONITOR_API_KEY": FAKE_KEY,
    }, clear=False)


class MonitorProbeTests(unittest.TestCase):
    def setUp(self):
        self._env = _with_auth_env(None)
        self._env.start()
        self.module = _load_monitor_module()

    def tearDown(self):
        sys.modules.pop("aios_tool_adapter", None)
        self._env.stop()

    # ---- 1-3: auth gate (covers existing auth for probe route) ----
    def test_01_no_header_returns_403(self):
        h = _make_handler(self.module, headers={})
        self.assertFalse(self.module.Handler._check_auth(h))

    def test_02_wrong_key_returns_403(self):
        h = _make_handler(self.module, headers={"X-AIOS-Key": "wrong"})
        self.assertFalse(self.module.Handler._check_auth(h))

    def test_03_correct_key_passes_auth(self):
        h = _make_handler(self.module, headers={"X-AIOS-Key": FAKE_KEY})
        self.assertTrue(self.module.Handler._check_auth(h))

    # ---- 4-9: name validation ----
    def test_04_empty_name_rejected(self):
        registry = _install_fake_registry(self.module, ["claude"])
        captured = {}
        real = self.module.Handler.do_POST

        def wrapped(self, *a, **kw):
            try:
                return real(self, *a, **kw)
            except Exception as exc:
                captured["exc"] = exc
                raise
        # Probe with empty name
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/")
        # _json returns dict; we need to intercept
        result = {}
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(result.get("code"), 400)
        self.assertEqual(result.get("error"), "invalid tool name")
        self.assertEqual(len(registry["claude"].probe_calls), 0)

    def test_05_overlong_name_rejected(self):
        registry = _install_fake_registry(self.module, ["claude"])
        long_name = "a" * 65  # 65 chars exceeds regex bound (max 64)
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path=f"/api/tools/probe/{long_name}")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(result.get("code"), 400)
        self.assertEqual(result.get("error"), "invalid tool name")
        self.assertEqual(len(registry["claude"].probe_calls), 0)

    def test_06_dotdot_name_rejected(self):
        registry = _install_fake_registry(self.module, ["claude"])
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/..")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(result.get("code"), 400)
        self.assertEqual(result.get("error"), "invalid tool name")
        self.assertEqual(len(registry["claude"].probe_calls), 0)

    def test_07_slash_name_rejected(self):
        registry = _install_fake_registry(self.module, ["claude"])
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/a/b")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        # 'a/b' will split as last='b', which IS format-valid, but 'b' is unregistered -> 404
        # We assert that it does NOT route to the registered 'claude'
        self.assertEqual(len(registry["claude"].probe_calls), 0)

    def test_08_url_encoded_bypass_rejected(self):
        registry = _install_fake_registry(self.module, ["claude"])
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/%2e%2e")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        # After URL decode, '%2e%2e' -> '..', which fails regex -> 400 invalid tool name
        self.assertEqual(result.get("code"), 400)
        self.assertEqual(result.get("error"), "invalid tool name")
        self.assertEqual(len(registry["claude"].probe_calls), 0)

    def test_09_whitespace_or_control_rejected(self):
        registry = _install_fake_registry(self.module, ["claude"])
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/abc%20def")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        # URL-decoded 'abc def' has a space -> fails regex -> 400
        self.assertEqual(result.get("code"), 400)
        self.assertEqual(result.get("error"), "invalid tool name")
        self.assertEqual(len(registry["claude"].probe_calls), 0)

    # ---- 10-12: registry membership ----
    def test_10_unregistered_name_returns_404(self):
        registry = _install_fake_registry(self.module, ["claude"])
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/zzz")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(result.get("code"), 404)
        self.assertEqual(result.get("error"), "tool not available")
        self.assertNotIn("name", result)
        self.assertNotIn("zzz", json.dumps(result))

    def test_11_unregistered_name_does_not_call_get_adapter(self):
        registry = _install_fake_registry(self.module, ["claude"])
        calls = {"n": 0}
        real = sys.modules["aios_tool_adapter"].get_adapter
        def spy(name):
            calls["n"] += 1
            return real(name)
        sys.modules["aios_tool_adapter"].get_adapter = spy
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/zzz")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(calls["n"], 0)

    def test_12_unregistered_name_does_not_call_probe(self):
        registry = _install_fake_registry(self.module, ["claude"])
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/zzz")
        result = {}
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(len(registry["claude"].probe_calls), 0)

    # ---- 13-14: probe(force) contract ----
    def test_13_registered_name_calls_probe_with_force_false(self):
        registry = _install_fake_registry(self.module, ["claude"])
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/claude")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(result.get("code"), 200)
        self.assertEqual(len(registry["claude"].probe_calls), 1)
        self.assertEqual(registry["claude"].probe_calls[0]["force"], False)

    def test_14_never_calls_probe_with_force_true(self):
        registry = _install_fake_registry(self.module, ["claude", "codex", "openclaw", "hermes", "opencode"])
        for name in ["claude", "codex", "openclaw", "hermes", "opencode"]:
            result = {}
            handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                    path=f"/api/tools/probe/{name}")
            handler._json = lambda payload, code=200, r=result: (r.update(payload), r.setdefault("code", code))
            self.module.Handler.do_POST(handler)
        for n, fake in registry.items():
            self.assertEqual(len(fake.probe_calls), 1, f"{n} not called once")
            self.assertFalse(fake.probe_calls[0]["force"], f"{n} called with force=True")

    # ---- 15: cache respected (no fresh probe if probe_required=False) ----
    def test_15_cache_path_does_not_trigger_real_probe(self):
        registry = _install_fake_registry(self.module, ["claude"])
        # Stub adapter.probe to mimic cache hit returning probe_required=False
        registry["claude"].behavior = "ok"

        def cached_probe(*a, **kw):
            return {
                "checked_at": "2026-01-01T00:00:00+00:00",
                "model_state": "available",
                "model_available": True,
                "reason": "cached",
                "probe_required": False,
            }
        registry["claude"].probe = cached_probe
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/claude")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(result.get("code"), 200)
        # Cached path must be reflected in response
        self.assertTrue(result.get("cached"))

    # ---- 16-17: concurrency control ----
    def test_16_concurrent_same_tool_returns_409(self):
        registry = _install_fake_registry(self.module, ["claude"], probe_sleep=0.4)
        # Make handler 1 block
        results = {}
        barrier = threading.Barrier(2)

        def fire(name_suffix):
            result = {}
            h = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                              path="/api/tools/probe/claude")
            h._json = lambda payload, code=200, r=result: (r.update(payload), r.setdefault("code", code))
            try:
                barrier.wait(timeout=2)
            except Exception:
                pass
            self.module.Handler.do_POST(h)
            results[name_suffix] = result

        t1 = threading.Thread(target=fire, args=("a",))
        t1.start()
        time.sleep(0.1)  # ensure t1 acquires lock first
        t2 = threading.Thread(target=fire, args=("b",))
        t2.start()
        t1.join(timeout=3)
        t2.join(timeout=3)
        codes = sorted([results["a"].get("code"), results["b"].get("code")])
        # At least one must be 409, the other 200
        self.assertIn(409, codes)
        self.assertIn(200, codes)
        # The 409 body must use the documented error
        loser = results["a"] if results["a"].get("code") == 409 else results["b"]
        self.assertEqual(loser.get("error"), "probe already running")

    def test_17_different_tools_run_independently(self):
        registry = _install_fake_registry(self.module, ["claude", "codex"], probe_sleep=0.2)
        results = {}
        barrier = threading.Barrier(2)

        def fire(name):
            result = {}
            h = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                              path=f"/api/tools/probe/{name}")
            h._json = lambda payload, code=200, r=result: (r.update(payload), r.setdefault("code", code))
            try:
                barrier.wait(timeout=2)
            except Exception:
                pass
            self.module.Handler.do_POST(h)
            results[name] = result

        t1 = threading.Thread(target=fire, args=("claude",))
        t2 = threading.Thread(target=fire, args=("codex",))
        t1.start(); t2.start()
        t1.join(timeout=3); t2.join(timeout=3)
        self.assertEqual(results["claude"].get("code"), 200)
        self.assertEqual(results["codex"].get("code"), 200)
        # No lock accumulation
        self.assertEqual(len(registry["claude"].probe_calls), 1)
        self.assertEqual(len(registry["codex"].probe_calls), 1)

    # ---- 18-19: exception handling ----
    def test_18_adapter_exception_returns_generic(self):
        registry = _install_fake_registry(self.module, ["claude"], raise_exc=True)
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/claude")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertIn(result.get("code"), (500, 502, 503))
        self.assertEqual(result.get("error"), "probe failed")
        body = json.dumps(result)
        self.assertNotIn("synthetic-error-with-secret", body)
        self.assertNotIn(FAKE_KEY, body)
        self.assertNotIn("/etc/aios.key", body)
        self.assertNotIn("traceback", body)
        self.assertNotIn("RuntimeError", body)

    def test_19_client_response_has_no_exception_text(self):
        registry = _install_fake_registry(self.module, ["claude"], raise_exc=True)
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/claude")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        body = json.dumps(result)
        for forbidden in ("KeyError", "RuntimeError", "Exception", "Traceback",
                          "${AIOS_HOME}", "/etc/", "secret"):
            self.assertNotIn(forbidden, body, f"leaked: {forbidden}")

    # ---- 20: server log contains exception type (smoke) ----
    def test_20_server_log_contains_exception_type(self):
        registry = _install_fake_registry(self.module, ["claude"], raise_exc=True)
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/claude")
        with mock.patch("sys.stderr") as fake_err:
            handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
            self.module.Handler.do_POST(handler)
        err_text = "".join(call.args[0] for call in fake_err.write.call_args_list)
        self.assertIn("claude", err_text)
        self.assertIn("RuntimeError", err_text)
        self.assertNotIn(FAKE_KEY, err_text)

    # ---- 21-22: success response whitelist ----
    def test_21_success_response_only_contains_allowed_fields(self):
        registry = _install_fake_registry(self.module, ["claude"])
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/claude")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(result.get("code"), 200)
        allowed = {"ok", "tool", "state", "cached", "checked_at", "code"}
        self.assertEqual(set(result.keys()) - {"code"}, allowed - {"code"} or allowed)
        for forbidden_field in ("secret_field", "internal_path", "stderr_blob", "evidence", "returncode"):
            self.assertNotIn(forbidden_field, result)

    def test_22_success_response_strips_secret_path_stderr(self):
        registry = _install_fake_registry(self.module, ["claude"])
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/claude")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        body = json.dumps(result)
        self.assertNotIn(FAKE_KEY, body)
        self.assertNotIn("${HOME}/.config/aios", body)
        self.assertNotIn("stderr garbage", body)

    # ---- 23-24: lock release ----
    def test_23_lock_released_on_success(self):
        registry = _install_fake_registry(self.module, ["claude"], probe_sleep=0.0)
        for _ in range(3):
            result = {}
            handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                    path="/api/tools/probe/claude")
            handler._json = lambda payload, code=200, r=result: (r.update(payload), r.setdefault("code", code))
            self.module.Handler.do_POST(handler)
        self.assertEqual(len(registry["claude"].probe_calls), 3)

    def test_24_lock_released_on_exception(self):
        registry = _install_fake_registry(self.module, ["claude"], raise_exc=True)
        for _ in range(3):
            result = {}
            handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                    path="/api/tools/probe/claude")
            handler._json = lambda payload, code=200, r=result: (r.update(payload), r.setdefault("code", code))
            self.module.Handler.do_POST(handler)
        # Second request should also enter probe (lock not leaked)
        self.assertEqual(len(registry["claude"].probe_calls), 3)
        # Disable raising for the second call
        registry["claude"].raise_exc = False
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                path="/api/tools/probe/claude")
        handler._json = lambda payload, code=200, r=result: (r.update(payload), r.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(result.get("code"), 200)

    # ---- 25: monitor auth tests still pass (proxy: verify same module imports cleanly) ----
    def test_25_monitor_module_unaffected(self):
        self.assertTrue(hasattr(self.module.Handler, "_check_auth"))
        self.assertTrue(hasattr(self.module.Handler, "do_POST"))


if __name__ == "__main__":
    unittest.main()

# ─────────────────────────────────────────────────────────────────────
# SEC-PROBE-01 合并前收口：未注册名称不构造 adapter 的严格保证
# ─────────────────────────────────────────────────────────────────────

class MonitorProbeAdapterInitTests(unittest.TestCase):
    """Prove registered_adapter_names() is a pure read and that the monitor
    probe route does not construct ToolAdapter for unknown names.
    """

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "AIOS_AUTH_REQUIRED": "1",
            "AIOS_MONITOR_API_KEY": FAKE_KEY,
        }, clear=False)
        self._env.start()
        self.module = _load_monitor_module()
        # Real adapter module: register in sys.modules BEFORE exec so
        # @dataclass can locate the module's __dict__.
        import importlib
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aios_tool_adapter",
            f"{REPO_ROOT}/kernel/tools/aios_tool_adapter.py",
        )
        self.real_adapter = importlib.util.module_from_spec(spec)
        sys.modules["aios_tool_adapter"] = self.real_adapter
        spec.loader.exec_module(self.real_adapter)

    def tearDown(self):
        sys.modules.pop("aios_tool_adapter", None)
        self._env.stop()

    def _do_probe(self, name, install_fake=False, fake_names=("claude",)):
        if install_fake:
            _install_fake_registry(self.module, list(fake_names))
        result = {}
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                f"/api/tools/probe/{name}")
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        return result

    # --- 1. invalid name → no registry read ---
    def test_26_invalid_name_does_not_read_registry(self):
        original = self.real_adapter.registered_adapter_names
        calls = {"n": 0}
        def spy(*a, **kw):
            calls["n"] += 1
            return original(*a, **kw)
        self.real_adapter.registered_adapter_names = spy
        sys.modules["aios_tool_adapter"] = self.real_adapter
        try:
            # '..' is invalid-format; must return 400 without any registry read.
            result = self._do_probe("..")
            self.assertEqual(result.get("code"), 400)
            self.assertEqual(calls["n"], 0)
        finally:
            self.real_adapter.registered_adapter_names = original

    # --- 2. valid-format unregistered name → no get_adapter ---
    def test_27_unregistered_name_does_not_call_get_adapter(self):
        sys.modules["aios_tool_adapter"] = self.real_adapter
        original_get = self.real_adapter.get_adapter
        calls = {"n": 0}
        def spy(name):
            calls["n"] += 1
            return original_get(name)
        self.real_adapter.get_adapter = spy
        try:
            result = self._do_probe("zzz")
            self.assertEqual(result.get("code"), 404)
            self.assertEqual(result.get("error"), "tool not available")
            self.assertEqual(calls["n"], 0)
        finally:
            self.real_adapter.get_adapter = original_get

    # --- 3. unregistered name → no ToolAdapter construction ---
    def test_28_unregistered_name_does_not_construct_ToolAdapter(self):
        sys.modules["aios_tool_adapter"] = self.real_adapter
        original_init = self.real_adapter.ToolAdapter.__init__
        inits = {"n": 0}
        def spy_init(self, *a, **kw):
            inits["n"] += 1
            return original_init(self, *a, **kw)
        self.real_adapter.ToolAdapter.__init__ = spy_init
        try:
            result = self._do_probe("zzz")
            self.assertEqual(result.get("code"), 404)
            self.assertEqual(inits["n"], 0)
        finally:
            self.real_adapter.ToolAdapter.__init__ = original_init

    # --- 4. unregistered name → no secret read on probe path ---
    def test_29_unregistered_name_does_not_read_secret(self):
        sys.modules["aios_tool_adapter"] = self.real_adapter
        # Spy ToolAdapter.__init__: with the new design, __init__ is never
        # called for an unregistered name. This test confirms that.
        original_init = self.real_adapter.ToolAdapter.__init__
        inits = {"n": 0}
        def spy_init(self, *a, **kw):
            inits["n"] += 1
            return original_init(self, *a, **kw)
        self.real_adapter.ToolAdapter.__init__ = spy_init
        try:
            result = self._do_probe("zzz_definitely_not_a_real_tool_xyz")
            self.assertEqual(result.get("code"), 404)
            self.assertEqual(inits["n"], 0)
        finally:
            self.real_adapter.ToolAdapter.__init__ = original_init

    # --- 5. unregistered name → no subprocess ---
    def test_30_unregistered_name_does_not_call_subprocess(self):
        sys.modules["aios_tool_adapter"] = self.real_adapter
        original_popen = self.real_adapter.subprocess.Popen
        original_run = self.real_adapter.subprocess.run
        calls = {"popen": 0, "run": 0}
        class FakePopen:
            def __init__(self, *a, **kw):
                calls["popen"] += 1
                raise AssertionError("Popen called for unregistered name")
        def fake_run(*a, **kw):
            calls["run"] += 1
            raise AssertionError("subprocess.run called for unregistered name")
        self.real_adapter.subprocess.Popen = FakePopen
        self.real_adapter.subprocess.run = fake_run
        try:
            result = self._do_probe("zzz")
            self.assertEqual(result.get("code"), 404)
            self.assertEqual(calls["popen"], 0)
            self.assertEqual(calls["run"], 0)
        finally:
            self.real_adapter.subprocess.Popen = original_popen
            self.real_adapter.subprocess.run = original_run

    # --- 6. unregistered name → no cache write (mtime unchanged on registered) ---
    def test_31_unregistered_name_does_not_write_cache(self):
        sys.modules["aios_tool_adapter"] = self.real_adapter
        # Inject a SpyAdapter that records cache_file accesses.
        # cache_file is computed at @property time; we just verify that
        # the unregistered path does not call get_adapter → no probe →
        # no _write_probe. Use a spy on _write_probe.
        original_write = self.real_adapter.ToolAdapter._write_probe
        writes = {"n": 0}
        def spy_write(self, result):
            writes["n"] += 1
            return original_write(self, result)
        self.real_adapter.ToolAdapter._write_probe = spy_write
        try:
            result = self._do_probe("zzz")
            self.assertEqual(result.get("code"), 404)
            # No probe was triggered → no _write_probe call.
            self.assertEqual(writes["n"], 0)
        finally:
            self.real_adapter.ToolAdapter._write_probe = original_write

    # --- 7. registered name → ToolAdapter constructed ---
    def test_32_registered_name_constructs_ToolAdapter(self):
        sys.modules["aios_tool_adapter"] = self.real_adapter
        original_init = self.real_adapter.ToolAdapter.__init__
        inits = {"n": 0}
        def spy_init(self, *a, **kw):
            inits["n"] += 1
            return original_init(self, *a, **kw)
        self.real_adapter.ToolAdapter.__init__ = spy_init
        try:
            result = self._do_probe("claude", install_fake=False)
            # Probe path may fail (e.g., subprocess not available in test
            # env), but ToolAdapter should at least have been constructed.
            self.assertGreaterEqual(inits["n"], 1,
                "ToolAdapter not constructed for registered name")
        finally:
            self.real_adapter.ToolAdapter.__init__ = original_init

    # --- 8. registered name → only probe(force=False) ---
    def test_33_registered_name_only_probe_force_false(self):
        # install_fake=True returns a NEW registry each call, so we must
        # install once and capture the registry, then probe the SAME name.
        registry = _install_fake_registry(self.module, ["claude"])
        # Build a handler manually that uses the SAME fake module.
        handler = _make_handler(self.module, {"X-AIOS-Key": FAKE_KEY},
                                "/api/tools/probe/claude")
        result = {}
        handler._json = lambda payload, code=200: (result.update(payload), result.setdefault("code", code))
        self.module.Handler.do_POST(handler)
        self.assertEqual(result.get("code"), 200)
        self.assertEqual(len(registry["claude"].probe_calls), 1)
        self.assertEqual(registry["claude"].probe_calls[0]["force"], False)

    # --- 9. original 25 probe tests still pass (smoke) ---
    def test_34_original_probe_tests_smoke(self):
        # Auth: no header / correct key
        h = _make_handler(self.module, headers={})
        self.assertFalse(self.module.Handler._check_auth(h))
        h = _make_handler(self.module, headers={"X-AIOS-Key": FAKE_KEY})
        self.assertTrue(self.module.Handler._check_auth(h))
        # Invalid name → 400
        result = self._do_probe("..")
        self.assertEqual(result.get("code"), 400)
        # Unregistered → 404
        result = self._do_probe("zzz")
        self.assertEqual(result.get("code"), 404)
        # Registered → 200 (with fake)
        result = self._do_probe("claude", install_fake=True)
        self.assertEqual(result.get("code"), 200)

    # --- 10. original 14 auth tests still pass (smoke) ---
    def test_35_original_auth_tests_smoke(self):
        h = _make_handler(self.module, headers={})
        self.assertFalse(self.module.Handler._check_auth(h))
        h = _make_handler(self.module, headers={"X-AIOS-Key": "wrong"})
        self.assertFalse(self.module.Handler._check_auth(h))
        h = _make_handler(self.module, headers={"X-AIOS-Key": "aios-internal"})
        self.assertFalse(self.module.Handler._check_auth(h))
        h = _make_handler(self.module, headers={"X-AIOS-Key": FAKE_KEY})
        self.assertTrue(self.module.Handler._check_auth(h))
