"""Synthetic S3 conditional-write semantics; no cloud credentials or SDK required."""

from hashlib import sha256
from io import BytesIO
from threading import RLock

import pytest

from .batch_storage import StorageScope
from .storage_payloads import S3Payloads


class S3Error(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class ConditionalS3:
    def __init__(self):
        self.objects = {}
        self.versions = {}
        self.calls = []
        self.lock = RLock()
        self.lose = None
        self.before = None

    def put_object(self, **request):
        with self.lock:
            assert request["ExpectedBucketOwner"] == "111111111111"
            assert ("IfMatch" in request) ^ ("IfNoneMatch" in request)
            self.calls.append(("put", request))
            key = request["Key"]
            old = self.objects.get(key)
            if self.before:
                hook, self.before = self.before, None
                hook(request)
            if "IfNoneMatch" in request and old is not None:
                raise S3Error("PreconditionFailed")
            if "IfMatch" in request and (old is None or old[1] != request["IfMatch"]):
                raise S3Error("PreconditionFailed")
            body = request["Body"]
            etag = '"' + sha256(body).hexdigest() + '"'
            self.objects[key] = (body, etag)
            self.versions.setdefault(key, []).append(body)
            if self.lose and self.lose(key):
                self.lose = None
                raise TimeoutError("synthetic response lost after storage")
            return {"ETag": etag}

    def get_object(self, **request):
        with self.lock:
            assert request["ExpectedBucketOwner"] == "111111111111"
            self.calls.append(("get", request))
            if request["Key"] not in self.objects:
                raise S3Error("NoSuchKey")
            body, etag = self.objects[request["Key"]]
            return {"Body": BytesIO(body), "ContentLength": len(body), "ETag": etag}


def journal(client=None, scope=None, name="run-a", **options):
    from .s3_journal import S3Journal

    scope = scope or StorageScope("account-a", "workspace-a")
    client = client or ConditionalS3()
    payloads = S3Payloads(client, "synthetic-bucket", "111111111111", scope)
    return S3Journal(payloads, "shared-batches", name, **options)


def test_create_commit_and_restart_read_canonical_s3_without_an_index():
    j = journal()
    first = j.initialize({"exact": "雪", "n": 9007199254740993})
    second = j.commit(first, {"exact": "changed"})
    reopened = journal(j.payloads.client)
    assert reopened.read().value == {"exact": "changed"}
    assert second.revision == 2 and first.revision == 1
    assert len(j.payloads.client.versions[j.head_key]) == 2
    assert all("IfMatch" in r or "IfNoneMatch" in r for kind, r in j.payloads.client.calls if kind == "put")


def test_stale_head_cannot_replace_successor_and_missing_head_cannot_reset_namespace():
    from .s3_journal import JournalConflict, JournalUnavailable

    j = journal()
    first = j.initialize({"budget": 1})
    j.commit(first, {"budget": 2})
    with pytest.raises(JournalConflict):
        j.commit(first, {"budget": 0})
    del j.payloads.client.objects[j.head_key]
    with pytest.raises(JournalUnavailable):
        j.read()
    with pytest.raises(JournalUnavailable):
        j.initialize({"budget": 0})


def test_lost_head_response_is_uncertain_even_if_written_and_never_regrants_a_claim():
    from .s3_journal import JournalUncertain

    j = journal()
    first = j.initialize({"claimed": False})
    j.payloads.client.lose = lambda key: key == j.head_key
    with pytest.raises(JournalUncertain):
        j.commit(first, {"claimed": True})
    assert journal(j.payloads.client).read().value == {"claimed": True}


def test_lost_creation_receipt_leaves_namespace_fail_closed():
    from .s3_journal import JournalUnavailable, JournalUncertain

    j = journal()
    j.payloads.client.lose = lambda key: key == j.anchor_key
    with pytest.raises(JournalUncertain):
        j.initialize({"budget": 0})
    with pytest.raises(JournalUnavailable):
        journal(j.payloads.client).initialize({"budget": 0})


def test_missing_or_changed_immutable_record_is_not_recovered_from_index_or_prior_version():
    from .s3_journal import JournalUnavailable

    j = journal()
    head = j.initialize({"authority": "s3"})
    key = j.payloads._key(head.reference.digest)
    body, etag = j.payloads.client.objects[key]
    j.payloads.client.objects[key] = (body + b"x", etag)
    with pytest.raises(JournalUnavailable):
        j.read()


def test_a_foreign_scope_fails_before_any_canonical_mutation_and_a_large_value_commits():
    j = journal()
    head = j.initialize({"x": 1})
    foreign = journal(j.payloads.client, StorageScope("foreign", "workspace-a"))
    with pytest.raises(PermissionError):
        foreign.commit(head, {"x": 2})
    assert j.read().value == {"x": 1}
    large = "a" * (5 * 1024 * 1024)
    j.commit(j.read(), {"x": large})
    assert journal(j.payloads.client).read().value == {"x": large}


def test_commits_past_the_old_commit_budget_advance_and_unknown_sdk_retry_configuration_fails_before_any_call():
    from types import SimpleNamespace

    j = journal()
    record = j.initialize({"x": 0})
    for n in range(1, 101):
        record = j.commit(record, {"x": n})
    assert journal(j.payloads.client).read().revision == 101
    client = ConditionalS3()
    client.meta = SimpleNamespace(config=SimpleNamespace(retries={"total_max_attempts": 3}))
    with pytest.raises(ValueError, match="total_max_attempts"):
        journal(client)
    assert client.calls == []


def test_deleted_anchor_is_not_silently_recreated_over_an_existing_head():
    from .s3_journal import JournalUnavailable

    j = journal()
    j.initialize({"charged": 1})
    del j.payloads.client.objects[j.anchor_key]
    before = len(j.payloads.client.calls)
    with pytest.raises(JournalUnavailable):
        j.initialize({"charged": 0})
    assert not any(kind == "put" for kind, _ in j.payloads.client.calls[before:])


def test_real_botocore_request_model_accepts_conditional_heads_with_single_attempt_configuration():
    import botocore.session
    from botocore.config import Config
    from botocore.stub import ANY, Stubber

    from .s3_journal import S3Journal

    client = botocore.session.get_session().create_client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="synthetic-only",
        aws_secret_access_key="synthetic-only",
        config=Config(signature_version="s3v4", retries={"total_max_attempts": 1}),
    )
    scope = StorageScope("synthetic", "workspace")
    payloads = S3Payloads(client, "synthetic-bucket", "111111111111", scope)
    j = S3Journal(payloads, "shared", "run")
    common = {
        "Bucket": "synthetic-bucket",
        "Key": j.head_key,
        "ExpectedBucketOwner": "111111111111",
        "Body": ANY,
        "ContentType": "application/json",
    }
    with Stubber(client) as stub:
        stub.add_response("put_object", {"ETag": '"created"'}, {**common, "IfNoneMatch": "*"})
        stub.add_response("put_object", {"ETag": '"updated"'}, {**common, "IfMatch": '"created"'})
        assert j._put(j.head_key, {"synthetic": True}) == '"created"'
        assert j._put(j.head_key, {"synthetic": False}, '"created"') == '"updated"'
        stub.assert_no_pending_responses()
    client.close()


def test_explicit_create_only_initialization_needs_no_list_permission_and_never_resets_hidden_heads():
    from .s3_journal import JournalUnavailable, JournalUncertain, S3Journal

    class NoListing(ConditionalS3):
        def get_object(self, **request):
            try:
                return super().get_object(**request)
            except S3Error as error:
                if error.response["Error"]["Code"] == "NoSuchKey":
                    raise S3Error("AccessDenied") from error
                raise

    client = NoListing()

    def open():
        base = journal(client)
        return S3Journal(base.payloads, "shared-batches", "run-a", create_only_initialization=True)

    j = open()
    j.initialize({"charged": 1})
    assert open().read().value == {"charged": 1}
    assert open().initialize({"charged": 0}).value == {"charged": 1}
    del client.objects[j.head_key]
    before = len(client.calls)
    with pytest.raises(JournalUnavailable):
        open().initialize({"charged": 0})
    assert not any(kind == "put" for kind, _ in client.calls[before:])
    # A surviving head blocks recreating a deleted anchor even in no-list mode.
    client = NoListing()
    j = open()
    j.initialize({"charged": 2})
    del client.objects[j.anchor_key]
    before = len(client.calls)
    with pytest.raises(JournalUnavailable):
        open().initialize({"charged": 0})
    assert not any(kind == "put" for kind, _ in client.calls[before:])
    # No confirmed anchor receipt means no head publication or execution grant.
    client = NoListing()
    j = open()
    client.lose = lambda key: key == j.anchor_key
    with pytest.raises(JournalUncertain):
        j.initialize({"charged": 0})
    assert j.head_key not in client.objects
    with pytest.raises(JournalUnavailable):
        open().initialize({"charged": 0})


def test_ambiguous_creation_probe_still_cannot_overwrite_an_existing_unreadable_head():
    from .s3_journal import JournalConflict, S3Journal

    client = ConditionalS3()
    old = journal(client)
    old.initialize({"paid": 1})
    original = client.objects[old.head_key]
    del client.objects[old.anchor_key]
    get = client.get_object

    def denied(**request):
        if request["Key"] in (old.anchor_key, old.head_key):
            raise S3Error("AccessDenied")
        return get(**request)

    client.get_object = denied
    new = S3Journal(old.payloads, "shared-batches", "run-a", create_only_initialization=True)
    with pytest.raises(JournalConflict):
        new.initialize({"paid": 0})
    assert client.objects[old.head_key] == original


class FlakyReads(ConditionalS3):
    """Raises ``error`` on the first ``failures`` GETs of keys matching ``only``."""

    def __init__(self, error, failures, only=lambda key: True):
        super().__init__()
        self.error, self.failures, self.only = error, failures, only

    def get_object(self, **request):
        if self.failures and self.only(request["Key"]):
            self.failures -= 1
            self.calls.append(("get", request))
            raise self.error
        return super().get_object(**request)


class ThrottleError(Exception):
    def __init__(self):
        super().__init__("https://bucket.s3.amazonaws.com/key?X-Amz-Signature=secret")
        self.response = {"Error": {"Code": "SlowDown"}, "ResponseMetadata": {"HTTPStatusCode": 503}}


def test_a_transient_head_read_failure_is_retried_once(monkeypatch):
    monkeypatch.setattr("decision_flywheel.s3_transient.RETRY_PAUSE_SECONDS", 0)
    seeded = journal()
    seeded.initialize({"n": 1})
    client = FlakyReads(ThrottleError(), 1, only=lambda key: key.endswith("head.json"))
    client.objects, client.versions = seeded.payloads.client.objects, seeded.payloads.client.versions
    assert journal(client).read().value == {"n": 1}
    assert [r["Key"] for kind, r in client.calls if kind == "get"].count(seeded.head_key) == 2


def test_a_transient_payload_read_failure_is_retried_once(monkeypatch):
    monkeypatch.setattr("decision_flywheel.s3_transient.RETRY_PAUSE_SECONDS", 0)
    seeded = journal()
    seeded.initialize({"n": 1})
    client = FlakyReads(ThrottleError(), 1, only=lambda key: "/payloads/" in key)
    client.objects, client.versions = seeded.payloads.client.objects, seeded.payloads.client.versions
    assert journal(client).read().value == {"n": 1}


def test_a_persistent_read_failure_is_a_journal_unavailable_that_names_the_class_only(monkeypatch, caplog):
    from .s3_journal import JournalUnavailable

    monkeypatch.setattr("decision_flywheel.s3_transient.RETRY_PAUSE_SECONDS", 0)
    seeded = journal()
    seeded.initialize({"n": 1})
    client = FlakyReads(ThrottleError(), 99, only=lambda key: "/payloads/" in key)
    client.objects, client.versions = seeded.payloads.client.objects, seeded.payloads.client.versions
    with pytest.raises(JournalUnavailable) as raised:
        journal(client).read()
    assert raised.value.cause == "ThrottleError" and "ThrottleError" in caplog.text
    assert "amazonaws" not in str(raised.value) and "Signature" not in caplog.text
    assert [kind for kind, r in client.calls if kind == "get" and "/payloads/" in r["Key"]] == ["get", "get"]


def test_a_head_that_fails_permanently_is_not_retried(monkeypatch):
    from .s3_journal import JournalUnavailable

    monkeypatch.setattr("decision_flywheel.s3_transient.RETRY_PAUSE_SECONDS", 0)
    seeded = journal()
    seeded.initialize({"n": 1})
    denied = ThrottleError()
    denied.response = {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}
    client = FlakyReads(denied, 1, only=lambda key: key.endswith("head.json"))
    client.objects, client.versions = seeded.payloads.client.objects, seeded.payloads.client.versions
    with pytest.raises(JournalUnavailable) as raised:
        journal(client).read()
    assert raised.value.cause == "ThrottleError"
    assert [r["Key"] for kind, r in client.calls if kind == "get"].count(seeded.head_key) == 1


def test_writes_and_conditional_puts_are_never_retried():
    from .s3_journal import JournalUncertain

    j = journal()
    first = j.initialize({"n": 1})

    class Throttled(Exception):
        response = {"Error": {"Code": "SlowDown"}, "ResponseMetadata": {"HTTPStatusCode": 503}}

    real = j.payloads.client.put_object
    attempts = []

    def failing(**request):
        attempts.append(request["Key"])
        raise Throttled()

    j.payloads.client.put_object = failing
    with pytest.raises(JournalUncertain):
        j.commit(first, {"n": 2})
    assert len(attempts) == 1
    j.payloads.client.put_object = real
