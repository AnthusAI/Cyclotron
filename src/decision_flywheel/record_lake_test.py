"""S3 key layout, create-only writes, lost responses, transient reads and coverage rules for the record lake."""

import pytest

from . import s3_transient
from .record_lake import (
    Coverage,
    FileRecordLake,
    MemoryRecordLake,
    RecordConflict,
    RecordCorrupt,
    RecordUnavailable,
    RecordUncertain,
    S3RecordLake,
    commit_with_records,
    coverage_in,
    with_coverage,
)
from .record_lake_contract_test import SCOPE, ListingS3
from .s3_journal_test import S3Error
from .s3_journal_test import journal as s3_journal
from .storage_payloads import FilePayloads


class Throttled(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "SlowDown"}, "ResponseMetadata": {"HTTPStatusCode": 503}}


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(s3_transient, "RETRY_PAUSE_SECONDS", 0)


def s3_lake(fake=None, kind="events", limit=2000):
    fake = fake or ListingS3()
    return fake, S3RecordLake(s3_journal(fake).payloads, "runtime", kind, max_record_bytes=limit)


def test_s3_records_live_under_the_scoped_records_prefix_and_are_written_create_only():
    fake, lake = s3_lake()
    lake.put(42, {"e": 1})
    (key,) = fake.objects
    assert key == "account-a/workspace-a/records/runtime/events/000000000042.json"
    (kind, request) = fake.calls[-1]
    assert kind == "put" and request["IfNoneMatch"] == "*" and "IfMatch" not in request


def test_the_file_layout_mirrors_the_s3_keys(tmp_path):
    lake = FileRecordLake(FilePayloads(tmp_path, SCOPE, max_bytes=1000), "runtime", "events", max_record_bytes=2000)
    lake.put(7, {"e": 1})
    assert (tmp_path / "account-a/workspace-a/records/runtime/events/000000000007.json").is_file()


def test_covered_reads_use_only_gets_so_they_need_no_list_permission():
    fake, lake = s3_lake()
    lake.append(Coverage(), [{"e": 1}, {"e": 2}])
    fake.calls.clear()
    assert lake.read_covered(Coverage(2)) == [(1, {"e": 1}), (2, {"e": 2})]
    assert {kind for kind, _ in fake.calls} == {"get"}


def test_a_lost_create_response_is_repaired_by_repeating_the_identical_record():
    fake, lake = s3_lake()
    fake.lose = lambda key: True
    with pytest.raises(RecordUncertain):
        lake.put(1, {"e": 1})
    ref = lake.put(1, {"e": 1})
    assert ref.seq == 1 and lake.read(1) == {"e": 1}


def test_a_failed_create_that_is_not_a_conflict_is_uncertain_not_a_skip():
    fake, lake = s3_lake()
    fake.before = lambda request: (_ for _ in ()).throw(S3Error("InternalError"))
    with pytest.raises(RecordUncertain):
        lake.append(Coverage(), [{"e": 1}])


def test_a_transient_get_failure_is_retried_once_and_other_failures_are_not():
    fake, lake = s3_lake()
    lake.put(1, {"e": 1})
    fake.flaky_gets = [Throttled()]
    assert lake.read(1) == {"e": 1}
    fake.flaky_gets = [Throttled(), Throttled()]
    with pytest.raises(RecordUnavailable):
        lake.read(1)
    fake.flaky_gets = [S3Error("AccessDenied"), Throttled()]
    with pytest.raises(RecordUnavailable):
        lake.read(1)
    assert len(fake.flaky_gets) == 1


def test_a_failure_message_names_only_the_exception_class():
    fake, lake = s3_lake()
    lake.put(1, {"e": 1})
    fake.flaky_gets = [RuntimeError("https://bucket/secret?X-Amz-Signature=abc")] * 2
    with pytest.raises(RecordUnavailable) as raised:
        lake.read(1)
    assert "secret" not in str(raised.value) and raised.value.cause == "RuntimeError"


def test_s3_listing_ignores_foreign_keys_and_pages_with_a_limit():
    fake, lake = s3_lake()
    for seq in (1, 2, 3, 4, 5, 6, 7):
        lake.put(seq, {"n": seq})
    fake.objects[lake.namespace + "/notes.txt"] = (b"x", '"e"')
    assert lake.sequences() == [1, 2, 3, 4, 5, 6, 7]
    assert lake.sequences(after=2, limit=4) == [3, 4, 5, 6]


def test_a_record_moved_to_another_position_is_corrupt_not_silently_read():
    fake, lake = s3_lake()
    lake.put(1, {"e": 1})
    fake.objects[lake._key(2)] = fake.objects[lake._key(1)]
    with pytest.raises(RecordCorrupt):
        lake.read(2)


def test_a_record_for_another_kind_is_corrupt_when_copied_into_this_one():
    fake, events = s3_lake()
    events.put(1, {"e": 1})
    _, answers = s3_lake(fake, "answers")
    fake.objects[answers._key(1)] = fake.objects[events._key(1)]
    with pytest.raises(RecordCorrupt):
        answers.read(1)


def test_a_real_sdk_client_must_not_retry_hidden_writes():
    class Config:
        retries = {"total_max_attempts": 5}

    class Meta:
        config = Config()

    fake, lake = s3_lake()
    fake.meta = Meta()
    with pytest.raises(ValueError):
        S3RecordLake(lake_payloads(fake), "runtime", "events", max_record_bytes=100)


def lake_payloads(fake):
    return s3_journal(fake).payloads


def test_non_json_values_are_refused():
    lake = MemoryRecordLake(SCOPE, "runtime", "events", max_record_bytes=2000)
    with pytest.raises(ValueError):
        lake.put(1, {"x": float("nan")})


def test_a_file_lake_ignores_leftover_temporary_files(tmp_path):
    lake = FileRecordLake(FilePayloads(tmp_path, SCOPE, max_bytes=1000), "runtime", "events", max_record_bytes=2000)
    lake.put(1, {"e": 1})
    (lake.directory / "tmp-leftover").write_bytes(b"half")
    assert lake.sequences() == [1]


@pytest.mark.parametrize(
    "bad",
    [
        {"last": -1, "skipped": []},
        {"last": 3, "skipped": [3]},
        {"last": 5, "skipped": [4, 2]},
        {"last": 5, "skipped": [2, 2]},
        {"last": "3", "skipped": []},
        {"last": 3},
        {"last": 3, "skipped": [], "extra": 1},
    ],
)
def test_malformed_coverage_is_refused(bad):
    with pytest.raises(ValueError):
        Coverage.from_json(bad)


def test_coverage_counts_only_unskipped_sequences_up_to_last():
    coverage = Coverage(6, (2, 4))
    assert [s for s in range(0, 9) if coverage.covers(s)] == [1, 3, 5, 6]
    assert coverage.sequences() == [1, 3, 5, 6] and coverage.sequences(after=3, limit=1) == [5]
    assert Coverage.from_json(coverage.to_json()) == coverage


def test_coverage_for_each_lake_is_kept_apart_inside_one_control_state():
    events = MemoryRecordLake(SCOPE, "runtime", "events", max_record_bytes=100)
    answers = MemoryRecordLake(SCOPE, "runtime", "answers", max_record_bytes=100)
    state = with_coverage({"owner": "w1"}, events, Coverage(3))
    assert coverage_in(state, events) == Coverage(3) and coverage_in(state, answers) == Coverage()
    assert state["owner"] == "w1"


def test_one_commit_can_cover_two_lakes_and_cite_their_records_in_the_small_state():
    fake = ListingS3()
    journal = s3_journal(fake)
    events = S3RecordLake(journal.payloads, "runtime", "events", max_record_bytes=2000)
    answers = S3RecordLake(journal.payloads, "runtime", "answers", max_record_bytes=2000)
    head = journal.initialize({"paid_calls": 0})

    def note(state, refs):
        state["paid_calls"] += 1
        state["last_answer"] = refs["runtime/answers"][0].digest

    record, refs = commit_with_records(journal, head, [(events, [{"e": 1}]), (answers, [{"raw": "reply"}])], note)
    assert record.value["last_answer"] == refs["runtime/answers"][0].digest
    assert coverage_in(record.value, events).last == 1 and coverage_in(record.value, answers).last == 1
    # The control state holds ids and counters only, never the record bodies.
    assert "reply" not in str(record.value)
    with pytest.raises(ValueError):
        commit_with_records(journal, record, [(events, [{"e": 2}]), (events, [{"e": 3}])])


def test_a_state_that_is_not_an_object_cannot_carry_coverage():
    fake = ListingS3()
    journal = s3_journal(fake)
    lake = S3RecordLake(journal.payloads, "runtime", "events", max_record_bytes=2000)
    with pytest.raises(ValueError):
        commit_with_records(journal, journal.initialize([1, 2]), [(lake, [{"e": 1}])])


def test_two_conflicting_creates_surface_as_record_conflict_for_the_shared_storage_family():
    from .storage_errors import StorageConflict

    assert issubclass(RecordConflict, StorageConflict)
