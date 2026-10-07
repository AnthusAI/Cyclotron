"""Specs for adapting local reviewer feedback to the reusable flywheel core."""
from __future__ import annotations

from .reviewer_core import reviewer_labeled_items, reviewer_task
from .reviewer_store import Article, LearningFeedback


def test_reviewer_feedback_becomes_trusted_core_items_and_keeps_comments_demo_only():
    records = {
        "one": Article("one", "Useful method", "A reproducible evaluation.", "2026-10-05", ("cs.AI",), "Ada"),
    }
    items = reviewer_labeled_items((LearningFeedback("one", "include", "Reproducible and directly useful."),),
                                   records.__getitem__)

    assert items[0].source == "trusted"
    assert items[0].label == "include"
    assert "Title: Useful method" in items[0].item.values["text"]
    assert items[0].context == {"human_feedback": "Reproducible and directly useful."}
    assert reviewer_task().labels == ("include", "exclude")


def test_recorded_pre_vote_answers_survive_the_reviewer_adapter_without_entering_example_context():
    article = Article("one", "Method", "Abstract", "2026-10-05", ("cs.AI",), "Ada")
    item = reviewer_labeled_items((LearningFeedback("one", "include", "Useful", "exclude"),),
                                 lambda _: article)[0]
    assert item.initial_answer_value == "exclude"
    assert item.context == {"human_feedback": "Useful"}
