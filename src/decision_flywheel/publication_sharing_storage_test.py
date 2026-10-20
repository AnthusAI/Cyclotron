"""Owner sharing records commit atomically through the existing canonical journal."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from .batch_storage import StorageScope
from .journal import decode, encode
from .publication_sharing import GrantSpec, RequestRecord, SharingConflict, SharingGrant, SharingUnavailable, SourceReference
from .publication_sharing_storage import JournalSharingStore, sharing_journal_name
from .s3_journal import S3Journal
from .s3_journal_test import ConditionalS3
from .storage_payloads import S3Payloads

SCOPE = StorageScope("owner", "sharing-workspace")
ENGINE = "central-engine"
NOW = "2026-10-20T10:00:00+00:00"


def journal(client=None, *, scope=SCOPE, engine=ENGINE, max_commits=1000):
    return S3Journal(
        S3Payloads(client or ConditionalS3(), "synthetic-bucket", "111111111111", scope, max_bytes=1_000_000),
        "publication-sharing",
        sharing_journal_name(engine),
        max_bytes=1_000_000,
        max_commits=max_commits,
    )


def store(j=None, *, max_records=1000, max_bytes=1_000_000):
    return JournalSharingStore(j or journal(), scope=SCOPE, engine_id=ENGINE, max_records=max_records, max_bytes=max_bytes)


def grant(identifier="grant-one"):
    return SharingGrant(
        GrantSpec(
            identifier,
            "owner",
            "consumer",
            "publication",
            ENGINE,
            "binding-one",
            ("visible", "private"),
            ("visible",),
            datetime(2027, 1, 1, tzinfo=timezone.utc),
            10,
            "owner",
            ("infer", "deliver"),
        ),
        "owner-person",
    )


def event(kind, value, **extra):
    spec = value.spec
    return {
        "kind": kind,
        "at": NOW,
        "grantId": spec.grant_id,
        "grantRevision": value.revision,
        "ownerAccountId": "owner",
        "consumerAccountId": "consumer",
        "publicationId": "publication",
        "engineId": ENGINE,
        "bindingRevision": "binding-one",
        "actorAccountId": "owner" if kind in ("issued", "revoked") else "consumer",
        "actorId": "owner-person" if kind in ("issued", "revoked") else "consumer-person",
        "billingAccountId": "owner",
        "billingPolicy": "owner-pays",
        **extra,
    }


def issue(storage, value=None):
    value = value or grant()
    with storage.transaction() as tx:
        tx.save_grant(value)
        tx.append_audit(event("issued", value))
    return value


def reserve(storage, *, identifier="request-one"):
    with storage.transaction() as tx:
        value = tx.load_grant("grant-one")
        request = RequestRecord(("consumer", "publication", "consumer-person", identifier), "a" * 64, "grant-one", 1, "b" * 64)
        source = SourceReference("consumer", None, "list", identifier, 1, "c" * 64)
        tx.save_grant(replace(value, reserved_requests=value.reserved_requests + 1))
        tx.save_request(request)
        tx.save_source(("consumer", "list", identifier, 1), "c" * 64)
        tx.append_audit(event("reserved", value, requestId=identifier, source=source.to_json()))
    return request


def revoke(storage, identifier="grant-one"):
    with storage.transaction() as tx:
        value = tx.load_grant(identifier)
        revoked = replace(value, status="revoked", revision=value.revision + 1)
        tx.save_grant(revoked)
        tx.append_audit(event("revoked", revoked))


def complete(storage, request):
    receipt = {
        "contractVersion": "cyclotron-sharing/v1",
        "receiptId": "receipt-one",
        "grantId": "grant-one",
        "grantRevision": 1,
        "ownerAccountId": "owner",
        "consumerAccountId": "consumer",
        "publicationId": "publication",
        "engineId": ENGINE,
        "bindingRevision": "binding-one",
        "requestId": request.key[3],
        "decisionId": "decision-one",
        "source": SourceReference("consumer", None, "list", request.key[3], 1, "c" * 64).to_json(),
        "outputs": {"visible": {"label": "match", "confidence": None, "version": 1}},
        "billing": {"accountId": "owner", "policy": "owner-pays", "reservedRequests": 1, "providerRequests": 1},
    }
    with storage.transaction() as tx:
        tx.save_request(replace(request, status="completed", receipt_json=encode(receipt).decode()))
        tx.append_audit(
            event(
                "completed",
                tx.load_grant("grant-one"),
                requestId=request.key[3],
                source=receipt["source"],
                receiptId=receipt["receiptId"],
                decisionId=receipt["decisionId"],
                providerRequests=1,
            )
        )
    return receipt


def test_initialization_is_explicit_and_reads_never_create_or_reset_authority():
    j = journal()
    storage = store(j)
    assert not j.payloads.client.calls
    with pytest.raises(SharingUnavailable):
        with storage.transaction():
            pass
    with pytest.raises(SharingUnavailable):
        storage.open()
    assert not any(kind == "put" for kind, _ in j.payloads.client.calls)
    assert storage.initialize() is storage
    issue(storage)
    del j.payloads.client.objects[j.head_key]
    before = len(j.payloads.client.calls)
    with pytest.raises(SharingUnavailable):
        store(journal(j.payloads.client)).initialize()
    assert not any(kind == "put" for kind, _ in j.payloads.client.calls[before:])


def test_scope_and_engine_are_checked_before_any_storage_access():
    j = journal()
    for scope, engine in [(StorageScope("other", SCOPE.workspace), ENGINE), (SCOPE, "other-engine")]:
        with pytest.raises(ValueError):
            JournalSharingStore(j, scope=scope, engine_id=engine, max_records=100, max_bytes=1_000_000)
    assert not j.payloads.client.calls


@pytest.mark.parametrize("backend", ["s3", "file"])
def test_records_survive_reopen_with_native_tuple_datetime_and_private_claim(backend, tmp_path):
    if backend == "file":
        from .file_journal import LocalFileJournal
        from .storage_payloads import FilePayloads

        def fresh():
            return LocalFileJournal(
                FilePayloads(tmp_path, SCOPE, max_bytes=1_000_000),
                "publication-sharing",
                sharing_journal_name(ENGINE),
                max_bytes=1_000_000,
                max_commits=1000,
            )
    else:
        client = ConditionalS3()
        fresh = lambda: journal(client)
    storage = store(fresh()).initialize()
    issued = issue(storage)
    request = reserve(storage)
    reopened = store(fresh()).open()
    with reopened.transaction() as tx:
        assert tx.load_grant(issued.spec.grant_id) == replace(issued, reserved_requests=1)
        assert tx.load_request(request.key) == request
        assert tx.load_source(("consumer", "list", "request-one", 1)) == "c" * 64
    assert len(reopened.journal.read().value["audit"]) == 2


def test_failed_transaction_rolls_back_grant_request_source_and_audit_together():
    storage = store().initialize()
    value = issue(storage)
    before = storage.journal.read()
    with pytest.raises(RuntimeError):
        with storage.transaction() as tx:
            tx.save_grant(replace(value, reserved_requests=1))
            tx.append_audit(event("reserved", value))
            raise RuntimeError("cancelled before commit")
    assert storage.journal.read() == before


def test_two_independent_transactions_cannot_overwrite_each_others_grants_or_audit():
    left = store().initialize()
    right = store(journal(left.journal.payloads.client)).open()
    a, b = grant("a"), grant("b")
    with pytest.raises(SharingConflict):
        with left.transaction() as first:
            first.save_grant(a)
            first.append_audit(event("issued", a))
            issue(right, b)
    with left.transaction() as tx:
        assert tx.load_grant("a") is None and tx.load_grant("b") == b
    assert len(left.journal.read().value["audit"]) == 1


def test_lost_commit_acknowledgement_poisoned_handle_never_regrants_a_request():
    storage = store().initialize()
    issue(storage)
    storage.journal.payloads.client.lose = lambda key: key == storage.journal.head_key
    with pytest.raises(SharingUnavailable):
        reserve(storage)
    before = len(storage.journal.payloads.client.calls)
    for action in [storage.open, storage.initialize]:
        with pytest.raises(SharingUnavailable):
            action()
    with pytest.raises(SharingUnavailable):
        with storage.transaction():
            pass
    assert len(storage.journal.payloads.client.calls) == before
    reopened = store(journal(storage.journal.payloads.client)).open()
    with reopened.transaction() as tx:
        assert tx.load_grant("grant-one").reserved_requests == 1
        assert tx.load_request(("consumer", "publication", "consumer-person", "request-one")).status == "reserved"
    assert len(reopened.journal.read().value["audit"]) == 2


@pytest.mark.parametrize("outcome", ["success", "rollback", "conflict", "uncertain"])
def test_transaction_objects_are_invalid_after_every_exit_and_nested_use_is_rejected(outcome):
    storage = store().initialize()
    retained = None
    try:
        with storage.transaction() as retained:
            with pytest.raises(SharingConflict):
                with storage.transaction():
                    pass
            value = grant()
            retained.save_grant(value)
            retained.append_audit(event("issued", value))
            if outcome == "rollback":
                raise RuntimeError("abort")
            if outcome == "conflict":
                issue(store(journal(storage.journal.payloads.client)).open(), grant("other"))
            if outcome == "uncertain":
                storage.journal.payloads.client.lose = lambda key: key == storage.journal.head_key
    except (RuntimeError, SharingConflict, SharingUnavailable):
        pass
    with pytest.raises(SharingUnavailable):
        retained.load_grant("grant-one")
    with pytest.raises(SharingUnavailable):
        retained.append_audit({})


def test_audit_inputs_are_copied_and_no_write_occurs_for_read_only_transactions():
    storage = store().initialize()
    value = grant()
    audit = event("issued", value)
    with storage.transaction() as tx:
        tx.save_grant(value)
        tx.append_audit(audit)
        audit["actorId"] = "changed"
    before = len(storage.journal.payloads.client.calls)
    with storage.transaction() as tx:
        assert tx.load_grant("grant-one") == value
    assert not any(kind == "put" for kind, _ in storage.journal.payloads.client.calls[before:])
    assert storage.journal.read().value["audit"][0]["event"]["actorId"] == "owner-person"


@pytest.mark.parametrize("change", ["scope", "counter", "request", "source", "audit", "reactivate"])
def test_existing_authority_cannot_be_rewritten_or_rolled_back(change):
    storage = store().initialize()
    issue(storage)
    request = reserve(storage)
    if change == "reactivate":
        revoke(storage)
    before = storage.journal.read()
    with pytest.raises((SharingConflict, SharingUnavailable, ValueError)):
        with storage.transaction() as tx:
            value = tx.load_grant("grant-one")
            if change == "scope":
                tx.save_grant(replace(value, spec=replace(value.spec, publication_id="different")))
            elif change == "counter":
                tx.save_grant(replace(value, reserved_requests=0))
            elif change == "request":
                tx.save_request(replace(request, fingerprint="d" * 64))
            elif change == "source":
                tx.save_source(("consumer", "list", "request-one", 1), "d" * 64)
            elif change == "audit":
                tx.append_audit(event("issued", value))
            else:
                tx.save_grant(replace(value, status="active", revision=1))
    assert storage.journal.read() == before


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(ownerAccountId="other"),
        lambda value: value.update(engineId="other"),
        lambda value: value["grants"][0]["spec"].update(visible_outputs=["private"]),
        lambda value: value["requests"][0].update(key=["other", "publication", "consumer-person", "request-one"]),
        lambda value: value["requests"][0].update(grant_revision=2),
        lambda value: value["audit"].clear(),
    ],
)
def test_corrupt_canonical_records_fail_closed_when_reopened(mutation):
    storage = store().initialize()
    issue(storage)
    reserve(storage)
    record = storage.journal.read()
    value = deepcopy(record.value)
    mutation(value)
    storage.journal.commit(record, value)
    with pytest.raises(SharingUnavailable):
        store(journal(storage.journal.payloads.client)).open()


@pytest.mark.parametrize("limit", ["records", "bytes", "commits"])
def test_admission_preserves_enough_capacity_to_revoke_every_active_grant_after_restart(limit):
    j = journal(max_commits=5 if limit == "commits" else 1000)
    storage = store(
        j, max_records=6 if limit == "records" else 1000, max_bytes=20_000 if limit == "bytes" else 1_000_000
    ).initialize()
    issue(storage, grant("a"))
    issue(storage, grant("b"))
    before = storage.journal.read()
    with pytest.raises(SharingUnavailable):
        issue(storage, grant("c"))
    assert storage.journal.read() == before
    for identifier in ("a", "b"):
        reopened = store(
            journal(j.payloads.client, max_commits=j.max_commits),
            max_records=storage.max_records,
            max_bytes=storage.max_bytes,
        ).open()
        revoke(reopened, identifier)
    with reopened.transaction() as tx:
        assert tx.load_grant("a").status == tx.load_grant("b").status == "revoked"


def test_private_receipt_rewrite_and_foreign_provenance_are_refused():
    storage = store().initialize()
    issue(storage)
    request = reserve(storage)
    before = storage.journal.read()
    with pytest.raises((SharingUnavailable, ValueError)):
        with storage.transaction() as tx:
            tx.save_request(replace(request, status="completed", receipt_json=encode({"grantId": "other"}).decode()))
    assert storage.journal.read() == before


def test_completed_receipts_reopen_after_revocation_without_becoming_mutable():
    storage = store().initialize()
    issue(storage)
    request = reserve(storage)
    receipt = complete(storage, request)
    revoke(storage)
    reopened = store(journal(storage.journal.payloads.client)).open()
    with reopened.transaction() as tx:
        completed = tx.load_request(request.key)
        assert completed.status == "completed" and completed.grant_revision == 1
        assert decode(completed.receipt_json) == receipt
        assert tx.load_grant("grant-one").revision == 2
    before = reopened.journal.read()
    for replacement in [replace(completed, status="reserved", receipt_json=None), replace(completed, receipt_json="{}")]:
        with pytest.raises(SharingConflict):
            with reopened.transaction() as tx:
                tx.save_request(replacement)
        assert reopened.journal.read() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("consumerAccountId", "other"),
        ("publicationId", "other"),
        ("grantId", "other"),
        ("requestId", "other"),
        ("outputs", {"private": {"label": "match", "confidence": None, "version": 1}}),
        ("billing", {"accountId": "consumer", "policy": "owner-pays", "reservedRequests": 1, "providerRequests": 1}),
    ],
)
def test_completed_private_receipts_cannot_be_rebound_to_a_different_authority(field, value):
    storage = store().initialize()
    issue(storage)
    request = reserve(storage)
    complete(storage, request)
    head = storage.journal.read()
    changed = deepcopy(head.value)
    receipt = decode(changed["requests"][0]["receipt_json"])
    receipt[field] = value
    changed["requests"][0]["receipt_json"] = encode(receipt).decode()
    storage.journal.commit(head, changed)
    with pytest.raises(SharingUnavailable):
        store(journal(storage.journal.payloads.client)).open()


def test_audit_cannot_contain_arbitrary_private_fields_or_disclose_a_pending_receipt():
    storage = store().initialize()
    value = grant()
    before = storage.journal.read()
    with pytest.raises(ValueError):
        with storage.transaction() as tx:
            tx.save_grant(value)
            tx.append_audit(event("issued", value, privatePolicy="must not be stored"))
    assert storage.journal.read() == before
    issue(storage)
    request = reserve(storage)
    before = storage.journal.read()
    with pytest.raises(ValueError):
        with storage.transaction() as tx:
            tx.append_audit(
                event(
                    "disclosed",
                    value,
                    requestId=request.key[3],
                    source=SourceReference("consumer", None, "list", "request-one", 1, "c" * 64).to_json(),
                    receiptId="receipt",
                    decisionId="decision",
                    providerRequests=1,
                )
            )
    assert storage.journal.read() == before


@pytest.mark.parametrize("field", ["providerRequests", "sourceRevision"])
def test_audit_integer_attribution_cannot_be_replaced_by_equal_python_booleans(field):
    storage = store().initialize()
    issue(storage)
    complete(storage, reserve(storage))
    head = storage.journal.read()
    changed = deepcopy(head.value)
    audit = changed["audit"][-1]["event"]
    if field == "providerRequests":
        audit["providerRequests"] = True
    else:
        audit["source"]["revision"] = True
    storage.journal.commit(head, changed)
    with pytest.raises(SharingUnavailable):
        store(journal(storage.journal.payloads.client)).open()


def test_concurrent_reservations_cannot_lose_consumed_quota_or_revocation():
    storage = store().initialize()
    value = issue(storage)
    other = store(journal(storage.journal.payloads.client)).open()
    with pytest.raises(SharingConflict):
        with storage.transaction() as tx:
            revoked = replace(value, status="revoked", revision=2)
            tx.save_grant(revoked)
            tx.append_audit(event("revoked", revoked))
            reserve(other)
    revoke(storage)
    with other.transaction() as tx:
        assert tx.load_grant("grant-one").status == "revoked"
        assert tx.load_grant("grant-one").reserved_requests == 1
        assert tx.load_request(("consumer", "publication", "consumer-person", "request-one")).status == "reserved"


def test_every_authority_mutation_requires_corresponding_audit_and_counter_evidence():
    storage = store().initialize()
    with pytest.raises((SharingUnavailable, ValueError)):
        with storage.transaction() as tx:
            tx.save_grant(grant())
    assert storage.journal.read().value["grants"] == []
    issue(storage)
    with pytest.raises((SharingUnavailable, ValueError)):
        with storage.transaction() as tx:
            tx.save_grant(replace(grant(), reserved_requests=1))
    assert storage.journal.read().value["grants"][0]["reserved_requests"] == 0
