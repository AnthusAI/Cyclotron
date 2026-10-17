"""Paid optimizer attempts use canonical S3 ownership/budgets for sync and async calls."""
import asyncio
import pytest
from .s3_batch_storage_test import store
from .s3_journal_test import journal
from .optimizer_agent import OptimizerReply,OptimizerAgent,FeedbackBriefing


def wrapped(transport,ledger=None,maximum=2):
    from .s3_optimizer_transport import S3OptimizerTransport
    return S3OptimizerTransport(transport,ledger or store(journal(name='optimizer')),identity={'provider':'synthetic','model':'synthetic'},
        max_calls=maximum,max_request_bytes=100000,max_reply_bytes=100000)


def test_completed_identical_prompts_are_distinct_authorized_calls_but_ceiling_survives_reopen():
    calls=[]
    def provider(messages):calls.append(messages);return OptimizerReply('{ "rationale":"雪" }','synthetic',{'tokens':1})
    transport=wrapped(provider)
    first=transport([{'role':'user','content':'same'}]);transport([{'role':'user','content':'same'}])
    assert first.content=='{ "rationale":"雪" }' and transport.calls==len(calls)==2
    restarted=wrapped(provider,store(journal(transport.storage.journal.payloads.client,name='optimizer')))
    with pytest.raises(RuntimeError,match='ceiling'):restarted([{'role':'user','content':'next'}])
    assert len(calls)==2


def test_lost_optimizer_claim_receipt_never_dispatches_and_cannot_be_automatically_repaid(monkeypatch):
    from .batch_storage import BatchUncertain,BatchInProgress
    calls=[]
    transport=wrapped(lambda messages:calls.append(messages));claim=transport.storage.claim
    def lose(key,owner):
        transport.storage.journal.payloads.client.lose=lambda key:key==transport.storage.journal.head_key
        return claim(key,owner)
    monkeypatch.setattr(transport.storage,'claim',lose)
    messages=[{'role':'user','content':'same'}]
    with pytest.raises(BatchUncertain):transport(messages)
    assert calls==[] and transport.calls==1
    with pytest.raises(BatchInProgress):transport(messages)


def test_async_optimizer_agent_preserves_event_loop_transport_and_records_raw_reply_without_sqlite(monkeypatch):
    import threading
    thread=threading.get_ident();calls=[]
    class Provider:
        async def acomplete(self,messages):
            await asyncio.sleep(0)
            assert threading.get_ident()==thread
            calls.append(messages);return OptimizerReply('{"rubric":"Synthetic improved rubric","rationale":"Explicit human judgment"}','synthetic',{'tokens':2})
    def forbidden(*args,**kwargs):raise AssertionError('cloud optimizer opened SQLite')
    monkeypatch.setattr('sqlite3.connect',forbidden)
    transport=wrapped(Provider());events=[];agent=OptimizerAgent(transport,observer=events.append)
    briefing=FeedbackBriefing('{"current":{},"examples":[]}')
    result=asyncio.run(agent.propose_async(briefing))
    assert result['rubric']=='Synthetic improved rubric' and len(calls)==1 and transport.calls==1
    record=transport.storage.page()[0][0]
    assert record.status=='complete' and 'Explicit human judgment' in record.payload
    assert [event['kind'] for event in events]==['optimizer-request','optimizer-response']


def test_async_cancellation_preserves_uncertain_claim_and_credentials_are_rejected_before_cost():
    from .batch_storage import BatchInProgress
    class Provider:
        async def acomplete(self,messages):raise asyncio.CancelledError()
    transport=wrapped(Provider());messages=[{'role':'user','content':'same'}]
    with pytest.raises(asyncio.CancelledError):asyncio.run(transport.acomplete(messages))
    with pytest.raises(BatchInProgress):asyncio.run(transport.acomplete(messages))
    assert transport.calls==1
    transport.redact=('synthetic-secret',)
    with pytest.raises(ValueError,match='credential'):asyncio.run(transport.acomplete([{'role':'user','content':'synthetic-secret'}]))
    assert transport.calls==1


def test_cloud_sdk_rejects_unmetered_optimizer_before_creating_a_namespace(monkeypatch):
    from .s3_embedded_storage import S3CyclotronStores
    from .s3_journal_test import ConditionalS3
    from .embedded_cyclotron import Cyclotron
    from .embedded_cyclotron_test import DEFINITION,Model
    from .flywheel_test import agent
    client=ConditionalS3();s=S3CyclotronStores(journal(client,name='root'),max_records=1000,journal_for=lambda domain,name:journal(client,name=domain+'-'+name))
    with pytest.raises(ValueError,match='paid-call transport'):
        Cyclotron.open(None,DEFINITION,Model(),agent([]),storage=s)
    assert client.calls==[]


def test_per_invocation_guard_denial_precedes_reservation_and_is_rechecked_before_dispatch():
    calls=[];checks=[]
    base=wrapped(lambda messages:calls.append(messages))
    def denied():raise PermissionError('revoked')
    guarded=base.with_execution_guard(denied)
    before=len(base.storage.journal.payloads.client.calls)
    with pytest.raises(PermissionError):guarded([{'role':'user','content':'synthetic'}])
    assert calls==[] and len(base.storage.journal.payloads.client.calls)==before
    assert base.calls==0
    def revoke_at_dispatch():
        checks.append(True)
        if len(checks)==2:raise PermissionError('revoked while reserving')
    guarded=base.with_execution_guard(revoke_at_dispatch)
    with pytest.raises(PermissionError):guarded([{'role':'user','content':'synthetic'}])
    assert calls==[] and base.calls==1
