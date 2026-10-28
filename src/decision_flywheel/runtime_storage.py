"""Runtime state and evidence contract with a native SQLite implementation.

The native host composes cache, learning and forward-evaluation repositories on
this connection. No cloud constructor or distributed authority is selected here.
"""

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from .active_labels import (
    ACTIVE_LABELS_STATE_KEY,
    FEEDBACK_EVENT_KIND,
    dump_active_labels,
    load_active_labels,
    rebuild_active_labels,
    record_feedback_event,
)


class RuntimeStateStorage(Protocol):
    def bind_contract(self, expected: str) -> None: ...
    def state(self, key: str) -> str | None: ...
    def put_state(self, key: str, payload: str) -> None: ...
    def append_event(self, payload: str) -> int: ...
    def history(self, limit: int = 100) -> tuple[dict, ...]: ...
    def trace_events(self, *, after_event_id=0, limit=100) -> dict: ...
    def event_payloads(self, kinds: tuple[str, ...]) -> Iterable[str]: ...
    def events_of(self, kinds: tuple[str, ...]) -> tuple[dict, ...]: ...
    def active_labels(self) -> tuple[dict, ...]: ...
    def next_cycle_number(self) -> int: ...
    def close(self) -> None: ...


class SQLiteRuntimeStateStorage:
    def __init__(self, path, *, timeout=30):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=timeout)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        """)

    def bind_contract(self, expected):
        json.loads(expected)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            saved = self.state("contract")
            if saved is not None and saved != expected:
                raise ValueError("runtime task/model contract does not match the saved study")
            self.db.execute("INSERT OR IGNORE INTO runtime_state VALUES ('contract',?)", (expected,))

    def state(self, key):
        row = self.db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put_state(self, key, payload):
        json.loads(payload)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runtime_state VALUES (?,?)", (key, payload))

    def append_event(self, payload):
        event = json.loads(payload)
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            identifier = self.db.execute("INSERT INTO runtime_events(payload) VALUES (?)", (payload,)).lastrowid
            if event.get("kind") == FEEDBACK_EVENT_KIND:
                active_by_item = self._active_labels_by_item()
                record_feedback_event(active_by_item, identifier, event)
                self._save_active_labels(active_by_item)
            return identifier

    def _active_labels_by_item(self):
        saved = self.state(ACTIVE_LABELS_STATE_KEY)
        if saved is not None:
            return load_active_labels(saved)
        return rebuild_active_labels(self.db.execute("SELECT id,payload FROM runtime_events ORDER BY id"))

    def _save_active_labels(self, active_by_item):
        self.db.execute(
            "INSERT OR REPLACE INTO runtime_state VALUES (?,?)", (ACTIVE_LABELS_STATE_KEY, dump_active_labels(active_by_item))
        )

    def active_labels(self):
        saved = self.state(ACTIVE_LABELS_STATE_KEY)
        if saved is None:
            with self.db:
                if not self.db.in_transaction:
                    self.db.execute("BEGIN IMMEDIATE")
                active_by_item = self._active_labels_by_item()
                self._save_active_labels(active_by_item)
            return tuple(active_by_item.values())
        return tuple(load_active_labels(saved).values())

    def history(self, limit=100):
        # Preserve the existing local history API; browser trace paging below
        # pages by its own cursor.
        rows = self.db.execute("SELECT id,payload FROM runtime_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return tuple({**json.loads(payload), "event_id": identifier} for identifier, payload in reversed(rows))

    def trace_events(self, *, after_event_id=0, limit=100):
        if type(after_event_id) is not int or after_event_id < 0 or type(limit) is not int or limit < 1:
            raise ValueError("trace cursor must be nonnegative and page size at least 1")
        rows = self.db.execute(
            "SELECT id,payload FROM runtime_events WHERE id>? ORDER BY id LIMIT ?", (after_event_id, limit)
        ).fetchall()
        return {
            "events": [{**json.loads(payload), "event_id": identifier} for identifier, payload in rows],
            "cursor": rows[-1][0] if rows else after_event_id,
        }

    def event_payloads(self, kinds):
        if not isinstance(kinds, tuple) or not kinds or any(not isinstance(kind, str) for kind in kinds):
            raise ValueError("explicit event kinds required")
        query = (
            "SELECT payload FROM runtime_events WHERE json_extract(payload,'$.kind') IN ("
            + ",".join("?" for _ in kinds)
            + ") ORDER BY id"
        )
        cursor = self.db.execute(query, kinds)
        for row in cursor:
            yield row[0]

    def events_of(self, kinds):
        if not isinstance(kinds, tuple) or not kinds or any(not isinstance(kind, str) for kind in kinds):
            raise ValueError("explicit event kinds required")
        sql = (
            "SELECT id,payload FROM runtime_events WHERE json_extract(payload,'$.kind') IN ("
            + ",".join("?" for _ in kinds)
            + ") ORDER BY id"
        )
        return tuple({**json.loads(payload), "event_id": identifier} for identifier, payload in self.db.execute(sql, kinds))

    def next_cycle_number(self):
        return self.db.execute("SELECT COALESCE(MAX(json_extract(payload,'$.cycle_number')),0)+1 FROM runtime_events").fetchone()[
            0
        ]

    def export_storage(self):
        from .runtime_snapshot import export_runtime

        return export_runtime(self.db)

    def restore_storage(self, snapshot):
        from .runtime_snapshot import restore_runtime

        return restore_runtime(self.db, snapshot)

    def close(self):
        self.db.close()
