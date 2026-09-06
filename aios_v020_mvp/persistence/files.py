"""Filesystem-backed result/artefact store.

Each workflow gets a directory under ``data_dir/results/<workflow_id>``
where its artefacts (file-tool outputs, executor reports, reviewer
verdicts) are written. Paths returned by file tools are always
relative to a workflow-scoped sandbox so the reviewer can verify
the Executor only touched files inside the approved boundary.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_id(raw: str) -> str:
    cleaned = _SAFE_NAME.sub("_", raw)
    cleaned = cleaned.strip("._-") or "workflow"
    return cleaned[:128]


@dataclass
class ArtifactRef:
    """A reference to a file written to the result store."""

    workflow_id: str
    rel_path: str
    abs_path: Path
    size_bytes: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "workflow_id": self.workflow_id,
            "path": self.rel_path,
            "abs_path": str(self.abs_path),
            "size_bytes": self.size_bytes,
        }


class FileResultStore:
    """Filesystem result store keyed by workflow id."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.results_dir = self.data_dir / "results"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def workflow_dir(self, workflow_id: str) -> Path:
        safe = _safe_id(workflow_id)
        d = self.results_dir / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write(self, workflow_id: str, rel_path: str, content: str) -> ArtifactRef:
        """Write ``content`` to ``<workflow>/<rel_path>`` atomically."""
        safe_rel = _SAFE_NAME.sub("_", rel_path)
        if not safe_rel or safe_rel.startswith("."):
            raise ValueError(f"unsafe rel_path: {rel_path!r}")
        if not isinstance(content, str):
            raise TypeError("content must be a str")
        encoded = content.encode("utf-8")
        with self._lock:
            wf_dir = self.workflow_dir(workflow_id)
            target = wf_dir / safe_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=".tmp_", dir=str(target.parent)
            )
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(encoded)
                os.replace(tmp, target)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            size = target.stat().st_size
            return ArtifactRef(
                workflow_id=workflow_id,
                rel_path=safe_rel,
                abs_path=target,
                size_bytes=size,
            )

    def read(self, workflow_id: str, rel_path: str) -> str:
        wf_dir = self.workflow_dir(workflow_id)
        target = (wf_dir / rel_path).resolve()
        try:
            target.relative_to(wf_dir.resolve())
        except ValueError as exc:
            raise FileNotFoundError(f"path escapes workflow dir: {rel_path}") from exc
        return target.read_text(encoding="utf-8")

    def exists(self, workflow_id: str, rel_path: str) -> bool:
        wf_dir = self.workflow_dir(workflow_id)
        return (wf_dir / rel_path).is_file()

    def list(self, workflow_id: str) -> List[ArtifactRef]:
        wf_dir = self.workflow_dir(workflow_id)
        refs: List[ArtifactRef] = []
        for path in sorted(wf_dir.rglob("*")):
            if path.is_file() and not path.name.startswith(".tmp_"):
                rel = path.relative_to(wf_dir).as_posix()
                refs.append(
                    ArtifactRef(
                        workflow_id=workflow_id,
                        rel_path=rel,
                        abs_path=path,
                        size_bytes=path.stat().st_size,
                    )
                )
        return refs

    def remove(self, workflow_id: str) -> None:
        wf_dir = self.workflow_dir(workflow_id)
        with self._lock:
            if wf_dir.exists():
                shutil.rmtree(wf_dir)
