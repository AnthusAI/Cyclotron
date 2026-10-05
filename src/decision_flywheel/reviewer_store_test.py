"""Specs for the local, human-first article-review event store."""
from __future__ import annotations

import pytest

from .reviewer_store import Article, ReviewStore


def _article(number: int = 1) -> Article:
    return Article(
        id=f"arxiv-{number}",
        title=f"Paper {number}",
        abstract=f"Abstract {number}",
        submitted_at="2026-10-05",
        categories=("cs.AI",),
    )


def test_a_review_assignment_is_deterministic_and_never_requires_a_model(tmp_path):
    path = tmp_path / "reviews.sqlite3"
    first = ReviewStore(path, study_seed="demo-seed", rolling_audit_rate=.2, final_audit_rate=.1)
    first.import_articles((_article(1), _article(2), _article(3)))
    initial = {article.id: first.assignment_for(article.id) for article in first.articles()}
    first.close()

    second = ReviewStore(path, study_seed="demo-seed", rolling_audit_rate=.2, final_audit_rate=.1)
    assert {article.id: second.assignment_for(article.id) for article in second.articles()} == initial
    second.close()


def test_a_vote_is_an_immutable_event_and_undo_restores_the_article_to_the_queue(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite3", study_seed="demo-seed")
    store.import_articles((_article(),))

    assert store.next_unreviewed().id == "arxiv-1"
    event = store.record_vote("arxiv-1", "include", comment="Important for our library.")
    assert store.next_unreviewed() is None

    restored = store.undo_last_vote()
    assert restored.id == "arxiv-1"
    events = store.events_for("arxiv-1")
    assert [entry.action for entry in events] == ["vote", "undo"]
    assert events[1].undoes_event_id == event.id
    assert store.current_label("arxiv-1") is None
    store.close()


def test_a_skip_is_recorded_but_does_not_create_a_training_label(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite3", study_seed="demo-seed")
    store.import_articles((_article(),))

    store.record_skip("arxiv-1")

    assert store.current_label("arxiv-1") is None
    assert store.events_for("arxiv-1")[0].action == "skip"
    store.close()


@pytest.mark.parametrize("label", ("approve", "", None))
def test_only_include_and_exclude_are_valid_human_votes(label, tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite3", study_seed="demo-seed")
    store.import_articles((_article(),))

    with pytest.raises(ValueError, match="include or exclude"):
        store.record_vote("arxiv-1", label)  # type: ignore[arg-type]
    store.close()


def test_a_final_audit_label_is_never_eligible_for_learning(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite3", study_seed="demo-seed", rolling_audit_rate=0,
                        final_audit_rate=1.0)
    store.import_articles((_article(),))
    store.record_vote("arxiv-1", "include")

    assert store.assignment_for("arxiv-1") == "final_audit"
    assert store.learning_labels() == ()
    store.close()
