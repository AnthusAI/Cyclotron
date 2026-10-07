"""Application runtime for a versioned multi-classifier scorecard.

This module owns the scorecard application's use of ``WorkspaceSession``.  It
keeps the API command runner independent of shared-request composition,
per-classifier wheels, and asynchronous decision providers.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .application_runtime import ApplicationSession, RuntimeCommand
from .selection_policy import SelectionPolicy
from .workspace_session import WorkspaceSession, freeze_configuration
from .decision_provider_settings import normalize_decision_settings


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

    def normalize_run_config(self, config: Mapping[str, object]) -> tuple[dict, list[dict]]:
        """Validate a scorecard run and freeze its definitions and item list.

        The returned configuration holds only immutable definition/item
        references.  No provider is constructed and no prediction is made.
        """
        values = dict(config)
        allowed = {
            "selection_policy", "max_requests", "max_optimizer_calls", "optimize_every",
            "rubric_changes_every", "seed", "decisions_model", "decisions_provider", "optimizer_model",
            "classifier_ids", "item_list_id", "scorecard_id", "scorecard_definition_revision",
        }
        if set(values) - allowed:
            raise ValueError("unknown run configuration option")
        if values.get("scorecard_id"):
            if values.get("classifier_ids"):
                raise ValueError("select a scorecard or independent classifiers, not both")
            definition = self.store.scorecard_definition(
                values["scorecard_id"], values.get("scorecard_definition_revision"),
            )
            settings = {key: value for key, value in definition["settings"].items()
                        if key in {"selection_policy", "seed", "decisions_model", "decisions_provider", "optimizer_model",
                                   "optimize_every", "rubric_changes_every"}}
            if ('decisions_provider' in values and
                values['decisions_provider']!=settings.get('decisions_provider','jev') and
                'decisions_model' not in values):
                settings.pop('decisions_model',None)
            values = {**settings, **values}
            values["scorecard_definition_revision"] = definition["revision"]
            values["scorecard_definition_fingerprint"] = definition["fingerprint"]
            values["classifier_ids"] = [row["id"] for row in definition["classifiers"]]
            values["classifier_revisions"] = {row["id"]: row["revision"] for row in definition["classifiers"]}
        if not values.get("classifier_ids") or not values.get("item_list_id"):
            raise ValueError("choose classifiers and an item list")
        for key, default in (("max_requests", 500), ("max_optimizer_calls", 1000),
                             ("optimize_every", 20), ("rubric_changes_every", 2)):
            value = values.setdefault(key, default)
            if type(value) is not int or value < 1:
                raise ValueError("run ceilings and cadences must be positive integers")
        values.setdefault("selection_policy", {"primary": "f1", "positive_class": "include"})
        values["selection_policy"] = asdict(SelectionPolicy(**values["selection_policy"]))
        values.setdefault("seed", "arxiv-web-v1")
        values=normalize_decision_settings(values)
        values.setdefault("optimizer_model", "gpt-6-luna")
        values["evaluation_protocol"] = "protected-feedback-v1"
        values = freeze_configuration(self.store, values)
        items: list[dict] = []
        offset = 0
        while page := self.store.list_items(values["item_list_id"], after=offset):
            items.extend(page)
            offset += len(page)
        return values, items

    def create_run(self, name: str, config: Mapping[str, object]) -> dict:
        """Persist a new scorecard run with a fully frozen input snapshot."""
        values, items = self.normalize_run_config(config)
        return self.store.create_run(name, "live", values, items=items)

    def create_replay(self, name: str, source_run_id: str, config: Mapping[str, object]) -> dict:
        """Create a chronology-preserving replay without leaking future labels."""
        source = self.store.run(source_run_id)
        if not config.get("scorecard_id"):
            raise ValueError("choose a versioned scorecard for replay")
        definition = self.store.scorecard_definition(config["scorecard_id"], config.get("scorecard_definition_revision"))
        refs = definition["classifiers"]
        source_refs = {row["id"]: row["revision"] for row in source["config"].get("classifiers", [])}
        if any(source_refs.get(row["id"]) != row["revision"] for row in refs):
            raise ValueError("source feedback must match every classifier definition; collect missing labels first")
        votes: dict[tuple[str, str], dict] = {}
        order: list[str] = []
        for row in self.store.all_events(source_run_id):
            event = row["payload"]
            if event.get("kind") != "human-feedback" or event.get("classifier_id") not in source_refs:
                continue
            feedback = event["feedback"]
            item_id, classifier_id = feedback["item_id"], event["classifier_id"]
            key = (item_id, classifier_id)
            if event.get("action") == "retracted":
                votes.pop(key, None)
                continue
            if item_id not in order:
                order.append(item_id)
            votes[key] = {"classifier_id": classifier_id, "label": feedback["final_answer_value"],
                          "comment": feedback.get("edit_comment_value") or "", "request_id": feedback["id"]}
        source_items = {row["id"]: row for row in self.store.items(source_run_id)}
        eligible = [item_id for item_id in order if item_id in source_items
                    and all((item_id, ref["id"]) in votes for ref in refs)]
        if not eligible:
            raise ValueError("no fully labeled source items available for this scorecard")
        values, _ = self.normalize_run_config({**config, "item_list_id": source["config"]["item_list_id"]})
        frozen = {**values, "input_mode": "replay", "source_run_id": source_run_id,
                  "learning_policy": "fresh-replay", "replay_count": len(eligible)}
        frozen["item_revisions"] = [{"id": source_items[item_id]["id"], "revision": source_items[item_id]["revision"],
                                     "fingerprint": source_items[item_id]["fingerprint"]} for item_id in eligible]
        return self.store.create_run(
            name, "live", frozen, items=[source_items[item_id] for item_id in eligible],
            frozen_feedback=[(item_id, ref["id"], votes[(item_id, ref["id"])])
                             for item_id in eligible for ref in refs],
            initial_events=[("replay-created", {"kind": "replay-created", "source_run_id": source_run_id,
                              "sample_count": len(eligible), "initial_state": "empty context and no fitted heads"})],
        )

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
        if kind == "correct":
            if not request_id:
                raise ValueError("scorecard correction needs a command identity")
            return RuntimeCommand(workspace.correct_feedback(payload, request_id))
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
