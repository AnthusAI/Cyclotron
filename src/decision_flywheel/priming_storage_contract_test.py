"""Priming drafts and accepted evidence behave the same on memory, SQLite and S3 (synthetic fake client)."""

import base64
import json
from contextlib import contextmanager
from copy import deepcopy

import pytest

from .priming import PrimingService
from .priming_context import catalog_priming
from .priming_test import ITEM, LABELS, refs
from .s3_journal_test import ConditionalS3, journal
from .web_store import WebStore

BACKENDS = ("memory", "sqlite", "s3")


class MemoryCatalog:
    """The smallest host that satisfies the priming storage protocol, with whole-state rollback."""

    def __init__(self):
        self.lists, self.classifiers = {}, {}
        self.drafts, self.items, self.feedback, self.retractions = {}, {}, [], set()

    @contextmanager
    def transaction(self):
        before = deepcopy((self.drafts, self.items, self.feedback))
        try:
            yield self
        except BaseException:
            self.drafts, self.items, self.feedback = before
            raise

    @property
    def priming_storage(self):
        return self

    def save_item_list(self, identifier, name):
        self.lists[identifier] = name

    def save_classifier(self, identifier, name, config):
        revisions = self.classifiers.setdefault(identifier, [])
        revisions.append({"id": identifier, "revision": len(revisions) + 1, "name": name, "config": config})

    def item_lists(self):
        return [{"id": key, "name": value} for key, value in self.lists.items()]

    def classifier(self, identifier, revision=None):
        revisions = self.classifiers[identifier]
        return revisions[-1] if revision is None else revisions[revision - 1]

    def draft(self, identifier):
        return self.drafts.get(identifier)

    def put_draft(self, identifier, payload):
        self.drafts[identifier] = payload

    def latest_classifier_revision(self, identifier):
        return len(self.classifiers[identifier]) or None

    def item_exists(self, list_id, identifier):
        return (list_id, identifier) in self.items

    def insert_item(self, row):
        self.items[row[0], row[1]] = row

    def insert_feedback(self, row):
        self.feedback.append((len(self.feedback) + 1, *row))

    def label_item(self, classifier_id, classifier_revision, list_id, item_id, item_revision, label, comment, request_id):
        row = (request_id, classifier_id, classifier_revision, list_id, item_id, item_revision, label, comment, "now")
        if not any(f[1] == request_id for f in self.feedback):
            self.insert_feedback(row)

    def retract_item_label(self, feedback_request_id, request_id):
        self.retractions.add(feedback_request_id)

    def list_items(self, list_id):
        return [row for (listed, _), row in self.items.items() if listed == list_id]

    def accepted_records(self, list_id, classifiers):
        records = []
        for (listed, _), row in self.items.items():
            if listed != list_id:
                continue
            votes = {}
            for classifier in classifiers:
                matching = [
                    f
                    for f in self.feedback
                    if (f[2], f[3], f[4], f[5]) == (classifier["id"], classifier["revision"], list_id, row[1])
                ]
                latest = max(matching, key=lambda f: f[0], default=None)
                if latest and latest[1] not in self.retractions:
                    votes[classifier["id"]] = {"label": latest[7], "comment": latest[8]}
            records.append({"payload": row[5], "revision": row[2], "votes": votes})
        return records


class Backend:
    def __init__(self, name, tmp_path):
        self.name, self.tmp_path = name, tmp_path
        self.memory, self.client = MemoryCatalog(), ConditionalS3()

    def open(self):
        if self.name == "memory":
            return self.memory
        if self.name == "sqlite":
            return WebStore(self.tmp_path / "priming.sqlite3")
        from .s3_console_storage import S3ConsoleStorage

        store = S3ConsoleStorage(journal(self.client, name="console"), max_records=10000)
        store.initialize()
        return store

    def seeded(self):
        host = self.open()
        host.save_item_list("examples", "Accepted examples")
        for cid, labels in [("editor", ["publish", "not publish"]), ("scope", ["yes", "no"])]:
            host.save_classifier(cid, cid, {"question": cid + "?", "classes": [{"label": v} for v in labels]})
        return host


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path):
    return Backend(request.param, tmp_path)


class FailingStorage:
    """Wrap any backend's priming storage so one named write fails inside the transaction."""

    def __init__(self, storage):
        self.storage, self.fail_on = storage, None

    @contextmanager
    def transaction(self):
        with self.storage.transaction() as db:
            yield _Proxy(db, self)

    def accepted_records(self, list_id, classifiers):
        return self.storage.accepted_records(list_id, classifiers)


class _Proxy:
    def __init__(self, db, owner):
        self.db, self.owner = db, owner

    def __getattr__(self, name):
        if name == self.owner.fail_on:
            raise RuntimeError("synthetic failure in " + name)
        return getattr(self.db, name)


def file_content(rows=None):
    return base64.b64encode(json.dumps(rows or [{**ITEM, "labels": LABELS}]).encode()).decode()


def preview(service, identifier="draft", rows=None):
    return service.start_file(identifier, "examples", refs(), "json", file_content(rows))


def item_ids(host):
    return [row["id"] if isinstance(row, dict) else row[1] for row in host.list_items("examples")]


def classifiers_of(host):
    return [host.classifier(row["id"], row["revision"]) for row in refs()]


def test_accepting_commits_the_item_its_votes_and_the_draft_together(backend):
    host = backend.seeded()
    service = PrimingService(host, storage=host.priming_storage)
    assert preview(service)["status"] == "preview"
    accepted = service.accept("draft", 1, None)
    assert accepted["status"] == "accepted" and service.get("draft")["status"] == "accepted"
    assert item_ids(host) == [ITEM["id"]]
    context = catalog_priming(host, "examples", classifiers_of(host), set(), storage=host.priming_storage)
    assert {cid: [row["item"]["id"] for row in rows] for cid, rows in context.items()} == {
        "editor": [ITEM["id"]],
        "scope": [ITEM["id"]],
    }
    assert context["editor"][0]["context"]["human_feedback"] == "Synthetic novelty"
    service.close()


def test_a_failure_during_acceptance_rolls_everything_back_and_the_same_acceptance_retries(backend):
    host = backend.seeded()
    storage = FailingStorage(host.priming_storage)
    service = PrimingService(host, storage=storage)
    preview(service)
    storage.fail_on = "insert_feedback"
    with pytest.raises(RuntimeError, match="synthetic failure"):
        service.accept("draft", 1, None)
    storage.fail_on = None
    assert service.get("draft")["status"] == "preview"
    assert not host.priming_storage.accepted_records("examples", classifiers_of(host))
    assert service.accept("draft", 1, None)["status"] == "accepted"
    assert len(host.priming_storage.accepted_records("examples", classifiers_of(host))) == 1
    service.close()


def test_a_failure_while_writing_a_new_draft_leaves_no_draft_behind(backend):
    host = backend.seeded()
    storage = FailingStorage(host.priming_storage)
    service = PrimingService(host, storage=storage)
    storage.fail_on = "put_draft"
    with pytest.raises(RuntimeError, match="synthetic failure"):
        preview(service)
    storage.fail_on = None
    with pytest.raises(ValueError, match="unknown import draft"):
        service.get("draft")
    assert preview(service)["status"] == "preview"
    service.close()


def test_repeating_an_acceptance_changes_nothing_and_a_different_acceptance_is_refused(backend):
    host = backend.seeded()
    service = PrimingService(host, storage=host.priming_storage)
    preview(service)
    first = service.accept("draft", 1, None)
    before = host.priming_storage.accepted_records("examples", classifiers_of(host))
    assert service.accept("draft", 1, None) == first
    assert host.priming_storage.accepted_records("examples", classifiers_of(host)) == before
    with pytest.raises(ValueError, match="different content"):
        service.accept("draft", 1, [{"editor": {"label": "not publish", "rationale": "Changed"}, "scope": LABELS["scope"]}])
    service.close()


def test_the_latest_vote_wins_and_a_retraction_does_not_revive_an_older_vote(backend):
    host = backend.seeded()
    service = PrimingService(host, storage=host.priming_storage)
    preview(service)
    service.accept("draft", 1, None)
    chosen = classifiers_of(host)

    def label():
        records = host.priming_storage.accepted_records("examples", chosen)
        return records[0]["votes"].get("editor", {}).get("label")

    assert label() == "publish"
    host.label_item("editor", 1, "examples", ITEM["id"], 1, "not publish", "Correction rationale", "correction")
    assert label() == "not publish"
    host.retract_item_label("correction", "retraction")
    assert label() is None
    frozen = catalog_priming(host, "examples", chosen, set(), storage=host.priming_storage)
    assert frozen["editor"] == [] and frozen["scope"][0]["label"] == "yes"
    with pytest.raises(ValueError, match="disjoint"):
        catalog_priming(host, "examples", chosen, {ITEM["id"]}, storage=host.priming_storage)
    service.close()


def test_a_draft_over_the_size_limit_is_refused_and_nothing_is_stored(backend):
    host = backend.seeded()
    service = PrimingService(host, storage=host.priming_storage)
    with pytest.raises(ValueError, match="size limit"):
        service.start_file("draft", "examples", refs(), "json", "A" * 9_000_000)
    with pytest.raises(ValueError, match="unknown import draft"):
        service.get("draft")
    assert preview(service)["status"] == "preview"
    service.close()


def test_a_stale_preview_or_changed_classifier_revision_is_refused_without_committing(backend):
    host = backend.seeded()
    service = PrimingService(host, storage=host.priming_storage)
    preview(service)
    with pytest.raises(ValueError, match="stale or closed"):
        service.accept("draft", 2, None)
    host.save_classifier(
        "editor", "editor", {"question": "Changed?", "classes": [{"label": "publish"}, {"label": "not publish"}]}
    )
    with pytest.raises(ValueError, match="classifier revision changed"):
        service.accept("draft", 1, None)
    assert service.get("draft")["status"] == "preview"
    assert not host.list_items("examples")
    service.close()


def test_an_existing_item_identity_is_refused_without_a_partial_commit(backend):
    host = backend.seeded()
    service = PrimingService(host, storage=host.priming_storage)
    preview(service, "first")
    service.accept("first", 1, None)
    again = preview(service, "second")
    assert again["status"] == "preview"
    with pytest.raises(ValueError, match="already exists"):
        service.accept("second", 1, None)
    assert service.get("second")["status"] == "preview" and len(host.list_items("examples")) == 1
    service.close()


def test_accepted_evidence_and_drafts_survive_reopening(backend):
    host = backend.seeded()
    service = PrimingService(host, storage=host.priming_storage)
    preview(service)
    accepted = service.accept("draft", 1, None)
    reopened = backend.open()
    again = PrimingService(reopened, storage=reopened.priming_storage)
    assert again.get("draft") == accepted
    assert again.accept("draft", 1, None) == accepted
    assert len(reopened.priming_storage.accepted_records("examples", classifiers_of(reopened))) == 1
    service.close()
    again.close()
