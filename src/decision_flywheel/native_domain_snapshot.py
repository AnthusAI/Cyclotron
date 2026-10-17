"""Shared bounded codec for known native source-store compositions.

Only caller-code-owned schema factories are accepted. No schema/SQL is imported
from a snapshot; it contains raw records, ordinals and sequence watermarks only.
"""

import math
import re
import sqlite3

from .catalog_snapshot import budgets, decode, encode


def normalize(sql):
    return re.sub(r"\s+", "", sql).lower()


class NativeDomainCodec:
    def __init__(self, schema, create, *, json_columns=None, sequences=None, flexible_columns=False, validate_records=None):
        self.schema = schema
        self.json_columns = json_columns or {}
        self.sequences = sequences or {}
        self.flexible_columns = flexible_columns
        self.validate_records = validate_records
        db = sqlite3.connect(":memory:")
        try:
            create(db)
            self.ddl = {
                name: sql
                for name, sql in db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
                )
            }
            self.indexes = [
                sql
                for (sql,) in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY rowid")
            ]
            self.layout = {table: tuple(tuple(row) for row in db.execute(f"PRAGMA table_info({table})")) for table in self.ddl}
            self.columns = {table: tuple(row[1] for row in layout) for table, layout in self.layout.items()}
            self.keys = {table: self._unique(db, table) for table in self.ddl}
            self.foreign = {table: self._foreign(db, table) for table in self.ddl}
        finally:
            db.close()

    @staticmethod
    def _unique(db, table):
        return sorted(
            tuple(row[0] for row in db.execute("SELECT name FROM pragma_index_info(?)", (index[1],)))
            for index in db.execute(f"PRAGMA index_list({table})")
            if index[2]
        )

    @staticmethod
    def _foreign(db, table):
        return sorted(tuple(row[2:]) for row in db.execute(f"PRAGMA foreign_key_list({table})"))

    def _schema(self, db):
        actual = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if actual != set(self.ddl):
            raise ValueError("unsupported native domain schema tables")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():
            raise ValueError("unsupported native domain schema behavior")
        for table in self.ddl:
            actual_layout = tuple(tuple(row) for row in db.execute(f"PRAGMA table_info({table})"))
            # Legacy reviewer migrations appended columns; field semantics stay
            # identical even when their physical column positions differ.
            shape = lambda rows: sorted(tuple(row[1:]) for row in rows)
            if shape(actual_layout) != shape(self.layout[table]):
                raise ValueError("unsupported native domain schema columns")
            sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            if not self.flexible_columns and normalize(sql) != normalize(self.ddl[table]):
                raise ValueError("unsupported native domain schema definition")
            if self.flexible_columns:
                checks = lambda source: sorted(normalize(part) for part in re.findall(r"CHECK\s*\((.*?)\)", source, re.I | re.S))
                if not set(checks(sql)) <= set(checks(self.ddl[table])):
                    raise ValueError("unsupported native domain schema checks")
            foreign = self._foreign(db, table)
            compatible_foreign = (
                set(foreign) <= set(self.foreign[table]) if self.flexible_columns else foreign == self.foreign[table]
            )
            if self._unique(db, table) != self.keys[table] or not compatible_foreign:
                raise ValueError("unsupported native domain schema constraints")

    def _insert(self, db, table, rows):
        columns = self.columns[table]
        names = ("_rowid", *columns)
        db.executemany(
            f"INSERT INTO {table}(rowid,{','.join(columns)}) VALUES ({','.join('?' for _ in names)})",
            [tuple(row[name] for name in names) for row in rows],
        )

    def validate(self, snapshot, *, max_records=10000, max_bytes=64000000):
        budgets(max_records, max_bytes)
        content = encode(snapshot)
        if len(content) > max_bytes:
            raise ValueError("domain snapshot byte budget exceeded")
        snapshot = decode(content)
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != {"schema", "tables", "metadata"}
            or snapshot["schema"] != self.schema
            or not isinstance(snapshot["tables"], dict)
            or set(snapshot["tables"]) != set(self.columns)
            or not isinstance(snapshot["metadata"], dict)
            or set(snapshot["metadata"]) != {"sequences"}
        ):
            raise ValueError("unsupported native domain snapshot")
        tables = snapshot["tables"]
        sequences = snapshot["metadata"]["sequences"]
        if any(not isinstance(rows, list) for rows in tables.values()):
            raise ValueError("invalid domain rows")
        if sum(map(len, tables.values())) > max_records:
            raise ValueError("domain snapshot record budget exceeded")
        for table, rows in tables.items():
            ordinals = set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(self.columns[table]) | {"_rowid"}:
                    raise ValueError("invalid domain row")
                ordinal = row["_rowid"]
                if type(ordinal) is not int or not 1 <= ordinal < 2**63 or ordinal in ordinals:
                    raise ValueError("invalid domain ordinal")
                ordinals.add(ordinal)
                for _, name, kind, notnull, _, pk in self.layout[table]:
                    value = row[name]
                    if value is None:
                        if notnull or kind == "INTEGER" and pk:
                            raise ValueError("invalid domain null")
                        continue
                    if kind == "TEXT" and not isinstance(value, str):
                        raise ValueError("invalid domain text")
                    if kind == "INTEGER" and (type(value) is not int or not -(2**63) <= value < 2**63):
                        raise ValueError("invalid domain integer")
                    if kind == "REAL" and (type(value) is not float or not math.isfinite(value)):
                        raise ValueError("invalid domain real")
                    if kind == "INTEGER" and pk and value != ordinal:
                        raise ValueError("domain integer identity mismatch")
                    if name in self.json_columns.get(table, ()):
                        decode(value)
        if not isinstance(sequences, dict) or set(sequences) != set(self.sequences):
            raise ValueError("invalid domain sequences")
        for table, column in self.sequences.items():
            value = sequences[table]
            if type(value) is not int or not max((row[column] for row in tables[table]), default=0) <= value < 2**63:
                raise ValueError("invalid domain sequence watermark")
        scratch = sqlite3.connect(":memory:")
        try:
            scratch.execute("PRAGMA foreign_keys=ON")
            for sql in self.ddl.values():
                scratch.execute(sql)
            for table in self.columns:
                self._insert(scratch, table, tables[table])
            if self.validate_records is not None:
                self.validate_records(snapshot)
        except (sqlite3.Error, OverflowError) as error:
            raise ValueError("invalid domain record identities or references") from error
        finally:
            scratch.close()
        return snapshot

    def export(self, db, *, max_records=10000, max_bytes=64000000):
        budgets(max_records, max_bytes)
        if db.in_transaction:
            raise ValueError("domain export requires no open native transaction")
        with db:
            db.execute("BEGIN")
            self._schema(db)
            if sum(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in self.columns) > max_records:
                raise ValueError("domain snapshot record budget exceeded")
            size = 0
            for table, columns in self.columns.items():
                expression = "+".join(f"COALESCE(length(CAST({column} AS BLOB)),0)" for column in columns)
                size += db.execute(f"SELECT COALESCE(SUM({expression}),0) FROM {table}").fetchone()[0]
            if size > max_bytes:
                raise ValueError("domain snapshot byte budget exceeded")
            tables = {}
            used = 0
            for table, columns in self.columns.items():
                rows = []
                for row in db.execute(f"SELECT rowid,{','.join(columns)} FROM {table} ORDER BY rowid"):
                    record = dict(zip(("_rowid", *columns), row))
                    used += len(encode(record))
                    if used > max_bytes:
                        raise ValueError("domain snapshot byte budget exceeded")
                    rows.append(record)
                tables[table] = rows
            watermarks = dict(db.execute("SELECT name,seq FROM sqlite_sequence")) if self.sequences else {}
            return self.validate(
                {
                    "schema": self.schema,
                    "tables": tables,
                    "metadata": {"sequences": {table: watermarks.get(table, 0) for table in self.sequences}},
                },
                max_records=max_records,
                max_bytes=max_bytes,
            )

    def restore(self, db, snapshot, *, max_records=10000, max_bytes=64000000):
        snapshot = self.validate(snapshot, max_records=max_records, max_bytes=max_bytes)
        if db.in_transaction:
            raise ValueError("domain restore requires no open native transaction")
        with db:
            db.execute("BEGIN IMMEDIATE")
            existing = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            }
            if existing:
                self._schema(db)
                for table in self.sequences:
                    watermark = db.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
                    if watermark is not None and watermark[0] != 0:
                        raise ValueError("domain restore requires empty sequence history")
                if any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in existing):
                    raise ValueError("domain restore requires empty native destination")
            else:
                for sql in self.ddl.values():
                    db.execute(sql)
                for sql in self.indexes:
                    db.execute(sql)
            for table in self.columns:
                self._insert(db, table, snapshot["tables"][table])
            for table, value in snapshot["metadata"]["sequences"].items():
                db.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
                db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (table, value))
