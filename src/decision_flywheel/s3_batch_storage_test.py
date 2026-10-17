"""Complete shared-request domain against canonical S3 CAS; no DynamoDB or SQLite."""
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from .batch_storage import BatchConflict, BatchInProgress, BatchUncertain
from .s3_journal_test import journal


def store(j=None,**options):
    from .s3_batch_storage import S3BatchStorage
    result=S3BatchStorage(j or journal(),max_records=1000,**options)
    result.initialize()
    return result


def test_claim_is_exclusive_and_original_operation_replay_retains_its_generation():
    s=store();first,second=str(uuid4()),str(uuid4())
    s.reserve('a'*64,3,first)
    assert s.claim('a'*64,first) and not s.claim('a'*64,first)
    text='{ "exact":"雪", "n":9007199254740993 }'
    s.settle('a'*64,first,'complete',text)
    s.reserve('a'*64,3,second,policy='refresh');s.claim('a'*64,second)
    s.settle('a'*64,second,'complete','{"new":true}')
    assert s.reserve('a'*64,3,first,policy='refresh').payload==text
    assert s.requests()==2 and s.get('a'*64).generation==2
    with pytest.raises(BatchConflict):s.settle('a'*64,first,'failed')


def test_independent_workers_race_for_one_atomic_budget_slot():
    s=store();client=s.journal.payloads.client
    def reserve(index):
        other=store(journal(client))
        try:other.reserve(str(index)*64,1,str(uuid4()));return True
        except RuntimeError:return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(reserve,range(1,9)))
    assert sum(results)==1 and s.requests()==1
    assert not any('dynamo' in key for key in client.objects)


def test_lost_claim_response_never_authorizes_a_provider_and_restart_does_not_steal():
    s=store();owner=str(uuid4());s.reserve('b'*64,2,owner)
    s.journal.payloads.client.lose=lambda key:key==s.journal.head_key
    with pytest.raises(BatchUncertain):s.claim('b'*64,owner)
    restarted=store(journal(s.journal.payloads.client))
    assert not restarted.claim('b'*64,owner)
    with pytest.raises(BatchInProgress):restarted.reserve('b'*64,2,str(uuid4()),retry_failed=True)
    assert restarted.requests()==1 and restarted.get('b'*64).claimed


def test_lost_reservation_and_settlement_can_reconcile_same_owner_without_repayment():
    s=store();owner=str(uuid4());client=s.journal.payloads.client
    client.lose=lambda key:key==s.journal.head_key
    with pytest.raises(BatchUncertain):s.reserve('c'*64,1,owner)
    assert s.reserve('c'*64,1,owner).owner==owner and s.requests()==1
    assert s.claim('c'*64,owner)
    client.lose=lambda key:key==s.journal.head_key
    with pytest.raises(BatchUncertain):s.settle('c'*64,owner,'complete','{"ok":true}')
    assert s.settle('c'*64,owner,'complete','{"ok":true}').status=='complete'
    assert s.requests()==1


def test_failed_retry_history_export_and_restore_match_native_without_cloud_sqlite(tmp_path,monkeypatch):
    from .batch_storage import SQLiteBatchStorage
    native=SQLiteBatchStorage(tmp_path/'native.sqlite')
    s=store();first,second=str(uuid4()),str(uuid4())
    for target in (s,native):
        target.reserve('d'*64,3,first);target.claim('d'*64,first)
        target.settle('d'*64,first,'failed')
        with pytest.raises(RuntimeError,match='retry'):target.reserve('d'*64,3,second)
        target.reserve('d'*64,3,second,retry_failed=True);target.claim('d'*64,second)
        target.settle('d'*64,second,'complete','{ "verbatim":true }')
    assert s.export()==native.export();native.close()
    def forbidden(*args,**kwargs):raise AssertionError('S3 adapter opened SQLite')
    monkeypatch.setattr('sqlite3.connect',forbidden)
    destination=store(journal(s.journal.payloads.client,name='run-b'))
    destination.restore(json.loads(json.dumps(s.export())))
    assert destination.export()==s.export() and destination.requests()==2
    with pytest.raises(BatchConflict):destination.restore(s.export())


def test_learning_abandonment_retains_attempt_history_and_cannot_remove_a_sibling_guard():
    s=store();first,second=str(uuid4()),str(uuid4())
    s.reserve('e'*64,3,first)
    s.reserve('f'*64,3,second);s.claim('f'*64,second)
    assert s.operation_mark()==2
    assert s.pending_operations()==[(1,'e'*64,first),(2,'f'*64,second)]
    with pytest.raises(BatchConflict):s.abandon_learning('e'*64,second)
    s.abandon_learning('e'*64,first)
    s.reserve('e'*64,3,str(uuid4()))
    assert s.requests()==3 and s.get('f'*64).status=='pending'
    assert s.export()['history'][0][3]=='interrupted'
    with pytest.raises(BatchUncertain):s.abandon_learning('f'*64,second)
    assert s.get('f'*64).status=='pending'


def test_record_limits_corrupt_state_and_missing_head_fail_closed():
    from .s3_batch_storage import S3BatchStorage
    from .s3_journal import JournalUnavailable
    j=journal();s=S3BatchStorage(j,max_records=2);s.initialize()
    s.reserve('1'*64,2,str(uuid4()))
    before=j.read().revision
    with pytest.raises(ValueError):s.reserve('2'*64,2,str(uuid4()))
    assert j.read().revision==before and s.requests()==1
    del j.payloads.client.objects[j.head_key]
    with pytest.raises(JournalUnavailable):s.initialize()


def test_real_shared_consumer_restarts_from_s3_without_sqlite_or_replaying_completed_provider(monkeypatch):
    import asyncio

    from .batched_classification import BatchedAnswers
    from .classifier_config import ClassifierConfig
    from .models import DecisionResult, DecisionTask, Item
    from .shared_decisions import SharedDecisions
    class Model:
        model_identity='synthetic-s3';calls=0
        async def classify_many(self,configs,*args,**kwargs):
            self.calls+=1
            return BatchedAnswers({cid:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})} for cid in configs},'synthetic',{},1)
    def forbidden(*args,**kwargs):raise AssertionError('cloud consumer opened SQLite')
    monkeypatch.setattr('sqlite3.connect',forbidden)
    s=store();model=Model();config=ClassifierConfig(DecisionTask('topic',('yes','no'),'Relevant?'))
    target=Item('target',{'text':'Synthetic'})
    for storage in (s,store(journal(s.journal.payloads.client))):
        consumer=SharedDecisions(None,model,max_requests=1,observer=lambda _:None,storage=storage)
        asyncio.run(consumer.prepare({'topic':config},target,{'topic':()}))
        assert consumer.requests==1
        consumer.close()
    assert model.calls==1


def test_real_shared_consumer_never_calls_provider_after_uncertain_claim(monkeypatch):
    import asyncio

    from .classifier_config import ClassifierConfig
    from .models import DecisionTask, Item
    from .shared_decisions import SharedDecisions
    class Model:
        model_identity='synthetic-s3';calls=0
        async def classify_many(self,*args,**kwargs):
            self.calls+=1;raise AssertionError('uncertain dispatch reached provider')
    s=store();model=Model();claim=s.claim
    def lose_claim(key,owner):
        s.journal.payloads.client.lose=lambda key:key==s.journal.head_key
        return claim(key,owner)
    monkeypatch.setattr(s,'claim',lose_claim)
    consumer=SharedDecisions(None,model,max_requests=1,observer=lambda _:None,storage=s)
    config=ClassifierConfig(DecisionTask('topic',('yes','no'),'Relevant?'))
    with pytest.raises(BatchUncertain):asyncio.run(consumer.prepare({'topic':config},Item('x',{'text':'Synthetic'}),{'topic':()}))
    assert model.calls==0 and s.requests()==1
