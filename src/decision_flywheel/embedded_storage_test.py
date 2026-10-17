"""Embedded root transactions keep derived evidence and its source cursor together."""

import asyncio

import pytest

from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import DEFINITION, Model, item
from .embedded_storage import SQLiteEmbeddedStorage


def test_derived_records_usage_and_cursor_all_rollback_together(tmp_path):
    store = SQLiteEmbeddedStorage(tmp_path / "root.sqlite")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.insert_review("r", "d", "c", '{"explanation":"exact"}')
            store.append_event('{"kind":"review"}')
            store.insert_usage("d", "optimizer", 1, 0, 2, "c:8")
            store.put_meta("cursor:c", "8")
            raise RuntimeError("synthetic interruption")
    assert store.meta("cursor:c") is None
    assert store.review("r") is None
    assert store.events(0, 100) == [] and store.usage_counts() == []
    store.close()


def test_embedded_public_operations_use_root_domain_storage_without_engine_sql(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as engine:
        native = engine.db

        class Forbidden:
            def __getattr__(self, name):
                raise AssertionError("embedded algorithm issued SQL outside root repository")

        engine.db = Forbidden()
        try:
            decision = asyncio.run(engine.decide(item("one")))
            asyncio.run(engine.review(decision.decision_id, "include", review_id="human", explanation="Exact reason"))
            assert engine.decision(decision.decision_id) == decision
            assert engine.labels()[0]["explanation"] == "Exact reason"
            assert engine.status().pending.decisions_awaiting_review == 0
            assert any(event["kind"] == "review" for event in engine.subscribe()["events"])
            assert engine.decision_log()[0]["reviews"]["relevant"]["label"] == "include"
            assert engine.usage(
                {
                    "decision_model": {"input_usd_per_mtok": 1, "output_usd_per_mtok": 1},
                    "optimizer": {"input_usd_per_mtok": 1, "output_usd_per_mtok": 1},
                }
            )["total"]
        finally:
            engine.db = native


def test_failed_cursor_commit_can_retry_same_source_page_without_losing_or_duplicating_review(tmp_path, monkeypatch):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as engine:
        decision = asyncio.run(engine.decide(item("one")))
        wheel = engine.wheels["relevant"]
        # Record canonical source feedback without running the root derivation.
        wheel._emit(
            {
                "kind": "human-skipped",
                "review_id": "human",
                "decision_id": decision.decision_id,
                "target_id": "one",
                "selected_by": "program",
                "explanation": "Exact explanation",
            }
        )
        before_seen = engine._seen["relevant"]
        before_cursor = engine.root_storage.meta("cursor:relevant")
        put = engine.root_storage.put_meta

        def interrupt(key, payload):
            if key == "cursor:relevant":
                raise RuntimeError("synthetic cursor commit failure")
            return put(key, payload)

        monkeypatch.setattr(engine.root_storage, "put_meta", interrupt)
        with pytest.raises(RuntimeError, match="cursor commit failure"):
            engine._sync()
        assert engine._seen["relevant"] == before_seen
        assert engine.root_storage.meta("cursor:relevant") == before_cursor
        assert engine._review("human") is None
        monkeypatch.setattr(engine.root_storage, "put_meta", put)
        engine._sync()
        assert engine._review("human").explanation == "Exact explanation"
        count = len([event for event in engine.subscribe()["events"] if event["kind"] == "review"])
        engine._sync()
        assert len([event for event in engine.subscribe()["events"] if event["kind"] == "review"]) == count == 1
