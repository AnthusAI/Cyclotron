"""Runtime state and evidence contract; learning/cache conversion is unfinished.

The native core composes this with its remaining SQLite learning repositories.
No cloud runtime constructor or partially portable authority selector is exposed.
"""
from collections.abc import Iterable
import json
from pathlib import Path
import sqlite3
from typing import Protocol


class RuntimeStateStorage(Protocol):
    def bind_contract(self, expected: str) -> None: ...
    def state(self, key: str) -> str | None: ...
    def put_state(self, key: str, payload: str) -> None: ...
    def append_event(self, payload: str) -> int: ...
    def history(self, limit: int = 100) -> tuple[dict,...]: ...
    def trace_events(self, *, after_event_id=0, limit=100) -> dict: ...
    def event_payloads(self, kinds: tuple[str,...]) -> Iterable[str]: ...
    def close(self) -> None: ...


class SQLiteRuntimeStateStorage:
    def __init__(self,path, *, timeout=30):
        path=Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=timeout)
        self.db.execute('PRAGMA journal_mode=WAL')
        # The last three native tables support the unchanged answer/learning
        # composition. They are not part of RuntimeStateStorage's interface.
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_answers (key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT);
            CREATE TABLE IF NOT EXISTS runtime_answer_history (id INTEGER PRIMARY KEY, key TEXT NOT NULL, status TEXT NOT NULL, payload TEXT);
            CREATE TABLE IF NOT EXISTS runtime_rounds (key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT);
        ''')

    def bind_contract(self,expected):
        json.loads(expected)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved=self.state('contract')
            if saved is not None and saved!=expected:
                raise ValueError('runtime task/model contract does not match the saved study')
            self.db.execute("INSERT OR IGNORE INTO runtime_state VALUES ('contract',?)",(expected,))

    def state(self,key):
        row=self.db.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()
        return row[0] if row else None

    def put_state(self,key,payload):
        json.loads(payload)
        with self.db: self.db.execute('INSERT OR REPLACE INTO runtime_state VALUES (?,?)',(key,payload))

    def append_event(self,payload):
        json.loads(payload)
        with self.db: return self.db.execute('INSERT INTO runtime_events(payload) VALUES (?)',(payload,)).lastrowid

    def history(self,limit=100):
        # Preserve the existing local history API; browser trace paging below
        # remains bounded. A cloud aggregate-history budget is not selected here.
        rows=self.db.execute('SELECT id,payload FROM runtime_events ORDER BY id DESC LIMIT ?',(limit,)).fetchall()
        return tuple({**json.loads(payload),'event_id':identifier} for identifier,payload in reversed(rows))

    def trace_events(self, *, after_event_id=0,limit=100):
        if type(after_event_id) is not int or after_event_id<0 or type(limit) is not int or not 1<=limit<=1000:
            raise ValueError('trace cursor must be nonnegative and page size between 1 and 1000')
        rows=self.db.execute('SELECT id,payload FROM runtime_events WHERE id>? ORDER BY id LIMIT ?',(after_event_id,limit)).fetchall()
        return {'events':[{**json.loads(payload),'event_id':identifier} for identifier,payload in rows],
            'cursor':rows[-1][0] if rows else after_event_id}

    def event_payloads(self,kinds):
        if not isinstance(kinds,tuple) or not 1<=len(kinds)<=100 or any(not isinstance(kind,str) for kind in kinds):
            raise ValueError('explicit bounded event kinds required')
        query="SELECT payload FROM runtime_events WHERE json_extract(payload,'$.kind') IN ("+','.join('?' for _ in kinds)+') ORDER BY id'
        cursor=self.db.execute(query,kinds)
        for row in cursor: yield row[0]

    def close(self): self.db.close()
