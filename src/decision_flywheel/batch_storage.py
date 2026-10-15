"""Shared-request storage contract and complete zero-cloud SQLite implementation."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Protocol
from uuid import UUID, uuid5, NAMESPACE_URL

from .decision_cache import CacheMiss


class BatchConflict(RuntimeError):
    """A stale owner or changed operation may not settle current work."""


class BatchInProgress(RuntimeError):
    """Pending provider work needs explicit reconciliation, not lease stealing."""


class BatchUncertain(RuntimeError):
    """Publication may have succeeded; never dispatch on an uncertain claim."""


@dataclass(frozen=True)
class StorageScope:
    account: str
    workspace: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value)
               for value in (self.account, self.workspace)):
            raise ValueError('fixed account and workspace storage scope required')

    @property
    def key(self): return f'{self.account}/{self.workspace}'


@dataclass(frozen=True)
class BatchRecord:
    fingerprint: str
    status: str
    payload: str | None
    generation: int
    owner: str
    claimed: bool = False


class BatchStorage(Protocol):
    def get(self, fingerprint: str) -> BatchRecord | None: ...
    def requests(self) -> int: ...
    def reserve(self, fingerprint: str, maximum: int, owner: str, *, policy='reuse', retry_failed=False) -> BatchRecord: ...
    def claim(self, fingerprint: str, owner: str) -> bool: ...
    def settle(self, fingerprint: str, owner: str, status: str, payload: str | None = None) -> BatchRecord: ...
    def operation_mark(self) -> int: ...
    def pending_operations(self) -> list[tuple[int,str,str]]: ...
    def abandon_learning(self,fingerprint: str,owner: str) -> BatchRecord: ...
    def page(self, *, after: str | None = None, limit: int = 100) -> tuple[list[BatchRecord], str | None]: ...
    def export(self, *, max_records: int = 10000) -> dict: ...
    def restore(self, snapshot: dict) -> None: ...
    def close(self) -> None: ...


def validate_request(fingerprint, owner):
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint):
        raise ValueError('complete request SHA256 required')
    try:
        parsed = UUID(owner)
        if parsed.version != 4 or str(parsed) != owner: raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('stable operation UUIDv4 required') from None


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('schema') != 'cyclotron-shared-batches/v1':
        raise ValueError('unsupported shared batch snapshot')
    batches, attempts, history = (snapshot.get(name) for name in ('batches','attempts','history'))
    if not all(isinstance(rows,list) for rows in (batches,attempts,history)):
        raise ValueError('complete shared batch snapshot required')
    generations = {}
    for row in batches:
        if not isinstance(row,list) or len(row)!=7: raise ValueError('invalid batch row')
        fingerprint,status,payload,generation,owner,claimed,revision = row
        validate_request(fingerprint,owner)
        if fingerprint in generations or status not in ('pending','complete','failed','interrupted') or type(generation) is not int or generation<1 or type(claimed) is not bool or type(revision) is not int or revision<1:
            raise ValueError('invalid batch state')
        if payload is not None:
            if not isinstance(payload,str): raise ValueError('payload text required')
            json.loads(payload)
        if status=='complete' and payload is None: raise ValueError('complete payload required')
        generations[fingerprint] = generation
    counts, attempt_owners, owners = {}, {}, set()
    for expected,row in enumerate(attempts,1):
        if not isinstance(row,list) or len(row)!=3 or row[0]!=expected: raise ValueError('ordered attempts required')
        validate_request(row[1],row[2])
        if row[2] in owners: raise ValueError('operation owner reused across attempts')
        owners.add(row[2])
        if row[1] not in generations: raise ValueError('attempt without batch')
        counts[row[1]] = counts.get(row[1],0)+1
        attempt_owners[row[1],counts[row[1]]] = row[2]
    if counts != generations: raise ValueError('attempt/generation mismatch')
    if any(attempt_owners[row[0],row[3]]!=row[4] for row in batches):
        raise ValueError('current owner does not match its attempt')
    ids, versions = set(), set()
    for row in history:
        if not isinstance(row,list) or len(row)!=6: raise ValueError('invalid history row')
        validate_request(row[1],row[5])
        if type(row[0]) is not int or row[0]<1 or row[0] in ids or type(row[2]) is not int or not 1<=row[2]<generations.get(row[1],0) or (row[1],row[2]) in versions or row[3] not in ('pending','complete','failed','interrupted'):
            raise ValueError('invalid retained generation')
        if row[4] is not None: json.loads(row[4])
        if row[3]=='complete' and row[4] is None: raise ValueError('history payload required')
        if attempt_owners[row[1],row[2]]!=row[5]: raise ValueError('historical owner mismatch')
        ids.add(row[0]); versions.add((row[1],row[2]))
    if versions!={(fingerprint,generation) for fingerprint,count in generations.items() for generation in range(1,count)}:
        raise ValueError('incomplete generation history')
    return batches,attempts,history


class SQLiteBatchStorage:
    """Keep existing cache tables; add exclusive ownership without erasing data."""
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(path,timeout=30,check_same_thread=False)
        self.lock = RLock()
        self.db.executescript('''CREATE TABLE IF NOT EXISTS batches(key TEXT PRIMARY KEY,status TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,key TEXT);
            CREATE TABLE IF NOT EXISTS batch_history(id INTEGER PRIMARY KEY,key TEXT,generation INTEGER,status TEXT,payload TEXT);''')
        self.db.execute('BEGIN IMMEDIATE')
        for table,columns in (('batches',{'owner':'TEXT','claimed':'INTEGER NOT NULL DEFAULT 1','revision':'INTEGER NOT NULL DEFAULT 1'}),('attempts',{'owner':'TEXT'}),('batch_history',{'owner':'TEXT'})):
            present = {row[1] for row in self.db.execute(f'PRAGMA table_info({table})')}
            for name,kind in columns.items():
                if name not in present: self.db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
        for fingerprint, in self.db.execute('SELECT key FROM batches WHERE owner IS NULL').fetchall():
            def legacy_owner(generation):
                return str(UUID(str(uuid5(NAMESPACE_URL,f'cyclotron-legacy-batch:{fingerprint}:{generation}')),version=4))
            attempts = self.db.execute('SELECT id FROM attempts WHERE key=? ORDER BY id',(fingerprint,)).fetchall()
            for generation,(identifier,) in enumerate(attempts,1):
                self.db.execute('UPDATE attempts SET owner=? WHERE id=? AND owner IS NULL',(legacy_owner(generation),identifier))
                self.db.execute('UPDATE batch_history SET owner=? WHERE key=? AND generation=? AND owner IS NULL',(legacy_owner(generation),fingerprint,generation))
            owner = legacy_owner(max(1,len(attempts)))
            self.db.execute('UPDATE batches SET owner=? WHERE key=?',(owner,fingerprint))
        self.db.commit()

    def _get(self, fingerprint):
        # One SQLite statement/snapshot: a second connection may refresh the
        # request between separate payload and attempt-count reads.
        row = self.db.execute('SELECT status,payload,owner,claimed,(SELECT COUNT(*) FROM attempts WHERE key=batches.key) FROM batches WHERE key=?',(fingerprint,)).fetchone()
        if row is None: return None
        return BatchRecord(fingerprint,row[0],row[1],max(1,row[4]),row[2],bool(row[3]))

    def get(self, fingerprint):
        with self.lock: return self._get(fingerprint)

    def requests(self):
        with self.lock: return self.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]

    def reserve(self, fingerprint, maximum, owner, *, policy='reuse', retry_failed=False):
        validate_request(fingerprint,owner)
        if type(maximum) is not int or maximum<0 or policy not in ('reuse','cache_only','refresh'):
            raise ValueError('valid request ceiling and policy required')
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self._get(fingerprint)
            prior = self.db.execute('SELECT key FROM attempts WHERE owner=? LIMIT 1',(owner,)).fetchone()
            if prior and prior[0]!=fingerprint: raise BatchConflict('operation belongs to a different request')
            if row and row.owner==owner: return row
            if prior:
                old = self.db.execute('SELECT status,payload,generation FROM batch_history WHERE key=? AND owner=? ORDER BY generation DESC LIMIT 1',(fingerprint,owner)).fetchone()
                if old: return BatchRecord(fingerprint,old[0],old[1],old[2],owner,True)
                raise BatchConflict('operation result requires reconciliation')
            if row and row.status=='pending': raise BatchInProgress('shared request in progress; explicit reconciliation required')
            if policy=='cache_only' and (not row or row.status!='complete'): raise CacheMiss('complete batch not cached')
            if row and row.status=='complete' and policy!='refresh': return row
            if row and row.status!='complete' and not retry_failed and not (row.status=='interrupted' and row.payload=='{"learning_abandoned":true}'): raise RuntimeError('explicit retry required for interrupted batch')
            if self.requests()>=maximum: raise RuntimeError('run decision request ceiling reached')
            if row:
                self.db.execute('INSERT INTO batch_history(key,generation,status,payload,owner) VALUES (?,?,?,?,?)',
                    (fingerprint,row.generation,row.status,row.payload,row.owner))
            self.db.execute('INSERT INTO attempts(key,owner) VALUES (?,?)',(fingerprint,owner))
            revision = self.db.execute('SELECT revision FROM batches WHERE key=?',(fingerprint,)).fetchone()
            self.db.execute('INSERT OR REPLACE INTO batches(key,status,payload,owner,claimed,revision) VALUES (?,"pending",NULL,?,0,?)',(fingerprint,owner,revision[0]+1 if revision else 1))
            return self._get(fingerprint)

    def claim(self, fingerprint, owner):
        with self.lock,self.db:
            return self.db.execute('UPDATE batches SET claimed=1,revision=revision+1 WHERE key=? AND owner=? AND status="pending" AND claimed=0',(fingerprint,owner)).rowcount==1

    def settle(self, fingerprint, owner, status, payload=None):
        if status not in ('complete','failed','interrupted') or status=='complete' and payload is None: raise ValueError('valid settlement required')
        if payload is not None: json.loads(payload)
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self._get(fingerprint)
            if row and row.owner==owner and row.status==status and row.payload==payload: return row
            if not row or row.owner!=owner or row.status!='pending': raise BatchConflict('stale shared request owner')
            if status=='complete' and not row.claimed: raise BatchConflict('completion requires dispatch claim')
            self.db.execute('UPDATE batches SET status=?,payload=?,revision=revision+1 WHERE key=?',(status,payload,fingerprint))
            return self._get(fingerprint)

    def operation_mark(self):
        with self.lock: return self.db.execute('SELECT COALESCE(MAX(id),0) FROM attempts').fetchone()[0]

    def pending_operations(self):
        with self.lock:
            return self.db.execute("SELECT attempts.id,batches.key,batches.owner FROM batches JOIN attempts ON attempts.key=batches.key AND attempts.owner=batches.owner WHERE batches.status='pending'").fetchall()

    def abandon_learning(self,fingerprint,owner):
        # Explicit SDK policy exception: preserve charged attempts and ownership
        # history while allowing this stopped pass's own request to be repeated.
        return self.settle(fingerprint,owner,'interrupted','{"learning_abandoned":true}')

    def page(self, *, after=None, limit=100):
        if type(limit) is not int or not 1<=limit<=1000: raise ValueError('bounded page required')
        with self.lock:
            keys = [row[0] for row in self.db.execute('SELECT key FROM batches WHERE key>? ORDER BY key LIMIT ?',(after or '',limit+1))]
            return [self._get(key) for key in keys[:limit]], keys[limit-1] if len(keys)>limit else None

    def export(self, *, max_records=10000):
        """A SQLite snapshot; caller quiesces provider work before migrating."""
        if type(max_records) is not int or max_records<1: raise ValueError('bounded export required')
        with self.lock,self.db:
            self.db.execute('BEGIN')
            total = sum(self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('batches','attempts','batch_history'))
            if total>max_records: raise ValueError('shared batch export budget exceeded')
            batches = [[row[0],row[1],row[2],self._get(row[0]).generation,row[3],bool(row[4]),row[5]] for row in self.db.execute('SELECT key,status,payload,owner,claimed,revision FROM batches ORDER BY key')]
            attempts = [list(row) for row in self.db.execute('SELECT id,key,owner FROM attempts ORDER BY id')]
            history = [list(row) for row in self.db.execute('SELECT id,key,generation,status,payload,owner FROM batch_history ORDER BY id')]
            return {'schema':'cyclotron-shared-batches/v1','batches':batches,'attempts':attempts,'history':history}

    def restore(self, snapshot):
        batches,attempts,history = validate_snapshot(snapshot)
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.requests() or self.db.execute('SELECT 1 FROM batches').fetchone(): raise BatchConflict('restore requires empty destination')
            self.db.executemany('INSERT INTO batches(key,status,payload,owner,claimed,revision) VALUES (?,?,?,?,?,?)',[(r[0],r[1],r[2],r[4],int(r[5]),r[6]) for r in batches])
            self.db.executemany('INSERT INTO attempts(id,key,owner) VALUES (?,?,?)',attempts)
            self.db.executemany('INSERT INTO batch_history(id,key,generation,status,payload,owner) VALUES (?,?,?,?,?,?)',history)

    def close(self): self.db.close()
