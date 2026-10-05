"""Text-free, restart-safe observations of reusable flywheel rounds.

Applications own their UI, artifacts, and source records.  This module records
only the structured facts a UI needs to show how a flywheel is operating:
which policy is active, what trials were measured, their outcomes, and whether
new input has made the latest policy stale.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from .context import input_hash
from .models import DecisionTask, LabeledItem


_OUTCOMES = frozenset({"promoted", "incumbent-retained", "incomplete"})
_TRIAL_STATUSES = frozenset({"completed", "incomplete", "not-run"})
_OBJECTIVES = frozenset({"accuracy", "macro-f1", "brier"})
_FEATURE_ACTIVITY_STATUSES = frozenset({"proposed", "awaiting-review", "rejected", "evaluated", "promoted",
                                        "not-promoted", "fit-failed", "guard-rejected"})


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).encode("utf-8")).hexdigest()


def _digest(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a SHA-256 fingerprint")
    return value


def feedback_fingerprint(task: DecisionTask, labels: tuple[LabeledItem, ...] | list[LabeledItem]) -> str:
    """Return a text-free identity for the trusted feedback available to a round."""
    records = []
    for row in sorted(labels, key=lambda candidate: candidate.item.id):
        context = json.dumps(dict(row.context), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        records.append((row.item.id, row.label, input_hash(task, row.item),
                        hashlib.sha256(context.encode("utf-8")).hexdigest()))
    return _fingerprint(records)


def _count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class TrialActivity:
    """One measured candidate trial, without items, labels, prompts, or outputs."""

    name: str
    status: str
    decisions: int
    cached_decisions: int
    objective: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("trial name must be non-empty")
        if self.status not in _TRIAL_STATUSES:
            raise ValueError("trial status is not recognized")
        _count("trial decisions", self.decisions)
        _count("cached trial decisions", self.cached_decisions)
        if self.cached_decisions > self.decisions:
            raise ValueError("cached trial decisions cannot exceed decisions")
        if self.objective is not None and (isinstance(self.objective, bool)
                                           or not isinstance(self.objective, (float, int))
                                           or not math.isfinite(self.objective)):
            raise ValueError("trial objective must be finite or omitted")


@dataclass(frozen=True)
class FeatureDefinition:
    """A currently active decision element, without its source records or prompt text."""

    key: str
    question_type: str
    feature_names: tuple[str, ...]
    definition_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("feature key must be non-empty")
        if not isinstance(self.question_type, str) or not self.question_type:
            raise ValueError("feature question type must be non-empty")
        if (not isinstance(self.feature_names, tuple) or not self.feature_names
                or any(not isinstance(name, str) or not name for name in self.feature_names)
                or len(set(self.feature_names)) != len(self.feature_names)):
            raise ValueError("feature names must be unique non-empty strings")
        _digest("feature definition fingerprint", self.definition_fingerprint)


@dataclass(frozen=True)
class FeatureActivity:
    """A structured optimizer action concerning one decision element.

    ``reason_code`` is deliberately a short code owned by the application; an
    optimizer's unbounded prose rationale is not a safe substitute for measured
    evidence and does not enter the reusable ledger.
    """

    proposal_fingerprint: str
    feature_key: str
    status: str
    reason_code: str | None
    objective_delta: float | None

    def __post_init__(self) -> None:
        _digest("feature proposal fingerprint", self.proposal_fingerprint)
        if not isinstance(self.feature_key, str) or not self.feature_key:
            raise ValueError("feature activity key must be non-empty")
        if self.status not in _FEATURE_ACTIVITY_STATUSES:
            raise ValueError("feature activity status is not recognized")
        if self.reason_code is not None and (not isinstance(self.reason_code, str) or not self.reason_code):
            raise ValueError("feature activity reason code must be non-empty or omitted")
        if self.objective_delta is not None and (isinstance(self.objective_delta, bool)
                                                 or not isinstance(self.objective_delta, (float, int))
                                                 or not math.isfinite(self.objective_delta)):
            raise ValueError("feature activity objective delta must be finite or omitted")


@dataclass(frozen=True)
class FlywheelRound:
    """A complete, UI-consumable observation of one measured flywheel round."""

    task_fingerprint: str
    input_fingerprint: str
    active_policy_fingerprint: str
    model_fingerprint: str
    feedback_count: int
    candidate_count: int
    development_count: int
    objective_name: str
    winner: str
    promoted: bool
    outcome: str
    calls_attempted: int
    calls_succeeded: int
    trials: tuple[TrialActivity, ...]
    active_features: tuple[FeatureDefinition, ...] = ()
    feature_activity: tuple[FeatureActivity, ...] = ()
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        for name in ("task_fingerprint", "input_fingerprint", "active_policy_fingerprint"):
            _digest(name, getattr(self, name))
        if not isinstance(self.model_fingerprint, str) or not self.model_fingerprint:
            raise ValueError("model fingerprint must be non-empty")
        for name in ("feedback_count", "candidate_count", "development_count", "calls_attempted", "calls_succeeded"):
            _count(name, getattr(self, name))
        if self.calls_succeeded > self.calls_attempted:
            raise ValueError("successful calls cannot exceed attempted calls")
        if self.objective_name not in _OBJECTIVES:
            raise ValueError("objective name is not recognized")
        if not isinstance(self.winner, str) or not self.winner:
            raise ValueError("winner must be non-empty")
        if not isinstance(self.promoted, bool):
            raise ValueError("promoted must be a boolean")
        if self.outcome not in _OUTCOMES:
            raise ValueError("outcome is not recognized")
        if self.promoted != (self.outcome == "promoted"):
            raise ValueError("promotion must match the recorded outcome")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise ValueError("created_at must be non-empty")
        if not isinstance(self.trials, tuple) or any(not isinstance(trial, TrialActivity) for trial in self.trials):
            raise ValueError("trials must be TrialActivity values")
        if len({trial.name for trial in self.trials}) != len(self.trials):
            raise ValueError("trial names must be unique")
        if (not isinstance(self.active_features, tuple)
                or any(not isinstance(feature, FeatureDefinition) for feature in self.active_features)
                or len({feature.key for feature in self.active_features}) != len(self.active_features)):
            raise ValueError("active features must have unique keys")
        if (not isinstance(self.feature_activity, tuple)
                or any(not isinstance(activity, FeatureActivity) for activity in self.feature_activity)):
            raise ValueError("feature activity must contain FeatureActivity values")

    @property
    def fingerprint(self) -> str:
        """Stable identity for an equivalent measured round across restarts."""
        document = self.to_document(include_created_at=False)
        return _fingerprint(document)

    def to_document(self, *, include_created_at: bool = True) -> dict[str, object]:
        value = asdict(self)
        if not include_created_at:
            value.pop("created_at")
            if not value["active_features"]:
                value.pop("active_features")
            if not value["feature_activity"]:
                value.pop("feature_activity")
        return value

    @classmethod
    def from_document(cls, value: object) -> "FlywheelRound":
        if not isinstance(value, dict):
            raise ValueError("run ledger entry must be an object")
        expected = {"task_fingerprint", "input_fingerprint", "active_policy_fingerprint", "model_fingerprint",
                    "feedback_count", "candidate_count", "development_count", "objective_name", "winner",
                    "promoted", "outcome", "calls_attempted", "calls_succeeded", "trials", "created_at"}
        optional = {"active_features", "feature_activity"}
        expected |= optional
        if ((set(value) != expected and set(value) != expected - optional) or not isinstance(value["trials"], list)
                or ("active_features" in value and not isinstance(value["active_features"], list))
                or ("feature_activity" in value and not isinstance(value["feature_activity"], list))):
            raise ValueError("run ledger entry has unexpected fields")
        try:
            trials = tuple(TrialActivity(**trial) for trial in value["trials"])
            active_features = tuple(FeatureDefinition(
                feature["key"], feature["question_type"], tuple(feature["feature_names"]),
                feature["definition_fingerprint"]
            ) for feature in value.get("active_features", []))
            feature_activity = tuple(FeatureActivity(**activity) for activity in value.get("feature_activity", []))
            return cls(**{key: value[key] for key in expected if key not in {
                "trials", "active_features", "feature_activity"
            } and key in value
            }, trials=trials, active_features=active_features, feature_activity=feature_activity)
        except (TypeError, ValueError) as error:
            raise ValueError("run ledger entry is invalid") from error


@dataclass(frozen=True)
class FlywheelStatus:
    """Small read model suitable for terminals, APIs, or a web UI."""

    phase: Literal["never-run", "current", "stale"]
    latest: FlywheelRound | None
    completed_rounds: int


class JsonlRunLedger:
    """A local append-only run ledger that survives process restarts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def history(self) -> tuple[FlywheelRound, ...]:
        if not self.path.exists():
            return ()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise ValueError("run ledger is unreadable") from error
        rounds = []
        seen = set()
        for line in lines:
            if not line.strip():
                continue
            try:
                round_ = FlywheelRound.from_document(json.loads(line))
            except (json.JSONDecodeError, ValueError) as error:
                raise ValueError("run ledger contains an invalid entry") from error
            if round_.fingerprint in seen:
                raise ValueError("run ledger contains a duplicate measured round")
            seen.add(round_.fingerprint)
            rounds.append(round_)
        return tuple(rounds)

    def append(self, round_: FlywheelRound) -> bool:
        if not isinstance(round_, FlywheelRound):
            raise ValueError("only FlywheelRound values may enter the run ledger")
        if round_.fingerprint in {entry.fingerprint for entry in self.history()}:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(round_.to_document(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        return True

    def status(self, current_input_fingerprint: str) -> FlywheelStatus:
        _digest("current input fingerprint", current_input_fingerprint)
        history = self.history()
        latest = history[-1] if history else None
        if latest is None:
            return FlywheelStatus("never-run", None, 0)
        phase: Literal["current", "stale"] = (
            "current" if latest.input_fingerprint == current_input_fingerprint else "stale"
        )
        return FlywheelStatus(phase, latest, len(history))
