"""Local, auditable storage for a human article-review session.

The reviewer records what a person saw and did.  It deliberately has no model,
network, or optimization dependency: predictions and later flywheel versions
can be joined to these immutable events without changing human feedback.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterable
import uuid


_ASSIGNMENTS = ("train", "rolling_audit", "final_audit")
_ACTIONS = ("vote", "skip", "undo")


@dataclass(frozen=True)
class Article:
    """An article shown to a reviewer, with only the title-and-abstract payload."""

    id: str
    title: str
    abstract: str
    submitted_at: str
    categories: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("article id must be a non-empty string")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("article title must be a non-empty string")
        if not isinstance(self.abstract, str) or not self.abstract.strip():
            raise ValueError("article abstract must be a non-empty string")
        if not isinstance(self.submitted_at, str) or not self.submitted_at.strip():
            raise ValueError("article submitted_at must be a non-empty string")
        if not self.categories or any(not isinstance(value, str) or not value.strip() for value in self.categories):
            raise ValueError("article categories must contain non-empty strings")


@dataclass(frozen=True)
class ReviewEvent:
    """An append-only human action; undo is a new event rather than a deletion."""

    id: str
    article_id: str
    action: str
    label: str | None
    comment: str | None
    created_at: str
    undoes_event_id: str | None


@dataclass(frozen=True)
class LearningLabel:
    """A current, explicitly eligible human vote for a later learning phase."""

    article_id: str
    label: str
    assignment: str


class ReviewStore:
    """SQLite event store with deterministic train/audit partition assignment."""

    def __init__(self, path: str | Path, *, study_seed: str, rolling_audit_rate: float = .2,
                 final_audit_rate: float = .1):
        if not isinstance(study_seed, str) or not study_seed:
            raise ValueError("study_seed must be a non-empty string")
        if any(isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1
               for rate in (rolling_audit_rate, final_audit_rate)):
            raise ValueError("audit rates must be probabilities")
        if rolling_audit_rate + final_audit_rate > 1:
            raise ValueError("combined audit rates cannot exceed one")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()
        self._set_or_check_study("study_seed", study_seed)
        self._set_or_check_study("rolling_audit_rate", repr(float(rolling_audit_rate)))
        self._set_or_check_study("final_audit_rate", repr(float(final_audit_rate)))
        self.study_seed = study_seed
        self.rolling_audit_rate = float(rolling_audit_rate)
        self.final_audit_rate = float(final_audit_rate)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ReviewStore":
        return self

    def __exit__(self, *_unused: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
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
                assignment TEXT NOT NULL CHECK (assignment IN ('train', 'rolling_audit', 'final_audit'))
            );
            CREATE TABLE IF NOT EXISTS review_events (
                id TEXT PRIMARY KEY,
                article_id TEXT NOT NULL REFERENCES articles(id),
                action TEXT NOT NULL CHECK (action IN ('vote', 'skip', 'undo')),
                label TEXT CHECK (label IN ('include', 'exclude')),
                comment TEXT,
                created_at TEXT NOT NULL,
                undoes_event_id TEXT REFERENCES review_events(id)
            );
            CREATE INDEX IF NOT EXISTS review_events_article_created
                ON review_events(article_id, created_at);
            """
        )
        self._connection.commit()

    def _set_or_check_study(self, key: str, value: str) -> None:
        row = self._connection.execute("SELECT value FROM study_metadata WHERE key = ?", (key,)).fetchone()
        if row is None:
            self._connection.execute("INSERT INTO study_metadata(key, value) VALUES (?, ?)", (key, value))
            self._connection.commit()
        elif row["value"] != value:
            raise ValueError(f"review database already belongs to a study with different {key}")

    def _assignment(self, article_id: str) -> str:
        digest = hashlib.sha256(f"{self.study_seed}:{article_id}".encode("utf-8")).digest()
        fraction = int.from_bytes(digest[:8], "big") / 2**64
        if fraction < self.final_audit_rate:
            return "final_audit"
        if fraction < self.final_audit_rate + self.rolling_audit_rate:
            return "rolling_audit"
        return "train"

    def import_articles(self, articles: Iterable[Article]) -> int:
        """Add immutable source records; duplicate IDs must have identical content."""
        imported = 0
        with self._connection:
            for article in articles:
                if not isinstance(article, Article):
                    raise ValueError("articles must be Article instances")
                existing = self._connection.execute(
                    "SELECT title, abstract, submitted_at, categories_json FROM articles WHERE id = ?", (article.id,)
                ).fetchone()
                categories = json.dumps(article.categories, ensure_ascii=False, separators=(",", ":"))
                if existing is not None:
                    if tuple(existing) != (article.title, article.abstract, article.submitted_at, categories):
                        raise ValueError("an imported article ID already has different content")
                    continue
                self._connection.execute(
                    "INSERT INTO articles(id, title, abstract, submitted_at, categories_json, assignment) VALUES (?, ?, ?, ?, ?, ?)",
                    (article.id, article.title, article.abstract, article.submitted_at, categories, self._assignment(article.id)),
                )
                imported += 1
        return imported

    def articles(self) -> tuple[Article, ...]:
        rows = self._connection.execute(
            "SELECT id, title, abstract, submitted_at, categories_json FROM articles ORDER BY id"
        ).fetchall()
        return tuple(self._article(row) for row in rows)

    @staticmethod
    def _article(row: sqlite3.Row) -> Article:
        return Article(row["id"], row["title"], row["abstract"], row["submitted_at"],
                       tuple(json.loads(row["categories_json"])))

    def assignment_for(self, article_id: str) -> str:
        row = self._connection.execute("SELECT assignment FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row is None:
            raise KeyError(article_id)
        return str(row["assignment"])

    def _active_actions(self) -> dict[str, ReviewEvent]:
        rows = self._connection.execute(
            "SELECT id, article_id, action, label, comment, created_at, undoes_event_id FROM review_events ORDER BY created_at, rowid"
        ).fetchall()
        undone = {row["undoes_event_id"] for row in rows if row["action"] == "undo"}
        active: dict[str, ReviewEvent] = {}
        for row in rows:
            if row["action"] in ("vote", "skip") and row["id"] not in undone:
                active[row["article_id"]] = self._event(row)
        return active

    def next_unreviewed(self) -> Article | None:
        active = self._active_actions()
        rows = self._connection.execute(
            "SELECT id, title, abstract, submitted_at, categories_json FROM articles ORDER BY id"
        ).fetchall()
        for row in rows:
            if row["id"] not in active:
                return self._article(row)
        return None

    def record_vote(self, article_id: str, label: str, *, comment: str | None = None) -> ReviewEvent:
        if label not in ("include", "exclude"):
            raise ValueError("a human vote must be include or exclude")
        if comment is not None and (not isinstance(comment, str) or not comment.strip()):
            raise ValueError("a comment must be omitted or non-empty")
        return self._record_action(article_id, "vote", label, comment)

    def record_skip(self, article_id: str) -> ReviewEvent:
        return self._record_action(article_id, "skip", None, None)

    def _record_action(self, article_id: str, action: str, label: str | None, comment: str | None) -> ReviewEvent:
        if action not in ("vote", "skip"):
            raise ValueError("only a vote or skip may be recorded")
        if self._connection.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone() is None:
            raise KeyError(article_id)
        if article_id in self._active_actions():
            raise ValueError("undo the current review action before recording another one")
        event = ReviewEvent(str(uuid.uuid4()), article_id, action, label, comment,
                            datetime.now(timezone.utc).isoformat(), None)
        with self._connection:
            self._connection.execute(
                "INSERT INTO review_events(id, article_id, action, label, comment, created_at, undoes_event_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(event.__dict__.values()),
            )
        return event

    def undo_last_vote(self) -> Article | None:
        active = self._active_actions()
        if not active:
            return None
        event = max(active.values(), key=lambda item: item.created_at)
        undo = ReviewEvent(str(uuid.uuid4()), event.article_id, "undo", None, None,
                           datetime.now(timezone.utc).isoformat(), event.id)
        with self._connection:
            self._connection.execute(
                "INSERT INTO review_events(id, article_id, action, label, comment, created_at, undoes_event_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(undo.__dict__.values()),
            )
        return self.article(event.article_id)

    def article(self, article_id: str) -> Article:
        row = self._connection.execute(
            "SELECT id, title, abstract, submitted_at, categories_json FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if row is None:
            raise KeyError(article_id)
        return self._article(row)

    def events_for(self, article_id: str) -> tuple[ReviewEvent, ...]:
        rows = self._connection.execute(
            "SELECT id, article_id, action, label, comment, created_at, undoes_event_id FROM review_events WHERE article_id = ? ORDER BY created_at, rowid",
            (article_id,),
        ).fetchall()
        return tuple(self._event(row) for row in rows)

    @staticmethod
    def _event(row: sqlite3.Row) -> ReviewEvent:
        return ReviewEvent(row["id"], row["article_id"], row["action"], row["label"], row["comment"],
                           row["created_at"], row["undoes_event_id"])

    def current_label(self, article_id: str) -> str | None:
        action = self._active_actions().get(article_id)
        return action.label if action is not None and action.action == "vote" else None

    def learning_labels(self) -> tuple[LearningLabel, ...]:
        """Return only train-assigned labels; both audit partitions remain sealed."""
        labels = []
        for article_id, event in self._active_actions().items():
            if event.action == "vote" and self.assignment_for(article_id) == "train":
                labels.append(LearningLabel(article_id, event.label, "train"))
        return tuple(sorted(labels, key=lambda item: item.article_id))

    def summary(self) -> dict[str, int]:
        active = self._active_actions()
        counts = {"articles": len(self.articles()), "include": 0, "exclude": 0, "skip": 0,
                  "unreviewed": 0, "train_labels": len(self.learning_labels())}
        for article in self.articles():
            event = active.get(article.id)
            if event is None:
                counts["unreviewed"] += 1
            elif event.action == "skip":
                counts["skip"] += 1
            else:
                counts[event.label] += 1
        return counts
