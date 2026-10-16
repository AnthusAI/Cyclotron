"""Canonical payload parity with injected S3 boundaries; no AWS credentials."""
from io import BytesIO

import pytest

from .batch_storage import StorageScope
from .storage_payloads import FilePayloads, S3Payloads, PayloadCorrupt, PayloadUnavailable, PayloadUncertain


class ConditionalFailure(Exception):
    response = {'Error':{'Code':'PreconditionFailed'}}


class S3:
    def __init__(self): self.objects = {}; self.calls = []; self.lose_response = False
    def put_object(self, **request):
        self.calls.append(('put',request))
        assert request['IfNoneMatch'] == '*' and request['ExpectedBucketOwner'] == '111111111111'
        if request['Key'] in self.objects: raise ConditionalFailure()
        self.objects[request['Key']] = request['Body']
        if self.lose_response: raise TimeoutError('synthetic lost response')
        return {}
    def get_object(self, **request):
        self.calls.append(('get',request))
        content = self.objects[request['Key']]
        return {'ContentLength':len(content),'Body':BytesIO(content)}


@pytest.fixture(params=['filesystem','s3'])
def payloads(request, tmp_path):
    scope = StorageScope('account-a','workspace-a')
    if request.param == 'filesystem': return FilePayloads(tmp_path,scope,max_bytes=1024)
    return S3Payloads(S3(),'synthetic-bucket','111111111111',scope,max_bytes=1024)


def test_raw_bytes_survive_reopen_and_identical_publication_is_idempotent(payloads):
    content = '{ "large": 9007199254740993, "text": "雪" }'.encode()
    first = payloads.put(content)
    assert payloads.put(content) == first and payloads.get(first) == content
    if isinstance(payloads,FilePayloads):
        reopened = FilePayloads(payloads.root,payloads.scope,max_bytes=1024)
    else:
        reopened = S3Payloads(payloads.client,payloads.bucket,payloads.owner,payloads.scope,max_bytes=1024)
    assert reopened.get(first) == content


def test_foreign_workspace_reference_is_denied_before_any_payload_read(payloads):
    from dataclasses import replace
    first = payloads.put(b'{}')
    foreign = replace(first,scope=StorageScope('account-b','workspace-a'))
    before = len(payloads.client.calls) if isinstance(payloads,S3Payloads) else None
    with pytest.raises(PermissionError): payloads.get(foreign)
    if before is not None: assert len(payloads.client.calls) == before


def test_oversized_input_fails_before_publication(payloads):
    with pytest.raises(ValueError): payloads.put(b'x'*1025)


def test_changed_bytes_under_an_existing_digest_fail_closed(payloads):
    reference = payloads.put(b'{ "preserve": true }')
    if isinstance(payloads,FilePayloads): payloads._path(reference.digest).write_bytes(b'corrupt')
    else: payloads.client.objects[payloads._key(reference.digest)] = b'corrupt'
    with pytest.raises(PayloadCorrupt): payloads.get(reference)
    with pytest.raises(PayloadCorrupt): payloads.put(b'{ "preserve": true }')


def test_lost_s3_response_can_retry_identical_immutable_bytes():
    client = S3()
    payloads = S3Payloads(client,'synthetic-bucket','111111111111',StorageScope('a','w'),max_bytes=100)
    client.lose_response = True
    with pytest.raises(PayloadUncertain): payloads.put(b'exact raw bytes')
    client.lose_response = False
    reference = payloads.put(b'exact raw bytes')
    assert payloads.get(reference) == b'exact raw bytes' and len(client.objects) == 1


def test_s3_stream_is_bounded_even_when_content_length_lies():
    client = S3()
    payloads = S3Payloads(client,'synthetic-bucket','111111111111',StorageScope('a','w'),max_bytes=100)
    reference = payloads.put(b'123')
    class Body(BytesIO):
        def read(self, size=-1):
            assert size == reference.length+1
            return super().read(size)
    body = Body(b'1234' + b'x'*10000)
    client.get_object = lambda **request: {'ContentLength':3,'Body':body}
    with pytest.raises(PayloadCorrupt): payloads.get(reference)
    assert body.closed


def test_invalid_fixed_configuration_is_rejected_without_an_sdk_call():
    from .storage_payloads import PayloadRef
    scope = StorageScope('a','w')
    with pytest.raises(ValueError): PayloadRef(scope,None,0)
    with pytest.raises(ValueError): S3Payloads(S3(),'synthetic-bucket',None,scope,max_bytes=100)
