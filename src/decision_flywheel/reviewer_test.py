"""Specs for the terminal article-review entry point."""
from __future__ import annotations

import json

import pytest

from .reviewer import _article_panel, load_articles_jsonl, load_flywheel_report
from .reviewer_predictor import ReviewerPrediction
from .reviewer_store import Article


def test_jsonl_import_accepts_only_title_abstract_records_with_explicit_metadata(tmp_path):
    path = tmp_path / "articles.jsonl"
    path.write_text(json.dumps({"id": "arxiv-1", "title": "A title", "abstract": "An abstract",
                                "submitted_at": "2026-10-05", "categories": ["cs.AI"],
                                "authors": "Ada Lovelace", "journal_ref": "Journal of Decisions (2026)"}) + "\n",
                    encoding="utf-8")

    articles = load_articles_jsonl(path)

    assert [(article.id, article.categories, article.authors, article.journal_ref) for article in articles] == [
        ("arxiv-1", ("cs.AI",), "Ada Lovelace", "Journal of Decisions (2026)")
    ]


def test_jsonl_import_rejects_source_records_without_the_full_reviewer_payload(tmp_path):
    path = tmp_path / "articles.jsonl"
    path.write_text('{"id": "arxiv-1", "title": "A title"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 1"):
        load_articles_jsonl(path)


def test_flywheel_report_loader_accepts_only_the_text_free_summary_shape(tmp_path):
    path = tmp_path / "flywheel.json"
    path.write_text(json.dumps({"version": 1, "model": "jev:jev-latest", "winner": "incumbent",
                                "promoted": False, "reason": "kept incumbent",
                                "calls": {"attempted": 12, "succeeded": 12},
                                "scores": {"incumbent": {"accuracy": .67, "brier": .33}}}), encoding="utf-8")

    report = load_flywheel_report(path)

    assert report["winner"] == "incumbent"


def test_article_panel_identifies_a_jev_prediction_as_the_measured_policy():
    article = Article("arxiv-1", "A title", "An abstract", "2026-10-05", ("cs.AI",))

    panel = _article_panel(article, ReviewerPrediction("include", .8, "jev:incumbent", "a" * 64, 12))

    assert "Measured Jev policy (incumbent)" in panel.renderable.plain
