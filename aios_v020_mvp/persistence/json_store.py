"""JSON-backed key-value store with atomic writes.

A minimal replacement for the Redis state bus used by the
v0.1.0-alpha.3 codebase. The MVP keeps workflow state in a
single JSON file that is rewritten atomically on every update.
For multi-instance deployment, swap this implementation with
Redis; the public API is intentionally narrow.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


class JSONStoreError(RuntimeError):
    """Raised when the JSON store cannot satisfy a request."""


class JSONStore:
    """File-backed JSON store.

    All reads and writes are serialized with a single lock so
    concurrent workers in the same process see consistent state.
    Writes are committed by replacing the target file atomically
    (``os.replace``) so a crash never leaves a partial document.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self.path.exists():
            self._write_atomic({})

    def _read(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise JSONStoreError(f"corrupt store at {self.path}: {exc}") from exc

    def _write_atomic(self, data: Dict[str, Any]) -> None:
        directory = str(self.path.parent)
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        with self._lock:
            return self._read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._read()
            data[key] = value
            self._write_atomic(data)

    def update(self, key: str, **changes: Any) -> Dict[str, Any]:
        """Merge ``changes`` into the value at ``key`` and return the new value."""
        with self._lock:
            data = self._read()
            current = data.get(key, {})
            if not isinstance(current, dict):
                raise JSONStoreError(f"value at {key!r} is not a dict")
            current.update(changes)
            data[key] = current
            self._write_atomic(data)
            return current

    def delete(self, key: str) -> None:
        with self._lock:
            data = self._read()
            if key in data:
                del data[key]
                self._write_atomic(data)

    def keys(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._read().keys()))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._read()))
