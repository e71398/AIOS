#!/usr/bin/env python3
"""Deterministic authentication tests for aios_monitor._check_auth.

SEC-MONITOR-AUTH-01 contract:
  * AIOS_AUTH_REQUIRED in {1, true, yes, on} -> enforce
  * AIOS_AUTH_REQUIRED not set / 0            -> dev mode, allow
  * Only AIOS_MONITOR_API_KEY is consulted.
  * No fallback to AIOS_API_KEY.
  * Empty / aios-internal key -> fail closed.
  * Missing X-AIOS-Key header -> reject.
  * Wrong key -> reject.
  * Correct key -> allow (via hmac.compare_digest).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from unittest import mock


REPO_ROOT = "${AIOS_HOME}"
MONITOR_PATH = f"{REPO_ROOT}/kernel/tools/aios_monitor.py"


def _load_monitor_module():
    """Load aios_monitor.py as a module without executing main()."""
    spec = importlib.util.spec_from_file_location(
        "aios_monitor_under_test", MONITOR_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    # __name__ != '__main__' so the server's main guard does not run.
    assert module.__name__ != "__main__"
    return module


def _make_handler(module, headers):
    """Construct a Handler object without invoking BaseHTTPRequestHandler.__init__.

    __init__ would call self.setup() which touches socket machinery; we only
    need .headers so _check_auth(self) can read X-AIOS-Key.
    """
    handler_cls = module.Handler
    instance = handler_cls.__new__(handler_cls)
    instance.headers = headers
    return instance


class MonitorAuthTests(unittest.TestCase):
    NEW_KEY = "new-deterministic-monitor-key-AAA111BBB222CCC"
    OLD_KEY = "old-monitor-key-XXX999YYY888"
    OTHER_KEY = "some-other-key-ZZZ"

    def setUp(self):
        self._saved = {}
        for k in ("AIOS_AUTH_REQUIRED", "AIOS_MONITOR_API_KEY", "AIOS_API_KEY"):
            self._saved[k] = os.environ.pop(k, None)
        self.module = _load_monitor_module()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- Dev mode ----
    def test_dev_mode_unset_allows(self):
        os.environ.pop("AIOS_AUTH_REQUIRED", None)
        os.environ.pop("AIOS_MONITOR_API_KEY", None)
        h = _make_handler(self.module, {})
        self.assertTrue(h._check_auth())

    def test_dev_mode_explicit_zero_allows(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "0"
        os.environ.pop("AIOS_MONITOR_API_KEY", None)
        h = _make_handler(self.module, {})
        self.assertTrue(h._check_auth())

    # ---- Fail closed: required + bad/missing key config ----
    def test_required_no_monitor_key_rejected(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ.pop("AIOS_MONITOR_API_KEY", None)
        h = _make_handler(self.module, {"X-AIOS-Key": self.NEW_KEY})
        self.assertFalse(h._check_auth())

    def test_required_empty_monitor_key_rejected(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ["AIOS_MONITOR_API_KEY"] = ""
        h = _make_handler(self.module, {"X-AIOS-Key": self.NEW_KEY})
        self.assertFalse(h._check_auth())

    def test_required_aios_internal_rejected(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ["AIOS_MONITOR_API_KEY"] = "aios-internal"
        h = _make_handler(self.module, {"X-AIOS-Key": "aios-internal"})
        self.assertFalse(h._check_auth())

    # ---- Required + missing header ----
    def test_required_no_header_rejected(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ["AIOS_MONITOR_API_KEY"] = self.NEW_KEY
        h = _make_handler(self.module, {})
        self.assertFalse(h._check_auth())

    # ---- Required + wrong / old key ----
    def test_required_wrong_key_rejected(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ["AIOS_MONITOR_API_KEY"] = self.NEW_KEY
        h = _make_handler(self.module, {"X-AIOS-Key": self.OTHER_KEY})
        self.assertFalse(h._check_auth())

    def test_required_old_key_rejected(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ["AIOS_MONITOR_API_KEY"] = self.NEW_KEY
        h = _make_handler(self.module, {"X-AIOS-Key": self.OLD_KEY})
        self.assertFalse(h._check_auth())

    # ---- Required + correct key ----
    def test_required_correct_key_allowed(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ["AIOS_MONITOR_API_KEY"] = self.NEW_KEY
        h = _make_handler(self.module, {"X-AIOS-Key": self.NEW_KEY})
        self.assertTrue(h._check_auth())

    # ---- No fallback to AIOS_API_KEY ----
    def test_required_only_aios_api_key_rejected(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ.pop("AIOS_MONITOR_API_KEY", None)
        os.environ["AIOS_API_KEY"] = self.OTHER_KEY
        h = _make_handler(self.module, {"X-AIOS-Key": self.OTHER_KEY})
        self.assertFalse(h._check_auth())

    # ---- Both keys set: only monitor counts ----
    def test_required_both_keys_only_monitor_counts(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ["AIOS_MONITOR_API_KEY"] = self.NEW_KEY
        os.environ["AIOS_API_KEY"] = self.OLD_KEY
        # Header = monitor key -> allow
        h_ok = _make_handler(self.module, {"X-AIOS-Key": self.NEW_KEY})
        self.assertTrue(h_ok._check_auth())
        # Header = AIOS_API_KEY only -> reject
        h_old = _make_handler(self.module, {"X-AIOS-Key": self.OLD_KEY})
        self.assertFalse(h_old._check_auth())

    # ---- Case sensitivity ----
    def test_case_variants_rejected(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ["AIOS_MONITOR_API_KEY"] = self.NEW_KEY
        h = _make_handler(self.module, {"X-AIOS-Key": self.NEW_KEY.lower()})
        self.assertFalse(h._check_auth())

    # ---- Truthy variants of AUTH_REQUIRED ----
    def test_required_truthy_values(self):
        for v in ("1", "true", "TRUE", "yes", "Yes", "on", "ON", " 1 "):
            os.environ["AIOS_AUTH_REQUIRED"] = v
            os.environ["AIOS_MONITOR_API_KEY"] = self.NEW_KEY
            h = _make_handler(self.module, {"X-AIOS-Key": "wrong"})
            self.assertFalse(h._check_auth(), f"value={v!r}")

    # ---- hmac.compare_digest must be called ----
    def test_compare_digest_used(self):
        os.environ["AIOS_AUTH_REQUIRED"] = "1"
        os.environ["AIOS_MONITOR_API_KEY"] = self.NEW_KEY
        h = _make_handler(self.module, {"X-AIOS-Key": self.NEW_KEY})
        with mock.patch(
            "hmac.compare_digest",
            wraps=__import__("hmac").compare_digest,
        ) as spy:
            self.assertTrue(h._check_auth())
            self.assertEqual(spy.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
