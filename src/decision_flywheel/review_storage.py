"""Native reviewer-source records; human eligibility rules stay shared."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .storage_errors import refusing_duplicate_identity


class ReviewStorage(Protocol):
    def bind_study(self, values: dict[str,str]) -> None: ...
    def transaction(self): ...
    def import_articles(self, records: list[tuple]) -> int: ...
    def articles(self) -> list[dict]: ...
    def article(self, identifier: str) -> dict | None: ...
    def actions(self, article_id: str | None=None) -> list[dict]: ...
    def presentations(self, article_id: str | None=None) -> list[dict]: ...
    def presentation(self, identifier: str) -> dict | None: ...
    def append_presentation(self, row: tuple) -> None: ...
    def append_action(self, row: tuple) -> None: ...
    def close(self) -> None: ...
    def export_storage(self,*,max_records=10000,max_bytes=64000000) -> dict: ...
    def restore_storage(self,snapshot: dict,*,max_records=10000,max_bytes=64000000) -> None: ...


class SQLiteReviewStorage:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=30)
        self.db.row_factory=sqlite3.Row
        self._create_schema()

    def close(self): self.db.close()

    @contextmanager
    def transaction(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            yield self

    @contextmanager
    def _joining_transaction(self):
        if self.db.in_transaction: yield self
        else:
            with self.transaction(): yield self

    def bind_study(self,values):
        with self._joining_transaction():
            for key,value in values.items():
                row=self.db.execute('SELECT value FROM study_metadata WHERE key=?',(key,)).fetchone()
                if row is not None and row[0]!=value:
                    raise ValueError(f'review database already belongs to a study with different {key}')
                self.db.execute('INSERT OR IGNORE INTO study_metadata VALUES (?,?)',(key,value))

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS study_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                abstract TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                categories_json TEXT NOT NULL,
                authors TEXT NOT NULL DEFAULT 'Not provided',
                journal_ref TEXT,
                assignment TEXT NOT NULL CHECK (assignment IN ('train', 'rolling_audit', 'final_audit'))
            );
            CREATE TABLE IF NOT EXISTS presentations (
                id TEXT PRIMARY KEY,
                article_id TEXT NOT NULL REFERENCES articles(id),
                predicted_label TEXT NOT NULL CHECK (predicted_label IN ('include', 'exclude')),
                confidence REAL NOT NULL,
                predictor_kind TEXT NOT NULL,
                predictor_fingerprint TEXT NOT NULL,
                training_label_count INTEGER NOT NULL,
                shown_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_events (
                id TEXT PRIMARY KEY,
                article_id TEXT NOT NULL REFERENCES articles(id),
                action TEXT NOT NULL CHECK (action IN ('vote', 'skip', 'undo')),
                label TEXT CHECK (label IN ('include', 'exclude')),
                comment TEXT,
                created_at TEXT NOT NULL,
                undoes_event_id TEXT REFERENCES review_events(id),
                presentation_id TEXT REFERENCES presentations(id)
            );
            CREATE INDEX IF NOT EXISTS review_events_article_created
                ON review_events(article_id, created_at);
            """
        )
        article_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(articles)")}
        if "authors" not in article_columns:
            self.db.execute("ALTER TABLE articles ADD COLUMN authors TEXT NOT NULL DEFAULT 'Not provided'")
        if "journal_ref" not in article_columns:
            self.db.execute("ALTER TABLE articles ADD COLUMN journal_ref TEXT")
        event_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(review_events)")}
        if "presentation_id" not in event_columns:
            self.db.execute("ALTER TABLE review_events ADD COLUMN presentation_id TEXT REFERENCES presentations(id)")
        self.db.commit()

    def import_articles(self,records):
        imported=0
        with self._joining_transaction():
            for identifier,title,abstract,submitted,categories,authors,journal,assignment in records:
                existing=self.db.execute('SELECT title,abstract,submitted_at,categories_json,authors,journal_ref FROM articles WHERE id=?',(identifier,)).fetchone()
                supplied=(title,abstract,submitted,categories,authors,journal)
                if existing is not None:
                    current=tuple(existing)
                    if current==supplied: continue
                    matches=current[:4]==supplied[:4]
                    legacy=current[4:]==('Not provided',None)
                    reviewed=self.db.execute('SELECT 1 FROM review_events WHERE article_id=? LIMIT 1',(identifier,)).fetchone() is not None
                    if matches and legacy and not reviewed:
                        self.db.execute('UPDATE articles SET authors=?,journal_ref=? WHERE id=?',(authors,journal,identifier))
                        continue
                    if matches: continue
                    raise ValueError('an imported article ID already has different content')
                self.db.execute('INSERT INTO articles(id,title,abstract,submitted_at,categories_json,authors,journal_ref,assignment) VALUES (?,?,?,?,?,?,?,?)',
                    (identifier,*supplied,assignment))
                imported+=1
        return imported

    def articles(self): return [dict(row) for row in self.db.execute('SELECT * FROM articles ORDER BY id')]
    def article(self,identifier):
        row=self.db.execute('SELECT * FROM articles WHERE id=?',(identifier,)).fetchone()
        return dict(row) if row else None
    def actions(self,article_id=None):
        sql,args='SELECT * FROM review_events',[]
        if article_id is not None: sql+=' WHERE article_id=?';args.append(article_id)
        return [dict(row) for row in self.db.execute(sql+' ORDER BY created_at,rowid',args)]
    def presentations(self,article_id=None):
        sql,args='SELECT * FROM presentations',[]
        if article_id is not None: sql+=' WHERE article_id=?';args.append(article_id)
        return [dict(row) for row in self.db.execute(sql+' ORDER BY shown_at,rowid',args)]
    def presentation(self,identifier):
        row=self.db.execute('SELECT * FROM presentations WHERE id=?',(identifier,)).fetchone()
        return dict(row) if row else None
    def append_presentation(self,row):
        with refusing_duplicate_identity('duplicate presentation identity'): self.db.execute('INSERT INTO presentations(id,article_id,predicted_label,confidence,predictor_kind,predictor_fingerprint,training_label_count,shown_at) VALUES (?,?,?,?,?,?,?,?)',row)
    def append_action(self,row):
        with refusing_duplicate_identity('duplicate review action identity'): self.db.execute('INSERT INTO review_events(id,article_id,action,label,comment,created_at,undoes_event_id,presentation_id) VALUES (?,?,?,?,?,?,?,?)',row)

    def export_storage(self,*,max_records=10000,max_bytes=64000000):
        from .domain_snapshots import review_codec
        return review_codec().export(self.db,max_records=max_records,max_bytes=max_bytes)

    def restore_storage(self,snapshot,*,max_records=10000,max_bytes=64000000):
        from .domain_snapshots import review_codec
        return review_codec().restore(self.db,snapshot,max_records=max_records,max_bytes=max_bytes)
