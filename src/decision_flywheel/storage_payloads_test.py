"""Canonical payload parity with injected S3 boundaries; no AWS credentials."""

from io import BytesIO

import pytest

from .batch_storage import StorageScope
from .storage_payloads import FilePayloads, PayloadCorrupt, PayloadUncertain, S3Payloads


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}}


class S3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.lose_response = False

    def put_object(self, **request):
        self.calls.append(("put", request))
        assert request["IfNoneMatch"] == "*" and request["ExpectedBucketOwner"] == "111111111111"
        if request["Key"] in self.objects:
            raise ConditionalFailure()
        self.objects[request["Key"]] = request["Body"]
        if self.lose_response:
            raise TimeoutError("synthetic lost response")
        return {}

    def get_object(self, **request):
        self.calls.append(("get", request))
        content = self.objects[request["Key"]]
        return {"ContentLength": len(content), "Body": BytesIO(content)}


@pytest.fixture(params=["filesystem", "s3"])
def payloads(request, tmp_path):
    scope = StorageScope("account-a", "workspace-a")
    if request.param == "filesystem":
        return FilePayloads(tmp_path, scope)
    return S3Payloads(S3(), "synthetic-bucket", "111111111111", scope)


def test_raw_bytes_survive_reopen_and_identical_publication_is_idempotent(payloads):
    content = '{ "large": 9007199254740993, "text": "雪" }'.encode()
    first = payloads.put(content)
    assert payloads.put(content) == first and payloads.get(first) == content
    if isinstance(payloads, FilePayloads):
        reopened = FilePayloads(payloads.root, payloads.scope)
    else:
        reopened = S3Payloads(payloads.client, payloads.bucket, payloads.owner, payloads.scope)
    assert reopened.get(first) == content


def test_foreign_workspace_reference_is_denied_before_any_payload_read(payloads):
    from dataclasses import replace

    first = payloads.put(b"{}")
    foreign = replace(first, scope=StorageScope("account-b", "workspace-a"))
    before = len(payloads.client.calls) if isinstance(payloads, S3Payloads) else None
    with pytest.raises(PermissionError):
        payloads.get(foreign)
    if before is not None:
        assert len(payloads.client.calls) == before


def test_a_payload_larger_than_the_old_byte_budget_publishes_and_verifies(payloads):
    # These adapters were opened with a 1,024-byte budget; payload adapters take none now.
    large = b"x" * (5 * 1024 * 1024)
    reference = payloads.put(large)
    assert reference.length == len(large) and payloads.get(reference) == large
    assert not hasattr(payloads, "max_bytes")


def test_non_bytes_input_fails_before_publication(payloads):
    with pytest.raises(ValueError):
        payloads.put("text, not bytes")


def test_changed_bytes_under_an_existing_digest_fail_closed(payloads):
    reference = payloads.put(b'{ "preserve": true }')
    if isinstance(payloads, FilePayloads):
        payloads._path(reference.digest).write_bytes(b"corrupt")
    else:
        payloads.client.objects[payloads._key(reference.digest)] = b"corrupt"
    with pytest.raises(PayloadCorrupt):
        payloads.get(reference)
    with pytest.raises(PayloadCorrupt):
        payloads.put(b'{ "preserve": true }')


def test_lost_s3_response_can_retry_identical_immutable_bytes():
    client = S3()
    payloads = S3Payloads(client, "synthetic-bucket", "111111111111", StorageScope("a", "w"))
    client.lose_response = True
    with pytest.raises(PayloadUncertain):
        payloads.put(b"exact raw bytes")
    client.lose_response = False
    reference = payloads.put(b"exact raw bytes")
    assert payloads.get(reference) == b"exact raw bytes" and len(client.objects) == 1


def test_s3_stream_is_bounded_even_when_content_length_lies():
    client = S3()
    payloads = S3Payloads(client, "synthetic-bucket", "111111111111", StorageScope("a", "w"))
    reference = payloads.put(b"123")

    class Body(BytesIO):
        def read(self, size=-1):
            assert size == reference.length + 1
            return super().read(size)

    body = Body(b"1234" + b"x" * 10000)
    client.get_object = lambda **request: {"ContentLength": 3, "Body": body}
    with pytest.raises(PayloadCorrupt):
        payloads.get(reference)
    assert body.closed


def test_invalid_fixed_configuration_is_rejected_without_an_sdk_call():
    from .storage_payloads import PayloadRef

    scope = StorageScope("a", "w")
    with pytest.raises(ValueError):
        PayloadRef(scope, None, 0)
    with pytest.raises(ValueError):
        S3Payloads(S3(), "synthetic-bucket", None, scope)


class FlakyS3(S3):
    """Fails the first ``failures`` GETs with ``error``; every PUT is counted."""

    def __init__(self, error, failures):
        super().__init__()
        self.error, self.failures = error, failures

    def get_object(self, **request):
        if self.failures:
            self.failures -= 1
            self.calls.append(("get", request))
            raise self.error
        return super().get_object(**request)


class ClientError(Exception):
    def __init__(self, code, status):
        super().__init__("https://bucket.s3.amazonaws.com/key?X-Amz-Signature=secret")
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


def flaky(error, failures):
    scope = StorageScope("account-a", "workspace-a")
    client = FlakyS3(error, failures)
    payloads = S3Payloads(client, "synthetic-bucket", "111111111111", scope)
    return payloads, payloads.put(b"{}"), client


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr("decision_flywheel.s3_transient.RETRY_PAUSE_SECONDS", 0)


def test_a_transient_s3_get_failure_is_retried_once_and_then_succeeds(quick):
    payloads, reference, client = flaky(ClientError("SlowDown", 503), 1)
    assert payloads.get(reference) == b"{}"
    assert [kind for kind, _ in client.calls].count("get") == 2


def test_a_repeated_transient_failure_is_reported_with_its_class_name_after_one_retry(quick, caplog):
    from .storage_payloads import PayloadUnavailable

    payloads, reference, client = flaky(ClientError("SlowDown", 503), 2)
    with pytest.raises(PayloadUnavailable) as raised:
        payloads.get(reference)
    assert raised.value.cause == "ClientError" and "(cause: ClientError)" in str(raised.value)
    assert "amazonaws" not in str(raised.value) and "amazonaws" not in caplog.text
    assert "ClientError" in caplog.text
    assert [kind for kind, _ in client.calls].count("get") == 2


def test_a_permanent_s3_get_failure_is_not_retried_and_is_typed(quick):
    from .storage_payloads import PayloadUnavailable

    payloads, reference, client = flaky(ClientError("AccessDenied", 403), 1)
    with pytest.raises(PayloadUnavailable) as raised:
        payloads.get(reference)
    assert raised.value.cause == "ClientError"
    assert [kind for kind, _ in client.calls].count("get") == 1


def test_a_corrupt_payload_is_never_retried(quick):
    payloads = S3Payloads(S3(), "synthetic-bucket", "111111111111", StorageScope("account-a", "workspace-a"))
    reference = payloads.put(b"{}")
    payloads.client.objects[payloads._key(reference.digest)] = b"[]"
    with pytest.raises(PayloadCorrupt):
        payloads.get(reference)
    assert [kind for kind, _ in payloads.client.calls].count("get") == 1


def test_a_filesystem_failure_keeps_its_class_name(tmp_path):
    from dataclasses import replace

    from .storage_payloads import PayloadUnavailable

    files = FilePayloads(tmp_path, StorageScope("account-a", "workspace-a"))
    reference = replace(files.put(b"{}"), digest="0" * 64)
    with pytest.raises(PayloadUnavailable) as raised:
        files.get(reference)
    assert raised.value.cause == "FileNotFoundError"
