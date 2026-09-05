#!/usr/bin/env python3
"""
AIOS Runtime Revision Helper
============================

Shared helper for computing a deterministic content-addressed revision over
an explicit list of source files.

This helper exists so that runtime-loaded revisions reflect **transitive
dependencies**, not only the entry-point script. It is consumed by:

* ``aios_executor_daemon.py`` — to publish ``LOADED_REVISION`` in heartbeat
* ``aios_acceptance.py``     — to compute the expected revision

The revision format is intentionally versioned so future layout changes can
break compatibility cleanly:

    manifest-v1:<sha256-hex>

The manifest is **fail-closed**: a missing file raises ``FileNotFoundError``
and a path that escapes the root raises ``ValueError``. There is no
silent-skip mode.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Sequence

MANIFEST_VERSION = "manifest-v1"


def compute_manifest_revision(
    root: Path,
    relative_paths: Iterable[str],
) -> str:
    """Return a deterministic ``manifest-v1:<sha256>`` for the given files.

    Algorithm:
      * Paths are normalized, de-duplicated and sorted (lexicographic).
      * For each path, the relative path string is fed into the digest,
        followed by a NUL separator and the file contents, then a NUL
        terminator. This ordering means renaming a file changes the
        digest even if its contents are unchanged.
      * Any ``..`` segment that would resolve outside ``root`` is rejected.
      * A missing file raises ``FileNotFoundError`` — never silently skipped.

    Args:
        root: Directory the relative paths are anchored to.
        relative_paths: Explicit list of relative paths (use forward slashes
            or ``os.sep`` — both are accepted).

    Returns:
        String in the form ``"manifest-v1:<hexdigest>"``.

    Raises:
        FileNotFoundError: If any declared file does not exist.
        ValueError: If any relative path escapes ``root``.
    """
    root_resolved = Path(root).resolve()
    if not root_resolved.is_dir():
        raise NotADirectoryError(f"revision root is not a directory: {root_resolved}")

    seen: set[str] = set()
    normalized: list[str] = []
    for raw in relative_paths:
        norm = str(raw).strip().replace("\\", "/")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        normalized.append(norm)

    if not normalized:
        raise ValueError("relative_paths is empty — refusing to compute revision")

    digest = hashlib.sha256()
    digest.update(MANIFEST_VERSION.encode("utf-8"))
    digest.update(b"\x00")

    for relative_path in sorted(normalized):
        candidate = (root_resolved / relative_path).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError(
                f"path escapes revision root: {relative_path} -> {candidate}"
            ) from exc
        if not candidate.is_file():
            raise FileNotFoundError(
                f"revision manifest file missing: {relative_path} ({candidate})"
            )

        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\x00")
        with candidate.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\x00")

    return f"{MANIFEST_VERSION}:{digest.hexdigest()}"


# Shared dependency manifest for the three production Executor daemons
# (opencode / claude / codex). All three execute the same entry-point and
# share the same direct + transitive imports. Update this tuple if a new
# production module is added to the executor runtime path.
EXECUTOR_REVISION_FILES: tuple[str, ...] = (
    "aios_executor_daemon.py",
    "aios_bus.py",
    "aios_enforcer.py",
    "aios_metadata_pipeline.py",
    "aios_observability.py",
    "aios_secure.py",
    "aios_tool_adapter.py",
    "aios_agent_mesh.py",
)


def compute_executor_revision(tools_dir: Path) -> str:
    """Compute the manifest revision shared by all three Executor daemons."""
    return compute_manifest_revision(tools_dir, EXECUTOR_REVISION_FILES)


__all__ = [
    "MANIFEST_VERSION",
    "EXECUTOR_REVISION_FILES",
    "compute_manifest_revision",
    "compute_executor_revision",
]