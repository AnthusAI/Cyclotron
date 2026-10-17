"""Learning repositories preserve native evidence and transaction boundaries."""

import json
import sqlite3

import pytest

from .learning_storage import SQLiteControlStorage, SQLiteFeatureStorage, SQLiteLearningStorage


def test_learning_artifacts_keep_pending_progress_and_complete_aliases_atomically(tmp_path):
    db = sqlite3.connect(tmp_path / "learning.sqlite")
    store = SQLiteLearningStorage(db)
    store.ensure("rubric")
    store.begin("rubric", "old", '{ "pending_evaluation": true, "note": "雪" }')
    store.complete("rubric", "old", '{ "pending_evaluation": true }')
    store.begin("rubric", "new", '{"progress":1}')
    db.execute(
        "CREATE TRIGGER fail_alias BEFORE INSERT ON optimization_stages WHEN NEW.id='alias' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.complete("rubric", "new", '{"done":true}', aliases=("alias",), clear_pending_key="old")
    assert store.artifact("rubric", "new") == ("pending", '{"progress":1}')
    assert json.loads(store.artifact("rubric", "old")[1])["pending_evaluation"] is True
    db.execute("DROP TRIGGER fail_alias")
    store.complete("rubric", "new", '{"done":true}', aliases=("alias",), clear_pending_key="old")
    assert store.artifact("rubric", "new") == store.artifact("rubric", "alias") == ("complete", '{"done":true}')
    assert json.loads(store.artifact("rubric", "old")[1])["pending_evaluation"] is False
    db.close()


def test_fixed_learning_kinds_reject_sql_names_and_preserve_exact_record_order(tmp_path):
    db = sqlite3.connect(tmp_path / "learning.sqlite")
    store = SQLiteLearningStorage(db)
    assert store.completed("examples") == []
    with pytest.raises(ValueError):
        store.ensure("optimization_stages; DROP TABLE x")
    store.ensure("examples")
    store.begin("examples", "b", '{ "source": 9007199254740993 }')
    store.complete("examples", "b", '{ "source": 9007199254740993 }')
    store.begin("examples", "a", "{}")
    store.complete("examples", "a", "{}")
    assert store.completed("examples") == [("a", "{}"), ("b", '{ "source": 9007199254740993 }')]
    assert store.count("examples") == 2
    db.close()


def test_features_and_control_ideas_preserve_raw_payloads_and_legacy_native_tables(tmp_path):
    path = tmp_path / "learning.sqlite"
    db = sqlite3.connect(path)
    feature, control = SQLiteFeatureStorage(db), SQLiteControlStorage(db)
    feature.save("f", '{ "id":"f", "explanation":"Exact 雪" }')
    control.save("i", '{ "id":"i", "parents":["prior"] }')
    db.close()
    db = sqlite3.connect(path)
    assert SQLiteFeatureStorage(db).payloads() == ('{ "id":"f", "explanation":"Exact 雪" }',)
    assert SQLiteControlStorage(db).payload("i") == '{ "id":"i", "parents":["prior"] }'
    assert SQLiteControlStorage(db).payloads() == ('{ "id":"i", "parents":["prior"] }',)
    db.close()
