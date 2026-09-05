#!/usr/bin/env python3
"""AIOS P8C-U Qwen Third-Provider Status.

Qwen is a model Provider, NOT a new AI tool.  This module is the
single place where the system inspects the Qwen environment and
declares its integration status.  It NEVER prints the actual
secret value; it only reports whether the credential is configured.

Status taxonomy:

* ``UNCONFIGURED``                       — env vars absent or empty.
* ``CREDENTIALS_PRESENT_NOT_IMPLEMENTED`` — env vars present but no
  implementation module yet.
* ``IMPLEMENTED_VERIFIED``               — env vars present AND the
  P8C-U implementation module exposes at least one real call path.
* ``DISABLED``                           — env vars present but
  explicit override flag set to ``off``.

This module is pure: it does not perform any HTTP call, it does not
read the secret value, it does not start subprocesses.  It is
intentionally small and side-effect-free.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple


# ---------------------------------------------------------------------------
# Environment contract
# ---------------------------------------------------------------------------

QWEN_API_KEY_ENV = "AIOS_QWEN_API_KEY"
QWEN_BASE_URL_ENV = "AIOS_QWEN_BASE_URL"
QWEN_MODEL_ENV = "AIOS_QWEN_MODEL"
QWEN_ENABLED_ENV = "AIOS_QWEN_ENABLED"

# Public status strings.
QWEN_STATUS_UNCONFIGURED = "UNCONFIGURED"
QWEN_STATUS_CREDENTIALS_PRESENT_NOT_IMPLEMENTED = "CREDENTIALS_PRESENT_NOT_IMPLEMENTED"
QWEN_STATUS_IMPLEMENTED_VERIFIED = "IMPLEMENTED_VERIFIED"
QWEN_STATUS_DISABLED = "DISABLED"
QWEN_STATUS_FAILED_HEALTHCHECK = "FAILED_HEALTHCHECK"


# Default well-known endpoint (Aliyun Bailian OpenAI-compatible).  We
# do NOT bake the key into the default; the base URL is public info.
QWEN_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_DEFAULT_MODEL = "qwen-plus"


# ---------------------------------------------------------------------------
# Status dataclass
# ---------------------------------------------------------------------------


@dataclass
class QwenStatus:
    vendor: str = "Qwen"
    primary_status: str = QWEN_STATUS_UNCONFIGURED
    api_key_present: bool = False
    base_url: str = ""
    model_id: str = ""
    credentials_present: bool = False
    implemented: bool = False
    verified_real_calls: int = 0
    enabled: bool = False
    binding_count: int = 0
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # Never serialise the API key.  Only the booleans / names.
        data.pop("api_key_present", None)
        data["primary_status"] = self.primary_status
        data["credentials_present"] = self.credentials_present
        return data


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _safe_getenv(name: str) -> str:
    """Return ``os.environ[name]`` or empty string.  Never raises."""
    try:
        value = os.environ.get(name, "")
    except Exception:
        return ""
    if value is None:
        return ""
    return str(value)


def detect_qwen_credentials() -> Tuple[bool, str, str, str]:
    """Return ``(api_key_present, base_url, model, enabled_flag)``.

    The API key value is NEVER returned.  Only its presence is
    reported.
    """
    key = _safe_getenv(QWEN_API_KEY_ENV).strip()
    base_url = _safe_getenv(QWEN_BASE_URL_ENV).strip() or QWEN_DEFAULT_BASE_URL
    model = _safe_getenv(QWEN_MODEL_ENV).strip() or QWEN_DEFAULT_MODEL
    enabled_raw = _safe_getenv(QWEN_ENABLED_ENV).strip().lower()
    enabled_flag = enabled_raw in ("1", "true", "yes", "on")
    return (bool(key), base_url, model, "on" if enabled_flag else "off")


def compute_qwen_status(
    *,
    binding_count: int = 0,
    verified_real_calls: int = 0,
    implemented: Optional[bool] = None,
    now_epoch: Optional[float] = None,
) -> QwenStatus:
    """Compute the current Qwen status without leaking secrets."""
    api_key_present, base_url, model, enabled_flag = detect_qwen_credentials()
    if not api_key_present:
        return QwenStatus(
            primary_status=QWEN_STATUS_UNCONFIGURED,
            api_key_present=False,
            base_url="",
            model_id="",
            credentials_present=False,
            implemented=False,
            verified_real_calls=0,
            enabled=False,
            binding_count=binding_count,
            note="AIOS_QWEN_API_KEY not set",
        )
    # Credentials are present.
    if enabled_flag != "on":
        return QwenStatus(
            primary_status=QWEN_STATUS_DISABLED,
            api_key_present=True,
            base_url=base_url,
            model_id=model,
            credentials_present=True,
            implemented=False,
            verified_real_calls=verified_real_calls,
            enabled=False,
            binding_count=binding_count,
            note="AIOS_QWEN_ENABLED is off",
        )
    impl = bool(implemented) if implemented is not None else (
        verified_real_calls > 0
    )
    if not impl:
        return QwenStatus(
            primary_status=QWEN_STATUS_CREDENTIALS_PRESENT_NOT_IMPLEMENTED,
            api_key_present=True,
            base_url=base_url,
            model_id=model,
            credentials_present=True,
            implemented=False,
            verified_real_calls=verified_real_calls,
            enabled=True,
            binding_count=binding_count,
            note="credentials present; no verified call yet",
        )
    return QwenStatus(
        primary_status=QWEN_STATUS_IMPLEMENTED_VERIFIED,
        api_key_present=True,
        base_url=base_url,
        model_id=model,
        credentials_present=True,
        implemented=True,
        verified_real_calls=verified_real_calls,
        enabled=True,
        binding_count=binding_count,
        note=f"{verified_real_calls} real call(s) verified",
    )


# ---------------------------------------------------------------------------
# Singleton accessor (for monitor / acceptance)
# ---------------------------------------------------------------------------


_LAST_STATUS: Optional[QwenStatus] = None


def get_last_qwen_status() -> Optional[QwenStatus]:
    return _LAST_STATUS


def set_last_qwen_status(status: QwenStatus) -> None:
    global _LAST_STATUS
    _LAST_STATUS = status


__all__ = [
    "QWEN_API_KEY_ENV", "QWEN_BASE_URL_ENV", "QWEN_MODEL_ENV",
    "QWEN_ENABLED_ENV",
    "QWEN_STATUS_UNCONFIGURED",
    "QWEN_STATUS_CREDENTIALS_PRESENT_NOT_IMPLEMENTED",
    "QWEN_STATUS_IMPLEMENTED_VERIFIED",
    "QWEN_STATUS_DISABLED",
    "QWEN_STATUS_FAILED_HEALTHCHECK",
    "QWEN_DEFAULT_BASE_URL", "QWEN_DEFAULT_MODEL",
    "QwenStatus",
    "detect_qwen_credentials",
    "compute_qwen_status",
    "get_last_qwen_status",
    "set_last_qwen_status",
]