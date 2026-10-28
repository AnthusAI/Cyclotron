"""Record-lake behaviour every back end must share: memory, local files and the synthetic S3 client."""

import pytest

from .batch_storage import StorageScope
from .file_journal import LocalFileJournal
from .journal import JournalConflict
from .record_lake import (
    Coverage,
    FileRecordLake,
    MemoryRecordLake,
    RecordConflict,
    RecordLake,
    RecordUnavailable,
    S3RecordLake,
    commit_with_records,
    coverage_in,
)
from .s3_journal_test import ConditionalS3
from .s3_journal_test import journal as s3_journal
from .storage_payloads import FilePayloads

SCOPE = StorageScope("account-a", "workspace-a")
# Larger than every byte budget the storage layer used to enforce (4 MiB per reply record and journal).
LARGE = "x" * (5 * 1024 * 1024)


class ListingS3(ConditionalS3):
    """The conditional-write fake plus paged listing, and optional scripted GET failures."""

    page_size = 3

    def __init__(self):
        super().__init__()
        self.flaky_gets = []

    def get_object(self, **request):
        if self.flaky_gets:
            raise self.flaky_gets.pop(0)
        return super().get_object(**request)

    def list_objects_v2(self, **request):
        assert request["ExpectedBucketOwner"] == "111111111111"
        keys = sorted(k for k in self.objects if k.startswith(request["Prefix"]))
        start = request.get("ContinuationToken") or request.get("StartAfter") or ""
        keys = [k for k in keys if k > start]
        page = keys[: self.page_size]
        truncated = len(keys) > self.page_size
        return {
            "Contents": [{"Key": k} for k in page],
            "IsTruncated": truncated,
            **({"NextContinuationToken": page[-1]} if truncated else {}),
        }


@pytest.fixture(params=["memory", "file", "s3"])
def world(request, tmp_path):
    """Opens the control journal and lakes over one shared underlying store."""
    fake = ListingS3()
    memory_store = {}

    class World:
        backend = request.param

        def journal(self, name="run-a"):
            if request.param == "s3":
                return s3_journal(fake, SCOPE, name)
            payloads = FilePayloads(tmp_path, SCOPE)
            return LocalFileJournal(payloads, "runtime", name)

        def lake(self, kind="events"):
            if request.param == "memory":
                return MemoryRecordLake(SCOPE, "runtime", kind, store=memory_store)
            if request.param == "file":
                return FileRecordLake(FilePayloads(tmp_path, SCOPE), "runtime", kind)
            return S3RecordLake(self.journal().payloads, "runtime", kind)

        def stored(self, kind="events"):
            """Raw bytes by sequence, straight from the underlying store."""
            lake = self.lake(kind)
            return {seq: lake._fetch(seq) for seq in lake.sequences()}

    return World()


def test_every_back_end_is_a_record_lake(world):
    assert isinstance(world.lake(), RecordLake)


def test_creating_a_sequence_twice_with_different_content_conflicts_and_keeps_the_original(world):
    lake = world.lake()
    first = lake.put(1, {"text": "original"})
    with pytest.raises(RecordConflict):
        lake.put(1, {"text": "different"})
    assert lake.read(1) == {"text": "original"}
    assert lake.put(1, {"text": "original"}) == first


def test_listing_is_in_sequence_order_whatever_the_write_order_and_pages_across_requests(world):
    lake = world.lake()
    for seq in (5, 2, 9, 1, 7, 3, 4):
        lake.put(seq, {"n": seq})
    assert lake.sequences() == [1, 2, 3, 4, 5, 7, 9]
    assert lake.sequences(after=3) == [4, 5, 7, 9]
    assert lake.sequences(after=1, limit=3) == [2, 3, 4]
    assert lake.sequences(limit=0) == []


def test_kinds_are_separate_collections(world):
    world.lake("events").put(1, {"a": 1})
    assert world.lake("answers").sequences() == []


def test_a_record_larger_than_the_old_four_mib_record_budget_is_written_and_reads_back(world):
    lake = world.lake()
    ref = lake.put(1, {"text": LARGE})
    assert ref.length > 5 * 1024 * 1024 and len(world.stored()[1]) == ref.length
    assert world.lake().read(1) == {"text": LARGE}


def test_a_lake_has_no_per_record_byte_budget_to_configure(world):
    assert not hasattr(world.lake(), "max_record_bytes")


def test_a_non_json_record_in_a_batch_stops_the_whole_batch_before_any_write(world):
    lake = world.lake()
    with pytest.raises(ValueError):
        lake.append(Coverage(), [{"text": "ok"}, {"value": float("nan")}])
    assert lake.sequences() == []


def test_records_written_without_a_head_commit_are_invisible_orphans(world):
    journal, lake = world.journal(), world.lake()
    head = journal.initialize({"status": "idle"})
    coverage, _ = lake.append(coverage_in(head.value, lake), [{"e": 1}, {"e": 2}])
    assert lake.sequences() == [1, 2] and coverage.last == 2
    current = journal.read()
    assert coverage_in(current.value, lake) == Coverage()
    assert lake.read_covered(coverage_in(current.value, lake)) == []
    assert lake.orphans(coverage_in(current.value, lake)) == [1, 2]


def test_a_commit_makes_the_records_and_the_state_change_visible_together(world):
    journal, lake = world.journal(), world.lake()
    head = journal.initialize({"status": "idle"})
    record, refs = commit_with_records(
        journal,
        head,
        [(lake, [{"e": 1}, {"e": 2}])],
        lambda state, made: state.update(status="busy", first=made["runtime/events"][0].seq),
    )
    seen = journal.read()
    assert seen.value["status"] == "busy" and seen.value["first"] == 1
    assert lake.read_covered(coverage_in(seen.value, lake)) == [(1, {"e": 1}), (2, {"e": 2})]
    assert [ref.seq for ref in refs["runtime/events"]] == [1, 2] and record.revision == 2


def test_a_lost_head_race_leaves_no_visible_change_and_the_loser_records_become_orphans(world):
    journal, lake = world.journal(), world.lake()
    head = journal.initialize({"status": "idle"})
    journal.commit(head, {"status": "taken-by-someone-else"})
    with pytest.raises(JournalConflict):
        commit_with_records(journal, head, [(lake, [{"e": "lost"}])], lambda state, made: state.update(status="mine"))
    seen = journal.read()
    assert seen.value == {"status": "taken-by-someone-else"}
    assert lake.read_covered(coverage_in(seen.value, lake)) == []
    assert lake.orphans(coverage_in(seen.value, lake)) == [1]


def test_a_crash_between_writing_records_and_flipping_the_head_changes_nothing_a_reader_sees(world):
    journal, lake = world.journal(), world.lake()
    head = journal.initialize({"status": "idle"})

    class Crash(Exception):
        pass

    def crash(*_):
        raise Crash()

    journal.commit = crash
    with pytest.raises(Crash):
        commit_with_records(journal, head, [(lake, [{"e": 1}])])
    assert lake.sequences() == [1]
    assert world.journal().read().value == {"status": "idle"}


def test_a_control_state_larger_than_the_old_journal_budget_commits_and_its_records_become_visible(world):
    journal, lake = world.journal(), world.lake()
    head = journal.initialize({"status": "idle"})
    commit_with_records(journal, head, [(lake, [{"e": 1}])], lambda state, made: state.update(big=LARGE))
    seen = world.journal().read()
    assert seen.value["big"] == LARGE
    assert lake.read_covered(coverage_in(seen.value, lake)) == [(1, {"e": 1})]


def test_a_retry_after_an_abandoned_attempt_adopts_an_identical_record_or_skips_a_different_one(world):
    journal, lake = world.journal(), world.lake()
    head = journal.initialize({})
    lake.put(1, {"e": "abandoned-same"})
    lake.put(2, {"e": "abandoned-other"})
    record, refs = commit_with_records(journal, head, [(lake, [{"e": "abandoned-same"}, {"e": "fresh"}])])
    coverage = coverage_in(record.value, lake)
    assert coverage == Coverage(3, (2,))
    assert lake.read_covered(coverage) == [(1, {"e": "abandoned-same"}), (3, {"e": "fresh"})]
    assert lake.orphans(coverage) == [2]


def test_a_reader_holding_an_older_head_keeps_a_stable_view_while_the_head_moves(world):
    journal, lake = world.journal(), world.lake()
    first, _ = commit_with_records(journal, journal.initialize({"n": 0}), [(lake, [{"e": 1}])])
    old_view = coverage_in(first.value, lake)
    second, _ = commit_with_records(journal, first, [(lake, [{"e": 2}, {"e": 3}])])
    assert lake.read_covered(old_view) == [(1, {"e": 1})]
    assert [seq for seq, _ in lake.read_covered(coverage_in(second.value, lake))] == [1, 2, 3]
    assert lake.read_covered(coverage_in(second.value, lake), after=1, limit=1) == [(2, {"e": 2})]


def test_two_writers_racing_for_the_same_sequences_never_both_become_visible(world):
    journal = world.journal()
    head = journal.initialize({})
    one, two = world.lake(), world.lake()
    commit_with_records(journal, head, [(one, [{"w": "one"}])])
    with pytest.raises(JournalConflict):
        commit_with_records(journal, head, [(two, [{"w": "two"}])])
    seen = coverage_in(journal.read().value, one)
    assert one.read_covered(seen) == [(1, {"w": "one"})]
    assert two.orphans(seen) == [2]


def test_a_covered_record_that_has_gone_missing_fails_closed(world):
    journal, lake = world.journal(), world.lake()
    record, _ = commit_with_records(journal, journal.initialize({}), [(lake, [{"e": 1}])])
    with pytest.raises(RecordUnavailable):
        lake.read_covered(Coverage(2))


def test_any_number_of_occupied_sequences_are_skipped_and_the_fresh_record_takes_the_next_one(world):
    # More orphans than the old skip (64) and per-record collision (16) budgets ever allowed.
    lake = world.lake()
    for seq in range(1, 101):
        lake.put(seq, {"orphan": seq})
    coverage, refs = lake.append(Coverage(), [{"fresh": 1}])
    assert coverage == Coverage(101, tuple(range(1, 101))) and [ref.seq for ref in refs] == [101]
    assert lake.read_covered(coverage) == [(101, {"fresh": 1})]


def test_a_foreign_scope_lake_cannot_join_a_journal_commit(world):
    journal = world.journal()
    foreign = MemoryRecordLake(StorageScope("other", "workspace-a"), "runtime", "events")
    with pytest.raises(PermissionError):
        commit_with_records(journal, journal.initialize({}), [(foreign, [{"e": 1}])])
