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
    initial_answer_value: str | None = None


class ReviewStore:
    """SQLite event store with deterministic train/audit partition assignment."""

    def __init__(self, path: str | Path, *, study_seed: str, rolling_audit_rate: float = .2,
                 final_audit_rate: float = .1, storage=None):
        if not isinstance(study_seed, str) or not study_seed:
            raise ValueError("study_seed must be a non-empty string")
        if any(isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1
               for rate in (rolling_audit_rate, final_audit_rate)):
            raise ValueError("audit rates must be probabilities")
        if rolling_audit_rate + final_audit_rate > 1:
            raise ValueError("combined audit rates cannot exceed one")
        self.path = Path(path) if path is not None else None
        if storage is None:
            if self.path is None:raise ValueError('native reviewer requires a local path or explicit cloud storage')
            self.path.parent.mkdir(parents=True, exist_ok=True)
        from .review_storage import SQLiteReviewStorage
        self.storage = storage if storage is not None else SQLiteReviewStorage(self.path)
        self._connection = getattr(self.storage,'db',None)  # Native compatibility only.
        self.storage.bind_study({'study_seed':study_seed,'rolling_audit_rate':repr(float(rolling_audit_rate)),
                                 'final_audit_rate':repr(float(final_audit_rate))})
        self.study_seed = study_seed
        self.rolling_audit_rate = float(rolling_audit_rate)
        self.final_audit_rate = float(final_audit_rate)

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> "ReviewStore":
        return self

    def __exit__(self, *_unused: object) -> None:
        self.close()





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
        records=[]
        for article in articles:
            if not isinstance(article,Article): raise ValueError('articles must be Article instances')
            records.append((article.id,article.title,article.abstract,article.submitted_at,
                json.dumps(article.categories,ensure_ascii=False,separators=(',',':')),
                article.authors,article.journal_ref,self._assignment(article.id)))
        return self.storage.import_articles(records)

    def articles(self) -> tuple[Article,...]:
        return tuple(self._article(row) for row in self.storage.articles())

    @staticmethod
    def _article(row: dict) -> Article:
        return Article(row["id"], row["title"], row["abstract"], row["submitted_at"],
                       tuple(json.loads(row["categories_json"])), row["authors"], row["journal_ref"])

    def assignment_for(self, article_id: str) -> str:
        row=self.storage.article(article_id)
        if row is None: raise KeyError(article_id)
        return str(row['assignment'])

    def _active_actions(self) -> dict[str, ReviewEvent]:
        rows = self.storage.actions()
        undone = {row["undoes_event_id"] for row in rows if row["action"] == "undo"}
        active: dict[str, ReviewEvent] = {}
        for row in rows:
            if row["action"] in ("vote", "skip") and row["id"] not in undone:
                active[row["article_id"]] = self._event(row)
        return active

    def next_unreviewed(self) -> Article | None:
        active = self._active_actions()
        rows = self.storage.articles()
        for row in rows:
            if row["id"] not in active:
                return self._article(row)
        return None

    def record_prediction(self, article_id: str, predicted_label: str, confidence: float, predictor_kind: str,
                          predictor_fingerprint: str, training_label_count: int) -> Presentation:
        """Save the exact prediction visible to a reviewer before their choice."""
        if self.storage.article(article_id) is None:
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
        with self.storage.transaction():
            self.storage.append_presentation(tuple(shown.__dict__.values()))
        return shown

    def presentations_for(self, article_id: str) -> tuple[Presentation,...]:
        return tuple(Presentation(**row) for row in self.storage.presentations(article_id))

    def record_vote(self, article_id: str, label: str, *, comment: str | None = None,
                    presentation_id: str | None = None) -> ReviewEvent:
        if label not in ("include", "exclude"):
            raise ValueError("a human vote must be include or exclude")
        if comment is not None and (not isinstance(comment, str) or not comment.strip()):
            raise ValueError("a comment must be omitted or non-empty")
        if presentation_id is not None:
            presentation = self.storage.presentation(presentation_id)
            if presentation is None or presentation["article_id"] != article_id:
                raise ValueError("a linked prediction must belong to the reviewed article")
        return self._record_action(article_id, "vote", label, comment, presentation_id)

    def record_skip(self, article_id: str) -> ReviewEvent:
        return self._record_action(article_id, "skip", None, None, None)

    def _record_action(self, article_id: str, action: str, label: str | None, comment: str | None,
                       presentation_id: str | None) -> ReviewEvent:
        if action not in ('vote','skip'): raise ValueError('only a vote or skip may be recorded')
        with self.storage.transaction():
            if self.storage.article(article_id) is None: raise KeyError(article_id)
            if article_id in self._active_actions():
                raise ValueError('undo the current review action before recording another one')
            event=ReviewEvent(str(uuid.uuid4()),article_id,action,label,comment,
                              datetime.now(timezone.utc).isoformat(),None,presentation_id)
            self.storage.append_action(tuple(event.__dict__.values()))
        return event

    def undo_last_vote(self) -> Article | None:
        with self.storage.transaction():
            active=self._active_actions()
            if not active: return None
            event=max(active.values(),key=lambda item:item.created_at)
            undo=ReviewEvent(str(uuid.uuid4()),event.article_id,'undo',None,None,
                             datetime.now(timezone.utc).isoformat(),event.id,None)
            self.storage.append_action(tuple(undo.__dict__.values()))
        return self.article(event.article_id)

    def article(self, article_id: str) -> Article:
        row=self.storage.article(article_id)
        if row is None: raise KeyError(article_id)
        return self._article(row)

    def events_for(self, article_id: str) -> tuple[ReviewEvent,...]:
        return tuple(self._event(row) for row in self.storage.actions(article_id))

    @staticmethod
    def _event(row: dict) -> ReviewEvent:
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
                presentation=self.storage.presentation(event.presentation_id) if event.presentation_id is not None else None
                if presentation and presentation['article_id']!=article_id: presentation=None
                labels.append(LearningFeedback(article_id, event.label, event.comment,
                    presentation["predicted_label"] if presentation is not None else None))
        return tuple(sorted(labels, key=lambda item: item.article_id))

    def hard_learning_example_ids(self) -> tuple[str, ...]:
        """Eligible human corrections of a displayed Jev policy, for core hard-swap trials."""
        hard: list[tuple[float, str]] = []
        for article_id, event in self._active_actions().items():
            if event.action != "vote" or event.presentation_id is None or self.assignment_for(article_id) != "train":
                continue
            presentation=self.storage.presentation(event.presentation_id)
            if (presentation is not None and str(presentation["predictor_kind"]).startswith("jev:")
                    and presentation["predicted_label"] != event.label):
                hard.append((float(presentation["confidence"]), article_id))
        return tuple(article_id for _confidence, article_id in sorted(hard, key=lambda row: (-row[0], row[1])))

    def prediction_metrics(self, *, assignments: tuple[str, ...] = _ASSIGNMENTS,
                           predictor_fingerprint: str | None = None) -> PredictionMetrics:
        """Measure pre-vote prediction agreement without treating skips as labels."""
        if any(role not in _ASSIGNMENTS for role in assignments):
            raise ValueError("unknown evaluation assignment")
        active = self._active_actions()
        scored = 0
        correct = 0
        for event in active.values():
            if event.action != "vote" or event.presentation_id is None:
                continue
            if self.assignment_for(event.article_id) not in assignments:
                continue
            row=self.storage.presentation(event.presentation_id)
            if row is None:  # Defensive: old or externally edited local databases cannot count as a score.
                continue
            if predictor_fingerprint is not None and row["predictor_fingerprint"] != predictor_fingerprint:
                continue
            scored += 1
            correct += row["predicted_label"] == event.label
        rows=self.storage.presentations()
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
