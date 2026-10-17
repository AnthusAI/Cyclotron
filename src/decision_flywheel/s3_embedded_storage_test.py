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
            max_records=10000,
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


def test_root_transaction_keeps_cursor_and_derived_evidence_atomic_on_size_failure():
    from .s3_embedded_storage import S3EmbeddedStorage

    root = S3EmbeddedStorage(journal(name="root"), max_records=2)
    root.initialize()
    root.acquire(str(uuid4()))
    with pytest.raises(ValueError):
        with root.transaction():
            root.insert_review("review", "decision", "classifier", '{"human":"exact"}')
            root.append_event('{"kind":"review"}')
            root.put_meta("cursor:classifier", "8")
    assert root.meta("cursor:classifier") is None and root.events(0, 100) == []
    assert root.review("review") is None
    root.release()


def test_cloud_guard_denial_precedes_all_operation_s3_access_and_budget_is_absolute(monkeypatch):
    from .embedded_cyclotron import Cyclotron
    from .s3_embedded_storage import S3CyclotronStores
    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()

    def stores():
        return S3CyclotronStores(
            journal(client, name="root"),
            max_records=10000,
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
    root = S3EmbeddedStorage(j, max_records=100)
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
    restored = S3EmbeddedStorage(journal(j.payloads.client, name="root"), max_records=100)
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
        journal(client, name="root"), max_records=1000, journal_for=lambda domain, name: journal(client, name=domain + "-" + name)
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

    storage = S3EmbeddedStorage(journal(name="root-item-lookup"), max_records=1000)
    storage.initialize()
    storage.acquire(str(uuid4()))
    with storage.transaction():
        storage.put_item("known", '{"text":"yes"}', "fingerprint")
    assert storage.item_values("known") == '{"text":"yes"}'
    assert storage.item_values("missing") is None
