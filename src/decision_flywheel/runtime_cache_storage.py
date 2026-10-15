"""Answer revisions and trial persistence, independent of SQL at the consumer.

This native adapter preserves the existing local retry/refresh protocol. A cloud
implementation must additionally use the selected durable request coordinator;
these operations alone do not establish distributed ownership or paid budgets.
"""
from typing import Protocol

CacheRecord = tuple[str, str | None]


class RuntimeCacheStorage(Protocol):
    def answer(self, key: str) -> CacheRecord | None: ...
    def begin_answer(self, key: str, previous: CacheRecord | None) -> None: ...
    def fail_answer(self, key: str) -> None: ...
    def complete_answer(self, key: str, payload: str, *, previous: CacheRecord | None,
                        current_key: str | None = None) -> None: ...
    def round(self, key: str) -> CacheRecord | None: ...
    def reset_pending_round(self, key: str) -> None: ...
    def begin_round(self, key: str) -> None: ...
    def complete_round(self, key: str, payload: str) -> None: ...
    def save_completed_round(self, key: str, payload: str) -> None: ...


class SQLiteRuntimeCacheStorage:
    def __init__(self, db):
        # Shared native connection preserves composition with existing learning
        # tables. This repository never owns or closes the host connection.
        self.db = db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS runtime_answers (key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT);
            CREATE TABLE IF NOT EXISTS runtime_answer_history (id INTEGER PRIMARY KEY, key TEXT NOT NULL, status TEXT NOT NULL, payload TEXT);
            CREATE TABLE IF NOT EXISTS runtime_rounds (key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT);
        ''')

    def answer(self, key):
        return self.db.execute('SELECT status,payload FROM runtime_answers WHERE key=?', (key,)).fetchone()

    def begin_answer(self, key, previous):
        with self.db:
            if previous:
                self.db.execute('INSERT INTO runtime_answer_history(key,status,payload) VALUES (?,?,?)', (key, *previous))
            self.db.execute("INSERT OR REPLACE INTO runtime_answers VALUES (?, 'pending', NULL)", (key,))

    def fail_answer(self, key):
        with self.db:
            self.db.execute("UPDATE runtime_answers SET status='failed' WHERE key=?", (key,))

    def complete_answer(self, key, payload, *, previous, current_key=None):
        with self.db:
            if current_key is not None and current_key != key:
                # A shared response refresh advances its identity. Preserve the
                # old identity's answer, not an interrupted alias of the new one.
                if previous:
                    self.db.execute('UPDATE runtime_answers SET status=?,payload=? WHERE key=?', (*previous, key))
                else:
                    self.db.execute('DELETE FROM runtime_answers WHERE key=?', (key,))
                key = current_key
                self.db.execute("INSERT OR REPLACE INTO runtime_answers VALUES (?, 'pending', NULL)", (key,))
            self.db.execute("UPDATE runtime_answers SET status='complete',payload=? WHERE key=?", (payload, key))

    def round(self, key):
        return self.db.execute('SELECT status,payload FROM runtime_rounds WHERE key=?', (key,)).fetchone()

    def reset_pending_round(self, key):
        with self.db:
            self.db.execute("DELETE FROM runtime_rounds WHERE key=? AND status='pending'", (key,))

    def begin_round(self, key):
        with self.db:
            self.db.execute("INSERT INTO runtime_rounds VALUES (?, 'pending', NULL)", (key,))

    def complete_round(self, key, payload):
        with self.db:
            self.db.execute("UPDATE runtime_rounds SET status='complete',payload=? WHERE key=?", (payload, key))

    def save_completed_round(self, key, payload):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runtime_rounds VALUES (?, 'complete', ?)", (key, payload))
