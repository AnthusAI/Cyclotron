"""Pass-owned pending artifacts; callers retain the policy for ownership intervals.

Ordinals are opaque native operation marks, never a cloud lease. The adapter
only removes an unchanged pending artifact with its original key and ordinal.
"""
from typing import Protocol

TABLES = ("runtime_rounds", "optimization_stages", "classifier_training", "question_measurements",
          "example_measurements", "control_cycles", "runtime_answers")

class PendingStorage(Protocol):
    def marks(self) -> dict[str,int]: ...
    def pending(self) -> list[tuple[str,int,str]]: ...
    def discard(self,kind: str,ordinal: int,key: str) -> bool: ...

class SQLitePendingStorage:
    def __init__(self,db): self.db=db
    def _tables(self):
        return {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")} & set(TABLES)
    def marks(self):
        return {table:self.db.execute(f'SELECT COALESCE(MAX(rowid),0) FROM {table}').fetchone()[0]
                for table in self._tables()}
    def pending(self):
        return [(table,row[0],row[1]) for table in TABLES if table in self._tables()
                for row in self.db.execute(f"SELECT rowid,* FROM {table} WHERE status='pending'").fetchall()]
    def discard(self,kind,ordinal,key):
        if kind not in TABLES: raise ValueError('unknown pending artifact')
        column='key' if kind in ('runtime_rounds','runtime_answers') else 'id'
        with self.db:
            return bool(self.db.execute(f"DELETE FROM {kind} WHERE rowid=? AND {column}=? AND status='pending'",(ordinal,key)).rowcount)
