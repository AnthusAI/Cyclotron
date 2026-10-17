"""Atomic priming drafts and accepted catalog evidence, using named operations.

The host supplies one authority for this entire transaction; drafts, imported
revisions and human feedback cannot be committed independently. Native SQL stays
inside this adapter. External transformation calls never run in a transaction.
"""

from contextlib import contextmanager
from typing import Protocol


class PrimingTransaction(Protocol):
    def draft(self, identifier: str) -> str | None: ...
    def put_draft(self, identifier: str, payload: str) -> None: ...
    def latest_classifier_revision(self, identifier: str) -> int | None: ...
    def item_exists(self, list_id: str, identifier: str) -> bool: ...
    def insert_item(self, row: tuple) -> None: ...
    def insert_feedback(self, row: tuple) -> None: ...


class PrimingStorage(Protocol):
    def transaction(self): ...
    def accepted_records(self, list_id: str, classifiers: list[dict]) -> list[dict]: ...


class SQLitePrimingStorage:
    def __init__(self, connect):
        self.connect = connect
        with connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS priming_drafts(id TEXT PRIMARY KEY,payload TEXT NOT NULL)")

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield _SQLitePrimingTransaction(db)

    def accepted_records(self, list_id, classifiers):
        with self.connect() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM item_lists WHERE id=?", (list_id,)).fetchone():
                raise ValueError("unknown priming item list")
            items = db.execute(
                "SELECT r.* FROM list_item_revisions r WHERE list_id=? AND revision=(SELECT MAX(revision) FROM list_item_revisions WHERE list_id=r.list_id AND id=r.id) ORDER BY occurred_at,id",
                (list_id,),
            ).fetchall()
            records = []
            for row in items:
                votes = {}
                for classifier in classifiers:
                    cid = classifier["id"]
                    vote = db.execute(
                        "SELECT f.* FROM item_feedback f WHERE list_id=? AND item_id=? AND item_revision=? AND classifier_id=? AND classifier_revision=? ORDER BY sequence DESC LIMIT 1",
                        (list_id, row["id"], row["revision"], cid, classifier["revision"]),
                    ).fetchone()
                    if (
                        vote
                        and not db.execute(
                            "SELECT 1 FROM item_feedback_retractions WHERE feedback_request_id=?", (vote["request_id"],)
                        ).fetchone()
                    ):
                        votes[cid] = dict(vote)
                records.append({"payload": row["payload"], "revision": row["revision"], "votes": votes})
            return records


class _SQLitePrimingTransaction:
    def __init__(self, db):
        self.db = db

    def draft(self, identifier):
        row = self.db.execute("SELECT payload FROM priming_drafts WHERE id=?", (identifier,)).fetchone()
        return row[0] if row else None

    def put_draft(self, identifier, payload):
        self.db.execute(
            "INSERT INTO priming_drafts VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (identifier, payload),
        )

    def latest_classifier_revision(self, identifier):
        return self.db.execute("SELECT MAX(revision) FROM classifiers WHERE id=?", (identifier,)).fetchone()[0]

    def item_exists(self, list_id, identifier):
        return bool(
            self.db.execute("SELECT 1 FROM list_item_revisions WHERE list_id=? AND id=?", (list_id, identifier)).fetchone()
        )

    def insert_item(self, row):
        self.db.execute("INSERT INTO list_item_revisions VALUES (?,?,?,?,?,?,?)", row)

    def insert_feedback(self, row):
        self.db.execute(
            "INSERT INTO item_feedback(request_id,classifier_id,classifier_revision,list_id,item_id,item_revision,label,comment,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            row,
        )
