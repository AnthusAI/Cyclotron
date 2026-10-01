"""Lexical retrieval v2: stop words on by default, Unicode tokens, optional TF-IDF or BM25.

``PerLabelLexicalRetrieval`` (v1) in ``context.py`` stays frozen: saved artifacts
and cached answers depend on its fingerprint. This module is a separate retriever
with its own versioned configuration, used through ``PerLabelRetrieval``.

Everything is pure Python and built over the labeled pool only (document
frequencies, average lengths), so it needs no downloads and no dependencies.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Mapping, Sequence

from .context import _text
from .models import DecisionTask, Item, LabeledItem
from .retrieval import pool_fingerprint, rank, stable_hash, validated_pool

LEXICAL_RETRIEVER_VERSION = "2"

# Vendored and versioned so fingerprints never depend on a download. It deliberately
# KEEPS negations (not, no, nor, never) and contraction fragments such as "t" and
# "didn" from "didn't"; stock lists drop them, which hurts sentiment-like tasks.
# It also keeps contrast and intensity words (but, very, too, only, more, most).
# Never edit this list in place: add "en-v2" instead.
STOPWORDS = {
    "en-v1": frozenset("""
        a about above after am an and any are as at be because been before being below
        between both by can could did do does doing during each for from had has have
        having he her here hers herself him himself his how i if in into is it its itself
        just me my myself of on or other our ours ourselves she should so some such than
        that the their theirs them themselves then there these they this those through to
        until was we were what when where which while who whom why will with would you
        your yours yourself yourselves s ll re ve d m
    """.split()),
    "none": frozenset(),
}

TOKENIZERS = {
    "unicode-v1": lambda text: re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()),
    "ascii-v1": lambda text: re.findall(r"[a-z]+", text.lower()),  # v1's tokenizer
}

WEIGHTINGS = ("binary-cosine", "tfidf-cosine", "bm25")


@dataclass(frozen=True)
class LexicalRetriever:
    """Configurable lexical retriever.

    - ``stopwords``: ``"en-v1"`` (default, negations kept) or ``"none"``.
    - ``tokenizer``: ``"unicode-v1"`` (default; NFKC + casefold + ``\\w+``, keeps
      digits and non-English text) or ``"ascii-v1"`` (v1's ``[a-z]+``).
    - ``weighting``: ``"binary-cosine"`` (default; v1's set overlap),
      ``"tfidf-cosine"`` or ``"bm25"`` (``k1``, ``b``). IDF uses the pool only.
    """

    stopwords: str = "en-v1"
    tokenizer: str = "unicode-v1"
    weighting: str = "binary-cosine"
    k1: float = 1.2
    b: float = 0.75

    def __post_init__(self) -> None:
        if self.stopwords not in STOPWORDS:
            raise ValueError(f"stopwords must be one of {sorted(STOPWORDS)}")
        if self.tokenizer not in TOKENIZERS:
            raise ValueError(f"tokenizer must be one of {sorted(TOKENIZERS)}")
        if self.weighting not in WEIGHTINGS:
            raise ValueError(f"weighting must be one of {list(WEIGHTINGS)}")
        for name in ("k1", "b"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must be a non-negative number")
        if self.b > 1:
            raise ValueError("b must be between 0 and 1")

    @property
    def configuration(self) -> Mapping[str, object]:
        config: dict[str, object] = {"kind": "lexical", "version": LEXICAL_RETRIEVER_VERSION,
                                     "stopwords": self.stopwords, "tokenizer": self.tokenizer,
                                     "weighting": self.weighting}
        if self.weighting == "bm25":  # k1 and b only matter, and only fingerprint, for BM25
            config.update(k1=float(self.k1), b=float(self.b))
        return config

    @property
    def fingerprint(self) -> str:
        return stable_hash(self.configuration)

    def tokens(self, text: str) -> list[str]:
        stop = STOPWORDS[self.stopwords]
        return [token for token in TOKENIZERS[self.tokenizer](text) if token not in stop]

    def index(self, task: DecisionTask, pool: Sequence[LabeledItem]) -> "LexicalIndex":
        return LexicalIndex(self, task, validated_pool(task, pool))


class LexicalIndex:
    """An inverted index over one labeled pool; scores only touch shared tokens."""

    def __init__(self, retriever: LexicalRetriever, task: DecisionTask, pool: Sequence[LabeledItem]):
        self.retriever = retriever
        self.task = task
        self._last: tuple[str, dict[str, float]] | None = None
        self.fingerprint = stable_hash([retriever.fingerprint, pool_fingerprint(task, pool)])
        self.ids_by_label: dict[str, list[str]] = defaultdict(list)
        for row in pool:
            self.ids_by_label[row.label].append(row.item.id)
        counts = {row.item.id: Counter(retriever.tokens(_text(row.item, task))) for row in pool}
        self.document_count = len(pool)
        self.document_frequency = Counter(token for terms in counts.values() for token in terms)
        lengths = {item_id: sum(terms.values()) for item_id, terms in counts.items()}
        self.average_length = (sum(lengths.values()) / len(lengths)) if lengths else 0.0
        self.postings: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for item_id, terms in counts.items():
            for token, weight in self._document_weights(terms, lengths[item_id]).items():
                self.postings[token].append((item_id, weight))

    def idf(self, token: str) -> float:
        df, n = self.document_frequency.get(token, 0), self.document_count
        if self.retriever.weighting == "bm25":
            return math.log(1 + (n - df + 0.5) / (df + 0.5))
        return math.log((n + 1) / (df + 1)) + 1  # smoothed TF-IDF

    def _document_weights(self, terms: Counter, length: int) -> dict[str, float]:
        weighting = self.retriever.weighting
        if not terms:
            return {}
        if weighting == "binary-cosine":
            return {token: 1 / math.sqrt(len(terms)) for token in terms}
        if weighting == "tfidf-cosine":
            raw = {token: count * self.idf(token) for token, count in terms.items()}
            norm = math.sqrt(sum(value * value for value in raw.values()))
            return {token: value / norm for token, value in raw.items()}
        k1, b = self.retriever.k1, self.retriever.b
        scale = k1 * (1 - b + b * length / self.average_length) if self.average_length else k1
        return {token: self.idf(token) * count * (k1 + 1) / (count + scale) for token, count in terms.items()}

    def _query_weights(self, text: str) -> dict[str, float]:
        terms = Counter(self.retriever.tokens(text))
        if not terms:
            return {}
        weighting = self.retriever.weighting
        if weighting == "binary-cosine":
            return {token: 1 / math.sqrt(len(terms)) for token in terms}
        if weighting == "tfidf-cosine":
            raw = {token: count * self.idf(token) for token, count in terms.items()}
            norm = math.sqrt(sum(value * value for value in raw.values()))
            return {token: value / norm for token, value in raw.items()}
        return {token: 1.0 for token in terms}  # BM25 counts each query term once

    def scores(self, target: Item) -> dict[str, float]:
        """Score every pooled item that shares a token with the target."""
        text = _text(target, self.task)
        if self._last is not None and self._last[0] == text:  # one target asks once per label
            return self._last[1]
        totals: dict[str, float] = defaultdict(float)
        for token, query_weight in self._query_weights(text).items():
            for item_id, weight in self.postings.get(token, ()):
                totals[item_id] += query_weight * weight
        self._last = (text, totals)
        return totals

    def top_per_label(self, target, label, k, exclude_ids):
        totals = self.scores(target)
        return rank({item_id: totals.get(item_id, 0.0) for item_id in self.ids_by_label.get(label, ())
                     if item_id not in exclude_ids}, k)
