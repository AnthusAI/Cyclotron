"""Catalog migration through immutable scoped bytes, never live cloud heads."""

import json
from dataclasses import replace

import pytest

from .batch_storage import StorageScope
from .catalog_archive import CatalogArchive
from .catalog_snapshot import encode
from .catalog_storage_test import populated
from .payload_index import DynamoPayloadIndex
from .payload_index_test import Dynamo
from .storage_payloads import FilePayloads, PayloadUnavailable, PayloadUncertain, S3Payloads
from .storage_payloads_test import S3
from .web_store import WebStore


@pytest.fixture(params=["file", "s3"])
def archive(request, tmp_path):
    scope = StorageScope("account-a", "workspace-a")
    payloads = (
        FilePayloads(tmp_path / "payloads", scope, max_bytes=1000000)
        if request.param == "file"
        else S3Payloads(S3(), "synthetic-bucket", "111111111111", scope, max_bytes=1000000)
    )
    return CatalogArchive(payloads, scope, max_records=100, max_bytes=1000000)


def test_canonical_records_roundtrip_exact_history_and_rebuildable_index(archive, tmp_path):
    source = populated(tmp_path / "source.sqlite")
    snapshot = source.catalog_storage.export()
    first = archive.publish(snapshot)
    assert archive.publish(snapshot) == first
    assert archive.read(first.manifest) == snapshot
    index = DynamoPayloadIndex(Dynamo(), "synthetic-index", archive.scope)
    assert index.rebuild(archive.payloads, [*first.records, first.manifest], max_records=100) == len(first.records) + 1
    destination = WebStore(tmp_path / "destination.sqlite")
    destination.catalog_storage.restore(archive.read(first.manifest))
    assert destination.catalog_storage.export() == snapshot
    assert destination.item_labels("papers", "one", 1) == []
    assert destination.item_labels("papers", "one", 2)[0]["comment"] == "Other revision explanation"
    assert not hasattr(archive, "activate") and not hasattr(index, "commit_catalog")


def test_manifest_scope_and_aggregate_budgets_are_checked_before_rows_are_read(archive, tmp_path):
    publication = archive.publish(populated(tmp_path / "source.sqlite").catalog_storage.export())
    with pytest.raises(PermissionError):
        archive.read(replace(publication.manifest, scope=StorageScope("foreign", "workspace-a")))
    bounded = CatalogArchive(archive.payloads, archive.scope, max_records=1, max_bytes=1000000)
    with pytest.raises(ValueError):
        bounded.read(publication.manifest)
    bounded = CatalogArchive(archive.payloads, archive.scope, max_records=100, max_bytes=100)
    with pytest.raises(ValueError):
        bounded.read(publication.manifest)


def test_changed_membership_or_missing_record_fails_despite_existing_index_hints(archive, tmp_path):
    publication = archive.publish(populated(tmp_path / "source.sqlite").catalog_storage.export())
    manifest = json.loads(archive.payloads.get(publication.manifest))
    manifest["records"].append(manifest["records"][0])
    tampered = archive.payloads.put(encode(manifest))
    with pytest.raises(ValueError):
        archive.read(tampered)
    missing = publication.records[0]
    if isinstance(archive.payloads, FilePayloads):
        archive.payloads._path(missing.digest).unlink()
    else:
        del archive.payloads.client.objects[archive.payloads._key(missing.digest)]
    with pytest.raises(PayloadUnavailable):
        archive.read(publication.manifest)


def test_lost_s3_row_publication_can_retry_same_catalog_without_selecting_a_head(tmp_path):
    client = S3()
    scope = StorageScope("a", "w")
    payloads = S3Payloads(client, "synthetic-bucket", "111111111111", scope, max_bytes=1000000)
    archive = CatalogArchive(payloads, scope, max_records=100, max_bytes=1000000)
    snapshot = populated(tmp_path / "source.sqlite").catalog_storage.export()
    client.lose_response = True
    with pytest.raises(PayloadUncertain):
        archive.publish(snapshot)
    assert len(client.objects) == 1
    client.lose_response = False
    publication = archive.publish(snapshot)
    assert archive.read(publication.manifest) == snapshot
    assert len(client.objects) == len(set(publication.records)) + 1


def test_empty_catalog_roundtrips_and_aggregate_limit_fails_before_publication(archive, tmp_path):
    snapshot = WebStore(tmp_path / "empty.sqlite").catalog_storage.export()
    publication = archive.publish(snapshot)
    assert publication.records == () and archive.read(publication.manifest) == snapshot
    snapshot = populated(tmp_path / "source.sqlite").catalog_storage.export()
    client = archive.payloads
    calls = []
    original = client.put

    def put(content):
        calls.append(content)
        return original(content)

    client.put = put
    limited = CatalogArchive(client, archive.scope, max_records=100, max_bytes=len(encode(snapshot)))
    with pytest.raises(ValueError, match="aggregate"):
        limited.publish(snapshot)
    assert calls == []
