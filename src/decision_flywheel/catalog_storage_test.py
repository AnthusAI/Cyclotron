"""Catalog storage parity, transaction composition and lossless migration."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from .catalog_storage import CatalogConflict
from .web_store import WebStore
from .workspace_catalog_test import configuration


def populated(path):
    store = WebStore(path)
    store.save_classifier("topic", "Topic", configuration())
    store.save_classifier("topic", "Renamed", {**configuration(), "question": "Revised?"})
    store.save_item_list("papers", "Papers")
    item = {
        "id": "one",
        "occurred_at": "2026-01-01",
        "values": {"text": "雪", "integer": 9007199254740993},
        "provenance": {"source": "synthetic"},
    }
    store.upsert_list_items("papers", [item])
    store.upsert_list_items("papers", [{**item, "values": {"text": "New"}}])
    store.label_item("topic", 1, "papers", "one", 1, "science", "Original explanation 雪", "vote-one")
    store.label_item("topic", 1, "papers", "one", 1, "business", "Corrected explanation", "vote-two")
    store.retract_item_label("vote-two", "undo")
    store.label_item("topic", 2, "papers", "one", 2, "sport", "Other revision explanation", "vote-three")
    store.record_item_prediction(
        "external-run", "papers", "one", 1, {"label": "science", "probabilities": {"science": 0.6, "sport": 0.2, "business": 0.2}}
    )
    return store


def test_catalog_roundtrip_retains_all_versions_explanations_tombstones_and_raw_json(tmp_path):
    source = populated(tmp_path / "source.sqlite")
    # A native legacy payload may have whitespace/order that must not be lost.
    with source.connect() as db:
        raw = db.execute("SELECT config FROM classifiers WHERE id=? AND revision=1", ("topic",)).fetchone()[0]
        db.execute("UPDATE classifiers SET config=? WHERE id=? AND revision=1", ("  " + raw + "\n", "topic"))
        db.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='item_feedback'")
    snapshot = source.catalog_storage.export()
    destination = WebStore(tmp_path / "destination.sqlite")
    destination.catalog_storage.restore(json.loads(json.dumps(snapshot)))
    assert destination.catalog_storage.export() == snapshot
    assert destination.classifier_versions("topic") == source.classifier_versions("topic")
    assert destination.snapshot_items("papers") == source.snapshot_items("papers")
    assert destination.item_revision("papers", "one", 1)["values"]["integer"] == 9007199254740993
    assert destination.item_labels("papers", "one", 1) == []
    assert destination.item_labels("papers", "one", 2)[0]["comment"] == "Other revision explanation"
    vote = destination.label_item("topic", 2, "papers", "one", 2, "science", "Next", "vote-four")
    assert vote["sequence"] == 101
    with pytest.raises(CatalogConflict):
        destination.catalog_storage.restore(snapshot)


def test_more_items_and_larger_items_than_the_old_caps_upsert_page_and_export(tmp_path):
    store = WebStore(tmp_path / "large.sqlite")
    store.save_item_list("papers", "Papers")
    items = [{"id": f"item-{n:04d}", "occurred_at": "2026-01-01", "values": {"n": n}} for n in range(250)]
    items.append({"id": "big", "occurred_at": "2026-01-02", "values": {"text": "x" * (5 * 1024 * 1024)}})
    assert store.upsert_list_items("papers", items)["inserted"] == 251
    assert len(store.list_items("papers", limit=500)) == 251
    snapshot = store.catalog_storage.export()
    assert len(snapshot["tables"]["list_item_revisions"]) == 251
    with pytest.raises(TypeError):
        store.catalog_storage.export(max_records=1)


def test_invalid_snapshot_never_changes_the_empty_destination(tmp_path):
    snapshot = populated(tmp_path / "source.sqlite").catalog_storage.export()
    for mutate in (
        lambda value: value["tables"]["list_item_revisions"][0].update(fingerprint="0" * 64),
        lambda value: value["tables"]["item_feedback"][0].update(classifier_id="foreign"),
        lambda value: value["tables"]["item_feedback"][0].update(item_revision=99),
        lambda value: value["tables"]["item_feedback_retractions"][0].update(feedback_request_id="unknown"),
        lambda value: value.update(feedback_sequence=0),
        lambda value: value["tables"]["classifiers"].append(value["tables"]["classifiers"][0]),
    ):
        candidate = copy.deepcopy(snapshot)
        mutate(candidate)
        destination = WebStore(tmp_path / "destination.sqlite")
        with pytest.raises(ValueError):
            destination.catalog_storage.restore(candidate)
        assert destination.catalog_storage.export()["tables"]["classifiers"] == []


def test_item_and_feedback_retries_are_atomic_across_two_catalog_clients(tmp_path):
    path = tmp_path / "shared.sqlite"
    store = WebStore(path)
    store.save_classifier("topic", "Topic", configuration())
    store.save_item_list("papers", "Papers")
    barrier = Barrier(2)
    item = {"id": "one", "occurred_at": "2026-01-01", "values": {"text": "Same"}}
    clients = [WebStore(path), WebStore(path)]

    def upsert(index):
        barrier.wait()
        return clients[index].upsert_list_items("papers", [item])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upsert, (0, 1)))
    assert sum(result["inserted"] for result in results) == 1
    assert sum(result["unchanged"] for result in results) == 1
    barrier = Barrier(2)

    def label(index):
        barrier.wait()
        return clients[index].label_item("topic", 1, "papers", "one", 1, "science", "Exact explanation", "same-request")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(label, (0, 1))
    assert first == second
    assert len(store.catalog_storage.export()["tables"]["item_feedback"]) == 1


def test_catalog_api_uses_injected_domain_storage_without_sql_on_the_facade(tmp_path):
    source = populated(tmp_path / "source.sqlite")

    class Storage:
        def __getattr__(self, name):
            return getattr(source.catalog_storage, name)

    facade = WebStore(tmp_path / "facade.sqlite", catalog_storage=Storage())

    def forbidden():
        raise AssertionError("catalog facade opened SQL")

    facade.connect = forbidden
    assert facade.classifier("topic")["revision"] == 2
    assert facade.item_lists()[0]["count"] == 1
    assert facade.list_items("papers")[0]["revision"] == 2
    assert facade.item_labels("papers", "one", 2)[0]["comment"] == "Other revision explanation"
    assert facade.label_item("topic", 2, "papers", "one", 2, "science", "Injected", "injected")["comment"] == "Injected"
    assert facade.retract_item_label("injected", "undo-injected")["feedback_request_id"] == "injected"


def test_classifier_revision_and_definition_advancement_still_commit_together(tmp_path, monkeypatch):
    store = WebStore(tmp_path / "catalog.sqlite")
    store.save_classifier("topic", "Topic", configuration())

    def reject(*args):
        raise RuntimeError("synthetic definition conflict")

    monkeypatch.setattr(store, "advance_classifier_definitions", reject)
    with pytest.raises(RuntimeError):
        store.save_classifier("topic", "Changed", {**configuration(), "question": "Changed?"})
    assert store.classifier("topic")["revision"] == 1


def test_export_is_one_read_snapshot_while_items_and_feedback_change(tmp_path, monkeypatch):
    from contextlib import contextmanager

    source = populated(tmp_path / "catalog.sqlite")
    before = source.catalog_storage.export()
    connect = source.connect
    refreshed = []

    @contextmanager
    def exporting():
        with connect() as db:

            def refresh(statement):
                if statement.startswith("SELECT list_id,id,revision,") and not refreshed:
                    refreshed.append(True)
                    with connect() as writer:
                        writer.execute("UPDATE item_lists SET name='Renamed during snapshot' WHERE id='papers'")
                        writer.execute("UPDATE sqlite_sequence SET seq=200 WHERE name='item_feedback'")

            db.set_trace_callback(refresh)
            yield db

    monkeypatch.setattr(source, "connect", exporting)
    assert source.catalog_storage.export() == before
    after = source.catalog_storage.export()
    assert refreshed == [True] and after["feedback_sequence"] == 200
    assert after["tables"]["item_lists"][0]["name"] == "Renamed during snapshot"


def test_restore_rolls_back_prior_rows_if_native_insertion_fails(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    snapshot = populated(tmp_path / "source.sqlite").catalog_storage.export()
    destination = WebStore(tmp_path / "destination.sqlite")
    connect = destination.connect

    @contextmanager
    def interrupted():
        with connect() as db:
            db.set_authorizer(
                lambda action, table, *args: (
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "item_feedback" else sqlite3.SQLITE_OK
                )
            )
            yield db

    monkeypatch.setattr(destination, "connect", interrupted)
    with pytest.raises(sqlite3.DatabaseError):
        destination.catalog_storage.restore(snapshot)
    monkeypatch.setattr(destination, "connect", connect)
    assert all(not rows for rows in destination.catalog_storage.export()["tables"].values())


def test_catalog_restore_refuses_to_merge_with_existing_run_composition(tmp_path):
    snapshot = populated(tmp_path / "source.sqlite").catalog_storage.export()
    destination = WebStore(tmp_path / "destination.sqlite")
    destination.create_run("Existing", "recorded", {})
    with pytest.raises(CatalogConflict):
        destination.catalog_storage.restore(snapshot)
    assert destination.classifiers() == []


def test_prediction_history_remains_immutable_and_keeps_existing_run_names(tmp_path):
    path = tmp_path / "catalog.sqlite"
    source = populated(path)
    run = source.create_run("Synthetic run", "recorded", {})
    prediction = {"label": "science", "explanation": "Original displayed explanation"}
    source.record_item_prediction(run["id"], "papers", "one", 1, prediction)
    source.record_item_prediction(run["id"], "papers", "one", 1, prediction)
    with pytest.raises(ValueError, match="immutable"):
        source.record_item_prediction(run["id"], "papers", "one", 1, {**prediction, "label": "sport"})
    reopened = WebStore(path)
    assert reopened.item_results("papers", "one", 1) == [
        {
            "run_id": run["id"],
            "list_id": "papers",
            "item_id": "one",
            "item_revision": 1,
            "payload": prediction,
            "run_name": "Synthetic run",
        }
    ]


def test_default_catalog_can_save_and_restart_without_aws_or_network(tmp_path):
    import os
    import subprocess
    import sys

    script = """
import builtins, socket, sys
original=builtins.__import__
def isolated(name,*args,**kwargs):
    if name.split('.')[0] in ('boto3','botocore'): raise AssertionError('AWS imported')
    return original(name,*args,**kwargs)
builtins.__import__=isolated
def forbidden(*args,**kwargs): raise AssertionError('network opened')
socket.socket.connect=forbidden
socket.create_connection=forbidden
from decision_flywheel.web_store import WebStore
store=WebStore(sys.argv[1])
store.save_classifier('c','C',{'question':'Which?', 'classes':[{'label':'yes'},{'label':'no'}]})
store.save_item_list('list','List')
store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'synthetic'}}])
store.label_item('c',1,'list','one',1,'yes','explanation','request')
assert WebStore(sys.argv[1]).item_labels('list','one',1)[0]['comment']=='explanation'
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "local.sqlite")],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_snapshot_rejects_invalid_classifier_semantics_and_integer_overflow_before_writes(tmp_path):
    snapshot = populated(tmp_path / "source.sqlite").catalog_storage.export()
    for mutate in (
        lambda value: value["tables"]["classifiers"][0].update(config=json.dumps({**configuration(), "question": "   "})),
        lambda value: value["tables"]["classifiers"][0].update(
            config=json.dumps({**configuration(), "classes": [{"label": "science", "role": "unreviewed"}, {"label": "sport"}]})
        ),
        lambda value: value["tables"]["item_lists"][0].update(name="  "),
        lambda value: value.update(feedback_sequence=2**63),
    ):
        candidate = copy.deepcopy(snapshot)
        mutate(candidate)
        destination = WebStore(tmp_path / "destination.sqlite")
        with pytest.raises(ValueError):
            destination.catalog_storage.restore(candidate)
        assert destination.classifiers() == []
