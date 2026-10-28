"""A keyed document is one small object per key whose entries count only once the journal covers their stamp."""

import pytest

from .batch_storage import StorageScope
from .keyed_records import FileKeyedDocuments, KeyedUncertain, S3KeyedDocuments, merged, resolve
from .record_lake import Coverage
from .s3_journal_test import ConditionalS3, S3Error
from .storage_payloads import FilePayloads, S3Payloads

SCOPE = StorageScope("account-a", "workspace-a")
PREFIX = "account-a/workspace-a/coordination/workspace/root"


@pytest.fixture(params=["s3", "file"])
def documents(request, tmp_path):
    if request.param == "s3":
        return S3KeyedDocuments(S3Payloads(ConditionalS3(), "synthetic-bucket", "111111111111", SCOPE), PREFIX)
    return FileKeyedDocuments(FilePayloads(tmp_path, SCOPE), PREFIX)


def test_an_entry_counts_only_once_the_coverage_vouches_for_its_stamp(documents):
    assert documents.load("item", "a") is None
    written = documents.store("item", "a", None, Coverage(), 1, {"v": 1})
    assert resolve(written, Coverage()) is None
    assert resolve(documents.load("item", "a"), Coverage(1)) == {"v": 1}


def test_an_abandoned_entry_stays_invisible_and_is_dropped_by_the_next_write(documents):
    first = documents.store("item", "a", None, Coverage(), 1, {"v": 1})
    # Stamp 2 was abandoned: its head never moved, and the next commit skipped that sequence.
    abandoned = documents.store("item", "a", first, Coverage(1), 2, {"v": "lost"})
    covered = Coverage(3, (2,))
    assert resolve(abandoned, covered) == {"v": 1}
    later = documents.store("item", "a", abandoned, covered, 4, {"v": 4})
    assert [at for at, _ in later.entries] == [1, 4]
    assert resolve(documents.load("item", "a"), Coverage(4, (2,))) == {"v": 4}


def test_an_entry_still_in_flight_above_the_coverage_is_kept_for_its_writer(documents):
    first = documents.store("item", "a", None, Coverage(), 1, {"v": 1})
    stale = documents.store("item", "a", first, Coverage(1), 2, {"v": "fenced-out writer"})
    mine = documents.store("item", "a", stale, Coverage(1), 3, {"v": 3})
    assert [at for at, _ in mine.entries] == [1, 2, 3]


def test_a_document_changed_since_it_was_read_is_merged_rather_than_overwritten(documents):
    first = documents.store("item", "a", None, Coverage(), 1, {"v": 1})
    documents.store("item", "a", first, Coverage(1), 2, {"v": "other writer"})
    mine = documents.store("item", "a", first, Coverage(1), 3, {"v": 3})
    assert [at for at, _ in mine.entries] == [1, 2, 3]


def test_the_merge_keeps_the_newest_covered_entry_and_the_ones_above_the_coverage():
    class Held:
        entries = ((1, "a"), (2, "b"), (3, "orphan"), (5, "in flight"))

    assert merged(Held, Coverage(4, (3,)), 6, "new") == ((2, "b"), (5, "in flight"), (6, "new"))


def test_without_list_permission_a_refused_read_is_settled_by_a_create_only_empty_document():
    client = ConditionalS3()
    real = client.get_object

    def refusing(**request):
        if request["Key"] not in client.objects:
            client.calls.append(("get", request))
            raise S3Error("AccessDenied")
        return real(**request)

    client.get_object = refusing
    documents = S3KeyedDocuments(S3Payloads(client, "synthetic-bucket", "111111111111", SCOPE), PREFIX)
    absent = documents.load("item", "a")
    assert absent is not None and absent.entries == () and resolve(absent, Coverage(9)) is None
    written = documents.store("item", "a", absent, Coverage(), 1, {"v": 1})
    assert resolve(documents.load("item", "a"), Coverage(1)) == {"v": 1} and written.entries == ((1, {"v": 1}),)


def test_a_refused_create_is_an_error_never_an_absent_key():
    client = ConditionalS3()

    def refused(**request):
        raise S3Error("AccessDenied")

    client.get_object = refused
    client.put_object = refused
    documents = S3KeyedDocuments(S3Payloads(client, "synthetic-bucket", "111111111111", SCOPE), PREFIX)
    with pytest.raises(KeyedUncertain):
        documents.load("item", "a")
