"""Embedded root records; SDK algorithms own decisions and learning policy.

Transactions group known root-domain operations, especially source cursors and
all derived reviews/events/usage. No SQL emulation or cloud lease is provided.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .storage_errors import StorageMissing, refusing_duplicate_identity

SCHEMA_SQL = '\n            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);\n            CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, item_values TEXT NOT NULL, fingerprint TEXT NOT NULL);\n            CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, item_id TEXT NOT NULL, item_fingerprint TEXT NOT NULL,\n                state TEXT NOT NULL, payload TEXT NOT NULL);\n            CREATE INDEX IF NOT EXISTS decisions_by_item ON decisions(item_id);\n            CREATE TABLE IF NOT EXISTS reviews (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, decision_id TEXT NOT NULL,\n                classifier TEXT NOT NULL, payload TEXT NOT NULL);\n            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);\n            CREATE TABLE IF NOT EXISTS learning_queue (seq INTEGER PRIMARY KEY, classifier TEXT UNIQUE NOT NULL,\n                since_count INTEGER NOT NULL, queued_at TEXT NOT NULL, superseded INTEGER NOT NULL DEFAULT 0);\n            CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, decision_id TEXT NOT NULL, model TEXT NOT NULL,\n                input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,\n                source TEXT);\n            CREATE TABLE IF NOT EXISTS usage_shares (usage_id INTEGER NOT NULL, classifier TEXT NOT NULL,\n                share REAL NOT NULL, PRIMARY KEY(usage_id, classifier));\n        '


class EmbeddedStorage(Protocol):
    def transaction(self): ...
    def meta(self,key: str) -> tuple | None: ...
    def insert_meta(self,key: str,payload: str,*,ignore=False) -> None: ...
    def put_meta(self,key: str,payload: str) -> None: ...
    def put_item(self,identifier: str,payload: str,fingerprint: str) -> None: ...
    def items(self) -> list[tuple]: ...
    def item_values(self,identifier: str) -> str | None: ...
    def insert_decision(self,row: tuple) -> None: ...
    def set_decision_state(self,identifier: str,state: str,*,unless_superseded=False) -> None: ...
    def decision_count(self) -> int: ...
    def decision_rows(self) -> list[tuple]: ...
    def decision_ids(self) -> list[tuple]: ...
    def decision_items(self) -> list[tuple]: ...
    def decision_payloads(self) -> list[tuple]: ...
    def window_decisions(self,limit: int,offset: int) -> list[tuple]: ...
    def decision(self,identifier: str) -> tuple | None: ...
    def decision_fingerprint(self,identifier: str) -> str: ...
    def latest_decision(self,item_id: str) -> tuple | None: ...
    def latest_id(self,item_id: str | None=None) -> tuple | None: ...
    def insert_review(self,identifier: str,decision_id: str,classifier: str,payload: str) -> None: ...
    def review(self,identifier: str) -> tuple | None: ...
    def reviews(self,decision_id: str,classifier: str) -> list[tuple]: ...
    def last_review(self,decision_id: str,classifier: str) -> tuple | None: ...
    def append_event(self,payload: str) -> int: ...
    def events(self,after: int,limit: int) -> list[tuple]: ...
    def insert_usage(self,decision_id: str,model: str,prompt: int,cached: int,output: int,source: str | None) -> int: ...
    def insert_usage_share(self,usage_id: int,classifier: str,share: float) -> None: ...
    def usage_counts(self) -> list[tuple]: ...
    def usage_shares(self) -> list[tuple]: ...
    def usage_by_output(self) -> list[tuple]: ...
    def enqueue_learning(self,classifier: str,since_count: int,at: str) -> None: ...
    def pending_learning(self) -> list[str]: ...
    def next_learning(self) -> tuple | None: ...
    def learning_rows(self) -> list[tuple]: ...
    def supersede_learning(self,seq: int) -> None: ...
    def requeue_learning(self,seq: int,covered: int) -> None: ...
    def review_count(self,classifier: str) -> int: ...
    def delete_learning(self,seq: int) -> None: ...
    def close(self) -> None: ...


class SQLiteEmbeddedSnapshots:
    """Native snapshot migration needs SQLite tables, so it is not part of the cross-backend protocol."""
    def export_storage(self,*,max_records=10000,max_bytes=64000000):
        from .domain_snapshots import embedded_codec
        return embedded_codec().export(self.db,max_records=max_records,max_bytes=max_bytes)

    def restore_storage(self,snapshot,*,max_records=10000,max_bytes=64000000):
        from .domain_snapshots import embedded_codec
        if snapshot.get('schema')=='cyclotron-embedded-root/v1':
            from copy import deepcopy
            snapshot=deepcopy(embedded_codec(1).validate(snapshot,max_records=max_records,max_bytes=max_bytes))
            snapshot['schema']='cyclotron-embedded-root/v2'
            for row in snapshot['tables']['learning_queue']: row['superseded']=0
        return embedded_codec().restore(self.db,snapshot,max_records=max_records,max_bytes=max_bytes)


class SQLiteEmbeddedStorage(SQLiteEmbeddedSnapshots):
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=30)
        self.db.executescript(SCHEMA_SQL)
        if "superseded" not in {row[1] for row in self.db.execute("PRAGMA table_info(learning_queue)")}:
            with self.db:
                self.db.execute("ALTER TABLE learning_queue ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def transaction(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            yield self

    def close(self): self.db.close()
    def meta(self,key): return self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    def insert_meta(self,key,payload,*,ignore=False):
        sql='INSERT OR IGNORE' if ignore else 'INSERT'
        with refusing_duplicate_identity('duplicate root metadata'): self.db.execute(sql+' INTO meta VALUES (?,?)',(key,payload))
    def put_meta(self,key,payload): self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,payload))
    def put_item(self,identifier,payload,fingerprint):
        self.db.execute('INSERT INTO items VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET item_values=excluded.item_values,fingerprint=excluded.fingerprint',(identifier,payload,fingerprint))
    def items(self): return self.db.execute('SELECT id,item_values FROM items ORDER BY rowid').fetchall()
    def item_values(self,identifier):
        row=self.db.execute('SELECT item_values FROM items WHERE id=?',(identifier,)).fetchone()
        return row[0] if row else None
    def insert_decision(self,row):
        with refusing_duplicate_identity('duplicate decision identity'): self.db.execute('INSERT INTO decisions VALUES (?,?,?,?,?)',row)
    def set_decision_state(self,identifier,state,*,unless_superseded=False):
        self.db.execute("UPDATE decisions SET state=? WHERE id=?"+(" AND state!='superseded'" if unless_superseded else ''),(state,identifier))
    def decision_count(self): return self.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]
    def decision_rows(self): return self.db.execute('SELECT id,state,payload FROM decisions ORDER BY rowid').fetchall()
    def decision_ids(self): return self.db.execute('SELECT id FROM decisions ORDER BY rowid').fetchall()
    def decision_items(self): return self.db.execute('SELECT id,item_id FROM decisions ORDER BY rowid').fetchall()
    def decision_payloads(self): return self.db.execute('SELECT payload FROM decisions ORDER BY rowid').fetchall()
    def window_decisions(self,limit,offset): return self.db.execute('SELECT id,payload FROM decisions ORDER BY rowid LIMIT ? OFFSET ?',(limit,offset)).fetchall()
    def decision(self,identifier): return self.db.execute('SELECT payload FROM decisions WHERE id=?',(identifier,)).fetchone()
    def decision_fingerprint(self,identifier):
        row = self.db.execute('SELECT item_fingerprint FROM decisions WHERE id=?',(identifier,)).fetchone()
        if row is None: raise StorageMissing(f'unknown decision {identifier}')
        return row[0]
    def latest_decision(self,item_id): return self.db.execute("SELECT state,payload FROM decisions WHERE item_id=? AND state!='superseded' ORDER BY rowid DESC LIMIT 1",(item_id,)).fetchone()
    def latest_id(self,item_id=None):
        if item_id is not None: return self.db.execute('SELECT id FROM decisions WHERE item_id=? ORDER BY rowid DESC LIMIT 1',(item_id,)).fetchone()
        return self.db.execute('SELECT id FROM decisions ORDER BY rowid DESC LIMIT 1').fetchone()
    def insert_review(self,identifier,decision_id,classifier,payload):
        with refusing_duplicate_identity('duplicate review identity'): self.db.execute('INSERT INTO reviews(id,decision_id,classifier,payload) VALUES (?,?,?,?)',(identifier,decision_id,classifier,payload))
    def review(self,identifier): return self.db.execute('SELECT payload FROM reviews WHERE id=?',(identifier,)).fetchone()
    def reviews(self,decision_id,classifier): return self.db.execute('SELECT payload FROM reviews WHERE decision_id=? AND classifier=? ORDER BY seq',(decision_id,classifier)).fetchall()
    def last_review(self,decision_id,classifier): return self.db.execute('SELECT payload FROM reviews WHERE decision_id=? AND classifier=? ORDER BY seq DESC LIMIT 1',(decision_id,classifier)).fetchone()
    def append_event(self,payload): return self.db.execute('INSERT INTO events(payload) VALUES (?)',(payload,)).lastrowid
    def events(self,after,limit): return self.db.execute('SELECT id,payload FROM events WHERE id>? ORDER BY id LIMIT ?',(after,limit)).fetchall()
    def insert_usage(self,decision_id,model,prompt,cached,output,source):
        return self.db.execute('INSERT INTO usage(decision_id,model,input_tokens,cached_input_tokens,output_tokens,source) VALUES (?,?,?,?,?,?)',(decision_id,model,prompt,cached,output,source)).lastrowid
    def insert_usage_share(self,usage_id,classifier,share):
        with refusing_duplicate_identity('duplicate usage share'): self.db.execute('INSERT INTO usage_shares VALUES (?,?,?)',(usage_id,classifier,share))
    def usage_counts(self): return self.db.execute('SELECT decision_id,model,input_tokens,cached_input_tokens,output_tokens FROM usage ORDER BY id').fetchall()
    def usage_shares(self): return self.db.execute('SELECT usage_id,classifier,share FROM usage_shares').fetchall()
    def usage_by_output(self): return self.db.execute('SELECT id,model,input_tokens,cached_input_tokens,output_tokens,source FROM usage ORDER BY id').fetchall()
    def enqueue_learning(self,classifier,since_count,at): self.db.execute('INSERT OR IGNORE INTO learning_queue(classifier,since_count,queued_at) VALUES (?,?,?)',(classifier,since_count,at))
    def pending_learning(self): return [row[0] for row in self.db.execute('SELECT classifier FROM learning_queue ORDER BY seq')]
    def next_learning(self): return self.db.execute('SELECT seq,classifier,since_count FROM learning_queue ORDER BY seq LIMIT 1').fetchone()
    def learning_rows(self): return self.db.execute('SELECT seq,classifier,since_count,superseded FROM learning_queue ORDER BY seq').fetchall()
    def supersede_learning(self,seq): self.db.execute('UPDATE learning_queue SET superseded=superseded+1 WHERE seq=?',(seq,))
    def requeue_learning(self,seq,covered): self.db.execute('UPDATE learning_queue SET since_count=?,superseded=0 WHERE seq=?',(covered,seq))
    def review_count(self,classifier): return self.db.execute('SELECT COUNT(*) FROM reviews WHERE classifier=?',(classifier,)).fetchone()[0]
    def delete_learning(self,seq): self.db.execute('DELETE FROM learning_queue WHERE seq=?',(seq,))
