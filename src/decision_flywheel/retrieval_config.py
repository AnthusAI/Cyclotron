"""The ``retrieval:`` configuration block and the one function that turns it into a policy.

This block is independent of the few-shot list optimizer: nothing here reads
optimizer settings, and the optimizer never reads this. With the default
configuration (``enabled: false``) ``build_retrieval_policy`` returns ``None``:
no index is built and no embedding is requested.

```yaml
retrieval:
  enabled: false
  per_label: 4
  combine: add-feature            # the only mode for now
  backend: lexical                # lexical | embedding
  lexical: {stopwords: en-v1, tokenizer: unicode-v1, weighting: binary-cosine, k1: 1.2, b: 0.75}
  embedding: {provider: openai, model: text-embedding-3-small, dimensions: 512,
              cache: var/embeddings.jsonl}
  store: {kind: memory}           # memory | s3vectors | dynamodb (design-only stubs)
```
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable, Mapping

from .embedding_retrieval import EmbeddingRetriever, HashingEmbedder, InMemoryVectorStore, OpenAIEmbedder, VectorCache
from .lexical_retrieval import LexicalRetriever
from .retrieval import PerLabelRetrieval, stable_hash
from .vector_stores import DynamoDBVectorStore, S3VectorsStore

BACKENDS = ("lexical", "embedding")
STORE_KINDS = ("memory", "s3vectors", "dynamodb")


@dataclass(frozen=True)
class LexicalOptions:
    stopwords: str = "en-v1"
    tokenizer: str = "unicode-v1"
    weighting: str = "binary-cosine"
    k1: float = 1.2
    b: float = 0.75

    def retriever(self) -> LexicalRetriever:
        return LexicalRetriever(self.stopwords, self.tokenizer, self.weighting, self.k1, self.b)


@dataclass(frozen=True)
class EmbeddingOptions:
    provider: str = "openai"  # openai | fake; anything else needs an injected embedder
    model: str = "text-embedding-3-small"
    dimensions: int = 512
    cache: str | None = "var/embeddings.jsonl"  # None disables the on-disk vector cache

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("embedding.provider must be a non-empty string")
        if isinstance(self.dimensions, bool) or not isinstance(self.dimensions, int) or self.dimensions < 1:
            raise ValueError("embedding.dimensions must be a positive integer")
        if self.cache is not None and (not isinstance(self.cache, str) or not self.cache):
            raise ValueError("embedding.cache must be a path or null")


@dataclass(frozen=True)
class StoreOptions:
    kind: str = "memory"
    vector_bucket: str | None = None  # s3vectors
    table_name: str | None = None     # dynamodb
    index_name: str | None = None     # s3vectors, dynamodb
    region: str | None = None         # s3vectors, dynamodb

    def __post_init__(self) -> None:
        if self.kind not in STORE_KINDS:
            raise ValueError(f"store.kind must be one of {list(STORE_KINDS)}")

    def factory(self):
        if self.kind == "memory":
            return InMemoryVectorStore
        if self.kind == "s3vectors":
            return lambda: S3VectorsStore(self.vector_bucket or "", self.index_name or "", self.region or "")
        return lambda: DynamoDBVectorStore(self.table_name or "", self.index_name or "", self.region or "")


@dataclass(frozen=True)
class RetrievalConfig:
    """Optional dynamic retrieval. Off by default; the fixed example list stays the product."""

    enabled: bool = False
    per_label: int = 4
    combine: str = "add-feature"
    backend: str = "lexical"
    lexical: LexicalOptions = field(default_factory=LexicalOptions)
    embedding: EmbeddingOptions = field(default_factory=EmbeddingOptions)
    store: StoreOptions = field(default_factory=StoreOptions)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("retrieval.enabled must be true or false")
        if isinstance(self.per_label, bool) or not isinstance(self.per_label, int) or self.per_label < 1:
            raise ValueError("retrieval.per_label must be a positive integer")
        if self.combine != "add-feature":
            raise ValueError("retrieval.combine must be 'add-feature' (the only mode for now)")
        if self.backend not in BACKENDS:
            raise ValueError(f"retrieval.backend must be one of {list(BACKENDS)}")
        if self.backend == "lexical" and self.store.kind != "memory":
            raise ValueError("vector stores apply only to the embedding backend")
        self.lexical.retriever()  # validates the lexical knobs

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "RetrievalConfig":
        """Parse a ``retrieval:`` block; unknown keys are rejected at every level."""
        data = dict(data or {})
        nested = {"lexical": LexicalOptions, "embedding": EmbeddingOptions, "store": StoreOptions}
        _require_known(data, cls, "retrieval")
        values = {key: value for key, value in data.items() if key not in nested}
        for key, kind in nested.items():
            block = data.get(key) or {}
            if not isinstance(block, Mapping):
                raise ValueError(f"retrieval.{key} must be a mapping")
            _require_known(block, kind, f"retrieval.{key}")
            values[key] = kind(**block)
        return cls(**values)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        return stable_hash(self.as_dict())


def _require_known(data: Mapping[str, Any], kind: type, where: str) -> None:
    unknown = set(data) - {item.name for item in fields(kind)}
    if unknown:
        raise ValueError(f"unknown {where} keys: {sorted(unknown)}")


def build_retrieval_policy(config: RetrievalConfig, *, embedder: Any = None,
                           protected_ids: Iterable[str] = (),
                           protected_input_hashes: Iterable[str] = ()) -> PerLabelRetrieval | None:
    """Return the retrieval ``ContextPolicy`` for a config, or ``None`` when retrieval is off.

    For the embedding backend, ``embedder`` (a callable with an ``identity``)
    overrides the configured provider; specs always pass a fake one. Without it,
    ``provider: fake`` builds ``HashingEmbedder`` and ``provider: openai`` builds
    ``OpenAIEmbedder.from_environment()``, a live, paid client. That happens only
    here, only when retrieval is enabled with that provider.
    """
    if not config.enabled:
        return None
    if config.backend == "lexical":
        retriever = config.lexical.retriever()
    else:
        options = config.embedding
        if embedder is None:
            if options.provider == "fake":
                embedder = HashingEmbedder(options.dimensions)
            elif options.provider == "openai":
                embedder = OpenAIEmbedder.from_environment(model=options.model, dimensions=options.dimensions)
            else:
                raise ValueError(f"embedding provider {options.provider!r} needs an injected embedder")
        retriever = EmbeddingRetriever.from_embedder(
            embedder, cache=VectorCache(options.cache) if options.cache else None,
            new_store=config.store.factory())
    return PerLabelRetrieval(retriever, per_label=config.per_label, protected_ids=frozenset(protected_ids),
                             protected_input_hashes=frozenset(protected_input_hashes))
