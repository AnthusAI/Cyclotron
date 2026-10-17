"""Native answer revisions and learning trials retain their restart semantics."""

import asyncio
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from .classifier_config import ClassifierConfig
from .decision_cache import CacheOptions
from .flywheel import DecisionFlywheel
from .flywheel_test import DEV, TASK, TRAIN, FakeModel, agent
from .runtime_cache_storage import SQLiteRuntimeCacheStorage


def test_refresh_preserves_old_response_and_history_when_provider_identity_advances(tmp_path):
    path = tmp_path / "runtime.sqlite"
    db = sqlite3.connect(path)
    store = SQLiteRuntimeCacheStorage(db)
    original = ("complete", '{ "answers": "original 雪" }')
    store.begin_answer("old", None)
    store.complete_answer("old", original[1], previous=None)
    store.begin_answer("old", original)
    store.complete_answer("old", '{"answers":"fresh"}', previous=original, current_key="new")
    assert store.answer("old") == original
    assert store.answer("new") == ("complete", '{"answers":"fresh"}')
    db.close()
    db = sqlite3.connect(path)
    store = SQLiteRuntimeCacheStorage(db)
    assert store.answer("old") == original
    assert db.execute("SELECT key,status,payload FROM runtime_answer_history").fetchall() == [("old", *original)]
    db.close()


def test_failed_and_pending_answers_and_trials_survive_restart_without_implicit_retry(tmp_path):
    path = tmp_path / "runtime.sqlite"
    db = sqlite3.connect(path)
    store = SQLiteRuntimeCacheStorage(db)
    store.begin_answer("pending", None)
    store.begin_answer("failed", None)
    store.fail_answer("failed")
    store.begin_round("trial")
    db.close()
    db = sqlite3.connect(path)
    store = SQLiteRuntimeCacheStorage(db)
    assert store.answer("pending") == ("pending", None)
    assert store.answer("failed") == ("failed", None)
    assert store.round("trial") == ("pending", None)
    with pytest.raises(sqlite3.IntegrityError):
        store.begin_round("trial")
    store.reset_pending_round("trial")
    store.begin_round("trial")
    store.complete_round("trial", '{ "answer_dependencies": [], "result": {} }')
    store.reset_pending_round("trial")
    assert json.loads(store.round("trial")[1]) == {"answer_dependencies": [], "result": {}}
    db.close()


def test_answer_cache_and_dependency_paths_do_not_issue_wheel_sql(tmp_path):
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), FakeModel(), agent([]))
    native = wheel.db

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("answer cache issued SQL outside its repository")

    wheel.db = Forbidden()
    try:
        config = wheel.active.config
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        asyncio.run(wheel._answers(config, DEV[0].item, TRAIN, now))
        dependency = wheel.answer_dependency(config, DEV[0].item, TRAIN, now)
        assert wheel.answer_dependencies_current([dependency])
        asyncio.run(wheel._answers(config, DEV[0].item, TRAIN, now, CacheOptions(policy="cache_only")))
        assert wheel.model.calls == 1
        wheel.runtime_cache.begin_round("trial")
        assert wheel.cached_artifact("runtime_rounds", "key", "trial") == ("trial", ("pending", None))
        wheel.runtime_cache.save_completed_round(
            "trial",
            json.dumps(
                {"answer_dependencies": [], "result": {"improved": False, "selection_policy": asdict(wheel.selection_policy)}}
            ),
        )
        assert wheel.cached_artifact("runtime_rounds", "key", "trial")[1][0] == "complete"
        with pytest.raises(ValueError, match="did not improve"):
            wheel.promote_trial("trial", TRAIN, DEV)
    finally:
        wheel.db = native
        wheel.close()


def test_identity_move_rolls_back_as_one_transaction_if_publication_fails(tmp_path):
    db = sqlite3.connect(tmp_path / "runtime.sqlite")
    store = SQLiteRuntimeCacheStorage(db)
    previous = ("complete", '{"answer":"old"}')
    store.begin_answer("old", None)
    store.complete_answer("old", previous[1], previous=None)
    store.begin_answer("old", previous)
    db.execute(
        "CREATE TRIGGER reject_completion BEFORE UPDATE ON runtime_answers WHEN NEW.key='new' AND NEW.status='complete' BEGIN SELECT RAISE(ABORT,'synthetic crash'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic crash"):
        store.complete_answer("old", '{"answer":"new"}', previous=previous, current_key="new")
    assert store.answer("old") == ("pending", None)
    assert store.answer("new") is None
    db.execute("DROP TRIGGER reject_completion")
    store.complete_answer("old", '{"answer":"new"}', previous=previous, current_key="new")
    assert store.answer("old") == previous
    assert store.answer("new") == ("complete", '{"answer":"new"}')
    db.close()
