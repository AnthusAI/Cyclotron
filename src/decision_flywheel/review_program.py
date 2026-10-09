"""How many decisions people review: the review-rate program.

The program starts at full review and steps the rate down only while the
evidence says the cyclotron can be trusted, and steps back to full review as
soon as it cannot. Every number here is configuration. The defaults are
placeholders until the "Longer editorial runs and review-rate heuristics"
experiments set them.

Selection never reads the hidden label: it uses the decision's confidence, the
current rate, and a deterministic random draw keyed by the decision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
from typing import Any, Mapping, Sequence

REASONS = ("program", "audit", "reviewer")


@dataclass(frozen=True)
class ReviewProgram:
    """Configuration of the default review-rate program."""
    rates: tuple[float, ...] = (1.0, .5, .25, .1)
    audit_share: float = .05
    confidence_threshold: float = .7
    target_accuracy: float = .85
    max_gap_points: float = 5.
    window: int = 100
    windows_required: int = 2
    calibration_window: int = 200
    min_window_labels: int = 20
    seed: str = "review-program-v1"

    def __post_init__(self):
        object.__setattr__(self, "rates", tuple(float(rate) for rate in self.rates))
        rates = self.rates
        if not rates or rates[0] != 1. or any(not 0 < rate <= 1 for rate in rates) or any(
                later >= earlier for earlier, later in zip(rates, rates[1:])):
            raise ValueError("rates must start at 1.0 and decrease strictly, each in (0, 1]")
        for name in ("audit_share", "confidence_threshold", "target_accuracy"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")
        if not 0 <= self.max_gap_points <= 100:
            raise ValueError("max_gap_points must be between 0 and 100")
        for name in ("window", "windows_required", "calibration_window", "min_window_labels"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.seed, str) or not self.seed:
            raise ValueError("seed must be a non-empty string")

    def manifest(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Selection:
    """Whether one decision goes to review, why, and with what probability."""
    selected: bool
    reason: str | None
    propensity: float
    detail: str


@dataclass(frozen=True)
class Override:
    rate: float
    expires_at: str | None
    set_by: str
    set_at: str

    def active(self, now: datetime) -> bool:
        return self.expires_at is None or datetime.fromisoformat(self.expires_at) > now


@dataclass
class ProgramState:
    """The program's durable state; the application stores it as JSON."""
    index: int = 0
    raised: bool = False
    consecutive: int = 0
    checked_windows: int = 0
    version: int = 1
    reason: str = "Onboarding: every decision is reviewed until the cyclotron earns a lower rate."
    last_check: dict[str, Any] | None = None
    override: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: Mapping[str, Any] | None) -> "ProgramState":
        return cls(**value) if value else cls()


def _percent(value: float) -> str:
    return f"{round(value * 100)}%"


def _draw(seed: str, key: str) -> float:
    return int.from_bytes(hashlib.sha256(f"{seed}:{key}".encode()).digest()[:8], "big") / 2 ** 64


def current_rate(program: ReviewProgram, state: ProgramState, now: datetime) -> float:
    override = Override(**state.override) if state.override else None
    if override and override.active(now):
        return override.rate
    return program.rates[state.index]


def select(program: ReviewProgram, rate: float, *, key: str, confidence: float | None) -> Selection:
    """Choose before the label exists: low confidence always, otherwise a random share."""
    if confidence is None or confidence < program.confidence_threshold:
        shown = "unknown" if confidence is None else _percent(confidence)
        return Selection(True, "program", 1.0,
                         f"Confidence {shown} is below the {_percent(program.confidence_threshold)} threshold.")
    propensity = max(rate, program.audit_share)
    draw = _draw(program.seed, key)
    if draw < program.audit_share:
        return Selection(True, "audit", propensity, "Random audit sample.")
    if draw < rate:
        return Selection(True, "program", propensity, f"Sampled at the {_percent(rate)} review rate.")
    return Selection(False, None, propensity, f"Confident decision not sampled at the {_percent(rate)} review rate.")


def state_name(program: ReviewProgram, state: ProgramState, now: datetime) -> str:
    if state.override and Override(**state.override).active(now):
        return "manual"
    if state.index == 0:
        return "raised" if state.raised else "onboarding"
    return "steady" if state.index == len(program.rates) - 1 else "tapering"


def window_evidence(program: ReviewProgram, reviews: Sequence[Mapping[str, Any]], gap_points: int | None) -> dict:
    """Evidence for one window: accuracy on confident decisions and the calibration gap.

    ``reviews`` are the window's program and audit reviews, each with the
    decision's ``confidence``, its ``decision`` label and the reviewer's ``label``.
    Confident decisions are sampled uniformly at the window's rate, so their
    reviews estimate accuracy at the threshold without bias.
    """
    confident = [row for row in reviews if row["confidence"] is not None
                 and row["confidence"] >= program.confidence_threshold]
    correct = sum(row["decision"] == row["label"] for row in confident)
    accuracy = correct / len(confident) if confident else None
    enough = len(confident) >= program.min_window_labels and gap_points is not None
    met = bool(enough and accuracy >= program.target_accuracy and gap_points < program.max_gap_points)
    return {"labels": len(confident), "accuracy": accuracy, "gap_points": gap_points,
            "enough_evidence": enough, "met": met}


def check_window(program: ReviewProgram, state: ProgramState, evidence: Mapping[str, Any]) -> tuple[ProgramState, str | None]:
    """Apply one finished window. Returns the new state and the change, if the rate moved."""
    state = ProgramState(**{**state.to_json(), "checked_windows": state.checked_windows + 1,
                            "last_check": dict(evidence)})
    if not evidence["enough_evidence"]:
        state.reason = (f"Holding at {_percent(program.rates[state.index])}: only {evidence['labels']} reviewed "
                        f"confident decisions in the last window; {program.min_window_labels} are needed.")
        return state, None
    if not evidence["met"]:
        if state.index == 0:
            state.consecutive = 0
            state.reason = ("Every decision is reviewed: " + _shortfall(program, evidence) + ".")
            return state, None
        state.index, state.raised, state.consecutive = 0, True, 0
        state.reason = "Back to full review: " + _shortfall(program, evidence) + "."
        return state, "raised"
    state.consecutive += 1
    if state.consecutive >= program.windows_required and state.index < len(program.rates) - 1:
        state.index, state.consecutive = state.index + 1, 0
        state.reason = (f"Stepped down to {_percent(program.rates[state.index])}: accuracy on confident decisions "
                        f"{_percent(evidence['accuracy'])} met the {_percent(program.target_accuracy)} target and the "
                        f"calibration gap was {evidence['gap_points']} points for {program.windows_required} windows in a row.")
        return state, "stepped-down"
    state.reason = (f"Conditions met for {state.consecutive} of {program.windows_required} windows at "
                    f"{_percent(program.rates[state.index])}.")
    return state, None


def raise_for_version(program: ReviewProgram, state: ProgramState, version: int) -> tuple[ProgramState, bool]:
    """A promoted version has unmeasured alignment: review everything again."""
    if version <= state.version:
        return state, False
    state = ProgramState(**{**state.to_json(), "version": version})
    if state.index == 0 and state.consecutive == 0:
        return state, False
    state.index, state.raised, state.consecutive = 0, True, 0
    state.reason = f"Back to full review: version {version} was promoted and its alignment is not yet measured."
    return state, True


def _shortfall(program: ReviewProgram, evidence: Mapping[str, Any]) -> str:
    parts = []
    if evidence["accuracy"] is not None and evidence["accuracy"] < program.target_accuracy:
        parts.append(f"accuracy on confident decisions was {_percent(evidence['accuracy'])}, "
                     f"below the {_percent(program.target_accuracy)} target")
    if evidence["gap_points"] is not None and evidence["gap_points"] >= program.max_gap_points:
        parts.append(f"the calibration gap was {evidence['gap_points']} points, "
                     f"not under {program.max_gap_points:g}")
    return " and ".join(parts) or "the conditions were not met"


def next_step(program: ReviewProgram, state: ProgramState, *, decisions_until_check: int, now: datetime) -> str:
    """The next step's conditions, in words a reviewer can read."""
    if state.override and Override(**state.override).active(now):
        override = Override(**state.override)
        until = f" until {override.expires_at}" if override.expires_at else ""
        return f"Manual rate of {_percent(override.rate)} set by {override.set_by}{until}; the program resumes after it."
    if state.index == len(program.rates) - 1:
        return (f"At the lowest rate. The next check is in {decisions_until_check} decisions; review goes back to "
                f"full if accuracy on confident decisions falls below {_percent(program.target_accuracy)} or the "
                f"calibration gap reaches {program.max_gap_points:g} points.")
    remaining = program.windows_required - state.consecutive
    return (f"Steps down to {_percent(program.rates[state.index + 1])} after {remaining} more window"
            f"{'s' if remaining != 1 else ''} of {program.window} decisions with accuracy on confident decisions "
            f"of at least {_percent(program.target_accuracy)} (at least {program.min_window_labels} reviewed) and a "
            f"calibration gap under {program.max_gap_points:g} points. Next check in {decisions_until_check} decisions.")


def expected_reviews(program: ReviewProgram, rate: float, confidences: Sequence[float | None]) -> float:
    """Expected reviews for these decisions at this rate (for example, last week's)."""
    low = sum(1 for c in confidences if c is None or c < program.confidence_threshold)
    return round(low + (len(confidences) - low) * max(rate, program.audit_share), 1)

