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
    authors: str = "Not provided"
    journal_ref: str | None = None

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
        if not isinstance(self.authors, str) or not self.authors.strip():
            raise ValueError("article authors must be a non-empty string")
        if self.journal_ref is not None and (not isinstance(self.journal_ref, str) or not self.journal_ref.strip()):
            raise ValueError("article journal_ref must be omitted or a non-empty string")


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
    presentation_id: str | None


@dataclass(frozen=True)
class Presentation:
    """The prediction displayed before a particular human review action."""

    id: str
    article_id: str
    predicted_label: str
    confidence: float
    predictor_kind: str
    predictor_fingerprint: str
    training_label_count: int
    shown_at: str


@dataclass(frozen=True)
class PredictionMetrics:
    """Online agreement for predictions displayed before a human vote."""

    scored_votes: int
    correct_votes: int
    accuracy: float | None
    model_refreshes: int
    latest_training_label_count: int


@dataclass(frozen=True)
class LearningLabel:
    """A current, explicitly eligible human vote for a later learning phase."""

    article_id: str
    label: str
    assignment: str


@dataclass(frozen=True)
class LearningFeedback:
    """An eligible human label plus the optional explanation attached to that vote."""

    article_id: str
    label: str
    comment: str | None


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
        article_columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(articles)")}
        if "authors" not in article_columns:
            self._connection.execute("ALTER TABLE articles ADD COLUMN authors TEXT NOT NULL DEFAULT 'Not provided'")
        if "journal_ref" not in article_columns:
            self._connection.execute("ALTER TABLE articles ADD COLUMN journal_ref TEXT")
        event_columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(review_events)")}
        if "presentation_id" not in event_columns:
            self._connection.execute("ALTER TABLE review_events ADD COLUMN presentation_id TEXT REFERENCES presentations(id)")
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
                    "SELECT title, abstract, submitted_at, categories_json, authors, journal_ref FROM articles WHERE id = ?", (article.id,)
                ).fetchone()
                categories = json.dumps(article.categories, ensure_ascii=False, separators=(",", ":"))
                if existing is not None:
                    current = tuple(existing)
                    supplied = (article.title, article.abstract, article.submitted_at, categories,
                                article.authors, article.journal_ref)
                    if current == supplied:
                        continue
                    source_content_matches = current[:4] == supplied[:4]
                    legacy_metadata = current[4:] == ("Not provided", None)
                    reviewed = self._connection.execute(
                        "SELECT 1 FROM review_events WHERE article_id = ? LIMIT 1", (article.id,)
                    ).fetchone() is not None
                    if source_content_matches and legacy_metadata and not reviewed:
                        self._connection.execute(
                            "UPDATE articles SET authors = ?, journal_ref = ? WHERE id = ?",
                            (article.authors, article.journal_ref, article.id),
                        )
                        continue
                    if source_content_matches:
                        # A human may already have reviewed the older record.
                        # Preserve exactly the metadata they saw while accepting
                        # the overlapping source batch.
                        continue
                    raise ValueError("an imported article ID already has different content")
                self._connection.execute(
                    "INSERT INTO articles(id, title, abstract, submitted_at, categories_json, authors, journal_ref, assignment) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (article.id, article.title, article.abstract, article.submitted_at, categories, article.authors,
                     article.journal_ref, self._assignment(article.id)),
                )
                imported += 1
        return imported

    def articles(self) -> tuple[Article, ...]:
        rows = self._connection.execute(
            "SELECT id, title, abstract, submitted_at, categories_json, authors, journal_ref FROM articles ORDER BY id"
        ).fetchall()
        return tuple(self._article(row) for row in rows)

    @staticmethod
    def _article(row: sqlite3.Row) -> Article:
        return Article(row["id"], row["title"], row["abstract"], row["submitted_at"],
                       tuple(json.loads(row["categories_json"])), row["authors"], row["journal_ref"])

    def assignment_for(self, article_id: str) -> str:
        row = self._connection.execute("SELECT assignment FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row is None:
            raise KeyError(article_id)
        return str(row["assignment"])

    def _active_actions(self) -> dict[str, ReviewEvent]:
        rows = self._connection.execute(
            "SELECT id, article_id, action, label, comment, created_at, undoes_event_id, presentation_id FROM review_events ORDER BY created_at, rowid"
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
            "SELECT id, title, abstract, submitted_at, categories_json, authors, journal_ref FROM articles ORDER BY id"
        ).fetchall()
        for row in rows:
            if row["id"] not in active:
                return self._article(row)
        return None

    def record_prediction(self, article_id: str, predicted_label: str, confidence: float, predictor_kind: str,
                          predictor_fingerprint: str, training_label_count: int) -> Presentation:
        """Save the exact prediction visible to a reviewer before their choice."""
        if self._connection.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone() is None:
            raise KeyError(article_id)
        if predicted_label not in ("include", "exclude"):
            raise ValueError("a prediction must be include or exclude")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("prediction confidence must be a probability")
        if not isinstance(predictor_kind, str) or not predictor_kind.strip():
            raise ValueError("predictor kind must be a non-empty string")
        if (not isinstance(predictor_fingerprint, str) or len(predictor_fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in predictor_fingerprint.lower())):
            raise ValueError("predictor fingerprint must be a SHA-256 hex digest")
        if isinstance(training_label_count, bool) or not isinstance(training_label_count, int) or training_label_count < 0:
            raise ValueError("training label count must be a non-negative integer")
        shown = Presentation(str(uuid.uuid4()), article_id, predicted_label, float(confidence), predictor_kind,
                             predictor_fingerprint, training_label_count, datetime.now(timezone.utc).isoformat())
        with self._connection:
            self._connection.execute(
                "INSERT INTO presentations(id, article_id, predicted_label, confidence, predictor_kind, predictor_fingerprint, training_label_count, shown_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(shown.__dict__.values()),
            )
        return shown

    def presentations_for(self, article_id: str) -> tuple[Presentation, ...]:
        rows = self._connection.execute(
            "SELECT id, article_id, predicted_label, confidence, predictor_kind, predictor_fingerprint, training_label_count, shown_at FROM presentations WHERE article_id = ? ORDER BY shown_at, rowid",
            (article_id,),
        ).fetchall()
        return tuple(Presentation(*tuple(row)) for row in rows)

    def record_vote(self, article_id: str, label: str, *, comment: str | None = None,
                    presentation_id: str | None = None) -> ReviewEvent:
        if label not in ("include", "exclude"):
            raise ValueError("a human vote must be include or exclude")
        if comment is not None and (not isinstance(comment, str) or not comment.strip()):
            raise ValueError("a comment must be omitted or non-empty")
        if presentation_id is not None:
            presentation = self._connection.execute(
                "SELECT article_id FROM presentations WHERE id = ?", (presentation_id,)
            ).fetchone()
            if presentation is None or presentation["article_id"] != article_id:
                raise ValueError("a linked prediction must belong to the reviewed article")
        return self._record_action(article_id, "vote", label, comment, presentation_id)

    def record_skip(self, article_id: str) -> ReviewEvent:
        return self._record_action(article_id, "skip", None, None, None)

    def _record_action(self, article_id: str, action: str, label: str | None, comment: str | None,
                       presentation_id: str | None) -> ReviewEvent:
        if action not in ("vote", "skip"):
            raise ValueError("only a vote or skip may be recorded")
        if self._connection.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone() is None:
            raise KeyError(article_id)
        if article_id in self._active_actions():
            raise ValueError("undo the current review action before recording another one")
        event = ReviewEvent(str(uuid.uuid4()), article_id, action, label, comment,
                            datetime.now(timezone.utc).isoformat(), None, presentation_id)
        with self._connection:
            self._connection.execute(
                "INSERT INTO review_events(id, article_id, action, label, comment, created_at, undoes_event_id, presentation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(event.__dict__.values()),
            )
        return event

    def undo_last_vote(self) -> Article | None:
        active = self._active_actions()
        if not active:
            return None
        event = max(active.values(), key=lambda item: item.created_at)
        undo = ReviewEvent(str(uuid.uuid4()), event.article_id, "undo", None, None,
                           datetime.now(timezone.utc).isoformat(), event.id, None)
        with self._connection:
            self._connection.execute(
                "INSERT INTO review_events(id, article_id, action, label, comment, created_at, undoes_event_id, presentation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(undo.__dict__.values()),
            )
        return self.article(event.article_id)

    def article(self, article_id: str) -> Article:
        row = self._connection.execute(
            "SELECT id, title, abstract, submitted_at, categories_json, authors, journal_ref FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if row is None:
            raise KeyError(article_id)
        return self._article(row)

    def events_for(self, article_id: str) -> tuple[ReviewEvent, ...]:
        rows = self._connection.execute(
            "SELECT id, article_id, action, label, comment, created_at, undoes_event_id, presentation_id FROM review_events WHERE article_id = ? ORDER BY created_at, rowid",
            (article_id,),
        ).fetchall()
        return tuple(self._event(row) for row in rows)

    @staticmethod
    def _event(row: sqlite3.Row) -> ReviewEvent:
        return ReviewEvent(row["id"], row["article_id"], row["action"], row["label"], row["comment"],
                           row["created_at"], row["undoes_event_id"], row["presentation_id"])

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

    def learning_feedback(self) -> tuple[LearningFeedback, ...]:
        """Return the only reviewed records a live context optimizer may consume."""
        labels = []
        for article_id, event in self._active_actions().items():
            if event.action == "vote" and self.assignment_for(article_id) == "train":
                labels.append(LearningFeedback(article_id, event.label, event.comment))
        return tuple(sorted(labels, key=lambda item: item.article_id))

    def prediction_metrics(self) -> PredictionMetrics:
        """Measure pre-vote prediction agreement without treating skips as labels."""
        active = self._active_actions()
        scored = 0
        correct = 0
        for event in active.values():
            if event.action != "vote" or event.presentation_id is None:
                continue
            row = self._connection.execute(
                "SELECT predicted_label FROM presentations WHERE id = ?", (event.presentation_id,)
            ).fetchone()
            if row is None:  # Defensive: old or externally edited local databases cannot count as a score.
                continue
            scored += 1
            correct += row["predicted_label"] == event.label
        rows = self._connection.execute(
            "SELECT predictor_kind, predictor_fingerprint, training_label_count FROM presentations ORDER BY shown_at, rowid"
        ).fetchall()
        refreshes = len({(row["predictor_kind"], row["predictor_fingerprint"]) for row in rows})
        latest_count = int(rows[-1]["training_label_count"]) if rows else 0
        return PredictionMetrics(scored, correct, correct / scored if scored else None, refreshes, latest_count)

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
