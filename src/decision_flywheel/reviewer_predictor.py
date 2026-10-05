"""A transparent, local baseline predictor for the human article reviewer.

This is deliberately small: it makes the initial review screen useful before a
provider-backed flywheel exists, while preserving the exact label set used for
each prediction.  It is not a substitute for the later Jev decision features.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Iterable

from .reviewer_store import Article


_TOKENS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}")
_LABELS = ("include", "exclude")


@dataclass(frozen=True)
class LabeledArticle:
    article: Article
    label: str

    def __post_init__(self) -> None:
        if self.label not in _LABELS:
            raise ValueError("a baseline training label must be include or exclude")


@dataclass(frozen=True)
class ReviewerPrediction:
    label: str
    confidence: float
    kind: str
    fingerprint: str
    training_label_count: int


def _tokens(article: Article) -> tuple[str, ...]:
    source = " ".join((article.title, article.abstract, article.submitted_at, " ".join(article.categories),
                        article.authors, article.journal_ref or ""))
    return tuple(token.lower() for token in _TOKENS.findall(source))


def _fingerprint(labels: Iterable[LabeledArticle]) -> str:
    payload = [{"id": row.article.id, "label": row.label} for row in sorted(labels, key=lambda row: row.article.id)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def predict_article(article: Article, labels: Iterable[LabeledArticle]) -> ReviewerPrediction:
    """Return a smoothed multinomial naïve-Bayes prediction over human labels."""
    rows = tuple(labels)
    fingerprint = _fingerprint(rows)
    if not rows:
        return ReviewerPrediction("include", .5, "cold_start_prior", fingerprint, 0)
    documents = Counter(row.label for row in rows)
    counts = {label: Counter() for label in _LABELS}
    totals = Counter()
    vocabulary = set()
    for row in rows:
        tokens = _tokens(row.article)
        counts[row.label].update(tokens)
        totals[row.label] += len(tokens)
        vocabulary.update(tokens)
    target = _tokens(article)
    vocabulary_size = max(len(vocabulary), 1)
    total_documents = len(rows)
    scores = {}
    for label in _LABELS:
        # A Laplace-smoothed class prior keeps one-class early sessions usable.
        score = math.log((documents[label] + 1) / (total_documents + len(_LABELS)))
        denominator = totals[label] + vocabulary_size
        for token in target:
            score += math.log((counts[label][token] + 1) / denominator)
        scores[label] = score
    maximum = max(scores.values())
    normalized = {label: math.exp(value - maximum) for label, value in scores.items()}
    probability_include = normalized["include"] / sum(normalized.values())
    label = "include" if probability_include >= .5 else "exclude"
    confidence = probability_include if label == "include" else 1 - probability_include
    return ReviewerPrediction(label, confidence, "lexical_naive_bayes", fingerprint, len(rows))
