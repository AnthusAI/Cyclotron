"""Application-side composition for an interactive article-review session.

This module deliberately depends on the reviewer adapter.  The model-neutral
core does not import it.  A web worker, terminal application, or another host
can use the same composition function without duplicating flywheel setup.
"""
from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .classifier_config import ClassifierConfig
from .classification_metrics import classification_metrics
from .flywheel import DecisionFlywheel
from .reviewer_core import reviewer_item, reviewer_task
from .reviewer_flywheel import ReviewerFlywheel
from .reviewer_store import Article, ReviewStore
from .selection_policy import SelectionPolicy


class ApplicationSession(Protocol):
    """Lifecycle surface that the generic API command worker needs."""

    current_cycle: Any

    def close(self) -> None:
        ...

    def abort(self, error: BaseException) -> None:
        ...


class WorkspaceRuntime(Protocol):
    """Application-owned session factory used by an API command runner.

    The command runner knows only persisted run metadata and item mappings. A
    runtime owns interpretation of those records, session construction, model
    factories, and provider credentials.
    """

    def normalize_run_config(self, config: Mapping[str, object], items: Sequence[Mapping[str, object]]) -> dict:
        ...

    def open_session(self, run_id: str, config: Mapping[str, object],
                     items: Sequence[Mapping[str, object]], current: Mapping[str, object] | None) -> ApplicationSession:
        ...

    def execute(self, session: ApplicationSession, kind: str, payload: Mapping[str, object],
                current: Mapping[str, object] | None, config: Mapping[str, object]) -> "RuntimeCommand":
        ...


@dataclass(frozen=True)
class ItemUpdate:
    """A runtime request for an API host to update one item record."""

    item_id: str
    prediction: Mapping[str, object] | None = None
    reviewed: bool | None = None


@dataclass(frozen=True)
class RuntimeCommand:
    """A completed application command, independent of API/storage transport."""

    result: Mapping[str, object]
    updates: tuple[ItemUpdate, ...] = ()


@dataclass
class LiveReviewSession:
    """The application-owned resources for one interactive review run."""

    reviewer: ReviewerFlywheel
    closed: bool = False

    @property
    def core(self) -> DecisionFlywheel:
        """Compatibility view for applications that inspect the active core."""
        return self.reviewer.core

    @property
    def current_cycle(self):
        """Expose the incomplete cycle state to an application command runner."""
        return self.reviewer.current_cycle

    @current_cycle.setter
    def current_cycle(self, value) -> None:
        self.reviewer.current_cycle = value

    def resume(self, current: Mapping[str, object] | None) -> None:
        """Restore an open cycle only when its displayed version is still active."""
        if not current:
            return
        prediction = current.get("prediction")
        item = current.get("item")
        if not isinstance(prediction, Mapping) or not isinstance(item, Mapping):
            return
        if prediction.get("version") != self.core.active.fingerprint:
            return
        item_id = item.get("id")
        if isinstance(item_id, str):
            self.reviewer.current_cycle = self.core.resume_cycle(
                reviewer_item(self.reviewer.store.article(item_id))
            )

    def close(self) -> None:
        """Close local resources without manufacturing a completed human cycle."""
        if self.closed:
            return
        self.core.close()
        self.reviewer.store.close()
        self.closed = True

    def abort(self, error: BaseException) -> None:
        """Close an incomplete cycle with its original failure, if present."""
        cycle, self.current_cycle = self.current_cycle, None
        if cycle and cycle.token is not None:
            cycle.__exit__(type(error), error, None)


def open_live_review_session(directory: str | Path, config: Mapping[str, object],
                             articles: Sequence[Article], *, model_factory: Callable,
                             event_sink: Callable[[dict], None], redact: Sequence[str] = ()) -> LiveReviewSession:
    """Compose the review adapter and reusable core from application-owned data.

    Configuration validation belongs at the API boundary.  This function only
    consumes the normalized run configuration and never reads a web store.
    """
    values = dict(config)
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    seed = values["seed"]
    if not isinstance(seed, str) or not seed:
        raise ValueError("run configuration needs a non-empty seed")
    reviews = ReviewStore(path / "reviews.sqlite3", study_seed=seed)
    reviews.import_articles(tuple(articles))
    try:
        model, optimizer = model_factory(values)
        policy = SelectionPolicy(**values["selection_policy"])
        core = DecisionFlywheel(
            path / "runtime.sqlite3", ClassifierConfig(reviewer_task()), model, optimizer,
            observer=event_sink, max_requests=values["max_requests"], selection_policy=policy,
            min_evaluation_per_class=2, redact=redact,
        )
    except Exception:
        reviews.close()
        raise
    # Persisted requests count against the ceiling after a restart. The outer
    # command processor decides whether an interrupted paid request may retry.
    events = [json.loads(row[0]) for row in core.db.execute("SELECT payload FROM runtime_events")]
    core.requests = sum(event["kind"] == "features-requested" for event in events)
    transport = getattr(optimizer, "complete", None)
    if hasattr(transport, "calls"):
        transport.calls = sum(event["kind"] == "optimizer-request" for event in events)
    reviewer = ReviewerFlywheel(
        reviews, core, min_stage_evaluation_per_class=2,
        rubric_changes_every=values.get("rubric_changes_every", 2),
        include_protected_guidance=False,
    )
    return LiveReviewSession(reviewer)


def live_review_labels() -> tuple[str, ...]:
    """The article-review adapter's public label vocabulary for an application."""
    return reviewer_task().labels


class ArticleReviewRuntime:
    """Workspace runtime for the optional arXiv article-review application."""

    def __init__(self, directory: str | Path, *, sink_factory: Callable,
                 model_factory: Callable, redact: Sequence[str] = ()):
        self.directory = Path(directory)
        self.sink_factory, self.model_factory = sink_factory, model_factory
        self.redact = tuple(redact)

    def normalize_run_config(self, config: Mapping[str, object], items: Sequence[Mapping[str, object]]) -> dict:
        values = dict(config)
        allowed = {"selection_policy", "max_requests", "max_optimizer_calls", "optimize_every",
                   "rubric_changes_every", "seed", "decisions_model", "optimizer_model"}
        if set(values) - allowed:
            raise ValueError("unknown run configuration option")
        for key, default in (("max_requests", 500), ("max_optimizer_calls", 10),
                             ("optimize_every", 20), ("rubric_changes_every", 2)):
            value = values.setdefault(key, default)
            if type(value) is not int or value < 1:
                raise ValueError("run ceilings and cadences must be positive integers")
        values.setdefault("selection_policy", {"primary": "f1", "positive_class": "include"})
        policy = SelectionPolicy(**values["selection_policy"])
        if policy.positive_class and policy.positive_class not in live_review_labels():
            raise ValueError("unknown positive class")
        from dataclasses import asdict
        values["selection_policy"] = asdict(policy)
        values.setdefault("seed", "arxiv-web-v1")
        values.setdefault("decisions_model", "jev-1.13.0")
        values.setdefault("optimizer_model", "gpt-6-luna")
        values["class_config"] = [{"label": "include", "role": "positive"},
                                  {"label": "exclude", "role": "negative"}]
        values["dataset_fingerprint"] = hashlib.sha256(
            json.dumps(tuple(items), sort_keys=True).encode()
        ).hexdigest()
        return values

    def open_session(self, run_id: str, config: Mapping[str, object],
                     items: Sequence[Mapping[str, object]], current: Mapping[str, object] | None) -> LiveReviewSession:
        session = open_live_review_session(
            self.directory / run_id, config, tuple(Article(**item) for item in items),
            model_factory=self.model_factory, event_sink=self.sink_factory(run_id), redact=self.redact,
        )
        session.resume(current)
        return session

    def execute(self, session: ApplicationSession, kind: str, payload: Mapping[str, object],
                current: Mapping[str, object] | None, config: Mapping[str, object]) -> RuntimeCommand:
        """Perform one article-review command without accessing the web store."""
        if not isinstance(session, LiveReviewSession):
            raise ValueError("article runtime requires a live review session")
        reviewer = session.reviewer
        if kind == "prepare":
            if current and reviewer.current_cycle is not None:
                return RuntimeCommand(current)
            article = reviewer.store.next_unreviewed()
            if article is None:
                return RuntimeCommand({"finished": True})
            prediction = reviewer.predict(article)
            shown = reviewer.store.record_prediction(
                article.id, prediction.label, prediction.confidence, prediction.kind,
                prediction.fingerprint, prediction.training_label_count,
            )
            value = {"label": prediction.label, "confidence": prediction.confidence,
                     "presentation_id": shown.id, "version": prediction.fingerprint}
            return RuntimeCommand({"item": asdict(article), "prediction": value},
                                  (ItemUpdate(article.id, prediction=value),))
        if kind in {"label", "skip"}:
            if not current or not isinstance(current.get("item"), Mapping):
                raise ValueError("prepare this item before submitting feedback")
            item_id = current["item"].get("id")
            if not isinstance(item_id, str) or payload.get("item_id") != item_id or reviewer.current_cycle is None:
                raise ValueError("prepare this item before submitting feedback")
            if kind == "label":
                prediction = current.get("prediction")
                if not isinstance(prediction, Mapping) or payload.get("presentation_id") != prediction.get("presentation_id"):
                    raise ValueError("feedback must reference the displayed prediction")
                label = payload.get("label")
                if not isinstance(label, str):
                    raise ValueError("a label command needs a label")
                comment = payload.get("comment")
                event = reviewer.store.record_vote(item_id, label,
                    comment=comment if isinstance(comment, str) and comment else None,
                    presentation_id=prediction["presentation_id"],
                )
                reviewer.record_review_event(event)
                reviewer.reconcile()
                self._optimize(reviewer, config)
                self._metrics(reviewer)
            else:
                reviewer.store.record_skip(item_id)
                reviewer.core._emit({"kind": "human-skipped", "target_id": item_id})
            reviewer.finish_cycle()
            return RuntimeCommand({"reviewed": item_id}, (ItemUpdate(item_id, reviewed=True),))
        if kind == "undo":
            reviewer.finish_cycle()
            article = reviewer.store.undo_last_vote()
            if article:
                event = reviewer.store.events_for(article.id)[-1]
                with reviewer.core.cycle(reviewer_item(article), reason="human-correction"):
                    reviewer.record_review_event(event)
                    reviewer.reconcile()
                    self._metrics(reviewer)
                return RuntimeCommand({"undone": article.id}, (ItemUpdate(article.id, reviewed=False),))
            return RuntimeCommand({"undone": None})
        raise ValueError("unknown workspace command")

    @staticmethod
    def _optimize(reviewer: ReviewerFlywheel, config: Mapping[str, object]) -> None:
        every = config["optimize_every"]
        if reviewer.feedback_trigger(every):
            reviewer.improve(stage="rubric", trigger="label-transitions")
        count = sum(event["kind"] == "human-feedback" and event.get("action") == "submitted"
                    for event in reviewer.core.history(100000))
        for stage in ("questions", "examples", "classifier"):
            due = count > 0 and count % every == 0
            reviewer.current_cycle.check_trigger(
                stage, due=due,
                reason="label cadence reached" if due else "label cadence not reached",
                details={"feedback_count": count, "threshold": every},
            )
            if due:
                reviewer.improve(stage=stage, trigger="feedback-cadence")

    @staticmethod
    def _metrics(reviewer: ReviewerFlywheel) -> None:
        truth, labels, probabilities = [], [], []
        for vote in reviewer.store._active_actions().values():
            if vote.action != "vote" or not vote.presentation_id:
                continue
            shown = next(presentation for presentation in reviewer.store.presentations_for(vote.article_id)
                         if presentation.id == vote.presentation_id)
            truth.append(vote.label)
            labels.append(shown.predicted_label)
            probabilities.append({label: shown.confidence if label == shown.predicted_label else 1 - shown.confidence
                                  for label in reviewer.core.initial.task.labels})
        metrics = classification_metrics(reviewer.core.initial.task.labels, truth, labels, probabilities)
        reviewer.core._emit({"kind": "cycle-metrics",
                             "metric_scope": "prequential reviewed predictions; not final held-out model accuracy",
                             "metrics": metrics})
