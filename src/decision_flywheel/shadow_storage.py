"""Native forward-evaluation records; spending/selection policy stays shared."""
from typing import Protocol


class ShadowStorage(Protocol):
    def ensure(self) -> None: ...
    def calls(self) -> int: ...
    def active(self) -> list[tuple]: ...
    def known(self) -> set[str]: ...
    def statuses(self) -> dict[str,str]: ...
    def set_status(self, idea_id: str, status: str) -> None: ...
    def join(self, idea_id: str, control: str, seq: int) -> None: ...
    def scored(self, idea_id: str, item_id: str, seq: int) -> None: ...
    def incumbent(self, incumbent_id: str, rubric: str, seq: int) -> None: ...
    def incumbent_ids(self) -> set[str]: ...
    def looks_taken(self, idea_id: str, incumbent: str) -> int: ...
    def look(self, row: tuple) -> None: ...


class SQLiteShadowStorage:
    def __init__(self,db): self.db=db

    def ensure(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS shadow_set (idea_id TEXT PRIMARY KEY, control TEXT NOT NULL,
                joined_seq INTEGER NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shadow_scored (idea_id TEXT, item_id TEXT, seq INTEGER,
                PRIMARY KEY (idea_id, item_id));
            CREATE TABLE IF NOT EXISTS shadow_looks (idea_id TEXT, incumbent_id TEXT, look INTEGER, shared INTEGER,
                gained INTEGER, lost INTEGER, p_value REAL, cleared INTEGER, seq INTEGER,
                PRIMARY KEY (idea_id, incumbent_id, look));
            CREATE TABLE IF NOT EXISTS shadow_incumbents (incumbent_id TEXT PRIMARY KEY, rubric TEXT, first_seq INTEGER);
        ''')

    def calls(self): return self.db.execute('SELECT COUNT(*) FROM shadow_scored').fetchone()[0]
    def active(self): return self.db.execute("SELECT idea_id,control,joined_seq FROM shadow_set WHERE status='active' ORDER BY joined_seq,idea_id").fetchall()
    def known(self): return {row[0] for row in self.db.execute('SELECT idea_id FROM shadow_set')}
    def statuses(self): return dict(self.db.execute('SELECT idea_id,status FROM shadow_set'))

    def set_status(self,idea_id,status):
        with self.db: self.db.execute('UPDATE shadow_set SET status=? WHERE idea_id=?',(status,idea_id))

    def join(self,idea_id,control,seq):
        with self.db: self.db.execute("INSERT INTO shadow_set VALUES (?,?,?,'active')",(idea_id,control,seq))

    def scored(self,idea_id,item_id,seq):
        with self.db: self.db.execute('INSERT OR IGNORE INTO shadow_scored VALUES (?,?,?)',(idea_id,item_id,seq))

    def incumbent(self,incumbent_id,rubric,seq):
        with self.db: self.db.execute('INSERT OR IGNORE INTO shadow_incumbents VALUES (?,?,?)',(incumbent_id,rubric,seq))

    def incumbent_ids(self):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_incumbents'").fetchone(): return set()
        return {row[0] for row in self.db.execute('SELECT incumbent_id FROM shadow_incumbents')}

    def looks_taken(self,idea_id,incumbent):
        return self.db.execute('SELECT COUNT(*) FROM shadow_looks WHERE idea_id=? AND incumbent_id=?',(idea_id,incumbent)).fetchone()[0]

    def look(self,row):
        with self.db: self.db.execute('INSERT INTO shadow_looks VALUES (?,?,?,?,?,?,?,?,?)',row)
