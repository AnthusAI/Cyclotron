"""Text-free events emitted while a reusable flywheel is measuring a round."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable


_EVENT_TYPES = frozenset({
    "round-started", "trial-started", "decision-reused", "decision-requested",
    "decision-completed", "decision-failed", "trial-completed", "trial-incomplete",
    "round-completed",
})


def _fingerprint(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("request fingerprint must be a SHA-256 fingerprint")
    return value


def _count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class FlywheelEvent:
    """One observable optimizer action, intentionally free of prompts and item text."""

    event_type: str
    trial_name: str | None
    request_fingerprint: str | None
    calls_attempted: int
    calls_succeeded: int
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if self.event_type not in _EVENT_TYPES:
            raise ValueError("event type is not recognized")
        if self.trial_name is not None and (not isinstance(self.trial_name, str) or not self.trial_name):
            raise ValueError("trial name must be non-empty or omitted")
        if self.request_fingerprint is not None:
            _fingerprint(self.request_fingerprint)
        _count("calls attempted", self.calls_attempted)
        _count("calls succeeded", self.calls_succeeded)
        if self.calls_succeeded > self.calls_attempted:
            raise ValueError("successful calls cannot exceed attempted calls")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise ValueError("created_at must be non-empty")

    @classmethod
    def from_document(cls, value: object) -> "FlywheelEvent":
        expected = {"event_type", "trial_name", "request_fingerprint", "calls_attempted", "calls_succeeded", "created_at"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("event stream entry has unexpected fields")
        try:
            return cls(**value)
        except (TypeError, ValueError) as error:
            raise ValueError("event stream entry is invalid") from error


EventSink = Callable[[FlywheelEvent], None]


class JsonlEventStream:
    """Durable append-only events that a terminal, service, or web UI can tail."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, event: FlywheelEvent) -> None:
        if not isinstance(event, FlywheelEvent):
            raise ValueError("only FlywheelEvent values may enter the event stream")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()

    def history(self) -> tuple[FlywheelEvent, ...]:
        if not self.path.exists():
            return ()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise ValueError("event stream is unreadable") from error
        events = []
        for line in lines:
            if not line.strip():
                continue
            try:
                events.append(FlywheelEvent.from_document(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as error:
                raise ValueError("event stream contains an invalid entry") from error
        return tuple(events)
