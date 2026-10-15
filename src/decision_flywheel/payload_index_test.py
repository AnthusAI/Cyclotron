"""Rebuildable DynamoDB candidates cannot replace canonical payload validation."""
import pytest

from .batch_storage import StorageScope
from .payload_index import DynamoPayloadIndex, IndexCorrupt
from .storage_payloads import FilePayloads, PayloadUnavailable


class Dynamo:
    def __init__(self): self.rows = {}; self.calls = []
    def put_item(self, **request):
        self.calls.append(request)
        assert request['ConditionExpression'] == 'attribute_not_exists(PK) OR #length = :length'
        row = request['Item']
        self.rows[row['PK']['S'],row['SK']['S']] = row
        return {}
    def query(self, **request):
        self.calls.append(request)
        partition = request['ExpressionAttributeValues'][':scope']['S']
        after = request.get('ExclusiveStartKey',{}).get('SK',{}).get('S','')
        rows = [row for (pk,sk),row in sorted(self.rows.items()) if pk == partition and sk > after]
        limit = request['Limit']
        result = {'Items':rows[:limit]}
        if len(rows)>limit:
            last = rows[limit-1]
            result['LastEvaluatedKey'] = {key:last[key] for key in ('PK','SK')}
        return result


def test_index_rebuilds_only_from_verified_canonical_bytes_and_pages_by_fixed_scope(tmp_path):
    scope = StorageScope('a','w')
    files = FilePayloads(tmp_path,scope,max_bytes=100)
    references = sorted([files.put(value) for value in (b'one',b'two',b'three')],key=lambda ref:ref.digest)
    client = Dynamo()
    index = DynamoPayloadIndex(client,'synthetic-index',scope)
    assert index.rebuild(files,references,max_records=3) == 3
    page,cursor = index.page(limit=2)
    assert page == references[:2] and cursor == references[1].digest
    assert index.page(after=cursor,limit=2) == (references[2:],None)
    assert index.rebuild(files,references,max_records=3) == 3
    assert len(client.rows)==3
    assert DynamoPayloadIndex(client,'synthetic-index',StorageScope('b','w')).page() == ([],None)
    assert all(call.get('ConsistentRead') is False for call in client.calls if 'Limit' in call)


def test_missing_payload_cannot_be_published_as_a_verified_index_candidate(tmp_path):
    scope = StorageScope('a','w')
    files = FilePayloads(tmp_path,scope,max_bytes=100)
    reference = files.put(b'exact')
    files._path(reference.digest).unlink()
    client = Dynamo()
    index = DynamoPayloadIndex(client,'synthetic-index',scope)
    with pytest.raises(PayloadUnavailable): index.rebuild(files,[reference],max_records=1)
    assert client.calls == []


def test_index_discovery_is_untrusted_and_foreign_or_malformed_rows_fail_closed():
    client = Dynamo()
    index = DynamoPayloadIndex(client,'synthetic-index',StorageScope('a','w'))
    client.query = lambda **request: {'Items':[{'PK':{'S':'b/w'},'SK':{'S':'f'*64},'length':{'N':'3'}}]}
    with pytest.raises(IndexCorrupt): index.page()
    for invalid in ('../foreign','',5):
        with pytest.raises(ValueError): index.page(after=invalid)
    with pytest.raises(ValueError): index.page(limit=1001)
    client.query = lambda **request: {'Items':[], 'LastEvaluatedKey':{'PK':{'S':'b/w'},'SK':{'S':'f'*64}}}
    with pytest.raises(IndexCorrupt): index.page()


def test_stale_index_candidate_does_not_authorize_or_hide_missing_canonical_bytes(tmp_path):
    scope = StorageScope('a','w')
    files = FilePayloads(tmp_path,scope,max_bytes=100)
    reference = files.put(b'exact')
    index = DynamoPayloadIndex(Dynamo(),'synthetic-index',scope)
    index.rebuild(files,[reference],max_records=1)
    files._path(reference.digest).unlink()
    candidates,_ = index.page()
    with pytest.raises(PayloadUnavailable): files.get(candidates[0])
    assert not hasattr(index,'reserve') and not hasattr(index,'claim')


def test_rebuild_budget_fails_explicitly_without_deleting_existing_candidates(tmp_path):
    scope = StorageScope('a','w')
    files = FilePayloads(tmp_path,scope,max_bytes=100)
    references = [files.put(value) for value in (b'one',b'two')]
    index = DynamoPayloadIndex(Dynamo(),'synthetic-index',scope)
    with pytest.raises(ValueError): index.rebuild(files,references,max_records=1)
    assert len(index.page()[0]) == 1  # Additive partial progress is safe to repeat.


def test_injected_real_sdk_serializes_the_bounded_dynamodb_query_without_network():
    session = pytest.importorskip('botocore.session').get_session()
    Stubber = pytest.importorskip('botocore.stub').Stubber
    client = session.create_client('dynamodb',region_name='us-east-1',
        aws_access_key_id='synthetic',aws_secret_access_key='synthetic',endpoint_url='https://synthetic.invalid')
    scope = StorageScope('a','w')
    with Stubber(client) as stub:
        stub.add_response('query',{'Items':[{'PK':{'S':'a/w'},'SK':{'S':'f'*64},'length':{'N':'3'}}]},
            {'TableName':'synthetic-index','KeyConditionExpression':'PK = :scope',
             'ExpressionAttributeValues':{':scope':{'S':'a/w'}},'ProjectionExpression':'PK, SK, #length',
             'ExpressionAttributeNames':{'#length':'length'},'ConsistentRead':False,'Limit':2,'ScanIndexForward':True})
        rows,cursor = DynamoPayloadIndex(client,'synthetic-index',scope).page(limit=2)
        assert rows[0].length==3 and rows[0].digest=='f'*64 and cursor is None
        stub.assert_no_pending_responses()
