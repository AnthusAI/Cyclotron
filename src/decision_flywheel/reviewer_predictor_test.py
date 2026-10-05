"""Specs for the transparent local baseline shown in the article reviewer."""
from __future__ import annotations

from .reviewer_predictor import LabeledArticle, predict_article
from .reviewer_store import Article


def _article(identifier: str, text: str) -> Article:
    return Article(identifier, text, text, "2026-10-05", ("cs.AI",))


def test_an_untrained_reviewer_prediction_is_an_explicit_fifty_fifty_prior():
    prediction = predict_article(_article("target", "new research"), ())

    assert prediction.label == "include"
    assert prediction.confidence == .5
    assert prediction.kind == "cold_start_prior"


def test_the_local_predictor_learns_only_from_the_explicitly_supplied_human_labels():
    labels = (LabeledArticle(_article("include", "reproducible benchmark evaluation"), "include"),
              LabeledArticle(_article("exclude", "clinical sleep wearable study"), "exclude"))

    prediction = predict_article(_article("target", "benchmark evaluation reproducible"), labels)

    assert prediction.label == "include"
    assert prediction.confidence > .5
    assert prediction.kind == "lexical_naive_bayes_metadata"


def test_the_predictor_fingerprint_changes_when_the_human_training_labels_change():
    article = _article("target", "benchmark evaluation")
    first = predict_article(article, (LabeledArticle(_article("one", "benchmark evaluation"), "include"),))
    second = predict_article(article, (LabeledArticle(_article("one", "benchmark evaluation"), "exclude"),))

    assert first.fingerprint != second.fingerprint


def test_the_local_baseline_can_learn_from_the_metadata_the_reviewer_is_shown():
    include = Article("include", "Ordinary title", "Ordinary abstract", "2026-10-05", ("cs.AI",),
                      "Ada Lovelace", "Journal of Decisions")
    exclude = Article("exclude", "Ordinary title", "Ordinary abstract", "2026-10-05", ("cs.CY",),
                      "Grace Hopper", None)
    target = Article("target", "Ordinary title", "Ordinary abstract", "2026-10-05", ("cs.AI",),
                     "Ada Lovelace", "Journal of Decisions")

    prediction = predict_article(target, (LabeledArticle(include, "include"), LabeledArticle(exclude, "exclude")))

    assert prediction.label == "include"
