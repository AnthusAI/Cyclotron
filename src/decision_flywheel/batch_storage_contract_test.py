"""Shared-request ledger behaviour that SQLite and S3 must share, run against both back ends."""

from uuid import uuid4

import pytest

from .batch_storage import BatchConflict, BatchInProgress
from .decision_cache import CacheMiss

FIRST_REQUEST = "a" * 64
SECOND_REQUEST = "b" * 64


@pytest.fixture
def storage(batch_backend):
    return batch_backend.open()


def settle_request(storage, fingerprint, status="complete", payload='{"ok":true}', maximum=10):
    owner = str(uuid4())
    storage.reserve(fingerprint, maximum, owner)
    storage.claim(fingerprint, owner)
    storage.settle(fingerprint, owner, status, payload)
    return owner


def test_a_reservation_starts_pending_and_unclaimed_with_one_charged_attempt(storage):
    owner = str(uuid4())
    record = storage.reserve(FIRST_REQUEST, 3, owner)
    assert (record.status, record.payload, record.generation, record.owner, record.claimed) == ("pending", None, 1, owner, False)
    assert storage.requests() == 1
    assert storage.get(FIRST_REQUEST) == record
    assert storage.get(SECOND_REQUEST) is None


def test_a_dispatch_claim_is_granted_once_to_the_current_owner(storage):
    owner, stranger = str(uuid4()), str(uuid4())
    storage.reserve(FIRST_REQUEST, 3, owner)
    assert storage.claim(FIRST_REQUEST, stranger) is False
    assert storage.claim(FIRST_REQUEST, owner) is True
    assert storage.claim(FIRST_REQUEST, owner) is False
    assert storage.get(FIRST_REQUEST).claimed is True


def test_replaying_the_same_operation_returns_its_record_without_a_second_charge(storage):
    owner = str(uuid4())
    first = storage.reserve(FIRST_REQUEST, 3, owner)
    assert storage.reserve(FIRST_REQUEST, 3, owner) == first
    assert storage.requests() == 1


def test_an_operation_cannot_be_reused_for_a_different_request(storage):
    owner = str(uuid4())
    storage.reserve(FIRST_REQUEST, 3, owner)
    with pytest.raises(BatchConflict):
        storage.reserve(SECOND_REQUEST, 3, owner)


def test_a_second_reservation_waits_while_the_first_is_pending(storage):
    storage.reserve(FIRST_REQUEST, 3, str(uuid4()))
    with pytest.raises(BatchInProgress):
        storage.reserve(FIRST_REQUEST, 3, str(uuid4()), retry_failed=True)


def test_completion_needs_a_claim_and_only_the_owner_may_settle(storage):
    owner = str(uuid4())
    storage.reserve(FIRST_REQUEST, 3, owner)
    with pytest.raises(BatchConflict):
        storage.settle(FIRST_REQUEST, owner, "complete", '{"ok":true}')
    storage.claim(FIRST_REQUEST, owner)
    with pytest.raises(BatchConflict):
        storage.settle(FIRST_REQUEST, str(uuid4()), "complete", '{"ok":true}')
    settled = storage.settle(FIRST_REQUEST, owner, "complete", '{"ok":true}')
    assert (settled.status, settled.payload) == ("complete", '{"ok":true}')
    assert storage.settle(FIRST_REQUEST, owner, "complete", '{"ok":true}') == settled
    with pytest.raises(BatchConflict):
        storage.settle(FIRST_REQUEST, owner, "failed")


def test_settlements_must_name_a_known_status_and_json_payload(storage):
    owner = str(uuid4())
    storage.reserve(FIRST_REQUEST, 3, owner)
    storage.claim(FIRST_REQUEST, owner)
    with pytest.raises(ValueError):
        storage.settle(FIRST_REQUEST, owner, "pending")
    with pytest.raises(ValueError):
        storage.settle(FIRST_REQUEST, owner, "complete")
    with pytest.raises(ValueError):
        storage.settle(FIRST_REQUEST, owner, "complete", "not json")
    assert storage.get(FIRST_REQUEST).status == "pending"


def test_requests_must_be_complete_fingerprints_and_uuid_operations(storage):
    with pytest.raises(ValueError):
        storage.reserve("short", 3, str(uuid4()))
    with pytest.raises(ValueError):
        storage.reserve(FIRST_REQUEST, 3, "not-a-uuid")
    with pytest.raises(ValueError):
        storage.reserve(FIRST_REQUEST, -1, str(uuid4()))
    with pytest.raises(ValueError):
        storage.reserve(FIRST_REQUEST, 3, str(uuid4()), policy="unknown")
    assert storage.requests() == 0


def test_a_completed_request_is_reused_and_refresh_starts_a_new_generation(storage):
    first_owner = settle_request(storage, FIRST_REQUEST, payload='{"v":1}')
    reused = storage.reserve(FIRST_REQUEST, 3, str(uuid4()))
    assert (reused.status, reused.payload, reused.generation) == ("complete", '{"v":1}', 1)
    assert storage.requests() == 1
    refresh_owner = str(uuid4())
    refreshed = storage.reserve(FIRST_REQUEST, 3, refresh_owner, policy="refresh")
    assert (refreshed.status, refreshed.generation, refreshed.owner) == ("pending", 2, refresh_owner)
    assert storage.requests() == 2
    replayed = storage.reserve(FIRST_REQUEST, 3, first_owner, policy="refresh")
    assert (replayed.payload, replayed.generation) == ('{"v":1}', 1)


def test_the_cache_only_policy_refuses_what_is_not_complete(storage):
    with pytest.raises(CacheMiss):
        storage.reserve(FIRST_REQUEST, 3, str(uuid4()), policy="cache_only")
    settle_request(storage, FIRST_REQUEST)
    assert storage.reserve(FIRST_REQUEST, 3, str(uuid4()), policy="cache_only").status == "complete"


def test_a_failed_request_needs_an_explicit_retry_and_the_ceiling_is_absolute(storage):
    settle_request(storage, FIRST_REQUEST, status="failed", payload=None)
    with pytest.raises(RuntimeError, match="explicit retry"):
        storage.reserve(FIRST_REQUEST, 3, str(uuid4()))
    retry = storage.reserve(FIRST_REQUEST, 3, str(uuid4()), retry_failed=True)
    assert (retry.status, retry.generation) == ("pending", 2)
    with pytest.raises(RuntimeError, match="ceiling"):
        storage.reserve(SECOND_REQUEST, 2, str(uuid4()))
    assert storage.requests() == 2


def test_abandoning_learning_lets_the_same_request_be_repeated_and_keeps_the_charge(storage):
    owner = str(uuid4())
    storage.reserve(FIRST_REQUEST, 5, owner)
    abandoned = storage.abandon_learning(FIRST_REQUEST, owner)
    assert (abandoned.status, abandoned.payload) == ("interrupted", '{"learning_abandoned":true}')
    repeated = storage.reserve(FIRST_REQUEST, 5, str(uuid4()))
    assert (repeated.status, repeated.generation) == ("pending", 2)
    assert storage.requests() == 2


def test_operation_marks_and_pending_operations_follow_the_attempt_log(storage):
    assert storage.operation_mark() == 0
    first_owner, second_owner = str(uuid4()), str(uuid4())
    storage.reserve(FIRST_REQUEST, 5, first_owner)
    storage.reserve(SECOND_REQUEST, 5, second_owner)
    assert storage.operation_mark() == 2
    assert sorted(storage.pending_operations()) == [(1, FIRST_REQUEST, first_owner), (2, SECOND_REQUEST, second_owner)]
    storage.claim(FIRST_REQUEST, first_owner)
    storage.settle(FIRST_REQUEST, first_owner, "complete", '{"ok":true}')
    assert storage.pending_operations() == [(2, SECOND_REQUEST, second_owner)]


def test_pages_walk_requests_in_fingerprint_order_with_a_continuation(storage):
    fingerprints = [character * 64 for character in "cabde"]
    for fingerprint in fingerprints:
        storage.reserve(fingerprint, 10, str(uuid4()))
    first_page, continuation = storage.page(limit=2)
    assert [record.fingerprint for record in first_page] == ["a" * 64, "b" * 64]
    second_page, continuation = storage.page(after=continuation, limit=2)
    assert [record.fingerprint for record in second_page] == ["c" * 64, "d" * 64]
    last_page, continuation = storage.page(after=continuation, limit=2)
    assert [record.fingerprint for record in last_page] == ["e" * 64] and continuation is None
    for bad_limit in (0, 1001):
        with pytest.raises(ValueError):
            storage.page(limit=bad_limit)


def test_state_survives_reopening_the_same_ledger(batch_backend):
    storage = batch_backend.open()
    owner = settle_request(storage, FIRST_REQUEST, payload='{"v":1}')
    reopened = batch_backend.reopen(storage)
    assert reopened.get(FIRST_REQUEST).payload == '{"v":1}'
    assert reopened.requests() == 1
    assert reopened.reserve(FIRST_REQUEST, 3, owner).payload == '{"v":1}'


def test_an_export_restores_into_an_empty_ledger_of_either_back_end(batch_backend, tmp_path):
    from .batch_storage import SQLiteBatchStorage
    from .s3_batch_storage import S3BatchStorage
    from .s3_journal_test import ConditionalS3, journal

    storage = batch_backend.open()
    settle_request(storage, FIRST_REQUEST, payload='{"v":1}')
    refresh_owner = str(uuid4())
    storage.reserve(FIRST_REQUEST, 5, refresh_owner, policy="refresh")
    snapshot = storage.export()
    sqlite_target = SQLiteBatchStorage(tmp_path / "restored.sqlite3")
    s3_target = S3BatchStorage(journal(ConditionalS3(), name="restored"), max_records=1000)
    s3_target.initialize()
    for target in (sqlite_target, s3_target):
        target.restore(snapshot)
        assert target.export() == snapshot
        assert target.get(FIRST_REQUEST).generation == 2
        with pytest.raises(BatchConflict):
            target.restore(snapshot)
    sqlite_target.close()


def test_an_export_that_exceeds_its_record_budget_is_refused(storage):
    settle_request(storage, FIRST_REQUEST)
    with pytest.raises(ValueError):
        storage.export(max_records=1)
    with pytest.raises(ValueError):
        storage.export(max_records=0)
