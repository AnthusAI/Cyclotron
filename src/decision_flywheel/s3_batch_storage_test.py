"""Complete shared-request domain against canonical S3 CAS; no DynamoDB or SQLite."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from .batch_storage import BatchConflict, BatchInProgress, BatchUncertain
from .s3_journal_test import journal


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


def test_real_shared_consumer_never_calls_provider_after_uncertain_claim(monkeypatch):
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
    claim = s.claim

    def lose_claim(key, owner):
        s.journal.payloads.client.lose = lambda key: key == s.journal.head_key
        return claim(key, owner)

    monkeypatch.setattr(s, "claim", lose_claim)
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


def test_a_reply_leaves_the_journal_and_the_journal_keeps_only_a_reference(lake_world):
    s = lake_world.open()
    text = reply_text(20000)
    paid(s, "a" * 64, text)
    state = s.journal.read().value
    row = state["batches"][0]
    assert text not in json.dumps(state) and set(row[2]) == {"seq", "sha256", "length"} and row[2]["seq"] == 1
    assert state["records"]["shared-batches/replies-run-a"] == {"last": 1, "skipped": []}
    assert s.get("a" * 64).payload == text and s.requests() == 1
    assert lake_world.stored_replies(s) == [1]


def test_journal_bytes_per_paid_request_no_longer_depend_on_the_reply(lake_world):
    from .s3_batch_storage import S3BatchStorage

    def grow(storage, size, count=25):
        base = journal_bytes(storage)
        for index in range(count):
            paid(storage, f"{index:064x}", reply_text(size, marker=chr(97 + index % 26)))
        return (journal_bytes(storage) - base) / count

    embedded = S3BatchStorage(journal(name="embedded"))
    embedded.initialize()
    before = grow(embedded, 23000)
    small, large = lake_world.open("small"), lake_world.open("large")
    after_small, after_large = grow(small, 2000), grow(large, 150000)
    assert before > 23000
    assert after_small < 700 and after_large < 700
    assert abs(after_large - after_small) < 8  # a hundred times larger replies add no journal bytes
    assert len(large.replies.sequences()) == 25


def test_the_journal_grows_linearly_with_requests_not_with_history_of_replies(lake_world):
    s = lake_world.open()
    sizes = []
    for index in range(30):
        paid(s, f"{index:064x}", reply_text(5000))
        sizes.append(journal_bytes(s))
    steps = [b - a for a, b in zip(sizes, sizes[1:])]
    assert max(steps) < 700 and max(steps) - min(steps) < 80  # constant step: each commit adds one small row


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
    assert "records" not in state  # but no control state covers it
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
    # The loser's first record (1) was beaten to its sequence and left behind; both flips use fresh numbers.
    assert sorted(r[2]["seq"] for r in reader.journal.read().value["batches"]) == [2, 3]
    assert reader.journal.read().value["records"]["shared-batches/replies-run-a"] == {"last": 3, "skipped": [1]}


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
    assert again.journal.read().value["batches"][0][2] == '{"paid":true}'  # embedded, legacy form


def test_a_reply_larger_than_the_old_four_mib_reply_budget_goes_to_the_lake_and_reads_back(lake_world):
    from . import s3_batch_storage

    assert not hasattr(s3_batch_storage, "REPLY_RECORD_BYTES")
    s = lake_world.open()
    large = reply_text(5 * 1024 * 1024)
    paid(s, "a" * 64, large)
    held = s.journal.read().value["batches"][0][2]
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


def test_legacy_embedded_replies_still_read_and_new_ones_join_them_in_the_lake(lake_world):
    from .s3_batch_storage import S3BatchStorage

    j = journal(lake_world.client)
    legacy = S3BatchStorage(j)
    legacy.initialize()
    old = paid(legacy, "a" * 64, '{"old":true}')
    upgraded = lake_world.open()
    assert upgraded.get("a" * 64).payload == '{"old":true}'
    assert upgraded.reserve("a" * 64, 5, str(uuid4())).payload == '{"old":true}'
    assert upgraded.reserve("a" * 64, 5, old, policy="refresh").payload == '{"old":true}'
    paid(upgraded, "b" * 64, '{"new":true}')
    snapshot = upgraded.export()
    assert [row[2] for row in snapshot["batches"]] == ['{"old":true}', '{"new":true}']
    assert isinstance(upgraded.journal.read().value["batches"][0][2], str)
    assert isinstance(upgraded.journal.read().value["batches"][1][2], dict)
    # a refresh moves the lake-held reply into history; its original owner still replays it
    new_owner = str(uuid4())
    upgraded.reserve("b" * 64, 5, new_owner, policy="refresh")
    replay_owner = upgraded.journal.read().value["attempts"][1][2]
    assert upgraded.reserve("b" * 64, 5, replay_owner, policy="refresh").payload == '{"new":true}'
    assert upgraded.export()["history"][-1][4] == '{"new":true}'
    # a ledger that cites reply records cannot be opened without a lake
    blind = S3BatchStorage(journal(lake_world.client))
    with pytest.raises(ValueError, match="lake"):
        blind.requests()


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
    assert target.replies.sequences() == [1, 2] and isinstance(target.journal.read().value["batches"][1][2], dict)
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
