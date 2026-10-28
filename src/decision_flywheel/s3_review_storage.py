"""Canonical S3 reviewer source, retaining native portable records and firewalls."""

import json
import math
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from .journal import Journal, commit_with_contract

COLUMNS = {
    "study_metadata": ("key", "value"),
    "articles": ("id", "title", "abstract", "submitted_at", "categories_json", "authors", "journal_ref", "assignment"),
    "presentations": (
        "id",
        "article_id",
        "predicted_label",
        "confidence",
        "predictor_kind",
        "predictor_fingerprint",
        "training_label_count",
        "shown_at",
    ),
    "review_events": ("id", "article_id", "action", "label", "comment", "created_at", "undoes_event_id", "presentation_id"),
}


def empty():
    return {"schema": "cyclotron-review-source/v1", "tables": {key: [] for key in COLUMNS}, "metadata": {"sequences": {}}}


def clean(row):
    return {k: v for k, v in row.items() if k != "_rowid"}


def append(value, table, row):
    if len(row) != len(COLUMNS[table]):
        raise ValueError("complete reviewer record required")
    values = value["tables"][table]
    record = {"_rowid": max((r["_rowid"] for r in values), default=0) + 1, **dict(zip(COLUMNS[table], row))}
    values.append(record)
    return record


class S3ReviewStorage:
    def __init__(self, journal):
        if not isinstance(journal, Journal):
            raise ValueError("canonical reviewer journal required")
        self.journal = journal
        self._tx = ContextVar("s3_review_transaction", default=None)

    def _validate(self, value):
        if (
            not isinstance(value, dict)
            or set(value) != set(empty())
            or value["schema"] != "cyclotron-review-source/v1"
            or value["metadata"] != {"sequences": {}}
            or not isinstance(value["tables"], dict)
            or set(value["tables"]) != set(COLUMNS)
        ):
            raise ValueError("invalid complete reviewer snapshot")
        tables = value["tables"]
        if any(not isinstance(r, list) for r in tables.values()):
            raise ValueError("reviewer tables must be lists")
        for table, columns in COLUMNS.items():
            ordinals, ids = set(), set()
            for row in tables[table]:
                if (
                    not isinstance(row, dict)
                    or set(row) != set(columns) | {"_rowid"}
                    or type(row["_rowid"]) is not int
                    or not 1 <= row["_rowid"] < 2**63
                    or row["_rowid"] in ordinals
                ):
                    raise ValueError("invalid reviewer row ordinal/fields")
                identity = row[columns[0]]
                if not isinstance(identity, str) or not identity or identity in ids:
                    raise ValueError("invalid reviewer identity")
                ordinals.add(row["_rowid"])
                ids.add(identity)
            if [r["_rowid"] for r in tables[table]] != sorted(ordinals):
                raise ValueError("reviewer records must retain ordinal order")
        if any(not isinstance(r["value"], str) for r in tables["study_metadata"]):
            raise ValueError("study metadata text required")
        articles = {r["id"]: r for r in tables["articles"]}
        for row in articles.values():
            if (
                any(not isinstance(row[k], str) or not row[k].strip() for k in ("title", "abstract", "submitted_at", "authors"))
                or row["journal_ref"] is not None
                and not isinstance(row["journal_ref"], str)
                or row["assignment"] not in ("train", "rolling_audit", "final_audit")
            ):
                raise ValueError("invalid source article")
            categories = json.loads(row["categories_json"])
            if not isinstance(categories, list) or not categories or any(not isinstance(c, str) or not c for c in categories):
                raise ValueError("invalid article categories")
        presentations = {r["id"]: r for r in tables["presentations"]}
        for row in presentations.values():
            if (
                row["article_id"] not in articles
                or row["predicted_label"] not in ("include", "exclude")
                or isinstance(row["confidence"], bool)
                or not isinstance(row["confidence"], (float, int))
                or not math.isfinite(row["confidence"])
                or not 0 <= row["confidence"] <= 1
                or type(row["training_label_count"]) is not int
                or row["training_label_count"] < 0
                or any(not isinstance(row[k], str) or not row[k] for k in ("predictor_kind", "predictor_fingerprint", "shown_at"))
            ):
                raise ValueError("invalid displayed prediction")
        previous = {}
        for row in tables["review_events"]:
            if (
                row["article_id"] not in articles
                or row["action"] not in ("vote", "skip", "undo")
                or row["label"] not in (None, "include", "exclude")
                or row["comment"] is not None
                and not isinstance(row["comment"], str)
                or not isinstance(row["created_at"], str)
            ):
                raise ValueError("invalid review action")
            if row["action"] == "vote" and row["label"] is None or row["action"] != "vote" and row["label"] is not None:
                raise ValueError("review action/label mismatch")
            if row["undoes_event_id"] is not None:
                target = previous.get(row["undoes_event_id"])
                if target is None or target["article_id"] != row["article_id"] or row["action"] != "undo":
                    raise ValueError("invalid undo reference")
            elif row["action"] == "undo":
                raise ValueError("undo requires prior action")
            if row["presentation_id"] is not None:
                shown = presentations.get(row["presentation_id"])
                if shown is None or shown["article_id"] != row["article_id"]:
                    raise ValueError("invalid displayed-prediction reference")
            previous[row["id"]] = row

    def initialize(self):
        self._validate(self.journal.initialize(empty()).value)

    def _record(self):
        record = self.journal.read()
        self._validate(record.value)
        return record

    def _value(self):
        return self._tx.get() if self._tx.get() is not None else self._record().value

    @contextmanager
    def transaction(self):
        if self._tx.get() is not None:
            raise ValueError("nested reviewer transaction")
        record = self._record()
        value = deepcopy(record.value)
        token = self._tx.set(value)
        try:
            yield self
            self._validate(value)
            if value != record.value:
                commit_with_contract(self.journal, record, value)
        finally:
            self._tx.reset(token)

    def _write(self, fn):
        if self._tx.get() is not None:
            return fn(self._tx.get())
        with self.transaction():
            return fn(self._tx.get())

    def bind_study(self, values):
        def apply(value):
            for key, payload in values.items():
                old = next((r for r in value["tables"]["study_metadata"] if r["key"] == key), None)
                if old:
                    if old["value"] != payload:
                        raise ValueError(f"review database already belongs to a study with different {key}")
                else:
                    append(value, "study_metadata", (key, payload))

        self._write(apply)

    def import_articles(self, records):
        def apply(value):
            imported = 0
            for record in records:
                identifier, title, abstract, submitted, categories, authors, journal, assignment = record
                old = next((r for r in value["tables"]["articles"] if r["id"] == identifier), None)
                supplied = (title, abstract, submitted, categories, authors, journal)
                if old:
                    current = tuple(
                        old[k] for k in ("title", "abstract", "submitted_at", "categories_json", "authors", "journal_ref")
                    )
                    if current == supplied:
                        continue
                    if current[:4] == supplied[:4]:
                        if current[4:] == ("Not provided", None) and not any(
                            r["article_id"] == identifier for r in value["tables"]["review_events"]
                        ):
                            old["authors"] = authors
                            old["journal_ref"] = journal
                        continue
                    raise ValueError("an imported article ID already has different content")
                append(value, "articles", record)
                imported += 1
            return imported

        return self._write(apply)

    def articles(self):
        return [clean(r) for r in sorted(self._value()["tables"]["articles"], key=lambda r: r["id"])]

    def article(self, identifier):
        return next((clean(r) for r in self._value()["tables"]["articles"] if r["id"] == identifier), None)

    def actions(self, article_id=None):
        return [
            clean(r)
            for r in sorted(self._value()["tables"]["review_events"], key=lambda r: (r["created_at"], r["_rowid"]))
            if article_id is None or r["article_id"] == article_id
        ]

    def presentations(self, article_id=None):
        return [
            clean(r)
            for r in sorted(self._value()["tables"]["presentations"], key=lambda r: (r["shown_at"], r["_rowid"]))
            if article_id is None or r["article_id"] == article_id
        ]

    def presentation(self, identifier):
        return next((clean(r) for r in self._value()["tables"]["presentations"] if r["id"] == identifier), None)

    def append_presentation(self, row):
        self._write(lambda value: append(value, "presentations", row))

    def append_action(self, row):
        self._write(lambda value: append(value, "review_events", row))

    def close(self):
        pass

    def export_storage(self):
        value = deepcopy(self._value())
        self._validate(value)
        return value

    def restore_storage(self, snapshot):
        snapshot = deepcopy(snapshot)
        self._validate(snapshot)

        def apply(value):
            if any(value["tables"].values()):
                raise ValueError("review source restore requires empty destination")
            value.update(snapshot)

        self._write(apply)
