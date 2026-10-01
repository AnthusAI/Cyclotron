"""Lightweight semantic retrieval: injected embeddings, a text-free cache, an in-memory index.

The embedding function is injected: ``embed(texts) -> vectors``. Nothing here
calls a network service unless the caller passes an embedder that does, such as
``OpenAIEmbedder.from_environment()``. Specs use ``HashingEmbedder``, a fake,
deterministic, offline embedder.

Vectors are cached on disk as JSON lines keyed by (embedder identity, SHA-256 of
the embedded text). The cache never stores text, so it can be kept beside
results. Search is exact cosine over an in-memory matrix (numpy if installed,
pure Python otherwise); ties break by ascending ID.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .context import _text, input_hash
from .models import DecisionTask, Item, LabeledItem
from .retrieval import VectorRow, VectorStore, pool_fingerprint, rank, stable_hash, validated_pool

EMBEDDING_RETRIEVER_VERSION = "1"
Embed = Callable[[Sequence[str]], Sequence[Sequence[float]]]


def embedding_text(text: str) -> str:
    """The exact string sent to an embedder: NFKC with whitespace collapsed (case kept)."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def text_sha256(text: str) -> str:
    return hashlib.sha256(embedding_text(text).encode("utf-8")).hexdigest()


def _unit(vector: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("embedding vectors must be non-empty and finite")
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values) if norm else values


@dataclass(frozen=True)
class HashingEmbedder:
    """A fake, offline, deterministic bag-of-words embedder for specs and $0 baselines."""

    dimensions: int = 64

    @property
    def identity(self) -> str:
        return f"fake-hashing-v1:{self.dimensions}"

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1.0 if digest[4] < 128 else -1.0
            vectors.append(vector)
        return vectors


@dataclass
class OpenAIEmbedder:
    """Reference embedder for OpenAI ``text-embedding-3-small`` (live, paid, opt-in).

    Constructing it with no ``client`` imports nothing and calls nothing. Use
    ``from_environment()`` to build a real client: it loads a gitignored ``.env``
    if python-dotenv is installed and lets the OpenAI SDK read its key from the
    environment. Install with ``decision-flywheel[openai-embeddings]``.
    """

    model: str = "text-embedding-3-small"
    dimensions: int = 512
    client: Any = None

    @property
    def identity(self) -> str:
        return f"openai:{self.model}:{self.dimensions}"

    @classmethod
    def from_environment(cls, *, model: str = "text-embedding-3-small", dimensions: int = 512) -> "OpenAIEmbedder":
        try:
            from openai import OpenAI
        except ImportError as error:  # pragma: no cover - optional dependency
            raise ImportError("Install decision-flywheel[openai-embeddings] to embed with OpenAI.") from error
        try:
            from dotenv import load_dotenv
        except ImportError:  # pragma: no cover - the environment may already hold the key
            pass
        else:
            load_dotenv(override=False)
        return cls(model=model, dimensions=dimensions, client=OpenAI())

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        if self.client is None:
            raise RuntimeError("OpenAIEmbedder has no client; build it with OpenAIEmbedder.from_environment()")
        response = self.client.embeddings.create(model=self.model, input=list(texts), dimensions=self.dimensions)
        return [list(row.embedding) for row in sorted(response.data, key=lambda row: row.index)]


class VectorCache:
    """Append-only JSON-lines cache: {"embedder", "text_sha256", "vector"}; never text."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._vectors: dict[tuple[str, str], tuple[float, ...]] | None = None

    def _load(self) -> dict[tuple[str, str], tuple[float, ...]]:
        if self._vectors is None:
            self._vectors = {}
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        self._vectors[(row["embedder"], row["text_sha256"])] = tuple(row["vector"])
        return self._vectors

    def get(self, embedder: str, digest: str) -> tuple[float, ...] | None:
        return self._load().get((embedder, digest))

    def put_many(self, embedder: str, vectors: Mapping[str, Sequence[float]]) -> None:
        loaded = self._load()
        new = {digest: tuple(float(value) for value in vector) for digest, vector in sorted(vectors.items())
               if (embedder, digest) not in loaded}
        if not new:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for digest, vector in new.items():
                handle.write(json.dumps({"embedder": embedder, "text_sha256": digest, "vector": list(vector)},
                                        separators=(",", ":"), sort_keys=True) + "\n")
                loaded[(embedder, digest)] = vector


class InMemoryVectorStore:
    """Exact cosine search over unit vectors held in memory (the default store)."""

    approximate = False
    configuration: Mapping[str, object] = {"kind": "memory", "approximate": False}

    def __init__(self) -> None:
        self.rows: dict[str, VectorRow] = {}
        self._matrix: Any = None

    def upsert(self, rows: Sequence[VectorRow]) -> None:
        for row in rows:
            self.rows[row.id] = VectorRow(row.id, row.label, row.input_hash, _unit(row.vector))
        self._matrix = None

    def query(self, vector, top_k, label_filter=None):
        rows = [row for row in self.rows.values() if label_filter is None or row.label == label_filter]
        if not rows or top_k < 1:
            return []
        query = _unit(vector)
        return rank(dict(zip((row.id for row in rows), self._cosines(rows, query, label_filter))), top_k)

    def _cosines(self, rows: list[VectorRow], query: tuple[float, ...], label_filter: str | None) -> list[float]:
        try:
            import numpy
        except ImportError:
            return [sum(a * b for a, b in zip(row.vector, query)) for row in rows]
        if self._matrix is None:
            self._matrix = {}
        if label_filter not in self._matrix:
            self._matrix[label_filter] = numpy.array([row.vector for row in rows], dtype=numpy.float64)
        return (self._matrix[label_filter] @ numpy.array(query, dtype=numpy.float64)).tolist()


@dataclass(frozen=True)
class EmbeddingRetriever:
    """Semantic retriever over an injected ``embed(texts) -> vectors`` function.

    ``embedder_identity`` must name the provider, model and dimensions (for example
    ``"openai:text-embedding-3-small:512"``); it keys the cache and the fingerprint.
    ``new_store`` builds one empty ``VectorStore`` per indexed pool.
    """

    embed: Embed
    embedder_identity: str
    cache: VectorCache | None = None
    new_store: Callable[[], VectorStore] = InMemoryVectorStore
    batch_size: int = 256
    _memo: dict = field(default_factory=dict, init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        if not isinstance(self.embedder_identity, str) or not self.embedder_identity.strip():
            raise ValueError("embedder_identity must name the provider, model and dimensions")
        if isinstance(self.batch_size, bool) or not isinstance(self.batch_size, int) or self.batch_size < 1:
            raise ValueError("batch_size must be a positive integer")

    @classmethod
    def from_embedder(cls, embedder: Any, **options: Any) -> "EmbeddingRetriever":
        """Use an embedder object with an ``identity`` (``HashingEmbedder``, ``OpenAIEmbedder``)."""
        return cls(embed=embedder, embedder_identity=embedder.identity, **options)

    @property
    def configuration(self) -> Mapping[str, object]:
        return {"kind": "embedding", "version": EMBEDDING_RETRIEVER_VERSION,
                "embedder": self.embedder_identity, "store": dict(self.new_store().configuration)}

    @property
    def fingerprint(self) -> str:
        return stable_hash(self.configuration)

    def vectors(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Vectors for texts, embedding only the ones missing from memory and the cache."""
        digests = [text_sha256(text) for text in texts]
        missing: dict[str, str] = {}
        for digest, text in zip(digests, texts):
            if digest not in self._memo:
                cached = self.cache.get(self.embedder_identity, digest) if self.cache else None
                if cached is not None:
                    self._memo[digest] = cached
                else:
                    missing.setdefault(digest, embedding_text(text))
        pending = sorted(missing.items())
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start:start + self.batch_size]
            vectors = list(self.embed([text for _, text in batch]))
            if len(vectors) != len(batch):
                raise ValueError("the embedder returned a different number of vectors than texts")
            fresh = {digest: tuple(float(value) for value in vector) for (digest, _), vector in zip(batch, vectors)}
            if self.cache is not None:
                self.cache.put_many(self.embedder_identity, fresh)
            self._memo.update(fresh)
        return [self._memo[digest] for digest in digests]

    def warm(self, task: DecisionTask, items: Sequence[Item]) -> None:
        """Embed targets in batches up front (otherwise each target is embedded on first use)."""
        self.vectors([_text(item, task) for item in items])

    def index(self, task: DecisionTask, pool: Sequence[LabeledItem]) -> "EmbeddingIndex":
        rows = validated_pool(task, pool)
        vectors = self.vectors([_text(row.item, task) for row in rows])
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) > 1:
            raise ValueError("the embedder returned vectors of different dimensions")
        store = self.new_store()
        store.upsert([VectorRow(row.item.id, row.label, input_hash(task, row.item), vector)
                      for row, vector in zip(rows, vectors)])
        return EmbeddingIndex(self, task, store, rows,
                              stable_hash([self.fingerprint, pool_fingerprint(task, rows)]))


@dataclass(frozen=True)
class EmbeddingIndex:
    retriever: EmbeddingRetriever
    task: DecisionTask
    store: VectorStore
    pool: tuple[LabeledItem, ...]
    fingerprint: str

    def top_per_label(self, target, label, k, exclude_ids):
        [vector] = self.retriever.vectors([_text(target, self.task)])
        excluded_here = sum(row.label == label and row.item.id in exclude_ids for row in self.pool)
        hits = self.store.query(vector, k + excluded_here, label_filter=label)
        return [(item_id, score) for item_id, score in hits if item_id not in exclude_ids][:k]
