"""Validate bounded streaming items submitted to a trusted standing publication run."""

import json
import re

MAX_VALUES_BYTES = 32768
MAX_INPUT_CHARS = 20000
_ITEM_ID = re.compile(r"[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]{0,198}[^\s\x00-\x1f\x7f])?")


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
