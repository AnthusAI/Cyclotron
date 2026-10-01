import pytest

from .embedding_retrieval import EmbeddingRetriever, HashingEmbedder, OpenAIEmbedder
from .lexical_retrieval import LexicalRetriever
from .models import DecisionTask, Item, LabeledItem
from .retrieval import PerLabelRetrieval
from .retrieval_config import RetrievalConfig, build_retrieval_policy
from .vector_stores import S3VectorsStore

TASK = DecisionTask("topic", ("a", "b"), "Classify the target.")
POOL = [LabeledItem(Item(f"{label}-{n}", {"text": f"{label} words {n}"}), label)
        for label in TASK.labels for n in range(3)]


def refuse_live_client(*args, **kwargs):
    raise AssertionError("a live embedding client must not be constructed")


def test_retrieval_is_off_by_default_and_builds_nothing(monkeypatch):
    monkeypatch.setattr(OpenAIEmbedder, "from_environment", refuse_live_client)

    assert RetrievalConfig().enabled is False
    assert build_retrieval_policy(RetrievalConfig()) is None
    assert build_retrieval_policy(RetrievalConfig.from_mapping(None)) is None
    assert build_retrieval_policy(RetrievalConfig.from_mapping({"backend": "embedding"})) is None


def test_the_default_schema_matches_the_design():
    assert RetrievalConfig().as_dict() == {
        "enabled": False, "per_label": 4, "combine": "add-feature", "backend": "lexical",
        "lexical": {"stopwords": "en-v1", "tokenizer": "unicode-v1", "weighting": "binary-cosine",
                    "k1": 1.2, "b": 0.75},
        "embedding": {"provider": "openai", "model": "text-embedding-3-small", "dimensions": 512,
                      "cache": "var/embeddings.jsonl"},
        "store": {"kind": "memory", "vector_bucket": None, "table_name": None, "index_name": None,
                  "region": None},
    }


def test_enabled_lexical_retrieval_builds_a_balanced_policy_from_its_knobs():
    config = RetrievalConfig.from_mapping({"enabled": True, "per_label": 2,
                                           "lexical": {"weighting": "bm25", "stopwords": "none"}})

    policy = build_retrieval_policy(config, protected_ids=["a-0"])

    assert isinstance(policy, PerLabelRetrieval)
    assert policy.retriever == LexicalRetriever(stopwords="none", weighting="bm25")
    assert policy.per_label == 2 and policy.protected_ids == {"a-0"}
    context = policy.select(TASK, Item("t", {"text": "a words"}), POOL, per_label=2)
    assert [row.label for row in context] == ["a", "a", "b", "b"] and "a-0" not in {r.item.id for r in context}


def test_enabled_embedding_retrieval_uses_an_injected_or_fake_embedder_and_never_a_live_one(monkeypatch, tmp_path):
    monkeypatch.setattr(OpenAIEmbedder, "from_environment", refuse_live_client)
    config = RetrievalConfig.from_mapping({"enabled": True, "per_label": 2, "backend": "embedding",
                                           "embedding": {"cache": str(tmp_path / "vectors.jsonl")}})

    injected = build_retrieval_policy(config, embedder=HashingEmbedder(8))
    fake = build_retrieval_policy(RetrievalConfig.from_mapping(
        {"enabled": True, "backend": "embedding", "embedding": {"provider": "fake", "dimensions": 8,
                                                                "cache": None}}))

    assert isinstance(injected.retriever, EmbeddingRetriever)
    assert injected.retriever.embedder_identity == "fake-hashing-v1:8"
    assert fake.retriever.cache is None and fake.fingerprint == PerLabelRetrieval(
        EmbeddingRetriever.from_embedder(HashingEmbedder(8)), per_label=4).fingerprint
    injected.select(TASK, Item("t", {"text": "b words"}), POOL, per_label=2)
    assert (tmp_path / "vectors.jsonl").exists()
    with pytest.raises(AssertionError, match="live embedding client"):
        build_retrieval_policy(config)  # openai is built only when enabled and not injected


@pytest.mark.parametrize("data, message", [
    ({"enable": True}, "unknown retrieval keys"),
    ({"optimizer": {"enabled": True}}, "unknown retrieval keys"),
    ({"lexical": {"stop_words": "en-v1"}}, "unknown retrieval.lexical keys"),
    ({"store": {"kind": "memory", "bucket": "x"}}, "unknown retrieval.store keys"),
    ({"enabled": "yes"}, "enabled"),
    ({"per_label": 0}, "per_label"),
    ({"combine": "replace"}, "combine"),
    ({"backend": "graph"}, "backend"),
    ({"lexical": {"weighting": "dense"}}, "weighting"),
    ({"store": {"kind": "redis"}}, "store.kind"),
    ({"store": {"kind": "s3vectors"}}, "only to the embedding backend"),
    ({"embedding": {"dimensions": -1}}, "dimensions"),
])
def test_invalid_or_unknown_retrieval_settings_are_rejected(data, message):
    with pytest.raises(ValueError, match=message):
        RetrievalConfig.from_mapping(data)


def test_the_config_fingerprint_changes_with_its_own_settings_only():
    base = RetrievalConfig()

    assert base.fingerprint == RetrievalConfig.from_mapping({}).fingerprint
    for change in ({"enabled": True}, {"per_label": 2}, {"lexical": {"stopwords": "none"}},
                   {"embedding": {"model": "text-embedding-3-large"}}):
        assert RetrievalConfig.from_mapping(change).fingerprint != base.fingerprint


def test_a_stub_store_can_be_configured_but_indexing_with_it_is_not_implemented():
    config = RetrievalConfig.from_mapping({
        "enabled": True, "per_label": 2, "backend": "embedding", "embedding": {"provider": "fake", "cache": None},
        "store": {"kind": "s3vectors", "vector_bucket": "b", "index_name": "pool-r1", "region": "us-east-1"}})

    policy = build_retrieval_policy(config)

    assert policy.retriever.new_store() == S3VectorsStore("b", "pool-r1", "us-east-1")
    with pytest.raises(NotImplementedError, match="design-only"):
        policy.select(TASK, Item("t", {"text": "x"}), POOL, per_label=2)
