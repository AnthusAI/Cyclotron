import pytest

from .embedding_retrieval import EmbeddingRetriever, HashingEmbedder
from .models import DecisionTask, Item, LabeledItem
from .retrieval import VectorRow, VectorStore
from .vector_stores import DynamoDBVectorStore, S3VectorsStore

STUBS = [S3VectorsStore("bucket", "pool-r1", "us-east-1"), DynamoDBVectorStore("table", "label-index", "us-east-1")]


@pytest.mark.parametrize("store", STUBS, ids=["s3vectors", "dynamodb"])
def test_aws_store_stubs_satisfy_the_vector_store_interface_and_declare_approximate_search(store):
    assert isinstance(store, VectorStore)
    assert store.approximate is True
    assert store.configuration["approximate"] is True
    assert store.configuration["kind"] in {"s3vectors", "dynamodb"}


@pytest.mark.parametrize("store", STUBS, ids=["s3vectors", "dynamodb"])
def test_aws_store_stubs_are_design_only_and_raise_clear_errors(store):
    with pytest.raises(NotImplementedError, match="design-only stub.*in-memory"):
        store.upsert([VectorRow("a", "a", "hash", (1.0,))])
    with pytest.raises(NotImplementedError, match="design-only stub.*in-memory"):
        store.query([1.0], 5, label_filter="a")


def test_an_embedding_retriever_on_a_stub_store_records_it_in_its_fingerprint_and_cannot_index():
    task = DecisionTask("topic", ("a",), "Classify the target.")
    memory = EmbeddingRetriever.from_embedder(HashingEmbedder())
    s3 = EmbeddingRetriever.from_embedder(HashingEmbedder(), new_store=lambda: STUBS[0])

    assert s3.configuration["store"]["approximate"] is True
    assert s3.fingerprint != memory.fingerprint
    with pytest.raises(NotImplementedError):
        s3.index(task, [LabeledItem(Item("a-1", {"text": "x"}), "a")])
