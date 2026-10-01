"""Design-only ``VectorStore`` stubs for AWS. Not built in this phase.

At our scale (about 5k vectors) the in-memory store is strictly better: exact,
free and offline. These classes exist so the configuration and the interface
are settled; every operation raises ``NotImplementedError``. They import no AWS
SDK and make no calls. Both would be approximate (recorded in fingerprints) and
eventually consistent right after writes, which can break exact replay.

AWS facts were checked on 2026-10-01 against official pages (see
DECISION_FLYWHEEL_DESIGN.md, Appendix B). Pricing came from a summarizing fetch,
and some items are unverified: re-check before building.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .retrieval import VectorRow


@dataclass(frozen=True)
class S3VectorsStore:
    """Amazon S3 Vectors adapter design (GA 2025-12-02; API ``s3vectors-2025-07-15``).

    Planned mapping:

    - One vector bucket and one vector index per pool revision. ``CreateIndex``
      with ``dataType="float32"``, ``dimension`` (1-4096) and
      ``distanceMetric="cosine"``. Filterable metadata: ``label``,
      ``pool_revision``, ``input_hash`` (never text).
    - ``upsert`` -> ``PutVectors`` in batches of at most 500 vectors.
    - ``query`` -> ``QueryVectors(queryVector, topK=k+1, filter={"label": L},
      returnDistance=True)``, paginated at 100; target exclusion stays client-side
      in ``EmbeddingIndex``. Cosine *distance* must be converted to similarity.
    - Client: ``boto3.client("s3vectors")`` (minimum boto3 version not found).
    - IAM: ``s3vectors:*`` actions; filtered or metadata queries also need
      ``s3vectors:GetVectors``.
    - Unverified: ``CreateVectorBucket``; pricing; GovCloud availability.
    """

    vector_bucket: str
    index_name: str
    region: str
    approximate = True

    @property
    def configuration(self) -> Mapping[str, object]:
        return {"kind": "s3vectors", "approximate": True, "vector_bucket": self.vector_bucket,
                "index_name": self.index_name, "region": self.region}

    def upsert(self, rows: Sequence[VectorRow]) -> None:
        raise NotImplementedError(
            "S3VectorsStore is a design-only stub: PutVectors (batches of <=500) is not implemented. "
            "Use the default in-memory store (store.kind: memory).")

    def query(self, vector, top_k, label_filter=None):
        raise NotImplementedError(
            "S3VectorsStore is a design-only stub: QueryVectors with a label filter is not implemented. "
            "Use the default in-memory store (store.kind: memory).")


@dataclass(frozen=True)
class DynamoDBVectorStore:
    """DynamoDB vector search adapter design (GA announced 2026-08-05).

    Planned mapping:

    - An on-demand table with a vector index (``CreateTable`` ``VectorIndexes`` or
      ``UpdateTable`` ``VectorIndexUpdates``): ``VectorAttribute``,
      ``Dimensions`` (<=4096), ``DistanceFunction="COSINE"`` and ``label`` as an
      ``INLINE_FILTER`` in the ``SearchSchema``. Vectors are an ``L`` list of
      ``N``; each new index needs a backfill.
    - ``upsert`` -> ``PutItem``/``BatchWriteItem`` with ``id``, ``label``,
      ``input_hash`` and the vector attribute (never text).
    - ``query`` -> ``SearchVectors(TableName, IndexName, SearchVector,
      TopK<=100, SearchConditionExpression="label = :L")`` on the separate search
      endpoint ``{account}.search-ddb.{region}.amazonaws.com``. Results are
      eventually consistent, unpaginated (16 MB) and approximate.
    - IAM: ``dynamodb:SearchVectors`` (plus normal write actions); no
      fine-grained access control.
    - Unverified: minimum SDK version for ``search_vectors``; pricing.
    """

    table_name: str
    index_name: str
    region: str
    approximate = True

    @property
    def configuration(self) -> Mapping[str, object]:
        return {"kind": "dynamodb", "approximate": True, "table_name": self.table_name,
                "index_name": self.index_name, "region": self.region}

    def upsert(self, rows: Sequence[VectorRow]) -> None:
        raise NotImplementedError(
            "DynamoDBVectorStore is a design-only stub: writing vector items is not implemented. "
            "Use the default in-memory store (store.kind: memory).")

    def query(self, vector, top_k, label_filter=None):
        raise NotImplementedError(
            "DynamoDBVectorStore is a design-only stub: SearchVectors is not implemented. "
            "Use the default in-memory store (store.kind: memory).")
