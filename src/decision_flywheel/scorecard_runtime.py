"""Application runtime for a versioned multi-classifier scorecard.

This module owns the scorecard application's use of ``WorkspaceSession``.  It
keeps the API command runner independent of shared-request composition,
per-classifier wheels, and asynchronous decision providers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .application_runtime import ApplicationSession, RuntimeCommand
from .workspace_session import WorkspaceSession


@dataclass
class ScorecardSession:
    """Lifecycle adapter for one scorecard workspace session."""

    workspace: WorkspaceSession

    @property
    def current_cycle(self) -> Any:
        return self.workspace.current_cycle

    def close(self) -> None:
        self.workspace.close()

    def abort(self, _error: BaseException) -> None:
        """Leave durable suspended cycles available for explicit recovery.

        A scorecard prediction is intentionally suspended between its shared
        decision request and human feedback.  Closing it on API delivery
        failure would destroy the very recovery state the web API persists.
        """


class ScorecardRuntime:
    """Compose and execute source-neutral scorecard sessions for one host."""

    def __init__(self, store, directory: str | Path, *, model_factory: Callable,
                 sink_factory: Callable, redact: Sequence[str] = ()):
        self.store = store
        self.directory = Path(directory)
        self.model_factory = model_factory
        self.sink_factory = sink_factory
        self.redact = tuple(redact)

    def open_session(self, run_id: str, config: Mapping[str, object],
                     _items: Sequence[Mapping[str, object]],
                     _current: Mapping[str, object] | None) -> ScorecardSession:
        """Open the pinned scorecard configuration without changing it."""
        if "classifiers" not in config:
            raise ValueError("scorecard runtime requires a frozen scorecard configuration")
        model, optimizer = self.model_factory(config)
        run = {"id": run_id, "config": dict(config)}
        workspace = WorkspaceSession(
            self.store, run, self.directory / run_id, model, optimizer,
            self.sink_factory(run_id), redact=self.redact,
        )
        return ScorecardSession(workspace)

    async def execute(self, session: ApplicationSession, kind: str,
                      payload: Mapping[str, object], _current: Mapping[str, object] | None,
                      config: Mapping[str, object], *, request_id: str | None = None) -> RuntimeCommand:
        """Execute a scorecard command; storage remains API-owned.

        ``WorkspaceSession`` persists its own scorecard-facing state because
        it needs atomic label, prediction, and checkpoint writes.  The command
        runner only records the durable command result and queues the next
        preparation after an acknowledged label.
        """
        if not isinstance(session, ScorecardSession):
            raise ValueError("scorecard runtime requires a scorecard session")
        workspace = session.workspace
        workspace.config = dict(config)
        workspace.shared.max_requests = config["max_requests"]
        for wheel in workspace.wheels.values():
            wheel.max_requests = config["max_requests"]
            transport = getattr(wheel.optimizer, "complete", None)
            if hasattr(transport, "max_calls"):
                transport.max_calls = config["max_optimizer_calls"]
        if kind == "prepare":
            return RuntimeCommand(await workspace.prepare())
        if kind == "label":
            if not request_id:
                raise ValueError("scorecard feedback needs a command identity")
            return RuntimeCommand(await workspace.feedback(payload, request_id))
        if kind == "skip":
            return RuntimeCommand(workspace.skip(payload))
        if kind == "optimize":
            return RuntimeCommand(await workspace.resume_optimization())
        if kind == "replay-next":
            if config.get("input_mode") != "replay":
                raise ValueError("this run is not a replay")
            shown = await workspace.prepare()
            if shown.get("finished"):
                return RuntimeCommand(shown)
            return RuntimeCommand(await workspace.feedback({
                "item_id": shown["item"]["id"],
                "presentation_id": shown["prediction"]["presentation_id"],
                "labels": [],
            }, f"replay:{shown['item']['id']}"))
        raise ValueError("use catalog label correction; automatic learning rollback is not supported")


def run_scorecard_command(runtime: ScorecardRuntime, session: ScorecardSession, kind: str,
                          payload: Mapping[str, object], current: Mapping[str, object] | None,
                          config: Mapping[str, object], *, request_id: str | None = None) -> RuntimeCommand:
    """Synchronous adapter for serialized command workers."""
    return asyncio.run(runtime.execute(session, kind, payload, current, config, request_id=request_id))
