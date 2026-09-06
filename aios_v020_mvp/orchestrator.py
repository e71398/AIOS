"""Orchestrator for the v0.2.0 MVP.

Owns the workflow lifecycle. The orchestrator wires together
the Planner, Executor, Reviewer, and persistence layers and
runs each workflow in a single-threaded fashion. This is a
deliberate simplification for v0.2.0; the v0.1.0-alpha.3
multi-process systemd model is replaced with an in-process
queue so the MVP can run on a single Python interpreter.

The orchestrator still keeps the role boundaries: each role
gets its own provider instance and the orchestrator never
short-circuits one role by calling the next role's model
directly.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .config import MVPConfig, load_config
from .executor import Executor
from .persistence import FileResultStore, JSONStore
from .planner import Planner
from .providers import (
    HTTPChatProvider,
    LocalProvider,
    Provider,
    ProviderError,
    build_http_provider,
)
from .reviewer import Reviewer
from .tools import ToolRegistry, register_default_file_tools
from .workflow import (
    Workflow,
    WorkflowStage,
    is_success,
    is_terminal,
)


log = logging.getLogger("aios_v020_mvp.orchestrator")


def _build_provider(spec, default_provider: str, config: MVPConfig) -> Provider:
    name = spec.provider
    if name == "local":
        return LocalProvider(model=spec.model)
    if name in ("minimax", "openai", "anthropic"):
        try:
            return build_http_provider(name, spec.model)
        except ProviderError:
            log.warning(
                "Falling back to local provider; %s requested but API key missing",
                name,
            )
            return LocalProvider(model="mvp-local")
    log.warning("Unknown provider %r; falling back to local", name)
    return LocalProvider(model="mvp-local")


@dataclass
class Orchestrator:
    config: MVPConfig
    json_store: JSONStore
    file_store: FileResultStore
    planner: Planner
    executor: Executor
    reviewer: Reviewer
    tools: ToolRegistry
    _workflows: Dict[str, Workflow] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Submission / lifecycle
    # ------------------------------------------------------------------

    def submit(self, task: str, source: str = "mvp") -> Workflow:
        if not task or not task.strip():
            raise ValueError("task must be a non-empty string")
        workflow = Workflow(id=self._generate_id(), task=task.strip(), source=source)
        self._workflows[workflow.id] = workflow
        self._save(workflow)
        return workflow

    def run(self, workflow_id: str) -> Workflow:
        workflow = self._load(workflow_id)
        if workflow is None:
            raise KeyError(f"unknown workflow: {workflow_id}")
        try:
            self.planner.build_plan(workflow)
            self._save(workflow)
            self.executor.execute(workflow)
            self._save(workflow)
            self.reviewer.review(workflow)
            self._save(workflow)
        except ProviderError as exc:
            workflow.error = f"provider_error: {exc}"
            if workflow.stage not in (
                WorkflowStage.REVIEWED.value,
                WorkflowStage.COMPLETED.value,
                WorkflowStage.FAILED.value,
            ):
                workflow.transition(WorkflowStage.FAILED, note=str(exc))
            self._save(workflow)
        except Exception as exc:  # last-resort guard
            workflow.error = f"orchestrator_error: {type(exc).__name__}: {exc}"
            if not is_terminal(workflow.stage):
                try:
                    workflow.transition(WorkflowStage.FAILED, note=str(exc))
                except ValueError:
                    pass
            self._save(workflow)
        return workflow

    def get(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        workflow = self._load(workflow_id)
        if workflow is None:
            return None
        return workflow.to_dict()

    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        return self._load(workflow_id)

    def list_recent(self, limit: int = 20) -> list:
        out: list = []
        for key in self.json_store.keys():
            if not key.startswith("wf:"):
                continue
            data = self.json_store.get(key)
            if isinstance(data, dict):
                out.append(data)
        out.sort(key=lambda d: d.get("created_at", 0.0), reverse=True)
        return out[:limit]

    def save_artefact(self, workflow_id: str, rel_path: str, content: str) -> Dict[str, Any]:
        return self.file_store.write(workflow_id, rel_path, content).to_dict()

    def read_artefact(self, workflow_id: str, rel_path: str) -> str:
        return self.file_store.read(workflow_id, rel_path)

    def list_artefacts(self, workflow_id: str) -> list:
        return [a.to_dict() for a in self.file_store.list(workflow_id)]

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _save(self, workflow: Workflow) -> None:
        self._workflows[workflow.id] = workflow
        self.json_store.set(self._key(workflow.id), workflow.to_dict())

    def _load(self, workflow_id: str) -> Optional[Workflow]:
        cached = self._workflows.get(workflow_id)
        if cached is not None:
            return cached
        data = self.json_store.get(self._key(workflow_id))
        if data is None:
            return None
        wf = Workflow.from_dict(data)
        self._workflows[workflow_id] = wf
        return wf

    @staticmethod
    def _key(workflow_id: str) -> str:
        return f"wf:{workflow_id}"

    @staticmethod
    def _generate_id() -> str:
        return uuid.uuid4().hex


# ----------------------------------------------------------------------
# Builder
# ----------------------------------------------------------------------


def build_orchestrator(config: Optional[MVPConfig] = None) -> Orchestrator:
    cfg = config or load_config()
    json_store = JSONStore(cfg.data_dir / "workflows.json")
    file_store = FileResultStore(cfg.data_dir)
    tools = ToolRegistry()
    register_default_file_tools(tools)

    planner_spec = cfg.planner
    executor_spec = cfg.executor
    reviewer_spec = cfg.reviewer
    default_provider = planner_spec.provider

    planner = Planner(
        provider=_build_provider(planner_spec, default_provider, cfg),
        model=planner_spec.model,
    )
    executor = Executor(
        provider=_build_provider(executor_spec, default_provider, cfg),
        model=executor_spec.model,
        tools=tools,
        file_store=file_store,
    )
    reviewer = Reviewer(
        provider=_build_provider(reviewer_spec, default_provider, cfg),
        model=reviewer_spec.model,
    )
    return Orchestrator(
        config=cfg,
        json_store=json_store,
        file_store=file_store,
        planner=planner,
        executor=executor,
        reviewer=reviewer,
        tools=tools,
    )


# ----------------------------------------------------------------------
# Background worker for the HTTP entry gateway
# ----------------------------------------------------------------------


class BackgroundWorker:
    """Run submitted workflows in a background thread.

    The MVP keeps a single worker to avoid the bookkeeping
    required by concurrent orchestration. The HTTP entry
    gateway submits work here and returns immediately; the
    client polls ``GET /task/<id>`` to read the result.
    """

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name="aios-mvp-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put("__stop__")
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def enqueue(self, workflow_id: str) -> None:
        self._queue.put(workflow_id)

    def _serve(self) -> None:
        log.info("background worker started")
        while not self._stop.is_set():
            try:
                workflow_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if workflow_id == "__stop__":
                break
            try:
                self._orchestrator.run(workflow_id)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("worker run failed for %s: %s", workflow_id, exc)
        log.info("background worker stopped")
