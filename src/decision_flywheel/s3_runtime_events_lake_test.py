"""Runtime trace events live in a write-once record lake; the runtime journal keeps only control state."""

import json
from uuid import uuid4

import pytest

from .journal import encode
from .record_lake import MemoryRecordLake, coverage_in
from .s3_journal_test import ConditionalS3, journal
from .s3_runtime_storage import RuntimeOwnershipError, S3RuntimeStores, events_kind, runtime_events_lake


def open_stores(client=None, name="classifier-a", *, lake=True, acquire=True, **options):
    client = client or ConditionalS3()
    j = journal(client, name=name)
    result = S3RuntimeStores(j, cache_owned=True, events_lake=runtime_events_lake(j) if lake else None, **options)
    result.initialize()
    if acquire:
        result.acquire(str(uuid4()))
    return result


def head(s):
    return s.journal.read()


def record_puts(client):
    return [r for kind, r in client.calls if kind == "put" and "/records/" in r["Key"]]


def record_gets(client):
    return [r for kind, r in client.calls if kind == "get" and "/records/" in r["Key"]]


def doc_bytes(s):
    return len(encode(head(s).value))


# --- The runtime document stays small -------------------------------------------------------------------


def decide(s, n):
    """What one decided item costs a runtime: seven trace events (about 3 KB each) around a reservation and a completion."""
    key = f"answer-{n}"

    def event(kind):
        s.runtime_storage.append_event(json.dumps({"kind": kind, "n": n, "detail": "y" * 3000}))

    event("item"), event("request")
    s.runtime_cache.begin_answer(key, None)
    event("dispatch"), event("response"), event("parsed")
    s.runtime_cache.complete_answer(key, '{"label":"yes"}', previous=None)
    event("decision"), event("done")


def growth(lake, items=30, coalesce=True):
    client = ConditionalS3()
    s = open_stores(client, lake=lake, coalesce_events=coalesce)
    sizes, written = [], []
    for n in range(items):
        before = len(client.calls)
        decide(s, n)
        puts = [r for kind, r in client.calls[before:] if kind == "put"]
        sizes.append(doc_bytes(s))
        written.append(sum(len(r["Body"]) for r in puts if "/payloads/" in r["Key"]))
    s.close()
    return sizes, written, client


def chunk_events(client):
    """Every event published, in order, from the chunk records written (one record per commit)."""
    return [p for r in record_puts(client) for p in json.loads(r["Body"])["value"]["events"]]


def test_the_runtime_document_does_not_grow_with_decided_items_and_each_commit_writes_one_event_record(capsys):
    embedded, embedded_written, _ = growth(lake=False)
    lake, lake_written, client = growth(lake=True)
    per_item = lambda sizes, a, b: (sizes[b] - sizes[a]) / (b - a)  # noqa: E731
    with capsys.disabled():
        print(
            f"\nruntime journal bytes per decided item (30 items): document growth "
            f"{per_item(embedded, 10, 29):.0f} -> {per_item(lake, 10, 29):.0f}; "
            f"bytes written to the journal per item {sum(embedded_written[10:]) / 20:.0f} -> {sum(lake_written[10:]) / 20:.0f}; "
            f"document after 30 items {embedded[-1]} -> {lake[-1]}"
        )
    assert per_item(embedded, 10, 29) > 20000  # every event used to be copied into the document
    assert per_item(lake, 10, 29) < 2  # answers live in a trie; only the coverage numbers' digits can grow
    assert lake[-1] < 3000 < embedded[-1] / 20
    assert sum(lake_written[10:]) < sum(embedded_written[10:]) / 20
    # Nothing was lost: every event is in the chunk of the commit that published it, one record per commit
    # (each item's reservation, then the release).
    assert len(chunk_events(client)) == 30 * 7 and len(record_puts(client)) == 30 + 1
    assert all(r["IfNoneMatch"] == "*" for r in record_puts(client))


def test_events_leave_the_document_and_the_decision_commit_carries_the_coverage():
    client = ConditionalS3()
    s = open_stores(client)
    s.runtime_storage.append_event('{"kind":"a"}')
    s.runtime_storage.append_event('{"kind":"b"}')
    s.runtime_cache.begin_answer("k", None)
    stored = head(s).value
    assert stored["data"]["events"] == [] and stored["data"]["answers"]["k"][0] == "pending"
    assert coverage_in(stored, s.events_lake).last == 1 and stored["chunks"] == {"from": 1, "last": 2}
    s.close()


# --- Ordering, pagination and bounded reads --------------------------------------------------------------


def seeded(count=250, per=10):
    """``count`` events published ``per`` to a commit, so ``count / per`` chunk records."""
    client = ConditionalS3()
    s = open_stores(client)
    for n in range(1, count + 1):
        s.runtime_storage.append_event(json.dumps({"kind": "step", "n": n}))
        if n % per == 0:
            s.flush()
    s.close()
    reader = open_stores(client)  # a new handle: nothing cached
    client.calls.clear()
    return client, reader


def test_a_trace_page_reads_only_the_chunk_holding_the_events_it_returns():
    client, reader = seeded()
    page = reader.runtime_storage.trace_events(after_event_id=240, limit=5)
    assert [e["event_id"] for e in page["events"]] == [241, 242, 243, 244, 245] and page["cursor"] == 245
    assert len(record_gets(client)) == 1


def test_an_old_trace_cursor_finds_its_chunk_with_a_logarithmic_number_of_reads():
    client, reader = seeded(2000)
    page = reader.runtime_storage.trace_events(after_event_id=37, limit=20)
    assert [e["event_id"] for e in page["events"]] == list(range(38, 58))
    assert len(record_gets(client)) <= 2 * 8 + 3  # galloping back from the newest of 200 chunks, then a search


def test_the_latest_history_reads_only_the_latest_chunk_and_keeps_their_order():
    client, reader = seeded()
    assert [e["event_id"] for e in reader.runtime_storage.history(3)] == [248, 249, 250]
    assert len(record_gets(client)) == 1
    assert [e["event_id"] for e in reader.runtime_storage.history(12)] == list(range(239, 251))
    assert len(record_gets(client)) == 2


def test_a_full_scan_pages_through_every_event_in_order_then_reuses_what_it_read():
    client, reader = seeded()
    assert [e["n"] for e in reader.runtime_storage.events_of(("step",))] == list(range(1, 251))
    assert len(record_gets(client)) == 25
    reader.runtime_storage.events_of(("step",))
    assert len(record_gets(client)) == 25  # immutable records are kept by the handle


def test_event_payloads_are_read_lazily_a_chunk_at_a_time():
    client, reader = seeded()
    payloads = reader.runtime_storage.event_payloads(("step",))
    assert record_gets(client) == []
    first = next(payloads)
    assert json.loads(first)["n"] == 1 and len(record_gets(client)) == 1


def test_ids_stay_dense_and_ordered_when_a_sequence_was_skipped():
    client = ConditionalS3()
    s = open_stores(client)
    s.events_lake.put(1, "squatter")  # an abandoned write already occupies sequence 1
    ids = [s.runtime_storage.append_event(json.dumps({"kind": "step", "n": n})) for n in (1, 2, 3)]
    assert ids == [1, 2, 3]
    s.flush()
    assert coverage_in(head(s).value, s.events_lake).skipped == (1,)
    assert [e["n"] for e in s.runtime_storage.history(-1)] == [1, 2, 3]
    assert [e["n"] for e in s.runtime_storage.trace_events(after_event_id=1)["events"]] == [2, 3]
    assert [e["event_id"] for e in s.runtime_storage.history(2)] == [2, 3]
    s.close()


def test_a_missing_covered_record_fails_closed():
    client, reader = seeded(3, per=1)
    del client.objects[next(k for k in client.objects if k.endswith("000000000002.json"))]
    with pytest.raises(Exception, match="record missing"):
        reader.runtime_storage.history(-1)


def test_feedback_events_still_drive_the_active_labels_after_a_reopen():
    from .runtime_storage_contract_test import feedback_event

    client = ConditionalS3()
    s = open_stores(client)
    s.runtime_storage.append_event(feedback_event("item-1", "yes"))
    s.runtime_storage.append_event(feedback_event("item-1", "no"))
    s.close()
    again = open_stores(client)
    assert [e["feedback"]["final_answer_value"] for e in again.runtime_storage.active_labels()] == ["no"]
    again.close()


# --- Events already embedded in an existing runtime document ------------------------------------------------


def legacy_runtime(client, count=3):
    old = open_stores(client, lake=False)
    for n in range(1, count + 1):
        old.runtime_storage.append_event(json.dumps({"kind": "old", "n": n, "cycle_number": n}))
    old.close()


def test_events_embedded_in_an_existing_document_read_as_a_prefix_before_the_lake():
    client = ConditionalS3()
    legacy_runtime(client)
    s = open_stores(client)
    assert [e["event_id"] for e in s.runtime_storage.history(-1)] == [1, 2, 3]
    assert s.runtime_storage.append_event('{"kind":"new","n":4}') == 4
    s.runtime_storage.append_event('{"kind":"new","n":5}')
    assert [(e["kind"], e["event_id"]) for e in s.runtime_storage.history(-1)] == [
        ("old", 1),
        ("old", 2),
        ("old", 3),
        ("new", 4),
        ("new", 5),
    ]
    assert [e["event_id"] for e in s.runtime_storage.trace_events(after_event_id=2, limit=2)["events"]] == [3, 4]
    assert [e["event_id"] for e in s.runtime_storage.history(3)] == [3, 4, 5]
    assert [e["kind"] for e in s.runtime_storage.events_of(("new",))] == ["new", "new"]
    s.close()
    stored = head(s).value
    assert len(stored["data"]["events"]) == 3  # the embedded prefix is left exactly as it was
    assert coverage_in(stored, s.events_lake).last == 1 and stored["chunks"] == {"from": 1, "last": 5}


def test_cycle_numbers_continue_across_the_legacy_prefix_and_the_lake():
    client = ConditionalS3()
    legacy_runtime(client)
    s = open_stores(client)
    assert s.runtime_storage.next_cycle_number() == 4
    s.runtime_storage.append_event('{"kind":"cycle-started","cycle_number":4}')
    assert s.runtime_storage.next_cycle_number() == 5
    s.close()


def test_a_handle_without_the_lake_refuses_to_read_or_write_events_it_cannot_see():
    client = ConditionalS3()
    s = open_stores(client)
    s.runtime_storage.append_event('{"kind":"a"}')
    s.close()
    plain = open_stores(client, lake=False)
    with pytest.raises(RuntimeOwnershipError):
        plain.runtime_storage.history()
    with pytest.raises(RuntimeOwnershipError):
        plain.runtime_storage.append_event('{"kind":"b"}')
    plain.runtime_storage.put_state("fine", "{}")  # control state is untouched by the lake
    plain.close()


def test_a_lake_of_another_runtime_or_scope_is_refused():
    client = ConditionalS3()
    j = journal(client, name="classifier-a")
    other = journal(client, name="classifier-b")
    with pytest.raises(ValueError):
        S3RuntimeStores(j, events_lake=runtime_events_lake(other))
    wrong_scope = MemoryRecordLake(other.boundary[0].__class__("account-b", "workspace-a"), "runtime", events_kind(j))
    with pytest.raises(ValueError):
        S3RuntimeStores(j, events_lake=wrong_scope)


def test_runtimes_in_one_scope_do_not_share_a_lake():
    client = ConditionalS3()
    a = open_stores(client, "classifier-a", coalesce_events=False)
    b = open_stores(client, "classifier-b", coalesce_events=False)
    a.runtime_storage.append_event('{"kind":"only-a"}')
    b.runtime_storage.append_event('{"kind":"only-b"}')
    assert [e["kind"] for e in a.runtime_storage.history()] == ["only-a"]
    assert [e["kind"] for e in b.runtime_storage.history()] == ["only-b"]
    assert len({k for k in client.objects if "/records/" in k}) == 2
    a.close(), b.close()


# --- An abandoned event write is invisible ---------------------------------------------------------------


def test_events_written_before_a_lost_head_race_are_invisible_and_poison_the_holder():
    client = ConditionalS3()
    s = open_stores(client)
    s.runtime_storage.append_event('{"kind":"a"}')
    s.runtime_storage.append_event('{"kind":"b"}')
    rival = journal(client, name="classifier-a")
    rival.commit(rival.read(), {**rival.read().value, "ordinal": 99})
    with pytest.raises(RuntimeOwnershipError):
        s.runtime_cache.begin_answer("k", None)
    assert len([k for k in client.objects if "/records/" in k]) == 1  # the chunk exists...
    seen = head(s).value
    assert coverage_in(seen, s.events_lake).last == 0  # ...but nothing covers them
    assert s.events_lake.read_covered(coverage_in(seen, s.events_lake)) == []
    assert "k" not in seen["data"]["answers"]


def test_a_conflict_retry_adopts_the_identical_record_instead_of_duplicating_the_event():
    client = ConditionalS3()
    s = open_stores(client, coalesce_events=False)

    def rival_commit(request):
        rival = journal(client, name="classifier-a")
        rival.commit(rival.read(), {**rival.read().value, "ordinal": 7})

    client.before = rival_commit  # runs just before the event record is created; the head then moves
    assert s.runtime_storage.append_event('{"kind":"a"}') == 1
    assert [e["kind"] for e in s.runtime_storage.history()] == ["a"]
    assert len([k for k in client.objects if "/records/" in k]) == 1
    assert coverage_in(head(s).value, s.events_lake).skipped == ()


def test_a_lost_head_response_poisons_the_handle_and_nothing_is_double_counted():
    client = ConditionalS3()
    s = open_stores(client, coalesce_events=False)
    client.lose = lambda key: key.endswith("head.json")
    with pytest.raises(Exception):
        s.runtime_storage.append_event('{"kind":"a"}')
    assert s.poisoned
    with pytest.raises(RuntimeOwnershipError):
        s.runtime_storage.history()


# --- Ownership, fences and coalescing are unchanged ------------------------------------------------------


def test_a_fenced_out_handle_writes_no_event_records():
    client = ConditionalS3()
    first = open_stores(client)
    first.close()
    second = open_stores(client, acquire=False)
    second.acquire(str(uuid4()))
    before = len(record_puts(client))
    with pytest.raises(RuntimeOwnershipError):
        first.runtime_storage.append_event('{"kind":"stale"}')
    assert len(record_puts(client)) == before
    second.close()


def test_a_second_owner_cannot_take_a_runtime_that_holds_lake_events():
    client = ConditionalS3()
    first = open_stores(client)
    first.runtime_storage.append_event('{"kind":"a"}')
    with pytest.raises(RuntimeOwnershipError):
        open_stores(client)
    first.close()


def test_held_events_publish_with_the_next_durable_change_in_one_commit():
    client = ConditionalS3()
    s = open_stores(client)
    before = head(s).revision
    assert [s.runtime_storage.append_event('{"kind":"a"}'), s.runtime_storage.append_event('{"kind":"b"}')] == [1, 2]
    assert head(s).revision == before and record_puts(client) == []
    assert [e["kind"] for e in s.runtime_storage.history()] == ["a", "b"]
    s.runtime_cache.begin_answer("k", None)
    assert head(s).revision == before + 1 and len(record_puts(client)) == 1  # both events, one chunk
    stored = head(s).value
    assert coverage_in(stored, s.events_lake).last == 1 and stored["data"]["answers"]["k"][0] == "pending"
    s.close()


def test_release_and_flush_publish_held_events_and_refresh_keeps_them_held():
    client = ConditionalS3()
    s = open_stores(client)
    s.runtime_storage.append_event('{"kind":"a"}')
    s.flush()
    assert coverage_in(head(s).value, s.events_lake).last == 1
    revision = head(s).revision
    s.runtime_storage.append_event('{"kind":"b"}')
    s.refresh()  # the head has not moved, so the held event stays held
    assert head(s).revision == revision and coverage_in(head(s).value, s.events_lake).last == 1
    s.runtime_storage.append_event('{"kind":"c"}')
    s.close()
    final = journal(client, name="classifier-a").read()
    assert final.revision == revision + 1 and final.value["owner"] is None
    assert coverage_in(final.value, s.events_lake).last == 2 and final.value["chunks"]["last"] == 3


def test_events_held_by_a_crashed_owner_are_not_durable_but_the_reservation_is():
    client = ConditionalS3()
    s = open_stores(client)
    s.runtime_cache.begin_answer("k", None)  # the paid-action reservation is always durable at once
    s.runtime_storage.append_event('{"kind":"after-dispatch"}')
    stored = journal(client, name="classifier-a").read().value
    assert stored["data"]["answers"]["k"][0] == "pending" and coverage_in(stored, s.events_lake).last == 0
    assert record_puts(client) == []


def test_a_head_moved_while_events_are_held_poisons_the_handle_instead_of_dropping_them_silently():
    client = ConditionalS3()
    s = open_stores(client)
    s.runtime_storage.append_event('{"kind":"a"}')
    rival = journal(client, name="classifier-a")
    rival.commit(rival.read(), {**rival.read().value, "ordinal": 99})
    with pytest.raises(RuntimeOwnershipError):
        s.runtime_cache.begin_answer("k", None)
    with pytest.raises(RuntimeOwnershipError):
        s.runtime_storage.history()


def test_without_coalescing_every_event_is_its_own_commit_with_its_record():
    client = ConditionalS3()
    s = open_stores(client, coalesce_events=False)
    before = head(s).revision
    s.runtime_storage.append_event('{"kind":"a"}')
    s.runtime_storage.append_event('{"kind":"b"}')
    assert head(s).revision == before + 2 and len(record_puts(client)) == 2
    s.close()


def test_coalescing_writes_far_fewer_commits_with_the_lake_too():
    def commits(coalesce):
        client = ConditionalS3()
        s = open_stores(client, coalesce_events=coalesce)
        start = head(s).revision
        for n in range(5):
            decide(s, n)
        s.close()
        return journal(client, name="classifier-a").read().revision - start

    assert commits(False) == 5 * 9 + 1  # seven events and two answer changes per item, plus the release
    # One commit per item, the reservation; the completion and the events wait for the next one or the release.
    assert commits(True) == 5 * 1 + 1
