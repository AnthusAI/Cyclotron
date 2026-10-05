"""Specs for the reusable text-free live optimizer event stream."""
from __future__ import annotations

from .events import FlywheelEvent, JsonlEventStream


def test_an_event_stream_persists_structured_optimizer_activity_without_source_text(tmp_path):
    path = tmp_path / "events.jsonl"
    stream = JsonlEventStream(path)
    event = FlywheelEvent("trial-started", "incumbent", None, 0, 0)

    stream.append(event)

    assert stream.history() == (event,)
    assert "text" not in path.read_text(encoding="utf-8").casefold()


def test_event_stream_rejects_unrecognized_event_types_or_non_fingerprint_request_keys(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="event type"):
        FlywheelEvent("the model reasoned", None, None, 0, 0)
    with pytest.raises(ValueError, match="request fingerprint"):
        FlywheelEvent("decision-requested", "incumbent", "not-a-hash", 1, 0)
