"""Trusted policy for standing publication runs: request allowance and streaming item admission.

A standing target is declared only in operator configuration. Callers never choose the run, the
allowance, or whether a source may be registered.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

DEFAULT_MAX_ITEMS = 500
MAX_VALUES_BYTES = 32768
MAX_INPUT_CHARS = 20000
_ITEM_ID = re.compile(r"[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]{0,198}[^\s\x00-\x1f\x7f])?")


def today():
    return datetime.now(timezone.utc).date()


@dataclass(frozen=True)
class RequestCeiling:
    """Cumulative paid-request cap for the run's whole lifetime.

    ``per_day`` with ``since`` makes the cap accrue: allowance(day) = min(total, per_day * days
    elapsed since ``since``, counting that day). Unused allowance carries forward.
    """

    total: int
    per_day: int | None = None
    since: str | None = None

    def __post_init__(self):
        for value in (self.total, self.per_day):
            if value is not None and (type(value) is not int or not 1 <= value <= 100000):
                raise ValueError("request ceiling must be a positive bounded integer")
        if (self.per_day is None) != (self.since is None):
            raise ValueError("per-day allowance and its start date must be declared together")
        if self.since is not None:
            if not isinstance(self.since, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", self.since):
                raise ValueError("allowance start must be an ISO date")
            date.fromisoformat(self.since)

    def allowed(self, on=None):
        if self.per_day is None:
            return self.total
        days = ((on or today()) - date.fromisoformat(self.since)).days + 1
        return 0 if days < 1 else min(self.total, self.per_day * days)


def validate_item(item, input_fields):
    """Bound and shape a never-before-seen source. Returns (id, values)."""
    if not isinstance(item, dict) or set(item) != {"id", "values"}:
        raise ValueError("a structured source with id and values is required")
    identifier, values = item["id"], item["values"]
    if not isinstance(identifier, str) or not _ITEM_ID.fullmatch(identifier):
        raise ValueError("source id must be 1-200 printable characters without edge whitespace")
    if (
        not isinstance(values, dict)
        or not 1 <= len(values) <= 32
        or any(not isinstance(k, str) or not 1 <= len(k) <= 64 for k in values)
    ):
        raise ValueError("source values must be a bounded object")
    try:
        encoded = json.dumps(values, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ValueError("source values must be plain JSON") from None
    if len(encoded.encode()) > MAX_VALUES_BYTES:
        raise ValueError("source values exceed the size limit")
    for field in input_fields:
        text = values.get(field)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_CHARS:
            raise ValueError(f"source lacks bounded text field {field!r}")
    return identifier, json.loads(encoded)
