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
        authors="Ada Lovelace, Grace Hopper",
        journal_ref="Journal of Careful Decisions (2026)",
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


def test_an_article_keeps_the_provenance_fields_a_reviewer_needs_to_see(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite3", study_seed="demo-seed")
    source = _article()
    store.import_articles((source,))

    restored = store.article("arxiv-1")

    assert restored.authors == "Ada Lovelace, Grace Hopper"
    assert restored.journal_ref == "Journal of Careful Decisions (2026)"
    store.close()


def test_an_unreviewed_legacy_article_can_be_enriched_with_new_source_metadata(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite3", study_seed="demo-seed")
    legacy = Article("arxiv-1", "Paper", "Abstract", "2026-10-05", ("cs.AI",))
    enriched = Article("arxiv-1", "Paper", "Abstract", "2026-10-05", ("cs.AI",),
                       "Ada Lovelace", "Journal of Decisions (2026)")
    store.import_articles((legacy,))

    assert store.import_articles((enriched,)) == 0
    assert store.article("arxiv-1") == enriched
    store.close()


def test_a_reviewed_article_keeps_the_metadata_that_the_reviewer_saw_when_source_metadata_changes(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite3", study_seed="demo-seed")
    legacy = Article("arxiv-1", "Paper", "Abstract", "2026-10-05", ("cs.AI",))
    enriched = Article("arxiv-1", "Paper", "Abstract", "2026-10-05", ("cs.AI",), "Ada Lovelace")
    store.import_articles((legacy,))
    store.record_vote("arxiv-1", "include")

    assert store.import_articles((enriched,)) == 0
    assert store.article("arxiv-1") == legacy
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


def test_learning_feedback_exposes_only_train_votes_and_their_human_comments(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite3", study_seed="demo-seed", rolling_audit_rate=0,
                        final_audit_rate=0)
    store.import_articles((_article(),))
    store.record_vote("arxiv-1", "include", comment="Useful implementation detail.")

    feedback = store.learning_feedback()

    assert [(row.article_id, row.label, row.comment) for row in feedback] == [
        ("arxiv-1", "include", "Useful implementation detail.")
    ]
    store.close()


def test_a_human_vote_can_be_linked_to_the_exact_prediction_shown_before_it(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite3", study_seed="demo-seed")
    store.import_articles((_article(),))
    shown = store.record_prediction("arxiv-1", "include", .72, "lexical_naive_bayes", "a" * 64, 4)

    vote = store.record_vote("arxiv-1", "exclude", presentation_id=shown.id)

    assert vote.presentation_id == shown.id
    assert store.presentations_for("arxiv-1") == (shown,)
    store.close()


def test_prediction_metrics_score_only_predictions_that_were_shown_before_active_votes(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite3", study_seed="demo-seed")
    store.import_articles((_article(1), _article(2)))
    first = store.record_prediction("arxiv-1", "include", .75, "lexical_naive_bayes", "a" * 64, 3)
    second = store.record_prediction("arxiv-2", "include", .50, "cold_start_prior", "b" * 64, 0)
    store.record_vote("arxiv-1", "include", presentation_id=first.id)
    store.record_vote("arxiv-2", "exclude", presentation_id=second.id)

    metrics = store.prediction_metrics()

    assert metrics.scored_votes == 2
    assert metrics.correct_votes == 1
    assert metrics.accuracy == .5
    assert metrics.model_refreshes == 2
    assert metrics.latest_training_label_count == 0
    store.close()
