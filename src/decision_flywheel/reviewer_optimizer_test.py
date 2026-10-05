"""Specs for the local, training-only reviewer context optimizer."""
from __future__ import annotations

from .reviewer_optimizer import optimize_reviewer_context
from .reviewer_predictor import LabeledArticle
from .reviewer_store import Article


def _article(identifier: str, category: str, label_hint: str) -> Article:
    return Article(identifier, "Same title", "Same abstract", "2026-10-05", (category,), label_hint)


def test_optimizer_waits_for_enough_both_class_human_labels_without_using_audit_data():
    labels = tuple(LabeledArticle(_article(f"i-{index}", "cs.AI", "Ada"), "include") for index in range(5))

    result = optimize_reviewer_context(labels, minimum_labels=6)

    assert not result.attempted
    assert result.selected.include_metadata
    assert "eligible labels" in result.reason


def test_optimizer_selects_the_metadata_candidate_when_metadata_improves_leave_one_out_accuracy():
    labels = tuple(
        LabeledArticle(_article(f"yes-{index}", "cs.AI", "Ada"), "include") for index in range(4)
    ) + tuple(
        LabeledArticle(_article(f"no-{index}", "cs.CY", "Grace"), "exclude") for index in range(4)
    )

    result = optimize_reviewer_context(labels, minimum_labels=8)

    assert result.attempted
    assert result.selected.include_metadata
    assert result.selected_accuracy is not None
    assert result.selected_accuracy >= result.candidates["content_only"]
