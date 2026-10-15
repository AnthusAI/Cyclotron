"""Frozen evaluation execution evidence, separate from the shared batch store."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Protocol


class EvaluationStorage(Protocol):
    def bind_clock(self, plan: str, proposed_clock: str) -> str: ...
    def append_trace(self, identity: str, payload: str) -> tuple[int,str]: ...
    def events(self, *, after: int=0, limit: int=100) -> list[tuple[int,str]]: ...
    def export_storage(self,*,max_records=10000,max_bytes=64000000) -> dict: ...
    def restore_storage(self,snapshot: dict,*,max_records=10000,max_bytes=64000000) -> None: ...


class SQLiteEvaluationStorage:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS execution(plan TEXT PRIMARY KEY, clock TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS trace(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, payload TEXT NOT NULL)')

    @contextmanager
    def _connect(self):
        db=sqlite3.connect(self.path,timeout=30)
        try:
            with db: yield db
        finally: db.close()

    def bind_clock(self,plan,proposed_clock):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO execution VALUES (?,?)',(plan,proposed_clock))
            return db.execute('SELECT clock FROM execution WHERE plan=?',(plan,)).fetchone()[0]

    def append_trace(self,identity,payload):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO trace(key,payload) VALUES (?,?)',(identity,payload))
            return db.execute('SELECT id,payload FROM trace WHERE key=?',(identity,)).fetchone()

    def events(self,*,after=0,limit=100):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=1000:
            raise ValueError('bounded trace cursor required')
        with self._connect() as db:
            return db.execute('SELECT id,payload FROM trace WHERE id>? ORDER BY id LIMIT ?',(after,limit)).fetchall()

    def export_storage(self,*,max_records=10000,max_bytes=64000000):
        from .domain_snapshots import evaluation_codec
        with self._connect() as db:
            return evaluation_codec().export(db,max_records=max_records,max_bytes=max_bytes)

    def restore_storage(self,snapshot,*,max_records=10000,max_bytes=64000000):
        from .domain_snapshots import evaluation_codec
        with self._connect() as db:
            return evaluation_codec().restore(db,snapshot,max_records=max_records,max_bytes=max_bytes)
