"""Core runtime repositories use S3 with exclusive fenced ownership, never SQLite."""
import asyncio
from uuid import uuid4
import pytest
from .s3_journal_test import journal


def stores(j=None):
    from .s3_runtime_storage import S3RuntimeStores
    result=S3RuntimeStores(j or journal(name='classifier-a'),max_records=10000)
    result.initialize();result.acquire(str(uuid4()))
    return result


def test_runtime_owner_is_exclusive_releases_explicitly_and_fences_the_old_handle():
    from .s3_runtime_storage import S3RuntimeStores,RuntimeOwnershipError
    first=stores();j=journal(first.journal.payloads.client,name='classifier-a')
    other=S3RuntimeStores(j,max_records=10000);other.initialize()
    with pytest.raises(RuntimeOwnershipError):other.acquire(str(uuid4()))
    first.runtime_storage.put_state('exact','{ "note":"雪" }')
    first.close();other.acquire(str(uuid4()))
    assert other.runtime_storage.state('exact')=='{ "note":"雪" }'
    with pytest.raises(RuntimeOwnershipError):first.runtime_storage.put_state('exact','{}')
    other.close()


def test_cache_refresh_learning_aliases_and_pending_cleanup_preserve_domain_semantics():
    s=stores();cache=s.runtime_cache
    cache.begin_answer('old',None);cache.complete_answer('old','{ "original":true }',previous=None)
    prior=cache.answer('old');cache.begin_answer('old',prior)
    cache.complete_answer('old','{"new":true}',previous=prior,current_key='new')
    assert cache.answer('old')==prior and cache.answer('new')==('complete','{"new":true}')
    learn=s.learning_storage;learn.ensure('rubric')
    learn.begin('rubric','old','{"pending_evaluation":true}');learn.complete('rubric','old','{"pending_evaluation":true}')
    learn.begin('rubric','next','{}');learn.complete('rubric','next','{"done":true}',aliases=('alias',),clear_pending_key='old')
    assert learn.artifact('rubric','next')==learn.artifact('rubric','alias')
    cache.begin_round('pending');marks=s.pending_storage.marks();rows=s.pending_storage.pending()
    row=next(row for row in rows if row[0]=='runtime_rounds')
    assert not s.pending_storage.discard(row[0],row[1],'wrong-key')
    assert s.pending_storage.discard(*row) and cache.round('pending') is None
    cache.begin_round('later')
    assert s.pending_storage.marks()['runtime_rounds']>marks['runtime_rounds']
    s.close()


def test_real_core_predict_restart_and_improve_use_only_injected_s3_repositories(monkeypatch):
    from .flywheel import DecisionFlywheel
    from .classifier_config import ClassifierConfig
    from .flywheel_test import TASK,TRAIN,DEV,FakeModel,agent
    def forbidden(*args,**kwargs):raise AssertionError('cloud core opened SQLite')
    monkeypatch.setattr('sqlite3.connect',forbidden)
    s=stores();j=s.journal;model=FakeModel()
    wheel=DecisionFlywheel(None,ClassifierConfig(TASK),model,agent([]),storage=s)
    asyncio.run(wheel.predict(DEV[0].item,TRAIN));calls=model.calls
    before=wheel.history(10000);wheel.close()
    s=stores(journal(j.payloads.client,name='classifier-a'))
    wheel=DecisionFlywheel(None,ClassifierConfig(TASK),model,agent([]),storage=s)
    assert wheel.history(10000)==before
    asyncio.run(wheel.predict(DEV[0].item,TRAIN))
    assert model.calls==calls
    result=asyncio.run(wheel.improve(TRAIN,DEV,protected=(),propensities={row.item.id:1. for row in TRAIN}))
    assert isinstance(result,dict) and wheel.history(10000)
    wheel.close()


def test_unknown_acquire_outcome_does_not_grant_runtime_execution():
    from .s3_runtime_storage import S3RuntimeStores,RuntimeOwnershipError
    from .s3_journal import JournalUncertain
    j=journal(name='classifier-a');s=S3RuntimeStores(j,max_records=10000);s.initialize()
    j.payloads.client.lose=lambda key:key==j.head_key
    with pytest.raises(JournalUncertain):s.acquire(str(uuid4()))
    with pytest.raises(RuntimeOwnershipError):s.runtime_storage.put_state('x','{}')
    with pytest.raises(RuntimeOwnershipError):stores(journal(j.payloads.client,name='classifier-a'))


def test_hypothesis_diagnosis_and_shadow_records_retain_native_policy_inputs():
    from .hypothesis_ledger import HypothesisLedger
    s=stores();ledger=HypothesisLedger(storage=s.hypothesis_storage,clock=lambda:'same timestamp')
    ledger.record_evaluations('idea',[{'item_id':'one','correct':True}],'v1')
    ledger.record_evaluations('idea',[{'item_id':'one','correct':False}],'v2')
    ledger.record_verdict('idea','advanced','original rationale',{'note':'雪'})
    ledger.record_verdict('idea','retired','later rationale')
    assert ledger.correctness_vector('idea',['one'])==[False]
    assert ledger._latest('idea',context_version='v1')=={'one':True}
    assert [r['reason'] for r in ledger.verdicts('idea')]==['original rationale','later rationale']
    assert ledger.running_score('idea')['n']==2
    assert ledger.running_score('idea','same timestamp',strict=True)['n']==0
    s.control_storage.save('idea','{"id":"idea","operator":"revise","parents":["ancestor"]}')
    assert ledger.lineage('idea')['parents']==['ancestor']
    shadow=s.shadow_storage
    shadow.ensure();shadow.join('b','rubric',3);shadow.join('a','rubric',3)
    shadow.scored('a','item',4);shadow.scored('a','item',5)
    shadow.incumbent('inc','exact rubric',4);shadow.incumbent('inc','changed',5)
    shadow.look(('a','inc',1,20,5,0,.02,1,4))
    assert shadow.active()==[('a','rubric',3),('b','rubric',3)]
    assert shadow.calls()==1 and shadow.looks_taken('a','inc')==1
    with pytest.raises(ValueError):shadow.look(('a','inc',1,20,5,0,.02,1,4))
    shadow.set_status('a','evicted')
    assert shadow.statuses()=={'b':'active','a':'evicted'}
    s.close()


def test_runtime_size_limit_does_not_partially_commit_learning_aliases():
    from .s3_runtime_storage import S3RuntimeStores
    j=journal(name='small');s=S3RuntimeStores(j,max_records=2);s.initialize();s.acquire(str(uuid4()))
    learn=s.learning_storage
    learn.begin('rubric','old','{"pending_evaluation":true}')
    learn.complete('rubric','old','{"pending_evaluation":true}')
    learn.begin('rubric','new','{}')
    with pytest.raises(ValueError,match='record budget'):
        learn.complete('rubric','new','{"done":true}',aliases=('extra',),clear_pending_key='old')
    assert learn.artifact('rubric','new')==('pending','{}')
    assert learn.artifact('rubric','old')==('complete','{"pending_evaluation":true}')
    s.close()


def test_uncertain_runtime_commit_poisoning_prevents_followup_exception_cleanup_writes():
    from .s3_journal import JournalUncertain
    from .s3_runtime_storage import RuntimeOwnershipError
    s=stores();s.journal.payloads.client.lose=lambda key:key==s.journal.head_key
    with pytest.raises(JournalUncertain):s.runtime_storage.append_event('{"kind":"committed-response-lost"}')
    before=len(s.journal.payloads.client.calls)
    with pytest.raises(RuntimeOwnershipError):s.runtime_storage.append_event('{"kind":"unsafe-followup"}')
    assert len(s.journal.payloads.client.calls)==before
    s.close()
    restarted=stores(journal(s.journal.payloads.client,name='classifier-a'))
    assert [e['kind'] for e in restarted.runtime_storage.history(10)]==['committed-response-lost']
    restarted.close()
