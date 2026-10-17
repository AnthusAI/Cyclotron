"""Learning-domain persistence; native algorithms remain in their consumers.

Fixed artifact kinds describe learning operations, not caller-supplied SQL tables.
No cloud authority, distributed owner or hosted backend is selected here.
"""

import json
from typing import Literal, Protocol

ArtifactKind = Literal["classifier", "questions", "examples", "rubric", "controls"]
ARTIFACTS = {
    "classifier": ("classifier_training", "id TEXT PRIMARY KEY, status TEXT, payload TEXT"),
    "questions": ("question_measurements", "id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT"),
    "examples": ("example_measurements", "id TEXT PRIMARY KEY,status TEXT,payload TEXT"),
    "rubric": ("optimization_stages", "id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT"),
    "controls": ("control_cycles", "id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT"),
}


class LearningStorage(Protocol):
    def ensure(self, kind: ArtifactKind) -> None: ...
    def artifact(self, kind: ArtifactKind, key: str) -> tuple | None: ...
    def completed(self, kind: ArtifactKind) -> list[tuple[str, str]]: ...
    def count(self, kind: ArtifactKind) -> int: ...
    def begin(self, kind: ArtifactKind, key: str, payload: str) -> None: ...
    def update_payload(self, kind: ArtifactKind, key: str, payload: str) -> None: ...
    def complete(
        self, kind: ArtifactKind, key: str, payload: str, *, aliases: tuple[str, ...] = (), clear_pending_key: str | None = None
    ) -> None: ...


class FeatureStorage(Protocol):
    def ensure(self) -> None: ...
    def payloads(self) -> tuple[str, ...]: ...
    def save(self, key: str, payload: str) -> None: ...


class ControlStorage(Protocol):
    def ensure(self) -> None: ...
    def payloads(self) -> tuple[str, ...]: ...
    def payload(self, key: str) -> str | None: ...
    def save(self, key: str, payload: str) -> None: ...


def artifact_table(kind):
    if not isinstance(kind, str) or kind not in ARTIFACTS:
        raise ValueError("unknown learning artifact kind")
    return ARTIFACTS[kind][0]


class SQLiteLearningStorage:
    def __init__(self, db):
        self.db = db

    def ensure(self, kind):
        table = artifact_table(kind)
        self.db.execute(f"CREATE TABLE IF NOT EXISTS {table} ({ARTIFACTS[kind][1]})")

    def _exists(self, kind):
        return bool(
            self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (artifact_table(kind),)).fetchone()
        )

    def artifact(self, kind, key):
        return self.db.execute(f"SELECT status,payload FROM {artifact_table(kind)} WHERE id=?", (key,)).fetchone()

    def completed(self, kind):
        if not self._exists(kind):
            return []
        return self.db.execute(
            f"SELECT id,payload FROM {artifact_table(kind)} WHERE status='complete' ORDER BY rowid DESC"
        ).fetchall()

    def count(self, kind):
        return self.db.execute(f"SELECT COUNT(*) FROM {artifact_table(kind)}").fetchone()[0]

    def begin(self, kind, key, payload):
        with self.db:
            self.db.execute(f"INSERT OR REPLACE INTO {artifact_table(kind)} VALUES (?, 'pending', ?)", (key, payload))

    def update_payload(self, kind, key, payload):
        with self.db:
            self.db.execute(f"UPDATE {artifact_table(kind)} SET payload=? WHERE id=?", (payload, key))

    def complete(self, kind, key, payload, *, aliases=(), clear_pending_key=None):
        table = artifact_table(kind)
        with self.db:
            if clear_pending_key:
                old = json.loads(self.artifact(kind, clear_pending_key)[1])
                old["pending_evaluation"] = False
                self.db.execute(
                    f"UPDATE {table} SET payload=? WHERE id=?",
                    (
                        json.dumps(old, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False),
                        clear_pending_key,
                    ),
                )
            self.db.execute(f"UPDATE {table} SET status='complete',payload=? WHERE id=?", (payload, key))
            for alias in aliases:
                self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, 'complete', ?)", (alias, payload))


class SQLiteFeatureStorage:
    def __init__(self, db):
        self.db = db

    def ensure(self):
        self.db.execute("CREATE TABLE IF NOT EXISTS feature_bank (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def payloads(self):
        self.ensure()
        return tuple(row[0] for row in self.db.execute("SELECT payload FROM feature_bank ORDER BY id"))

    def save(self, key, payload):
        self.ensure()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO feature_bank VALUES (?,?)", (key, payload))


class SQLiteControlStorage:
    def __init__(self, db):
        self.db = db

    def ensure(self):
        self.db.execute("CREATE TABLE IF NOT EXISTS control_ideas (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def payloads(self):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='control_ideas'").fetchone():
            return ()
        return tuple(row[0] for row in self.db.execute("SELECT payload FROM control_ideas ORDER BY id"))

    def payload(self, key):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='control_ideas'").fetchone():
            return None
        row = self.db.execute("SELECT payload FROM control_ideas WHERE id=?", (key,)).fetchone()
        return row[0] if row else None

    def save(self, key, payload):
        self.ensure()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO control_ideas VALUES (?,?)", (key, payload))
