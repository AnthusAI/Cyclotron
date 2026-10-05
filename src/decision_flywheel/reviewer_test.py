"""Specs for the terminal article-review entry point."""
from __future__ import annotations

import json

import pytest

from .reviewer import load_articles_jsonl


def test_jsonl_import_accepts_only_title_abstract_records_with_explicit_metadata(tmp_path):
    path = tmp_path / "articles.jsonl"
    path.write_text(json.dumps({"id": "arxiv-1", "title": "A title", "abstract": "An abstract",
                                "submitted_at": "2026-10-05", "categories": ["cs.AI"]}) + "\n",
                    encoding="utf-8")

    articles = load_articles_jsonl(path)

    assert [(article.id, article.categories) for article in articles] == [("arxiv-1", ("cs.AI",))]


def test_jsonl_import_rejects_source_records_without_the_full_reviewer_payload(tmp_path):
    path = tmp_path / "articles.jsonl"
    path.write_text('{"id": "arxiv-1", "title": "A title"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 1"):
        load_articles_jsonl(path)
