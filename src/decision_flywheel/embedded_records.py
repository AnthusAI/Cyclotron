"""Value types and the default lease of an embedded cyclotron.

These are the records an application sees (definitions, decisions, reviews) and
the single-writer lease. ``embedded_cyclotron`` re-exports every public name.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .models import DecisionTask
from .selection_policy import SelectionPolicy
from .sibling_check import SiblingPolicy


ExecutionGuard = Callable[[], None | Awaitable[None]]


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ClassifierSpec:
    """One classifier: a reviewed output of the cyclotron, with its own labels and ML model."""
    id: str
    labels: tuple[str, ...]
    question: str
    positive_label: str | None = None
    input_field: str = "text"

    def __post_init__(self):
        object.__setattr__(self, "labels", tuple(self.labels))
        task = self.task
        if self.positive_label is not None:
            task.validate_label(self.positive_label)

    @property
    def task(self) -> DecisionTask:
        return DecisionTask(self.id, self.labels, self.question, self.input_field)

    @property
    def selection_policy(self) -> SelectionPolicy:
        if self.positive_label:
            return SelectionPolicy("f1", positive_class=self.positive_label)
        return SelectionPolicy("f1", aggregation="macro")


@dataclass(frozen=True)
class CyclotronDefinition:
    """The application's cyclotron: an identity, its classifiers, and learning cadence."""
    id: str
    classifiers: tuple[ClassifierSpec, ...]
    seed: str = "cyclotron-v1"
    optimize_every: int = 20
    rubric_changes_every: int = 2
    # What promotion does when a change to one classifier would regress a
    # sibling on the sibling's development labels. Policy, not identity: it
    # is left out of the fingerprint so a store can change it.
    sibling_policy: SiblingPolicy = field(default_factory=SiblingPolicy)

    def __post_init__(self):
        object.__setattr__(self, "classifiers", tuple(self.classifiers))
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("cyclotron id must be a non-empty string")
        ids = [spec.id for spec in self.classifiers]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("a cyclotron needs distinct classifiers")
        if not isinstance(self.seed, str) or not self.seed:
            raise ValueError("seed must be a non-empty string")
        for value in (self.optimize_every, self.rubric_changes_every):
            if type(value) is not int or value < 1:
                raise ValueError("learning cadences must be positive integers")
        if not isinstance(self.sibling_policy, SiblingPolicy):
            raise ValueError("sibling_policy must be SiblingPolicy")

    @property
    def fingerprint(self) -> str:
        identity = asdict(self)
        identity.pop("sibling_policy")
        return hashlib.sha256(_json(identity).encode()).hexdigest()


@dataclass(frozen=True)
class ReviewSelection:
    """Whether a decision goes to a reviewer, why, and with what probability.

    ``reason`` is ``program`` (low confidence or sampled at the review rate),
    ``audit`` (the random audit share), or None when the decision is not sent
    to review. ``detail`` says the same in words.
    """
    selected: bool
    reason: str | None
    propensity: float
    detail: str = ""
    # With several outputs, each output's own selection; the fields above
    # summarize them (selected when any output is requested).
    outputs: Mapping[str, "ReviewSelection"] | None = None

    def for_output(self, classifier: str) -> "ReviewSelection":
        """This output's selection: its own with several outputs, else the decision's."""
        return self.outputs[classifier] if self.outputs else self

    @property
    def requested(self) -> tuple[str, ...] | None:
        """The outputs sent to review (None when the decision is selected as a whole)."""
        return None if self.outputs is None else tuple(cid for cid, s in self.outputs.items() if s.selected)


@dataclass(frozen=True)
class ClassifierDecision:
    classifier: str
    label: str
    confidence: float | None
    probabilities: Mapping[str, float] | None
    version: int
    fingerprint: str


@dataclass(frozen=True)
class Decision:
    """The cyclotron's decision about one item, with one result per classifier."""
    decision_id: str
    item_id: str
    created_at: str
    results: Mapping[str, ClassifierDecision]
    review: ReviewSelection

    def _only(self) -> ClassifierDecision:
        if len(self.results) != 1:
            raise ValueError("this cyclotron has several classifiers; read decision.results")
        return next(iter(self.results.values()))

    @property
    def label(self) -> str:
        return self._only().label

    @property
    def confidence(self) -> float | None:
        return self._only().confidence

    @property
    def probabilities(self) -> Mapping[str, float] | None:
        return self._only().probabilities

    @property
    def version(self) -> int:
        return self._only().version

    def to_json(self) -> dict[str, Any]:
        return {"decisionId": self.decision_id, "itemId": self.item_id, "createdAt": self.created_at,
                "classifiers": {cid: {"label": r.label, "confidence": r.confidence,
                                      "probabilities": dict(r.probabilities) if r.probabilities else None,
                                      "version": r.version, "fingerprint": r.fingerprint}
                                for cid, r in self.results.items()},
                "review": _selection_json(self.review)}

    @classmethod
    def from_json(cls, value) -> "Decision":
        return cls(value["decisionId"], value["itemId"], value["createdAt"],
                   {cid: ClassifierDecision(cid, r["label"], r["confidence"], r["probabilities"], r["version"],
                                            r["fingerprint"]) for cid, r in value["classifiers"].items()},
                   _selection(value["review"]))


def _selection_json(selection: ReviewSelection) -> dict[str, Any]:
    value = {"selected": selection.selected, "reason": selection.reason,
             "propensity": selection.propensity, "detail": selection.detail}
    if selection.outputs is not None:
        value["outputs"] = {cid: _selection_json(output) for cid, output in selection.outputs.items()}
    return value


def _selection(value: Mapping[str, Any]) -> ReviewSelection:
    if "detail" not in value:  # decisions saved before the review-rate program
        return ReviewSelection(value["selected"], "program", value["propensity"], value["reason"])
    outputs = value.get("outputs")
    return ReviewSelection(value["selected"], value["reason"], value["propensity"], value["detail"],
                           {cid: _selection(output) for cid, output in outputs.items()} if outputs is not None else None)


@dataclass(frozen=True)
class Review:
    """One recorded review: a label, a correction, an undo, or a review without a label."""
    review_id: str
    decision_id: str
    item_id: str
    classifier: str
    kind: str
    label: str | None
    explanation: str | None = None
    reason_code: str | None = None
    reviewer: str | None = None
    selected_by: str = "program"
    created_at: str = field(default_factory=_now)
    optimization_warnings: tuple = ()

    def to_json(self) -> dict[str, Any]:
        return {"reviewId": self.review_id, "decisionId": self.decision_id, "itemId": self.item_id,
                "classifier": self.classifier, "kind": self.kind, "label": self.label,
                "explanation": self.explanation, "reasonCode": self.reason_code, "reviewer": self.reviewer,
                "selectedBy": self.selected_by, "createdAt": self.created_at}


class StoreLocked(RuntimeError):
    """Another writer holds this cyclotron store."""


class FileLease:
    """The default single-writer lease: an exclusive lock file in the store directory.

    The operating system releases it when the process ends, including a crash.
    An application with several workers supplies its own lease with the same
    two methods (Papyrus uses an exclusive Assignment claim).
    """

    def __init__(self, directory):
        self.path = Path(directory) / "writer.lock"
        self._handle = None

    def acquire(self) -> None:
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise StoreLocked(f"another writer holds {self.path.parent}") from None
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            import fcntl
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
