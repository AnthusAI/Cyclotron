"""Bounded migration of one native classifier runtime, never cloud activation.

Includes all twenty known runtime/learning tables, preserving optional table
presence and physical ordinals. The embedded root/shared/reviewer/evaluation
stores are separate compositions and must be migrated with matching snapshots.
"""

import math
import re
import sqlite3

from .catalog_archive import CatalogArchive
from .catalog_snapshot import decode, encode
from .learning_storage import ARTIFACTS

SCHEMA = "cyclotron-runtime/v1"
DEFINITIONS = {
    "runtime_state": "key TEXT PRIMARY KEY, value TEXT NOT NULL",
    "runtime_events": "id INTEGER PRIMARY KEY, payload TEXT NOT NULL",
    "runtime_answers": "key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT",
    "runtime_answer_history": "id INTEGER PRIMARY KEY, key TEXT NOT NULL, status TEXT NOT NULL, payload TEXT",
    "runtime_rounds": "key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT",
    **{table: definition for table, definition in ARTIFACTS.values()},
    "feature_bank": "id TEXT PRIMARY KEY, payload TEXT NOT NULL",
    "control_ideas": "id TEXT PRIMARY KEY, payload TEXT NOT NULL",
    "idea_evaluations": """idea_id TEXT, item_id TEXT, correct INTEGER, label TEXT, choice TEXT, confidence REAL,
        context_version TEXT, evaluated_at TEXT, PRIMARY KEY (idea_id,item_id,context_version)""",
    "idea_verdicts": "idea_id TEXT, at TEXT, verdict TEXT, reason TEXT, evidence TEXT",
    "diagnosis_causes": "cause_id TEXT PRIMARY KEY, round INTEGER, payload TEXT",
    "element_evaluations": """element TEXT, round INTEGER, question TEXT, source TEXT, n INTEGER, auc_label REAL,
        mean_log_loss_gain REAL, lower_log_loss_gain REAL, mean_accuracy_gain REAL, kept INTEGER, reason TEXT,
        payload TEXT, evaluated_at TEXT, PRIMARY KEY (element,round)""",
    "shadow_set": "idea_id TEXT PRIMARY KEY, control TEXT NOT NULL, joined_seq INTEGER NOT NULL, status TEXT NOT NULL",
    "shadow_scored": "idea_id TEXT, item_id TEXT, seq INTEGER, PRIMARY KEY (idea_id,item_id)",
    "shadow_looks": """idea_id TEXT, incumbent_id TEXT, look INTEGER, shared INTEGER, gained INTEGER, lost INTEGER,
        p_value REAL, cleared INTEGER, seq INTEGER, PRIMARY KEY (idea_id,incumbent_id,look)""",
    "shadow_incumbents": "incumbent_id TEXT PRIMARY KEY, rubric TEXT, first_seq INTEGER",
}


def _create(db, table):
    db.execute(f"CREATE TABLE {table} ({DEFINITIONS[table]})")


def _normalize(sql):
    return re.sub(r"\s+", "", sql).lower()


def _layout():
    db = sqlite3.connect(":memory:")
    try:
        for table in DEFINITIONS:
            _create(db, table)
        layout = {table: tuple(tuple(row) for row in db.execute(f"PRAGMA table_info({table})")) for table in DEFINITIONS}
        schemas = {name: _normalize(sql) for name, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
        return layout, schemas
    finally:
        db.close()


LAYOUT, SCHEMAS = _layout()
TABLES = {table: tuple(row[1] for row in columns) for table, columns in LAYOUT.items()}
JSON_COLUMNS = {"payload", "value", "evidence"}


def _present(value):
    if (
        not isinstance(value, list)
        or any(not isinstance(table, str) or table not in TABLES for table in value)
        or len(value) != len(set(value))
    ):
        raise ValueError("invalid runtime table presence")
    return set(value)


def _schema(db):
    actual = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if not actual <= set(TABLES):
        raise ValueError("unsupported native runtime schema tables")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():
        raise ValueError("unsupported native runtime schema behavior")
    for table in actual:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        if _normalize(sql) != SCHEMAS[table]:
            raise ValueError("unsupported native runtime schema definition")
        if tuple(tuple(row) for row in db.execute(f"PRAGMA table_info({table})")) != LAYOUT[table]:
            raise ValueError("unsupported native runtime schema columns")
        if any(row[2] and row[3] != "pk" for row in db.execute(f"PRAGMA index_list({table})")):
            raise ValueError("unsupported native runtime schema uniqueness")
    return actual


def _insert(db, table, rows):
    columns = TABLES[table]
    names = ("_rowid", *columns)
    db.executemany(
        f"INSERT INTO {table}(rowid,{','.join(columns)}) VALUES ({','.join('?' for _ in names)})",
        [tuple(row[name] for name in names) for row in rows],
    )


def validate(snapshot):
    snapshot = decode(encode(snapshot))
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"schema", "tables", "present_tables"}
        or snapshot["schema"] != SCHEMA
        or not isinstance(snapshot["tables"], dict)
        or set(snapshot["tables"]) != set(TABLES)
    ):
        raise ValueError("unsupported runtime snapshot")
    present = _present(snapshot["present_tables"])
    tables = snapshot["tables"]
    if any(not isinstance(rows, list) for rows in tables.values()):
        raise ValueError("invalid runtime rows")
    for table, rows in tables.items():
        if rows and table not in present:
            raise ValueError("runtime rows require present table")
        ordinals = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(TABLES[table]) | {"_rowid"}:
                raise ValueError("invalid runtime row columns")
            ordinal = row["_rowid"]
            if type(ordinal) is not int or not 1 <= ordinal < 2**63 or ordinal in ordinals:
                raise ValueError("invalid runtime ordinal")
            ordinals.add(ordinal)
            for _, name, kind, notnull, _, pk in LAYOUT[table]:
                value = row[name]
                if value is None:
                    if notnull or kind == "INTEGER" and pk:
                        raise ValueError("invalid runtime null")
                    continue
                if kind == "TEXT" and not isinstance(value, str):
                    raise ValueError("invalid runtime text")
                if kind == "INTEGER" and (type(value) is not int or not -(2**63) <= value < 2**63):
                    raise ValueError("invalid runtime integer")
                if kind == "REAL" and (type(value) is not float or not math.isfinite(value)):
                    raise ValueError("invalid runtime real")
                if name in JSON_COLUMNS:
                    decode(value)
            if table in ("runtime_events", "runtime_answer_history") and row["id"] != ordinal:
                raise ValueError("runtime event/history identity mismatch")
    # A trusted schema applies every native unique/key constraint, including
    # SQLite's nullable composite keys. No SQL from the snapshot is executed.
    db = sqlite3.connect(":memory:")
    try:
        for table in present:
            _create(db, table)
            _insert(db, table, tables[table])
    except (sqlite3.Error, OverflowError) as error:
        raise ValueError("invalid native runtime record identities") from error
    finally:
        db.close()
    return snapshot


def export_runtime(db):
    if db.in_transaction:
        raise ValueError("runtime export requires no open native transaction")
    with db:
        db.execute("BEGIN")
        present = _schema(db)
        tables = {table: [] for table in TABLES}
        for table in TABLES:
            if table not in present:
                continue
            names = ("_rowid", *TABLES[table])
            for row in db.execute(f"SELECT rowid,{','.join(TABLES[table])} FROM {table} ORDER BY rowid"):
                tables[table].append(dict(zip(names, row)))
        return validate({"schema": SCHEMA, "tables": tables, "present_tables": [table for table in TABLES if table in present]})


def restore_runtime(db, snapshot):
    snapshot = validate(snapshot)
    if db.in_transaction:
        raise ValueError("runtime restore requires no open native transaction")
    with db:
        db.execute("BEGIN IMMEDIATE")
        actual = _schema(db)
        if any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in actual):
            raise ValueError("runtime restore requires empty native destination")
        present = set(snapshot["present_tables"])
        if not actual <= present:
            raise ValueError("empty runtime destination has incompatible table presence")
        for table in TABLES:
            if table not in present:
                continue
            if table not in actual:
                _create(db, table)
            _insert(db, table, snapshot["tables"][table])


class RuntimeArchive(CatalogArchive):
    """Same immutable record/manifest transport as catalog/console migration."""

    columns = TABLES
    snapshot_schema = SCHEMA
    manifest_schema = "cyclotron-runtime-manifest/v1"
    schema_field = "runtimeSchema"
    metadata_field = "presentTables"
    snapshot_metadata_field = "present_tables"
    validator = staticmethod(validate)

    def _check_metadata(self, value):
        _present(value)
