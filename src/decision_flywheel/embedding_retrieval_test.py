import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from .embedding_retrieval import (EmbeddingRetriever, HashingEmbedder, InMemoryVectorStore, OpenAIEmbedder,
                                  VectorCache, text_sha256)
from .models import DecisionTask, Item, LabeledItem
from .retrieval import PerLabelRetrieval, VectorRow

TASK = DecisionTask("topic", ("a", "b"), "Classify the target.")
VECTORS = {
    "target": [1.0, 0.0],
    "a near": [0.9, 0.1], "a tie one": [0.5, 0.5], "a tie two": [0.5, 0.5], "a far": [0.0, 1.0],
    "b near": [1.0, 0.05], "b mid": [0.6, 0.4], "b far": [-1.0, 0.0],
}
POOL = [LabeledItem(Item(item_id, {"text": text}), text[0])
        for item_id, text in [("a-1", "a near"), ("a-3", "a tie two"), ("a-2", "a tie one"), ("a-4", "a far"),
                              ("b-1", "b near"), ("b-2", "b mid"), ("b-3", "b far")]]


class CountingEmbed:
    """A fake embed(texts) function that records each batch; never touches a network."""

    def __init__(self, vectors=VECTORS):
        self.vectors, self.batches = vectors, []

    def __call__(self, texts):
        self.batches.append(list(texts))
        return [self.vectors[text] for text in texts]


def test_embedding_retrieval_returns_exact_cosine_neighbours_per_label_without_the_target():
    pool = POOL + [LabeledItem(Item("t", {"text": "target"}), "a")]
    retriever = EmbeddingRetriever(CountingEmbed(), "fake-table:2")

    index = retriever.index(TASK, pool)
    context = PerLabelRetrieval(retriever).select(TASK, Item("t", {"text": "target"}), pool, per_label=3)

    assert [item_id for item_id, _ in index.top_per_label(Item("t", {"text": "target"}), "a", 3, {"t"})] == [
        "a-1", "a-2", "a-3"]  # a-2 and a-3 tie exactly, so ascending ID decides
    assert [row.item.id for row in context] == ["a-1", "a-2", "a-3", "b-1", "b-2", "b-3"]


def test_cached_vectors_are_reused_and_the_cache_holds_no_text(tmp_path):
    cache_path = tmp_path / "embeddings.jsonl"
    first = CountingEmbed()
    EmbeddingRetriever(first, "fake-table:2", cache=VectorCache(cache_path)).index(TASK, POOL)
    second = CountingEmbed()

    EmbeddingRetriever(second, "fake-table:2", cache=VectorCache(cache_path)).index(TASK, POOL)

    assert sum(len(batch) for batch in first.batches) == len(POOL)
    assert second.batches == []
    lines = [json.loads(line) for line in cache_path.read_text().splitlines()]
    assert {line["text_sha256"] for line in lines} == {text_sha256(row.item.values["text"]) for row in POOL}
    assert "near" not in cache_path.read_text()


def test_the_cache_is_keyed_by_embedder_identity(tmp_path):
    cache = VectorCache(tmp_path / "embeddings.jsonl")
    EmbeddingRetriever(CountingEmbed(), "fake-table:2", cache=cache).index(TASK, POOL)
    other = CountingEmbed()

    EmbeddingRetriever(other, "fake-table-v2:2", cache=VectorCache(tmp_path / "embeddings.jsonl")).index(TASK, POOL)

    assert sum(len(batch) for batch in other.batches) == len(POOL)


def test_texts_are_embedded_in_sorted_bounded_batches_and_each_text_once():
    embed = CountingEmbed()
    retriever = EmbeddingRetriever(embed, "fake-table:2", batch_size=3)

    retriever.vectors(["b far", "a near", "b far", "a far", "b mid"])
    retriever.vectors(["a near"])

    assert [len(batch) for batch in embed.batches] == [3, 1]
    assert sorted(text for batch in embed.batches for text in batch) == ["a far", "a near", "b far", "b mid"]


def test_an_embedder_that_returns_the_wrong_number_of_vectors_is_rejected():
    with pytest.raises(ValueError, match="number of vectors"):
        EmbeddingRetriever(lambda texts: [[1.0, 0.0]], "broken:2").index(TASK, POOL)


def test_the_retriever_fingerprint_changes_with_the_embedder_identity():
    small = EmbeddingRetriever.from_embedder(OpenAIEmbedder(dimensions=512))
    large = EmbeddingRetriever.from_embedder(OpenAIEmbedder(dimensions=1536))

    assert small.configuration == {"kind": "embedding", "version": "1",
                                   "embedder": "openai:text-embedding-3-small:512",
                                   "store": {"kind": "memory", "approximate": False}}
    assert small.fingerprint != large.fingerprint
    assert small.fingerprint != EmbeddingRetriever.from_embedder(HashingEmbedder(512)).fingerprint
    with pytest.raises(ValueError, match="embedder_identity"):
        EmbeddingRetriever(CountingEmbed(), "")


def test_the_hashing_embedder_is_deterministic_and_offline():
    embedder = HashingEmbedder(16)

    assert embedder(["Same words here"]) == embedder(["same  WORDS here"]) == HashingEmbedder(16)(["same words here"])
    assert len(embedder(["x"])[0]) == 16 and embedder.identity == "fake-hashing-v1:16"


def test_the_memory_store_searches_exactly_with_label_filters_and_id_tie_breaks():
    store = InMemoryVectorStore()
    store.upsert([VectorRow("z", "a", "h1", (2.0, 0.0)), VectorRow("y", "a", "h2", (1.0, 0.0)),
                  VectorRow("x", "b", "h3", (0.0, 3.0))])

    assert store.query([5.0, 0.0], 2) == [("y", 1.0), ("z", 1.0)]
    assert store.query([0.0, 1.0], 5, label_filter="b") == [("x", 1.0)]
    assert store.approximate is False


def test_the_openai_embedder_sends_one_request_through_an_injected_client_and_needs_a_client():
    calls = []

    def create(**request):
        calls.append(request)
        return SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[0.0, 1.0]),
                                     SimpleNamespace(index=0, embedding=[1.0, 0.0])])

    embedder = OpenAIEmbedder(client=SimpleNamespace(embeddings=SimpleNamespace(create=create)))

    assert embedder(["first", "second"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert calls == [{"model": "text-embedding-3-small", "input": ["first", "second"], "dimensions": 512}]
    with pytest.raises(RuntimeError, match="from_environment"):
        OpenAIEmbedder()(["text"])


def test_importing_retrieval_modules_does_not_import_provider_or_aws_sdks():
    code = ("import sys, decision_flywheel.retrieval_config; "
            "print(sorted(m for m in ('openai', 'boto3', 'dotenv') if m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                            env={"PYTHONPATH": ":".join(sys.path)})

    assert result.stdout.strip() == "[]"
