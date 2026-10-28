"""Complete shared-request domain against canonical S3 CAS; no DynamoDB or SQLite."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from .batch_storage import BatchConflict, BatchInProgress, BatchUncertain
from .s3_journal_test import ConditionalS3, S3Error, journal


def store(j=None, **options):
    from .s3_batch_storage import S3BatchStorage

    result = S3BatchStorage(j or journal(), **options)
    result.initialize()
    return result


def test_claim_is_exclusive_and_original_operation_replay_retains_its_generation():
    s = store()
    first, second = str(uuid4()), str(uuid4())
    s.reserve("a" * 64, 3, first)
    assert s.claim("a" * 64, first) and not s.claim("a" * 64, first)
    text = '{ "exact":"雪", "n":9007199254740993 }'
    s.settle("a" * 64, first, "complete", text)
    s.reserve("a" * 64, 3, second, policy="refresh")
    s.claim("a" * 64, second)
    s.settle("a" * 64, second, "complete", '{"new":true}')
    assert s.reserve("a" * 64, 3, first, policy="refresh").payload == text
    assert s.requests() == 2 and s.get("a" * 64).generation == 2
    with pytest.raises(BatchConflict):
        s.settle("a" * 64, first, "failed")


def test_independent_workers_race_for_one_atomic_budget_slot():
    s = store()
    client = s.journal.payloads.client

    def reserve(index):
        other = store(journal(client))
        try:
            other.reserve(str(index) * 64, 1, str(uuid4()))
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(1, 9)))
    assert sum(results) == 1 and s.requests() == 1
    assert not any("dynamo" in key for key in client.objects)


def test_uncapped_reservations_keep_durable_attempt_accounting_and_idempotency():
    s = store()
    owners = []
    for index in range(4):
        owner = str(uuid4())
        owners.append(owner)
        s.reserve(str(index + 1) * 64, None, owner)
    assert s.requests() == 4
    assert s.reserve("1" * 64, None, owners[0]).fingerprint == "1" * 64
    assert s.requests() == 4


def test_lost_claim_response_never_authorizes_a_provider_and_restart_does_not_steal():
    s = store()
    owner = str(uuid4())
    s.reserve("b" * 64, 2, owner)
    s.journal.payloads.client.lose = lambda key: key == s.journal.head_key
    with pytest.raises(BatchUncertain):
        s.claim("b" * 64, owner)
    restarted = store(journal(s.journal.payloads.client))
    assert not restarted.claim("b" * 64, owner)
    with pytest.raises(BatchInProgress):
        restarted.reserve("b" * 64, 2, str(uuid4()), retry_failed=True)
    assert restarted.requests() == 1 and restarted.get("b" * 64).claimed


def test_lost_reservation_and_settlement_can_reconcile_same_owner_without_repayment():
    s = store()
    owner = str(uuid4())
    client = s.journal.payloads.client
    client.lose = lambda key: key == s.journal.head_key
    with pytest.raises(BatchUncertain):
        s.reserve("c" * 64, 1, owner)
    assert s.reserve("c" * 64, 1, owner).owner == owner and s.requests() == 1
    assert s.claim("c" * 64, owner)
    client.lose = lambda key: key == s.journal.head_key
    with pytest.raises(BatchUncertain):
        s.settle("c" * 64, owner, "complete", '{"ok":true}')
    assert s.settle("c" * 64, owner, "complete", '{"ok":true}').status == "complete"
    assert s.requests() == 1


def test_failed_retry_history_export_and_restore_match_native_without_cloud_sqlite(tmp_path, monkeypatch):
    from .batch_storage import SQLiteBatchStorage

    native = SQLiteBatchStorage(tmp_path / "native.sqlite")
    s = store()
    first, second = str(uuid4()), str(uuid4())
    for target in (s, native):
        target.reserve("d" * 64, 3, first)
        target.claim("d" * 64, first)
        target.settle("d" * 64, first, "failed")
        with pytest.raises(RuntimeError, match="retry"):
            target.reserve("d" * 64, 3, second)
        target.reserve("d" * 64, 3, second, retry_failed=True)
        target.claim("d" * 64, second)
        target.settle("d" * 64, second, "complete", '{ "verbatim":true }')
    assert s.export() == native.export()
    native.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("S3 adapter opened SQLite")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    destination = store(journal(s.journal.payloads.client, name="run-b"))
    destination.restore(json.loads(json.dumps(s.export())))
    assert destination.export() == s.export() and destination.requests() == 2
    with pytest.raises(BatchConflict):
        destination.restore(s.export())


def test_learning_abandonment_retains_attempt_history_and_cannot_remove_a_sibling_guard():
    s = store()
    first, second = str(uuid4()), str(uuid4())
    s.reserve("e" * 64, 3, first)
    s.reserve("f" * 64, 3, second)
    s.claim("f" * 64, second)
    assert s.operation_mark() == 2
    assert s.pending_operations() == [(1, "e" * 64, first), (2, "f" * 64, second)]
    with pytest.raises(BatchConflict):
        s.abandon_learning("e" * 64, second)
    s.abandon_learning("e" * 64, first)
    s.reserve("e" * 64, 3, str(uuid4()))
    assert s.requests() == 3 and s.get("f" * 64).status == "pending"
    assert s.export()["history"][0][3] == "interrupted"
    with pytest.raises(BatchUncertain):
        s.abandon_learning("f" * 64, second)
    assert s.get("f" * 64).status == "pending"


def test_more_ledger_records_than_the_old_budget_commit_and_a_missing_head_fails_closed():
    from .s3_batch_storage import S3BatchStorage
    from .s3_journal import JournalUnavailable

    j = journal()
    s = S3BatchStorage(j)
    s.initialize()
    # The old record budget refused the second request here; there is no record budget now.
    for character in "123456789":
        s.reserve(character * 64, 100, str(uuid4()))
    assert s.requests() == 9 and len(S3BatchStorage(j).export()["batches"]) == 9
    del j.payloads.client.objects[j.head_key]
    with pytest.raises(JournalUnavailable):
        s.initialize()


def test_real_shared_consumer_restarts_from_s3_without_sqlite_or_replaying_completed_provider(monkeypatch):
    import asyncio

    from .batched_classification import BatchedAnswers
    from .classifier_config import ClassifierConfig
    from .models import DecisionResult, DecisionTask, Item
    from .shared_decisions import SharedDecisions

    class Model:
        model_identity = "synthetic-s3"
        calls = 0

        async def classify_many(self, configs, *args, **kwargs):
            self.calls += 1
            return BatchedAnswers(
                {cid: {"decision": DecisionResult("yes", {"yes": 0.8, "no": 0.2})} for cid in configs}, "synthetic", {}, 1
            )

    def forbidden(*args, **kwargs):
        raise AssertionError("cloud consumer opened SQLite")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    s = store()
    model = Model()
    config = ClassifierConfig(DecisionTask("topic", ("yes", "no"), "Relevant?"))
    target = Item("target", {"text": "Synthetic"})
    for storage in (s, store(journal(s.journal.payloads.client))):
        consumer = SharedDecisions(None, model, max_requests=1, observer=lambda _: None, storage=storage)
        asyncio.run(consumer.prepare({"topic": config}, target, {"topic": ()}))
        assert consumer.requests == 1
        consumer.close()
    assert model.calls == 1


def test_real_shared_consumer_never_calls_provider_after_an_uncertain_reservation_and_claim(monkeypatch):
    import asyncio

    from .classifier_config import ClassifierConfig
    from .models import DecisionTask, Item
    from .shared_decisions import SharedDecisions

    class Model:
        model_identity = "synthetic-s3"
        calls = 0

        async def classify_many(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("uncertain dispatch reached provider")

    s = store()
    model = Model()
    reserve = s.reserve

    def lose_reservation(*args, **kwargs):
        # The consumer reserves and claims in one commit; losing that commit's response is the uncertain claim.
        s.journal.payloads.client.lose = lambda key: key == s.journal.head_key
        return reserve(*args, **kwargs)

    monkeypatch.setattr(s, "reserve", lose_reservation)
    consumer = SharedDecisions(None, model, max_requests=1, observer=lambda _: None, storage=s)
    config = ClassifierConfig(DecisionTask("topic", ("yes", "no"), "Relevant?"))
    with pytest.raises(BatchUncertain):
        asyncio.run(consumer.prepare({"topic": config}, Item("x", {"text": "Synthetic"}), {"topic": ()}))
    assert model.calls == 0 and s.requests() == 1


# ---------------------------------------------------------------------------
# Raw provider replies in the record lake (S2b)
# ---------------------------------------------------------------------------

REPLY_KINDS = ("memory", "file", "s3")


def reply_lake(kind, j, tmp_path=None, memory=None, ledger="run-a"):
    from .record_lake import FileRecordLake, MemoryRecordLake, S3RecordLake
    from .storage_payloads import FilePayloads

    options = dict(domain="shared-batches", kind=f"replies-{ledger}")
    if kind == "memory":
        return MemoryRecordLake(j.boundary[0], store=memory, **options)
    if kind == "file":
        return FileRecordLake(FilePayloads(tmp_path, j.boundary[0]), **options)
    return S3RecordLake(j.payloads, **options)


@pytest.fixture(params=REPLY_KINDS)
def lake_world(request, tmp_path):
    """A ledger whose replies live in the lake, reopenable over the same underlying store."""
    from .record_lake_contract_test import ListingS3
    from .s3_batch_storage import S3BatchStorage

    client, memory = ListingS3(), {}

    class World:
        def open(self, name="run-a", **options):
            j = journal(client, name=name)
            s = S3BatchStorage(j, replies=reply_lake(request.param, j, tmp_path, memory, name), **options)
            s.initialize()
            return s

        client = property(lambda self: client)
        lake_kind = request.param

        def stored_replies(self, s):
            return s.replies.sequences()

    return World()


def reply_text(size=0, marker="r"):
    return json.dumps({"result": {"answers": {"topic": {"decision": "yes"}}}, "exchanges": [marker * size]})


def paid(s, fingerprint, text, maximum=1000, policy="reuse"):
    owner = str(uuid4())
    s.reserve(fingerprint, maximum, owner, policy=policy)
    assert s.claim(fingerprint, owner)
    s.settle(fingerprint, owner, "complete", text)
    return owner


def journal_bytes(s):
    from .journal import encode

    return len(encode(s.journal.read().value))


def batch_record(s, fingerprint, generation=1):
    from .s3_batch_storage import batch_key

    return s.records.get(batch_key(fingerprint, generation))


def legacy_ledger(client, name, operations, *, lake=None, tmp_path):
    """A ledger in the old whole-state layout, as production journals hold it today, built from real operations."""
    from .batch_storage import SQLiteBatchStorage
    from .s3_batch_storage import S3BatchStorage

    native = SQLiteBatchStorage(tmp_path / f"{name}.sqlite")
    operations(native)
    j = journal(client, name=name)
    j.initialize(native.export())
    native.close()
    if lake is not None:
        assert S3BatchStorage(j, replies=lake).convert_embedded_replies() is not None  # replies already in the lake
    return j


def test_a_reply_leaves_the_journal_and_the_journal_keeps_only_a_reference(lake_world):
    s = lake_world.open()
    text = reply_text(20000)
    paid(s, "a" * 64, text)
    state = s.journal.read().value
    cited = batch_record(s, "a" * 64)["payload"]
    assert text not in json.dumps(state) and state["held"] == []
    assert set(cited) == {"seq", "sha256", "length"} and cited["seq"] == 1
    assert state["records"]["shared-batches/replies-run-a"] == {"last": 1, "skipped": []}
    assert s.get("a" * 64).payload == text and s.requests() == 1
    assert lake_world.stored_replies(s) == [1]


def test_the_head_does_not_grow_with_paid_requests_whatever_their_reply_size(lake_world):
    from .s3_batch_storage import S3BatchStorage

    def grow(storage, size, count=25):
        paid(storage, f"{999:064x}", reply_text(size))  # past the first commit, whose coverage is new
        base = journal_bytes(storage)
        for index in range(count):
            paid(storage, f"{index:064x}", reply_text(size, marker=chr(97 + index % 26)))
        return journal_bytes(storage) - base

    embedded = S3BatchStorage(journal(name="embedded"))
    embedded.initialize()
    small, large = lake_world.open("small"), lake_world.open("large")
    # Only the digits of the counters change: a finished request leaves the head.
    assert grow(embedded, 23000) <= 3 and grow(small, 2000) <= 6 and grow(large, 150000) <= 6
    assert len(large.replies.sequences()) == 26


def test_the_head_stays_the_same_size_however_many_requests_came_before(lake_world):
    s = lake_world.open()
    sizes = []
    for index in range(30):
        paid(s, f"{index:064x}", reply_text(5000))
        sizes.append(journal_bytes(s))
    steps = [b - a for a, b in zip(sizes[1:], sizes[2:])]
    assert max(steps) <= 3 and min(steps) >= 0  # only a counter's digits ever change


def test_cache_hits_read_the_reply_from_the_lake_by_reference(lake_world):
    s = lake_world.open()
    text = '{ "exact":"雪", "n":9007199254740993 }'
    paid(s, "a" * 64, text)
    reopened = lake_world.open()
    reads = []
    original = reopened.replies.read
    reopened.replies.read = lambda seq: reads.append(seq) or original(seq)
    hit = reopened.reserve("a" * 64, 1, str(uuid4()))
    assert (hit.status, hit.payload) == ("complete", text) and reads == [1]
    assert reopened.requests() == 1 and reopened.reserve("a" * 64, 1, str(uuid4()), policy="cache_only").payload == text
    reads.clear()
    assert reopened.get("a" * 64, payload=False).payload is None and reads == []
    assert [r.payload for r in reopened.page()[0]] == [text]


def test_shared_decisions_serve_a_repeat_from_the_lake_without_a_second_provider_call(lake_world):
    import asyncio

    from .batched_classification import BatchedAnswers
    from .classifier_config import ClassifierConfig
    from .models import DecisionResult, DecisionTask, Item
    from .shared_decisions import SharedDecisions

    class Model:
        model_identity = "synthetic-lake"
        calls = 0

        async def classify_many(self, configs, *args, **kwargs):
            self.calls += 1
            return BatchedAnswers(
                {cid: {"decision": DecisionResult("yes", {"yes": 0.8, "no": 0.2})} for cid in configs}, "synthetic", {}, 1
            )

    model, config, target = Model(), ClassifierConfig(DecisionTask("topic", ("yes", "no"), "Relevant?")), Item("t", {"text": "x"})
    for storage in (lake_world.open(), lake_world.open()):
        consumer = SharedDecisions(None, model, max_requests=1, observer=lambda _: None, storage=storage)
        asyncio.run(consumer.prepare({"topic": config}, target, {"topic": ()}))
        assert consumer.requests == 1
    assert model.calls == 1


# Crash points. "Crash" means the process stops; a new handle reopens the same store.


def test_crash_after_reserve_before_claim_keeps_the_charge_and_blocks_a_second_reservation(lake_world):
    s = lake_world.open()
    owner = str(uuid4())
    s.reserve("a" * 64, 1, owner)
    again = lake_world.open()
    assert again.requests() == 1 and again.get("a" * 64).claimed is False
    with pytest.raises(BatchInProgress):
        again.reserve("a" * 64, 1, str(uuid4()), retry_failed=True)
    with pytest.raises(RuntimeError, match="ceiling"):
        again.reserve("b" * 64, 1, str(uuid4()))
    assert again.reserve("a" * 64, 1, owner).owner == owner and again.requests() == 1


def test_crash_after_claim_before_any_reply_never_allows_a_second_provider_call(lake_world):
    s = lake_world.open()
    owner = str(uuid4())
    s.reserve("a" * 64, 1, owner)
    s.claim("a" * 64, owner)
    again = lake_world.open()
    assert not again.claim("a" * 64, owner) and again.requests() == 1
    with pytest.raises(BatchInProgress):
        again.reserve("a" * 64, 1, str(uuid4()), retry_failed=True)
    assert again.replies.sequences() == []


def test_crash_after_the_reply_record_but_before_the_flip_leaves_an_unreadable_orphan_and_no_second_payment(
    lake_world, monkeypatch
):
    s = lake_world.open()
    owner, text = str(uuid4()), reply_text(3000)
    s.reserve("a" * 64, 1, owner)
    s.claim("a" * 64, owner)

    def die(*args, **kwargs):
        raise KeyboardInterrupt("process stopped between the reply record and the flip")

    monkeypatch.setattr(s.journal, "commit", die)
    with pytest.raises(KeyboardInterrupt):
        s.settle("a" * 64, owner, "complete", text)
    monkeypatch.undo()

    again = lake_world.open()
    assert again.replies.sequences() == [1]  # the orphan exists in the lake
    state = again.journal.read().value
    assert "shared-batches/replies-run-a" not in state["records"]  # but no control state covers it
    assert [g["status"] for g in state["held"]] == ["pending"]  # and the head, not the record, decides
    assert again.get("a" * 64).status == "pending" and again.get("a" * 64).payload is None
    assert again.requests() == 1  # usage is not double counted and not lost
    with pytest.raises(BatchInProgress):
        again.reserve("a" * 64, 1, str(uuid4()), retry_failed=True)  # still not payable again
    with pytest.raises(RuntimeError, match="ceiling"):
        again.reserve("b" * 64, 1, str(uuid4()))
    # The owner finishing the job adopts the orphan record instead of writing a second one.
    settled = again.settle("a" * 64, owner, "complete", text)
    assert settled.payload == text and again.replies.sequences() == [1]
    assert again.requests() == 1 and lake_world.open().get("a" * 64).payload == text


def test_a_different_reply_after_an_orphan_takes_the_next_sequence_and_the_orphan_stays_unreadable(lake_world, monkeypatch):
    s = lake_world.open()
    owner = str(uuid4())
    s.reserve("a" * 64, 1, owner)
    s.claim("a" * 64, owner)
    monkeypatch.setattr(s.journal, "commit", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        s.settle("a" * 64, owner, "complete", '{"first":1}')
    monkeypatch.undo()
    again = lake_world.open()
    again.settle("a" * 64, owner, "complete", '{"second":2}')
    assert again.get("a" * 64).payload == '{"second":2}'
    assert again.journal.read().value["records"]["shared-batches/replies-run-a"] == {"last": 2, "skipped": [1]}
    assert again.replies.orphans(
        __import__("decision_flywheel.record_lake", fromlist=["x"]).coverage_in(again.journal.read().value, again.replies)
    ) == [1]
    assert again.requests() == 1


def test_a_lost_flip_response_is_uncertain_and_repeating_it_neither_repays_nor_double_counts(lake_world):
    s = lake_world.open()
    owner, text = str(uuid4()), reply_text(500)
    s.reserve("a" * 64, 1, owner)
    s.claim("a" * 64, owner)
    lake_world.client.lose = lambda key: key == s.journal.head_key
    with pytest.raises(BatchUncertain):
        s.settle("a" * 64, owner, "complete", text)
    again = lake_world.open()  # the flip did land
    assert again.get("a" * 64).payload == text and again.requests() == 1
    assert again.settle("a" * 64, owner, "complete", text).payload == text
    assert again.replies.sequences() == [1] and again.requests() == 1
    with pytest.raises(BatchConflict):
        again.settle("a" * 64, owner, "complete", reply_text(501))  # a different reply never overwrites it
    assert again.get("a" * 64).payload == text


def test_after_the_flip_a_crash_changes_nothing(lake_world):
    s = lake_world.open()
    owner = paid(s, "a" * 64, reply_text(100))
    again = lake_world.open()
    assert again.reserve("a" * 64, 1, str(uuid4())).payload == reply_text(100)
    assert again.reserve("a" * 64, 1, owner).payload == reply_text(100)
    assert again.requests() == 1 and again.replies.sequences() == [1]


def test_a_flip_that_loses_the_head_race_retries_with_a_fresh_sequence_and_both_replies_survive(lake_world):
    one, two = lake_world.open(), lake_world.open()
    owner_one, owner_two = str(uuid4()), str(uuid4())
    one.reserve("a" * 64, 5, owner_one)
    one.claim("a" * 64, owner_one)
    two.reserve("b" * 64, 5, owner_two)
    two.claim("b" * 64, owner_two)
    real, raced = one.journal.commit, []

    def race(expected, value):
        if not raced:
            raced.append(True)
            two.settle("b" * 64, owner_two, "complete", '{"second":true}')
        return real(expected, value)

    one.journal.commit = race
    one.settle("a" * 64, owner_one, "complete", '{"first":true}')
    reader = lake_world.open()
    assert reader.get("a" * 64).payload == '{"first":true}' and reader.get("b" * 64).payload == '{"second":true}'
    assert reader.requests() == 2
    # The racer found sequence 1 taken and moved on; the loser's retry reuses the batch record it already wrote.
    assert [batch_record(reader, fp)["payload"]["seq"] for fp in ("a" * 64, "b" * 64)] == [1, 2]
    assert reader.replies.sequences() == [1, 2] and reader.journal.read().value["held"] == []


def test_a_failed_reply_write_embeds_the_paid_reply_instead_of_dropping_it(lake_world):
    s = lake_world.open()
    owner = str(uuid4())
    s.reserve("a" * 64, 1, owner)
    s.claim("a" * 64, owner)
    from .record_lake import RecordUncertain

    s.replies._create = lambda seq, content: (_ for _ in ()).throw(RecordUncertain("records not permitted"))
    settled = s.settle("a" * 64, owner, "complete", '{"paid":true}')
    assert settled.payload == '{"paid":true}'
    again = lake_world.open()
    assert again.get("a" * 64).payload == '{"paid":true}' and again.requests() == 1
    assert batch_record(again, "a" * 64)["payload"] == '{"paid":true}'  # inline in the batch record


def test_a_reply_larger_than_the_old_four_mib_reply_budget_goes_to_the_lake_and_reads_back(lake_world):
    from . import s3_batch_storage

    assert not hasattr(s3_batch_storage, "REPLY_RECORD_BYTES")
    s = lake_world.open()
    large = reply_text(5 * 1024 * 1024)
    paid(s, "a" * 64, large)
    held = batch_record(s, "a" * 64)["payload"]
    assert isinstance(held, dict) and held["length"] == len(large.encode())
    assert lake_world.open().get("a" * 64).payload == large


def test_a_missing_or_altered_reply_record_fails_closed_and_never_looks_unpaid(lake_world):
    s = lake_world.open()
    paid(s, "a" * 64, reply_text(100))
    again = lake_world.open()
    good = again.replies._fetch(1)
    # altered: another valid envelope at the same position is refused by the digest check
    forged = good.replace(b"rrrr", b"xxxx", 1)
    assert forged != good
    if lake_world.lake_kind == "memory":
        again.replies.store[(again.replies.namespace, 1)] = forged
    elif lake_world.lake_kind == "s3":
        lake_world.client.objects[again.replies._key(1)] = (forged, "etag")
    else:
        again.replies._path(1).write_bytes(forged)
    from .record_lake import RecordCorrupt, RecordUnavailable

    with pytest.raises(RecordCorrupt):
        again.reserve("a" * 64, 1, str(uuid4()))
    assert again.requests() == 1 and again.get("a" * 64, payload=False).status == "complete"
    if lake_world.lake_kind == "memory":
        del again.replies.store[(again.replies.namespace, 1)]
    elif lake_world.lake_kind == "s3":
        del lake_world.client.objects[again.replies._key(1)]
    else:
        again.replies._path(1).unlink()
    with pytest.raises(RecordUnavailable):
        again.reserve("a" * 64, 1, str(uuid4()))
    assert again.requests() == 1


def test_legacy_embedded_replies_still_read_and_new_ones_join_them_in_the_lake(lake_world, tmp_path):
    from .s3_batch_storage import S3BatchStorage

    owners = []
    legacy_ledger(
        lake_world.client, "run-a", lambda native: owners.append(paid(native, "a" * 64, '{"old":true}')), tmp_path=tmp_path
    )
    upgraded = lake_world.open()
    assert upgraded.get("a" * 64).payload == '{"old":true}'
    assert upgraded.reserve("a" * 64, 5, str(uuid4())).payload == '{"old":true}'
    assert upgraded.reserve("a" * 64, 5, owners[0], policy="refresh").payload == '{"old":true}'
    assert upgraded.journal.read().value["schema"] == "cyclotron-shared-batches/v1"  # reads never migrate
    paid(upgraded, "b" * 64, '{"new":true}')
    snapshot = upgraded.export()
    assert [row[2] for row in snapshot["batches"]] == ['{"old":true}', '{"new":true}']
    assert isinstance(batch_record(upgraded, "a" * 64)["payload"], str)
    assert isinstance(batch_record(upgraded, "b" * 64)["payload"], dict)
    # a refresh moves the lake-held reply into history; its original owner still replays it
    replay_owner = snapshot["attempts"][1][2]
    upgraded.reserve("b" * 64, 5, str(uuid4()), policy="refresh")
    assert upgraded.reserve("b" * 64, 5, replay_owner, policy="refresh").payload == '{"new":true}'
    assert upgraded.export()["history"][-1][4] == '{"new":true}'
    # a ledger that cites reply records cannot read them without a lake
    blind = S3BatchStorage(journal(lake_world.client))
    with pytest.raises(ValueError, match="lake"):
        blind.export()


def test_export_and_restore_round_trip_through_the_lake_and_match_the_embedded_form(lake_world):
    from .s3_batch_storage import S3BatchStorage

    s = lake_world.open()
    first = paid(s, "a" * 64, '{"v":1}')
    s.reserve("a" * 64, 5, str(uuid4()), policy="refresh")
    paid(s, "b" * 64, '{"v":2}')
    snapshot = s.export()
    embedded = S3BatchStorage(journal(name="plain"))
    embedded.initialize()
    embedded.restore(snapshot)
    assert embedded.export() == snapshot
    target = lake_world.open("restored")
    target.restore(json.loads(json.dumps(snapshot)))
    assert target.export() == snapshot and target.requests() == s.requests()
    assert target.replies.sequences() == [] and isinstance(batch_record(target, "b" * 64)["payload"], str)
    assert target.reserve("a" * 64, 5, first, policy="refresh").payload == '{"v":1}'
    with pytest.raises(BatchConflict):
        target.restore(snapshot)


def test_the_ceiling_stays_atomic_in_the_control_state_when_replies_live_in_the_lake(lake_world):
    s = lake_world.open()

    def reserve(index):
        other = lake_world.open()
        try:
            other.reserve(str(index) * 64, 1, str(uuid4()))
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(1, 9)))
    assert sum(results) == 1 and s.requests() == 1 and s.replies.sequences() == []


def test_a_lake_must_belong_to_the_journals_scope():
    from .batch_storage import StorageScope
    from .record_lake import MemoryRecordLake
    from .s3_batch_storage import S3BatchStorage

    with pytest.raises(ValueError):
        S3BatchStorage(
            journal(),
            replies=MemoryRecordLake(StorageScope("other", "scope"), "shared-batches", "replies"),
        )
    with pytest.raises(ValueError):
        S3BatchStorage(journal(), replies=object())


# ---------------------------------------------------------------------------
# Constant cost: finished requests are records found by key, the head keeps only work in flight
# ---------------------------------------------------------------------------


class Model:
    """A decision model that answers every batch the same way and counts its paid calls."""

    model_identity = "synthetic-constant"

    def __init__(self):
        self.calls = 0

    async def classify_many(self, configs, *args, **kwargs):
        from .batched_classification import BatchedAnswers
        from .models import DecisionResult

        self.calls += 1
        return BatchedAnswers(
            {cid: {"decision": DecisionResult("yes", {"yes": 0.8, "no": 0.2})} for cid in configs}, "synthetic", {}, 1
        )


def decide(storage, model, name, *, maximum=None):
    import asyncio
    from datetime import datetime, timezone

    from .classifier_config import ClassifierConfig
    from .models import DecisionTask, Item
    from .shared_decisions import SharedDecisions

    config = ClassifierConfig(DecisionTask("topic", ("yes", "no"), "Relevant?"))
    consumer = SharedDecisions(None, model, max_requests=maximum, observer=lambda _: None, storage=storage)
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    asyncio.run(consumer.prepare({"topic": config}, Item(name, {"text": "Synthetic " + name}), {"topic": ()}, now=now))


def test_the_bytes_and_calls_one_decision_spends_on_the_ledger_do_not_depend_on_how_many_came_before(lake_world):
    s, model = lake_world.open(), Model()
    client = lake_world.client
    spent = {}
    for number in range(1, 96):
        before = len(client.calls)
        decide(s, model, f"item-{number:03d}")
        calls = client.calls[before:]
        spent[number] = (
            sum(len(request["Body"]) for kind, request in calls if kind == "put"),
            sum(kind == "put" for kind, _ in calls),
            sum(kind == "get" for kind, _ in calls),
            sum(kind == "put" and request["Key"] == s.journal.head_key for kind, request in calls),
        )
    # Decision 55 and decision 95 (same counter widths) write the same bytes with the same calls.
    assert spent[55] == spent[95]
    assert spent[95][3] == 2  # two commits: reserve with its claim, then the reply
    assert spent[95][0] < 5000
    assert model.calls == 95 and s.journal.read().value["held"] == [] and s.requests() == 95


def test_reserving_with_the_claim_is_one_commit_and_the_claim_then_needs_no_write():
    s = store()
    owner = str(uuid4())
    client = s.journal.payloads.client

    def heads():
        return sum(kind == "put" and r["Key"] == s.journal.head_key for kind, r in client.calls)

    before = heads()
    row = s.reserve("a" * 64, 1, owner, claim=True)
    assert row.claimed and row.status == "pending" and heads() == before + 1
    assert s.claim("a" * 64, owner) and heads() == before + 1
    assert not s.claim("a" * 64, owner)  # the claim is taken once
    assert not store(journal(client)).claim("a" * 64, owner)  # and never by another handle
    s.settle("a" * 64, owner, "complete", '{"ok":true}')
    assert heads() == before + 2 and s.get("a" * 64).payload == '{"ok":true}'
    # A plain reservation still leaves the claim to an explicit, separate commit.
    other = str(uuid4())
    assert not s.reserve("b" * 64, 3, other).claimed and s.claim("b" * 64, other)


def test_an_uncertain_reservation_with_the_claim_never_authorizes_dispatch():
    s = store()
    owner = str(uuid4())
    s.journal.payloads.client.lose = lambda key: key == s.journal.head_key
    with pytest.raises(BatchUncertain):
        s.reserve("a" * 64, 1, owner, claim=True)
    assert not s.claim("a" * 64, owner)  # the commit landed, but its claim is never inferred from rereading
    assert s.requests() == 1 and s.get("a" * 64).claimed


def test_an_owner_reused_for_a_different_request_is_still_refused_after_its_request_finished():
    s = store()
    owner = str(uuid4())
    s.reserve("a" * 64, 5, owner, claim=True)
    s.settle("a" * 64, owner, "complete", '{"a":1}')
    reopened = store(journal(s.journal.payloads.client))
    with pytest.raises(BatchConflict, match="different request"):
        reopened.reserve("b" * 64, 5, owner)
    assert reopened.reserve("a" * 64, 5, owner).payload == '{"a":1}' and reopened.requests() == 1


def test_a_settled_request_whose_record_cannot_be_written_stays_in_the_head_until_a_later_write():
    from .record_lake import RecordUncertain

    s = store()
    owner = str(uuid4())
    s.reserve("a" * 64, 5, owner, claim=True)
    real = s.records._create

    def unreachable(key, content):
        raise RecordUncertain("records unreachable")

    s.records._create = unreachable
    assert s.settle("a" * 64, owner, "complete", '{"paid":true}').payload == '{"paid":true}'
    held = s.journal.read().value["held"]
    assert [(g["status"], g["payload"]) for g in held] == [("complete", '{"paid":true}')]
    assert store(journal(s.journal.payloads.client)).get("a" * 64).payload == '{"paid":true}'
    s.records._create = real
    s.reserve("b" * 64, 5, str(uuid4()))  # the next commit writes it out
    assert [g["fingerprint"] for g in s.journal.read().value["held"]] == ["b" * 64]
    assert batch_record(s, "a" * 64)["payload"] == '{"paid":true}' and s.get("a" * 64).payload == '{"paid":true}'


class DeniedS3(ConditionalS3):
    """S3 that refuses reads of the batch records, written or not, as a principal lacking the grant sees it."""

    denied = ()

    def get_object(self, **request):
        if any(part in request["Key"] for part in self.denied):
            raise S3Error("AccessDenied")
        return super().get_object(**request)


def test_a_denied_batch_record_read_refuses_and_never_looks_unpaid():
    from .record_lake import RecordUnavailable

    client, model = DeniedS3(), Model()
    s = store(journal(client))
    decide(s, model, "one")
    client.denied = ("/records/shared-batches/batches-",)
    for name in ("one", "two"):  # a written record, and a key nobody wrote: neither reads as absent
        blind = store(journal(client))
        with pytest.raises(RecordUnavailable):
            decide(blind, model, name)
    assert model.calls == 1 and s.requests() == 1  # no second payment, no new reservation


def test_an_old_format_ledger_answers_an_already_paid_request_from_its_record_then_migrates_on_first_write(tmp_path):
    from .batch_storage import SQLiteBatchStorage
    from .record_lake import S3RecordLake
    from .record_lake_contract_test import ListingS3
    from .s3_batch_storage import S3BatchStorage

    client, model = ListingS3(), Model()
    pending_owner, failed_owner = str(uuid4()), str(uuid4())

    def history(native):
        for name in ("paid-1", "paid-2", "paid-3"):
            decide(native, model, name)
        native.reserve("f" * 64, None, pending_owner, claim=True)  # dispatched before the deploy, never answered
        native.reserve("e" * 64, None, failed_owner, claim=True)
        native.settle("e" * 64, failed_owner, "failed")

    j = journal(client, name="ledger")
    lake = S3RecordLake(j.payloads, "shared-batches", "replies-ledger")
    legacy_ledger(client, "ledger", history, lake=lake, tmp_path=tmp_path)
    old = j.read().value
    assert old["schema"] == "cyclotron-shared-batches/v1" and old["records"]["shared-batches/replies-ledger"]["last"] == 3
    paid_calls = model.calls

    ledger = S3BatchStorage(journal(client, name="ledger"), replies=lake)
    ledger.initialize()
    before = ledger.export()
    decide(ledger, model, "paid-2")  # served from the old ledger: no model call and no write
    assert model.calls == paid_calls and j.read().value == old
    with pytest.raises(RuntimeError, match="explicit retry"):
        ledger.reserve("e" * 64, None, str(uuid4()))  # a failed exact decision still needs an explicit retry
    with pytest.raises(BatchInProgress):
        ledger.reserve("f" * 64, None, str(uuid4()), retry_failed=True)

    decide(ledger, model, "new-1")  # the first write migrates, then reserves
    head = j.read().value
    assert head["schema"] == "cyclotron-shared-batches/v2" and model.calls == paid_calls + 1
    assert [(g["fingerprint"], g["owner"]) for g in head["held"]] == [("f" * 64, pending_owner)]
    assert ledger.requests() == len(before["attempts"]) + 1
    after = ledger.export()
    assert after["history"] == before["history"] and after["attempts"][: len(before["attempts"])] == before["attempts"]
    assert [row for row in after["batches"] if row[0] in {r[0] for r in before["batches"]}] == before["batches"]
    fresh = S3BatchStorage(journal(client, name="ledger"), replies=lake)
    decide(fresh, model, "paid-1")
    assert model.calls == paid_calls + 1  # still answered by key after migration
    assert not fresh.claim("f" * 64, pending_owner) and fresh.get("f" * 64).claimed
    with pytest.raises(BatchInProgress):
        fresh.reserve("f" * 64, None, str(uuid4()), retry_failed=True)
    with pytest.raises(BatchConflict, match="different request"):
        fresh.reserve("d" * 64, None, failed_owner)  # an old owner is still bound to its request
    native = SQLiteBatchStorage(tmp_path / "compare.sqlite")
    native.restore(after)
    assert native.export() == after


class SlowRecordsS3(ConditionalS3):
    """Record writes take a moment outside the store lock, so concurrent writers overlap; the overlap is measured."""

    def __init__(self):
        super().__init__()
        self.flight, self.widest, self.order, self.fail = 0, 0, [], None
        self.gate = __import__("threading").Lock()

    def put_object(self, **request):
        import time

        key = request["Key"]
        if "/records/" not in key:
            self.order.append(key)
            return super().put_object(**request)
        with self.gate:
            self.flight += 1
            self.widest = max(self.widest, self.flight)
        try:
            time.sleep(0.002)
            if self.fail and self.fail(key):
                self.fail = None
                raise TimeoutError("synthetic interruption")
            result = super().put_object(**request)
            self.order.append(key)
            return result
        finally:
            with self.gate:
                self.flight -= 1


def old_ledger_of(client, tmp_path, requests):
    def history(native):
        for index in range(requests):
            owner = str(uuid4())
            native.reserve(f"{index:064x}", None, owner, claim=True)
            native.settle(f"{index:064x}", owner, "complete", json.dumps({"reply": index}))

    return legacy_ledger(client, "ledger", history, tmp_path=tmp_path)


def test_a_migration_writes_its_records_concurrently_and_commits_the_head_only_after_all_of_them(tmp_path):
    from .s3_batch_storage import MIGRATION_WORKERS

    client = SlowRecordsS3()
    j = old_ledger_of(client, tmp_path, 60)
    before = store(journal(client, name="ledger")).export()
    client.order.clear()
    s = store(journal(client, name="ledger"))
    s.reserve("f" * 64, None, str(uuid4()), claim=True)  # the first write migrates
    assert 1 < client.widest <= MIGRATION_WORKERS
    migration = client.order[: client.order.index(j.head_key) + 1]
    records = [k for k in migration if "/records/" in k]
    assert len(records) == 3 * 60  # every attempt, owner and finished request, nothing left out
    assert migration[-1] == j.head_key and all("/records/" in k for k in migration[:-2])  # then blob and head
    assert s.export()["batches"][:60] == before["batches"] and s.journal.read().value["held"][0]["fingerprint"] == "f" * 64


def test_a_migration_interrupted_midway_leaves_the_old_head_and_a_rerun_finishes_it_without_paying_again(tmp_path):
    client, model = SlowRecordsS3(), Model()
    j = old_ledger_of(client, tmp_path, 40)
    old = j.read().value
    before = store(journal(client, name="ledger")).export()
    client.fail = lambda key: "/owner-" in key
    with pytest.raises(BatchUncertain):
        store(journal(client, name="ledger")).reserve("f" * 64, None, str(uuid4()), claim=True)
    assert j.read().value == old  # nothing committed: the old ledger still decides
    rerun = store(journal(client, name="ledger"))
    owner = str(uuid4())
    rerun.reserve("f" * 64, None, owner, claim=True)
    assert rerun.claim("f" * 64, owner) and rerun.requests() == 41
    assert rerun.journal.read().value["schema"] == "cyclotron-shared-batches/v2"
    assert rerun.journal.read().value["records"]["shared-batches/attempts-ledger"] == {"last": 41, "skipped": []}
    assert rerun.export()["batches"][:40] == before["batches"] and model.calls == 0
