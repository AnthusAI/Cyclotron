"""Embedded SDK can compose S3 root/shared/runtime stores without local files."""

import asyncio
from uuid import uuid4

import pytest

from .embedded_cyclotron_test import DEFINITION, Model, item
from .s3_journal_test import journal


def test_actual_embedded_decide_review_reopen_and_priming_use_no_sqlite_or_files(tmp_path, monkeypatch):
    from .embedded_cyclotron import Cyclotron
    from .priming_context_test import seed
    from .s3_embedded_storage import S3CyclotronStores
    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()

    def stores():
        return S3CyclotronStores(
            journal(client, name="embedded-root"),
            journal_for=lambda domain, name: journal(client, name=domain + "-" + name),
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("cloud embedded opened SQLite or created a local store")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("pathlib.Path.mkdir", forbidden)
    model = Model()
    with Cyclotron.open(None, DEFINITION, model, storage=stores(), priming_examples=seed()) as engine:
        decision = asyncio.run(engine.decide(item("operational")))
        asyncio.run(engine.review(decision.decision_id, "include", review_id="human", explanation="Exact human rationale 雪"))
        before = engine.subscribe()
        calls = model.calls
        assert engine.db is None and engine.wheels["relevant"].db is None and engine.shared.db is None
        assert engine.labels()[0]["explanation"] == "Exact human rationale 雪"
    with Cyclotron.open(None, DEFINITION, model, storage=stores()) as reopened:
        assert asyncio.run(reopened.decide(item("operational"))) == decision
        assert reopened.subscribe() == before and model.calls == calls
        assert reopened._partitions("relevant")[0][0].item.id == "synthetic-prime"
        with pytest.raises(ValueError, match="priming"):
            asyncio.run(reopened.decide(item("synthetic-prime")))
        from .priming_context_test import accepted

        asyncio.run(reopened.add_priming_examples(accepted("added-in-cloud", "Added", "exclude", "Reason"), added_by="editor-1"))
    with Cyclotron.open(None, DEFINITION, model, storage=stores(), priming_examples=seed()) as primed_again:
        assert primed_again.wheels["relevant"].active.config.example_ids == ("synthetic-prime", "added-in-cloud")
        assert primed_again.priming_additions()[0]["examples"] == {"relevant": ["added-in-cloud"]}
        asyncio.run(primed_again.retract_priming_examples({"relevant": ["added-in-cloud"]}, retracted_by="editor-2"))
    with Cyclotron.open(None, DEFINITION, model, storage=stores(), priming_examples=seed()) as retracted_again:
        assert retracted_again.wheels["relevant"].active.config.example_ids == ("synthetic-prime",)
        assert "added-in-cloud" not in [row.item.id for row in retracted_again._partitions("relevant")[0]]
        assert retracted_again.priming_additions()[0]["retractions"][0]["retracted_by"] == "editor-2"


def test_root_transaction_keeps_cursor_and_derived_evidence_atomic_on_failure():
    from .s3_embedded_storage import S3EmbeddedStorage

    class Interrupted(Exception):
        pass

    root = S3EmbeddedStorage(journal(name="root"))
    root.initialize()
    root.acquire(str(uuid4()))
    with pytest.raises(Interrupted):
        with root.transaction():
            root.insert_review("review", "decision", "classifier", '{"human":"exact"}')
            root.append_event('{"kind":"review"}')
            root.put_meta("cursor:classifier", "8")
            raise Interrupted()
    assert root.meta("cursor:classifier") is None and root.events(0, 100) == []
    assert root.review("review") is None
    root.release()


def test_root_records_past_the_old_record_budget_commit_together():
    from .s3_embedded_storage import S3EmbeddedStorage

    root = S3EmbeddedStorage(journal(name="root"))
    root.initialize()
    root.acquire(str(uuid4()))
    with root.transaction():
        root.insert_review("review", "decision", "classifier", '{"human":"exact"}')
        root.append_event('{"kind":"review","pad":"' + "x" * (5 * 1024 * 1024) + '"}')
        root.put_meta("cursor:classifier", "8")
    assert root.meta("cursor:classifier") == ("8",) and len(root.events(0, 100)) == 1
    assert root.review("review") is not None
    root.release()


def test_cloud_guard_denial_precedes_all_operation_s3_access_and_budget_is_absolute(monkeypatch):
    from .embedded_cyclotron import Cyclotron
    from .s3_embedded_storage import S3CyclotronStores
    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()

    def stores():
        return S3CyclotronStores(
            journal(client, name="root"),
            journal_for=lambda domain, name: journal(client, name=domain + "-" + name),
        )

    model = Model()
    with Cyclotron.open(None, DEFINITION, model, storage=stores(), max_requests=1) as engine:

        async def deny():
            raise PermissionError("revoked")

        before = len(client.calls)
        with pytest.raises(PermissionError):
            asyncio.run(engine.decide(item("denied"), execution_guard=deny))
        assert len(client.calls) == before and model.calls == 0
        asyncio.run(engine.decide(item("first")))
    with Cyclotron.open(None, DEFINITION, model, storage=stores(), max_requests=1) as engine:
        assert engine.shared.max_requests == 1 and engine.shared.requests == 1
        with pytest.raises(RuntimeError):
            asyncio.run(engine.decide(item("second", "different request")))
    assert model.calls == 1


def test_uncertain_root_commit_prevents_duplicate_derivation_until_a_fresh_owner_reopens():
    from .s3_embedded_storage import S3EmbeddedStorage
    from .s3_journal import JournalUncertain
    from .s3_runtime_storage import RuntimeOwnershipError

    j = journal(name="root")
    root = S3EmbeddedStorage(j)
    root.initialize()
    root.acquire(str(uuid4()))
    j.payloads.client.lose = lambda key: key == j.head_key
    with pytest.raises(JournalUncertain):
        with root.transaction():
            root.append_event('{"kind":"derived-once"}')
            root.put_meta("cursor:classifier", "8")
    before = len(j.payloads.client.calls)
    with pytest.raises(RuntimeOwnershipError):
        root.events(0, 100)
    assert len(j.payloads.client.calls) == before
    root.release()
    restored = S3EmbeddedStorage(journal(j.payloads.client, name="root"))
    restored.initialize()
    restored.acquire(str(uuid4()))
    assert restored.meta("cursor:classifier") == ("8",)
    assert restored.events(0, 100) == [(1, '{"kind":"derived-once"}')]
    restored.release()


def test_owned_snapshot_refresh_detects_operator_fence_change_before_operation_state_or_provider():
    from copy import deepcopy

    from .embedded_cyclotron import Cyclotron
    from .s3_embedded_storage import S3CyclotronStores
    from .s3_journal_test import ConditionalS3
    from .s3_runtime_storage import RuntimeOwnershipError

    client = ConditionalS3()
    composition = S3CyclotronStores(
        journal(client, name="root"), journal_for=lambda domain, name: journal(client, name=domain + "-" + name)
    )
    model = Model()
    engine = Cyclotron.open(None, DEFINITION, model, storage=composition)
    root = composition.root_storage
    record = root.journal.read()
    value = deepcopy(record.value)
    value["owner"] = str(uuid4())
    value["fence"] += 1
    root.journal.commit(record, value)
    with pytest.raises(RuntimeOwnershipError):
        asyncio.run(engine.decide(item("denied"), execution_guard=lambda: None))
    assert model.calls == 0 and root._cached is None
    with pytest.raises(RuntimeOwnershipError):
        root.meta("definition")
    # Simulated operator restores the previous owner only to clean this fixture;
    # production has no online takeover/reset endpoint.
    record = root.journal.read()
    value = deepcopy(record.value)
    value["owner"], value["fence"] = root.authority
    root.journal.commit(record, value)
    engine.close()


def test_one_item_can_be_read_without_listing_every_item():
    from .s3_embedded_storage import S3EmbeddedStorage
    from .s3_journal_test import journal

    storage = S3EmbeddedStorage(journal(name="root-item-lookup"))
    storage.initialize()
    storage.acquire(str(uuid4()))
    with storage.transaction():
        storage.put_item("known", '{"text":"yes"}', "fingerprint")
    assert storage.item_values("known") == '{"text":"yes"}'
    assert storage.item_values("missing") is None


def _root_bytes(client, root, since):
    """Bytes one root journal wrote after call ``since``: head, state blobs, records and keyed documents."""
    from .journal import decode

    prefix = root.head_key.rsplit("/", 1)[0] + "/"
    lakes = tuple(f"/records/workspace/{kind}-root/" for kind in ("decisions", "events", "usage", "items", "commits"))
    total = 0
    for kind, request in client.calls[since:]:
        key, body = request["Key"], request.get("Body", b"")
        if kind != "put":
            continue
        if key.startswith(prefix) or any(lake in key for lake in lakes):
            total += len(body)
        elif "/payloads/" in key:
            try:
                value = decode(body)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("namespace") == root.namespace:
                total += len(body)
    return total


def _stores(client):
    from .s3_embedded_storage import S3CyclotronStores

    return S3CyclotronStores(
        journal(client, name="root"), journal_for=lambda domain, name: journal(client, name=domain + "-" + name)
    )


def test_the_bytes_a_decision_writes_to_the_root_do_not_grow_with_the_decisions_before_it():
    from .embedded_cyclotron import Cyclotron
    from .s3_journal_test import ConditionalS3

    client, model = ConditionalS3(), Model()
    root = journal(client, name="root")

    def decide(number):
        before = len(client.calls)
        with Cyclotron.open(None, DEFINITION, model, storage=_stores(client), max_requests=None) as engine:
            asyncio.run(engine.decide(item(f"item-{number:03d}")))
        return _root_bytes(client, root, before)

    written = [decide(number) for number in range(1, 61)]
    # Nothing earlier is rewritten; only counters that gained a digit (positions, cursors) may differ.
    assert abs(written[59] - written[9]) <= 64, (written[9], written[59])


def test_one_decision_moves_the_root_head_once():
    from .embedded_cyclotron import Cyclotron
    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()
    stores = _stores(client)
    head = stores.root_storage.journal.head_key
    with Cyclotron.open(None, DEFINITION, Model(), storage=stores, max_requests=None) as engine:
        for number in range(3):
            before = len(client.calls)
            asyncio.run(engine.decide(item(f"item-{number}")))
            assert [r["Key"] for kind, r in client.calls[before:] if kind == "put"].count(head) == 1
        assert engine.root_storage.decision_count() == 3


def _owned_root(client=None):
    from .s3_embedded_storage import S3EmbeddedStorage

    root = S3EmbeddedStorage(journal(client, name="root"), cache_owned=True)
    root.initialize()
    root.acquire(str(uuid4()))
    return root


def _reopened(client):
    from .s3_embedded_storage import S3EmbeddedStorage

    root = S3EmbeddedStorage(journal(client, name="root"))
    root.initialize()
    root.acquire(str(uuid4()))
    return root


def _head_puts(client, head, since):
    return [r["Key"] for kind, r in client.calls[since:] if kind == "put"].count(head)


def test_coalesced_transactions_publish_in_one_commit_and_each_still_rolls_back_alone():
    root = _owned_root()
    client, head = root.journal.payloads.client, root.journal.head_key
    before = len(client.calls)
    with root.coalesced():
        root.append_event('{"n":1}')
        with pytest.raises(RuntimeError):
            with root.transaction():
                root.append_event('{"n":"rolled back"}')
                raise RuntimeError("synthetic failure")
        root.put_item("item-a", '{"v":1}', "fp")
        assert root.events(0, 10) == [(1, '{"n":1}')] and root.item_values("item-a") == '{"v":1}'
        assert _head_puts(client, head, before) == 0
    assert _head_puts(client, head, before) == 1
    root.release()
    fresh = _reopened(client)
    assert fresh.events(0, 10) == [(1, '{"n":1}')] and fresh.items() == [("item-a", '{"v":1}')]
    fresh.release()


def test_leaving_a_coalesced_scope_by_an_exception_still_publishes_what_it_held():
    root = _owned_root()
    with pytest.raises(LookupError):
        with root.coalesced():
            root.put_meta("cursor", "7")
            raise LookupError("the model call failed")
    root.refresh()
    assert root.meta("cursor") == ("7",)
    root.release()


def test_records_and_keyed_entries_of_a_commit_whose_head_never_moved_stay_invisible():
    from copy import deepcopy

    from .s3_journal_test import S3Error
    from .storage_errors import StorageUncertain

    root = _owned_root()
    client, head = root.journal.payloads.client, root.journal.head_key
    real = client.put_object

    def head_fails(**request):
        if request["Key"] == head:
            raise S3Error("InternalError")
        return real(**request)

    client.put_object = head_fails
    with pytest.raises(StorageUncertain):
        with root.transaction():
            root.put_item("item-a", '{"v":1}', "fp")
            root.insert_decision(("d1", "item-a", "fp", "open", '{"payload":"d1"}'))
            root.append_event('{"kind":"decision"}')
    client.put_object = real
    # An operator reconciles the poisoned owner; the head still holds the state before that commit.
    record = root.journal.read()
    value = deepcopy(record.value)
    value["owner"] = None
    root.journal.commit(record, value)
    fresh = _reopened(client)
    assert fresh.decision("d1") is None and fresh.item_values("item-a") is None and fresh.events(0, 10) == []
    assert fresh.decision_count() == 0 and fresh.latest_decision("item-a") is None
    # The next commit skips the abandoned sequences, and the abandoned keyed entries never count.
    fresh.insert_decision(("d2", "item-a", "fp", "open", '{"payload":"d2"}'))
    assert fresh.latest_decision("item-a") == ("open", '{"payload":"d2"}') and fresh.decision_ids() == [("d2",)]
    assert fresh.decision("d1") is None and fresh.latest_id("item-a") == ("d2",)
    fresh.release()


def test_the_live_decision_of_an_item_follows_supersede_and_revival_in_any_order():
    root = _owned_root()
    for name in ("d1", "d2", "d3"):
        root.insert_decision((name, "item-a", "fp-" + name, "open", '{"payload":"' + name + '"}'))
    root.set_decision_state("d3", "superseded")
    root.set_decision_state("d2", "superseded")
    assert root.latest_decision("item-a") == ("open", '{"payload":"d1"}')
    root.set_decision_state("d1", "superseded")
    assert root.latest_decision("item-a") is None
    root.set_decision_state("d2", "reviewed")
    assert root.latest_decision("item-a") == ("reviewed", '{"payload":"d2"}')
    root.set_decision_state("d1", "open")
    assert root.latest_decision("item-a") == ("reviewed", '{"payload":"d2"}')
    root.set_decision_state("d3", "open")
    assert root.latest_decision("item-a") == ("open", '{"payload":"d3"}')
    root.set_decision_state("d3", "superseded")
    assert root.latest_decision("item-a") == ("reviewed", '{"payload":"d2"}')
    assert root.latest_id("item-a") == ("d3",) and root.decision_fingerprint("d1") == "fp-d1"
    root.release()


def _v1_root(client):
    """A v1 root as production holds it: every record inside the head, item values inline and by reference."""
    from .s3_embedded_storage import legacy_empty

    j = journal(client, name="root")
    stored = j.payloads.put(b'{"v":"by reference"}')
    value = legacy_empty()
    data = value["data"]
    data["meta"] = {"definition": "{}", "cursor:relevant": "4"}
    data["items"] = {
        "item-a": ['{"v":"inline"}', "fp-a"],
        "item-b": [{"digest": stored.digest, "length": stored.length}, "fp-b"],
    }
    data["decisions"] = [
        ["d1", "item-a", "fp-a", "superseded", '{"payload":"d1"}'],
        ["d2", "item-a", "fp-a", "reviewed", '{"payload":"d2"}'],
        ["d3", "item-b", "fp-b", "open", '{"payload":"d3"}'],
    ]
    data["reviews"] = [[1, "r1", "d2", "relevant", '{"review":1}']]
    data["events"] = [[1, '{"kind":"decision"}'], [2, '{"kind":"review"}'], [3, '{"kind":"decision"}']]
    data["learning"] = [[1, "relevant", 3, "2026-01-01", 0]]
    data["usage"] = [[1, "d1", "decision_model", 10, 2, 5, "shared-batch"], [2, "d3", "optimizer", 7, 0, 1, None]]
    data["shares"] = [[1, "relevant", 0.25], [1, "urgent", 0.75]]
    j.initialize(value)
    return j


def test_a_v1_root_is_converted_by_its_first_owner_and_keeps_every_record_readable():
    from .s3_embedded_storage import SCHEMA
    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()
    legacy = _v1_root(client)
    root = _reopened(client)
    head = root.journal.read().value
    assert head["schema"] == SCHEMA and set(head["data"]) == {"meta", "reviews", "learning", "latest", "late_shares"}
    assert root.meta("cursor:relevant") == ("4",)
    assert root.items() == [("item-a", '{"v":"inline"}'), ("item-b", '{"v":"by reference"}')]
    assert root.decision_rows() == [
        ("d1", "superseded", '{"payload":"d1"}'),
        ("d2", "reviewed", '{"payload":"d2"}'),
        ("d3", "open", '{"payload":"d3"}'),
    ]
    assert root.latest_decision("item-a") == ("reviewed", '{"payload":"d2"}') and root.latest_id("item-a") == ("d2",)
    assert root.latest_id() == ("d3",) and root.decision_fingerprint("d3") == "fp-b"
    assert root.decision("d1") == ('{"payload":"d1"}',)
    assert root.events(1, 10) == [(2, '{"kind":"review"}'), (3, '{"kind":"decision"}')]
    assert root.review("r1") == ('{"review":1}',) and root.next_learning() == (1, "relevant", 3)
    assert root.usage_by_output() == [(1, "decision_model", 10, 2, 5, "shared-batch"), (2, "optimizer", 7, 0, 1, None)]
    assert sorted(root.usage_shares()) == [(1, "relevant", 0.25), (1, "urgent", 0.75)]
    # New records continue after the converted ones, so subscribe cursors and usage ids keep their meaning.
    assert root.append_event('{"kind":"new"}') == 4 and root.insert_usage("d3", "m", 1, 0, 1, None) == 3
    with pytest.raises(ValueError):
        root.insert_decision(("d2", "item-b", "fp", "open", "{}"))
    with pytest.raises(ValueError):
        root.insert_usage_share(1, "relevant", 0.5)
    root.release()
    reopened = _reopened(client)
    assert reopened.events(3, 10) == [(4, '{"kind":"new"}')] and reopened.decision_count() == 3
    assert legacy.read().value["schema"] == SCHEMA
    reopened.release()


def test_a_conversion_whose_head_never_moved_leaves_the_v1_root_to_convert_on_the_next_acquire():
    from .s3_embedded_storage import LEGACY_SCHEMA
    from .s3_journal_test import ConditionalS3, S3Error
    from .storage_errors import StorageUncertain

    client = ConditionalS3()
    legacy = _v1_root(client)
    real = client.put_object

    def head_fails(**request):
        if request["Key"] == legacy.head_key:
            raise S3Error("InternalError")
        return real(**request)

    client.put_object = head_fails
    with pytest.raises(StorageUncertain):
        _reopened(client)
    client.put_object = real
    assert legacy.read().value["schema"] == LEGACY_SCHEMA
    root = _reopened(client)
    assert root.decision_ids() == [("d1",), ("d2",), ("d3",)] and root.events(0, 10)[-1] == (3, '{"kind":"decision"}')
    assert root.items()[0] == ("item-a", '{"v":"inline"}') and root.latest_decision("item-b") == ("open", '{"payload":"d3"}')
    root.release()


def test_a_conversion_writes_its_records_and_keyed_documents_concurrently_and_moves_the_head_last():
    import threading
    import time

    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()
    legacy = _v1_root(client)
    real, lock, state = client.put_object, threading.Lock(), {"now": 0, "most": 0}

    def overlapping(**request):
        with lock:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
        time.sleep(0.005)  # long enough for concurrent writers to overlap
        try:
            return real(**request)
        finally:
            with lock:
                state["now"] -= 1

    client.put_object = overlapping
    before = len(client.calls)
    root = _reopened(client)
    puts = [r["Key"] for kind, r in client.calls[before:] if kind == "put"]
    assert state["most"] > 1
    assert puts[-1] == legacy.head_key and puts.count(legacy.head_key) == 1
    assert sum("/records/workspace/" in key for key in puts) == 3 + 3 + 2 + 2 + 1
    assert sum("/keys/" in key for key in puts) == 3 + 2
    root.release()


def test_a_v1_root_whose_event_ids_are_not_one_to_n_is_refused_rather_than_renumbered():
    from .s3_embedded_storage import legacy_empty
    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()
    value = legacy_empty()
    value["data"]["events"] = [[1, "{}"], [3, "{}"]]
    journal(client, name="root").initialize(value)
    with pytest.raises(ValueError, match="1..n"):
        _reopened(client)


# Owned reads -----------------------------------------------------------------------------------------------


def _gets(client, since):
    return [r["Key"] for kind, r in client.calls[since:] if kind == "get"]


def test_an_owned_root_serves_repeated_reads_without_a_head_get():
    root = _owned_root()
    root.put_meta("cursor", "7")
    client = root.journal.payloads.client
    mark = len(client.calls)
    for _ in range(20):
        assert root.meta("cursor") == ("7",)
    assert _gets(client, mark) == []
    root.release()


def test_a_conflicting_external_commit_still_poisons_the_owning_root_on_its_next_write():
    from copy import deepcopy

    from .s3_runtime_storage import RuntimeOwnershipError
    from .storage_errors import StorageConflict

    root = _owned_root()
    root.put_meta("cursor", "7")
    rival = journal(root.journal.payloads.client, name="root")
    record = rival.read()
    value = deepcopy(record.value)
    value["data"]["meta"]["cursor"] = "rival"
    rival.commit(record, value)
    assert root.meta("cursor") == ("7",)  # served from the record this owner committed
    with pytest.raises(StorageConflict):
        root.put_meta("cursor", "8")
    assert root.poisoned
    with pytest.raises(RuntimeOwnershipError):
        root.meta("cursor")


def _composition(client):
    from .s3_embedded_storage import S3CyclotronStores

    composition = S3CyclotronStores(
        journal(client, name="root"), journal_for=lambda domain, name: journal(client, name=domain + "-" + name)
    )
    composition.acquire()
    return composition


def test_a_guarded_call_reads_each_owned_head_once_at_the_same_time_and_no_transaction_again():
    import threading
    import time

    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()
    composition = _composition(client)
    runtimes = [composition.runtime_for_classifier(name) for name in ("a", "b", "c")]
    runtimes[0].runtime_storage.put_state("k", '{"v":1}')
    real, lock, state = client.get_object, threading.Lock(), {"now": 0, "most": 0}

    def overlapping(**request):
        with lock:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
        time.sleep(0.005)  # long enough for concurrent reads to overlap
        try:
            return real(**request)
        finally:
            with lock:
                state["now"] -= 1

    client.get_object = overlapping
    mark = len(client.calls)
    composition.execution_boundary()
    heads = [composition.root_storage.journal.head_key, *(runtime.journal.head_key for runtime in runtimes)]
    assert sorted(_gets(client, mark)) == sorted(heads)  # one head GET per owned journal, nothing else
    assert state["most"] > 1
    client.get_object = real
    composition.release()


def test_a_guarded_call_still_refuses_a_runtime_whose_fence_an_operator_moved():
    from copy import deepcopy

    from .s3_journal_test import ConditionalS3
    from .s3_runtime_storage import RuntimeOwnershipError

    client = ConditionalS3()
    composition = _composition(client)
    runtime = composition.runtime_for_classifier("a")
    operator = journal(client, name=runtime.journal.namespace.rsplit("/", 1)[-1])
    record = operator.read()
    value = deepcopy(record.value)
    value["owner"], value["fence"] = str(uuid4()), value["fence"] + 1
    operator.commit(record, value)
    with pytest.raises(RuntimeOwnershipError):
        composition.execution_boundary()
