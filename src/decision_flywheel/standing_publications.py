"""Validate streaming items submitted to a trusted standing publication run."""

import json
import re

_ITEM_ID = re.compile(r"[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]*[^\s\x00-\x1f\x7f])?")


def validate_item(item, input_fields):
    """Shape a never-before-seen source. Returns (id, values)."""
    if not isinstance(item, dict) or set(item) != {"id", "values"}:
        raise ValueError("a structured source with id and values is required")
    identifier, values = item["id"], item["values"]
    if not isinstance(identifier, str) or not _ITEM_ID.fullmatch(identifier):
        raise ValueError("source id must be printable characters without edge whitespace")
    if not isinstance(values, dict) or not values or any(not isinstance(k, str) or not k for k in values):
        raise ValueError("source values must be a non-empty object with named fields")
    try:
        encoded = json.dumps(values, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ValueError("source values must be plain JSON") from None
    for field in input_fields:
        text = values.get(field)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"source lacks text field {field!r}")
    return identifier, json.loads(encoded)
