"""Allowlisted dynamic context for decision-model requests.

Dynamic context is intentionally explicit and narrow.  It is never sampled
from a host clock during fitting or replay: callers provide one timezone-aware
instant for the request, and the resulting state can be recorded with it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from .feedback import Scorecard


CURRENT_DATETIME_ELEMENT = "current_datetime"
CURRENT_DATETIME_QUESTION_TYPE = "programmatic_datetime"


def optimizer_dynamic_element_instruction() -> str:
    """The prompt fragment supplied to a steering analyst about safe dynamic context."""
    return (
        "One allowlisted programmatic decision element is available: current_datetime. "
        "It passes the request-time UTC datetime into the decision model as state.current_datetime. "
        "Use it only when the human decision may reasonably depend on recency, deadlines, "
        "or a time-relative policy. To propose it, return exactly "
        '{"add_programmatic_element":{"kind":"current_datetime"}}. '
        "It provides context, not learned weights, calibration, or a human label."
    )


def current_datetime_state(scorecard: Scorecard, now: datetime) -> dict[str, str]:
    """Return the UTC state field when the active scorecard selected this element."""
    if not any(element.key == CURRENT_DATETIME_ELEMENT for element in scorecard.elements):
        return {}
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("current datetime must be timezone-aware")
    utc = now.astimezone(timezone.utc).replace(microsecond=0)
    return {CURRENT_DATETIME_ELEMENT: utc.isoformat().replace("+00:00", "Z")}


def with_dynamic_state(state: Mapping[str, object], scorecard: Scorecard, now: datetime) -> dict[str, object]:
    """Return a request state with every selected programmatic value attached."""
    if not isinstance(state, Mapping):
        raise ValueError("request state must be a mapping")
    return {**state, **current_datetime_state(scorecard, now)}
