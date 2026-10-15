"""Queued work retains its actor/account and reauthorizes at every boundary."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import asyncio
import pytest
from .account_sessions import SessionRejected
from .prospectus_accounts import AccountAccessDenied, ProspectusUnavailable
from .s3_sessions_test import sessions


def fixture():
    from .queued_account_authorization import QueuedAccountAuthorization
    store,_,_=sessions();cookie,session=store.create('original-access',{'sub':'pool-sub','exp':1100})
    session.person_id=str(uuid4());session.account_id=str(uuid4());store.save(session)
    state={'person':session.person_id,'binding':'binding-a','denied':False,'calls':[]}
    def me(token):
        assert token=='original-access'
        if isinstance(state['person'],Exception):raise state['person']
        return {'id':state['person']} if state['person'] else None
    def authorize(token,account,person,operation):
        state['calls'].append((token,account,person,operation))
        if state['denied']:raise AccountAccessDenied('denied')
    boundary=QueuedAccountAuthorization(store,SimpleNamespace(access=lambda token:{'sub':'pool-sub'}),
        SimpleNamespace(me=me),SimpleNamespace(authorize=authorize),lambda account,run:state['binding'])
    actor=asyncio.run(boundary.capture(session,session.account_id,str(uuid4()),'binding-a'))
    return boundary,store,cookie,actor,state


def test_each_guard_rechecks_person_membership_and_binding_without_using_new_tab_selection():
    boundary,store,cookie,actor,state=fixture()
    changed=store.get(cookie);changed.account_id=str(uuid4());store.save(changed)
    guard=boundary.guard(actor)
    asyncio.run(guard());asyncio.run(guard())
    assert len(state['calls'])==3
    assert all(call[1]==actor.account_id and call[3]=='console:write' for call in state['calls'])
    state['binding']='replacement'
    with pytest.raises(AccountAccessDenied):asyncio.run(guard())


@pytest.mark.parametrize('change',['logout','revocation','person','null','outage','expired','wrong-subject'])
def test_queued_actor_is_denied_after_logout_revocation_identity_change_expiry_or_outage(change):
    boundary,store,cookie,actor,state=fixture()
    if change=='logout':store.revoke(cookie)
    elif change=='revocation':state['denied']=True
    elif change=='person':state['person']=str(uuid4())
    elif change=='null':state['person']=None
    elif change=='outage':state['person']=ProspectusUnavailable('offline')
    elif change=='expired':store.clock=lambda:1200
    elif change=='wrong-subject':boundary.verifier.access=lambda token:{'sub':'another'}
    with pytest.raises((AccountAccessDenied,ProspectusUnavailable,SessionRejected)):
        asyncio.run(boundary.guard(actor)())


def test_queued_snapshot_contains_no_credentials_and_cannot_be_retargeted_to_another_session():
    boundary,store,cookie,actor,state=fixture()
    assert 'original-access' not in repr(actor) and cookie not in repr(actor)
    other,session=store.create('another-access',{'sub':'pool-sub','exp':1100})
    session.person_id=actor.person_id;store.save(session)
    with pytest.raises(AccountAccessDenied):
        asyncio.run(boundary.guard(replace(actor,session_ref=session.session_ref))())


@pytest.mark.parametrize('change',['logout','revocation','person','outage','binding'])
def test_actual_s3_sdk_rechecks_after_waiting_for_execution_ownership_before_state_or_provider(change,monkeypatch):
    from .embedded_cyclotron import Cyclotron
    from .embedded_cyclotron_test import DEFINITION,Model,item
    from .s3_embedded_storage import S3CyclotronStores
    from .s3_journal_test import ConditionalS3,journal
    boundary,sessions,cookie,actor,state=fixture()
    client=ConditionalS3();model=Model()
    composition=S3CyclotronStores(journal(client,name='guard-root'),max_records=1000,
        journal_for=lambda domain,name:journal(client,name=domain+'-'+name))
    def forbidden(*args,**kwargs):raise AssertionError('cloud worker touched SQLite or a local store')
    monkeypatch.setattr('sqlite3.connect',forbidden);monkeypatch.setattr('pathlib.Path.mkdir',forbidden)
    with Cyclotron.open(None,DEFINITION,model,storage=composition) as engine:
        async def queued():
            async with engine.execution():
                task=asyncio.create_task(engine.decide(item('queued'),execution_guard=boundary.guard(actor)))
                await asyncio.sleep(0)
                if change=='logout':sessions.revoke(cookie)
                elif change=='revocation':state['denied']=True
                elif change=='person':state['person']=str(uuid4())
                elif change=='outage':state['person']=ProspectusUnavailable('offline')
                elif change=='binding':state['binding']='new-binding'
                before=len(client.calls)
            with pytest.raises((AccountAccessDenied,ProspectusUnavailable)):
                await task
            assert model.calls==0 and len(client.calls)==before
        asyncio.run(queued())


@pytest.mark.parametrize('field',['person_id','subject','account_id','run_id','binding','operation'])
def test_queued_actor_integrity_proof_rejects_individual_target_and_operation_changes(field):
    boundary,_,_,actor,_=fixture()
    replacement=str(uuid4()) if field.endswith('_id') else ('console:read' if field=='operation' else 'different')
    with pytest.raises(AccountAccessDenied):
        asyncio.run(boundary.guard(replace(actor,**{field:replacement}))())
