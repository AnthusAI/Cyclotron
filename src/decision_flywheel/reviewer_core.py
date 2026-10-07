"""Adapter from the local article reviewer to the reusable Decision Flywheel core.

There is intentionally no optimization logic here.  This module converts
reviewer records into the core's provider-neutral contracts; the existing
``improve_example_list`` and ``search_context_policies`` machinery performs
the actual candidate search and evaluation.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from .models import DecisionTask, Item, LabeledItem
from .reviewer_store import Article, LearningFeedback


def reviewer_task() -> DecisionTask:
    return DecisionTask(
        "knowledge_base_inclusion", ("include", "exclude"),
        "Decide whether the target research article belongs in this reviewer's knowledge base. "
        "Infer the reviewer's criteria from the labeled examples and their optional human_feedback. "
        "Classify only the target article.",
    )


def reviewer_item(article: Article) -> Item:
    lines = [f"Title: {article.title}", f"Submitted: {article.submitted_at}",
             f"Categories: {', '.join(article.categories)}", f"Authors: {article.authors}"]
    if article.journal_ref:
        lines.append(f"Published as: {article.journal_ref}")
    lines.append(f"Abstract: {article.abstract}")
    return Item(article.id, {"text": "\n".join(lines)})


def reviewer_labeled_items(feedback: Iterable[LearningFeedback],
                           article_for: Callable[[str], Article]) -> tuple[LabeledItem, ...]:
    """Convert only pre-filtered, eligible feedback; audit labels never enter here."""
    items = []
    for row in feedback:
        context = {"human_feedback": row.comment} if row.comment else {}
        items.append(LabeledItem(reviewer_item(article_for(row.article_id)), row.label, "trusted", context,
                                 initial_answer_value=row.initial_answer_value))
    return tuple(sorted(items, key=lambda row: row.item.id))
