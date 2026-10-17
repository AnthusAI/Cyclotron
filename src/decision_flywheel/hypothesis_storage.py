"""Raw hypothesis/diagnosis evidence; statistical algorithms stay shared."""

from typing import Protocol

from .learning_storage import SQLiteControlStorage


class HypothesisStorage(Protocol):
    def ensure(self) -> None: ...
    def record_evaluations(self, rows: list[tuple]) -> None: ...
    def append_verdict(self, row: tuple) -> None: ...
    def verdicts(self, idea_id: str) -> list[tuple]: ...
    def latest(self, idea_id: str, context_version: str | None = None) -> list[tuple]: ...
    def marks(self, idea_id: str, since: str | None = None, *, strict=False) -> list[int]: ...
    def verdict_idea_ids(self) -> list[str]: ...
    def idea_ids(self, context_version: str | None = None) -> list[str]: ...
    def idea_payload(self, idea_id: str) -> str | None: ...
    def idea_payloads(self) -> tuple[str, ...]: ...
    def ensure_diagnosis(self) -> None: ...
    def save_cause(self, row: tuple) -> None: ...
    def causes(self, round_number: int | None = None) -> list[str]: ...
    def save_element(self, row: tuple) -> None: ...
    def elements(self) -> list[tuple]: ...
    def wrong(self, idea_id: str, context_version: str | None = None, *, recent: int | None = None) -> list[tuple]: ...


class SQLiteHypothesisStorage:
    def __init__(self, db, *, controls=None):
        self.db = db
        self.controls = controls if controls is not None else SQLiteControlStorage(db)

    def ensure(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS idea_evaluations (idea_id TEXT, item_id TEXT, correct INTEGER, label TEXT,
                choice TEXT, confidence REAL, context_version TEXT, evaluated_at TEXT,
                PRIMARY KEY (idea_id, item_id, context_version));
            CREATE TABLE IF NOT EXISTS idea_verdicts (idea_id TEXT, at TEXT, verdict TEXT, reason TEXT, evidence TEXT);
        """)

    def record_evaluations(self, rows):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO idea_evaluations VALUES (?,?,?,?,?,?,?,?)", rows)

    def append_verdict(self, row):
        with self.db:
            self.db.execute("INSERT INTO idea_verdicts VALUES (?,?,?,?,?)", row)

    def verdicts(self, idea_id):
        return self.db.execute(
            "SELECT at,verdict,reason,evidence FROM idea_verdicts WHERE idea_id=? ORDER BY rowid", (idea_id,)
        ).fetchall()

    def latest(self, idea_id, context_version=None):
        sql, args = "SELECT item_id,correct FROM idea_evaluations WHERE idea_id=?", [idea_id]
        if context_version is not None:
            sql, args = sql + " AND context_version=?", [*args, context_version]
        return self.db.execute(sql + " ORDER BY evaluated_at,rowid", args).fetchall()

    def marks(self, idea_id, since=None, *, strict=False):
        sql, args = "SELECT correct FROM idea_evaluations WHERE idea_id=?", [idea_id]
        if since is not None:
            sql, args = sql + (" AND evaluated_at>?" if strict else " AND evaluated_at>=?"), [*args, since]
        return [row[0] for row in self.db.execute(sql, args)]

    def verdict_idea_ids(self):
        return [row[0] for row in self.db.execute("SELECT DISTINCT idea_id FROM idea_verdicts ORDER BY idea_id")]

    def idea_ids(self, context_version=None):
        sql, args = "SELECT DISTINCT idea_id FROM idea_evaluations", []
        if context_version is not None:
            sql, args = sql + " WHERE context_version=?", [context_version]
        return [row[0] for row in self.db.execute(sql, args)]

    def idea_payload(self, idea_id):
        return self.controls.payload(idea_id)

    def idea_payloads(self):
        return self.controls.payloads()

    def ensure_diagnosis(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS diagnosis_causes (cause_id TEXT PRIMARY KEY, round INTEGER, payload TEXT);
            CREATE TABLE IF NOT EXISTS element_evaluations (element TEXT, round INTEGER, question TEXT, source TEXT,
                n INTEGER, auc_label REAL, mean_log_loss_gain REAL, lower_log_loss_gain REAL,
                mean_accuracy_gain REAL, kept INTEGER, reason TEXT, payload TEXT, evaluated_at TEXT,
                PRIMARY KEY (element, round));
        """)

    def save_cause(self, row):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO diagnosis_causes VALUES (?,?,?)", row)

    def causes(self, round_number=None):
        sql, args = "SELECT payload FROM diagnosis_causes", []
        if round_number is not None:
            sql, args = sql + " WHERE round=?", [round_number]
        return [row[0] for row in self.db.execute(sql + " ORDER BY round,cause_id", args)]

    def save_element(self, row):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO element_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)

    def elements(self):
        return self.db.execute(
            "SELECT element,round,question,source,n,auc_label,mean_log_loss_gain,"
            "lower_log_loss_gain,mean_accuracy_gain,kept,reason,payload FROM element_evaluations ORDER BY round,rowid"
        ).fetchall()

    def wrong(self, idea_id, context_version=None, *, recent=None):
        sql = "SELECT item_id,label,choice FROM idea_evaluations WHERE idea_id=? AND correct=0"
        args = [idea_id]
        if context_version is not None:
            sql += " AND context_version=?"
            args.append(context_version)
        if recent is None:
            sql += " ORDER BY evaluated_at,rowid"
        else:
            sql += " ORDER BY evaluated_at DESC,rowid DESC LIMIT ?"
            args.append(recent)
        return self.db.execute(sql, args).fetchall()
