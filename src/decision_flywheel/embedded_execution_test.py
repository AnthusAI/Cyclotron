"""Trusted authorization runs after ownership and before any operation state."""
import asyncio
import pytest
from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import DEFINITION, Model, item


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize('failure',[PermissionError,ConnectionError,asyncio.CancelledError])
@pytest.mark.parametrize('operation',['decide','review','undo','learn','replay','read'])
def test_queued_denial_outage_or_cancellation_leaves_every_store_and_provider_untouched(tmp_path,failure,operation):
    model=Model()
    with Cyclotron.open(tmp_path/'c',DEFINITION,model,learning='deferred') as engine:
        decision=run(engine.decide(item('existing')))
        run(engine.review(decision.decision_id,'include',review_id='original'))
        pending_before=engine.pending_learning()
        async def scenario():
            started=asyncio.Event()
            allowed=True
            checks=[]
            async def guard():
                checks.append('checked')
                assert engine._lock.locked()
                if not allowed: raise failure('synthetic authorization revoked')
            async def invoke():
                started.set()
                if operation=='decide': return await engine.decide(item('new'),execution_guard=guard)
                if operation=='review': return await engine.review(decision.decision_id,'exclude',review_id='changed',execution_guard=guard)
                if operation=='undo': return await engine.undo_review(decision.decision_id,review_id='undo',execution_guard=guard)
                if operation=='learn': return await engine.learn(execution_guard=guard)
                if operation=='replay': return await engine.replay([{'item':item('replay'),'label':'exclude'}],execution_guard=guard)
                async with engine.execution(execution_guard=guard): return engine.subscribe()
            before=engine.db.total_changes,model.calls
            # A SQLite authorizer catches even a SELECT attempted before guard.
            for db in [engine.db,engine.shared.db,*[wheel.db for wheel in engine.wheels.values()]]:
                db.set_authorizer(lambda *_: (_ for _ in ()).throw(AssertionError('operation touched state before authorization')))
            try:
                await engine._lock.acquire()
                task=asyncio.create_task(invoke())
                await started.wait()
                await asyncio.sleep(0)
                assert checks==[] and not task.done()
                allowed=False
                engine._lock.release()
                with pytest.raises(failure,match='synthetic authorization revoked'): await task
                assert checks==['checked']
                assert (engine.db.total_changes,model.calls)==before
                assert not engine._lock.locked()
            finally:
                for db in [engine.db,engine.shared.db,*[wheel.db for wheel in engine.wheels.values()]]: db.set_authorizer(None)
            assert len(engine.labels())==1
            assert engine.labels()[0]['label']=='include'
            assert engine.pending_learning()==pending_before
        run(scenario())


def test_sync_and_async_guards_are_per_call_and_a_denied_call_does_not_poison_the_next(tmp_path):
    model=Model()
    with Cyclotron.open(tmp_path/'c',DEFINITION,model) as engine:
        calls=[]
        def sync_guard():
            assert engine._lock.locked()
            calls.append('sync')
        async def async_guard():
            await asyncio.sleep(0)
            assert engine._lock.locked()
            calls.append('async')
        def deny(): raise PermissionError('denied')
        async def scenario():
            with pytest.raises(PermissionError): await engine.decide(item('denied'),execution_guard=deny)
            first=await engine.decide(item('first'),execution_guard=sync_guard)
            await engine.review(first.decision_id,'include',execution_guard=async_guard)
            async with engine.execution(execution_guard=async_guard):
                assert engine.status().pending.decisions_awaiting_review==0
            await engine.decide(item('second','no, distinct content'))
        run(scenario())
        assert calls==['sync','async','async']
        assert model.calls==2


def test_cancellation_while_guard_is_awaiting_releases_ownership_without_mutation(tmp_path):
    model=Model()
    with Cyclotron.open(tmp_path/'c',DEFINITION,model) as engine:
        async def scenario():
            started=asyncio.Event()
            async def guard():
                started.set()
                await asyncio.Event().wait()
            task=asyncio.create_task(engine.decide(item('cancelled'),execution_guard=guard))
            await started.wait()
            assert engine._lock.locked()
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert not engine._lock.locked()
            assert engine.decision_log()==[] and model.calls==0
            await engine.decide(item('next'))
        run(scenario())


@pytest.mark.parametrize('guard',[False,lambda:False,lambda:True])
def test_invalid_guard_or_non_none_result_cannot_silently_authorize(tmp_path,guard):
    model=Model()
    with Cyclotron.open(tmp_path/'c',DEFINITION,model) as engine:
        with pytest.raises(TypeError): run(engine.decide(item('no'),execution_guard=guard))
        assert engine.decision_log()==[] and model.calls==0


def test_replay_rechecks_each_separately_owned_decision_and_review(tmp_path):
    model=Model()
    with Cyclotron.open(tmp_path/'c',DEFINITION,model) as engine:
        checks=[]
        def guard():
            checks.append('checked')
            if len(checks)==2: raise PermissionError('revoked between decision and label')
        with pytest.raises(PermissionError):
            run(engine.replay([{'item':item('one'),'label':'include'}],execution_guard=guard))
        assert checks==['checked','checked']
        assert model.calls==1 and engine.labels()==[]


def test_deferred_learning_reauthorizes_before_reading_each_next_pass(tmp_path):
    from dataclasses import replace
    from .embedded_cyclotron import ClassifierSpec
    other=ClassifierSpec('other',('include','exclude'),'Is this a fit?',positive_label='include')
    definition=replace(DEFINITION,classifiers=(*DEFINITION.classifiers,other))
    with Cyclotron.open(tmp_path/'c',definition,Model(),learning='deferred') as engine:
        engine._enqueue_learning('relevant')
        engine._enqueue_learning('other')
        checks=[]
        passes=[]
        def guard():
            checks.append('check')
            if len(checks)>1: raise PermissionError('next pass revoked')
        async def learn(classifier,cycle,*,since_count=None):
            passes.append(classifier)
            return []
        engine._learn=learn
        with pytest.raises(PermissionError,match='next pass revoked'):
            run(engine.learn(execution_guard=guard))
        assert checks==['check','check'] and passes==['relevant']
        assert engine.pending_learning()==['other']


def test_cancellation_while_waiting_for_ownership_never_invokes_guard(tmp_path):
    with Cyclotron.open(tmp_path/'c',DEFINITION,Model()) as engine:
        async def scenario():
            called=[]
            async with engine.execution():
                task=asyncio.create_task(engine.decide(item('cancelled'),execution_guard=lambda:called.append('called')))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
            assert called==[] and engine.decision_log()==[]
        run(scenario())


@pytest.mark.parametrize('revocation',['membership','publication','binding','account','person'])
def test_host_guard_rechecks_central_membership_and_separate_pinned_publication_authority_after_wait(tmp_path,revocation):
    from .prospectus_accounts import AccountsClient,AccountAccessDenied
    current={'membership':True,'publication':True,'binding':'revision-1','account':'a','person':'p'}
    checks=[]
    def transport(method,path,token,body):
        checks.append('central')
        if not current['membership']: raise AccountAccessDenied('revoked')
        return dict(allowed=True,accountId='a',personId='p',product='cyclotron',role='member',operation=body['operation'],revision=1)
    client=AccountsClient(transport)
    async def guard():
        if current['account']!='a' or current['person']!='p': raise AccountAccessDenied('scope changed')
        await asyncio.to_thread(client.authorize,'synthetic','a','p','console:write')
        if not current['publication'] or current['binding']!='revision-1': raise AccountAccessDenied('publication changed')
    model=Model()
    with Cyclotron.open(tmp_path/'c',DEFINITION,model) as engine:
        async def scenario():
            # Initial HTTP/SDK precheck succeeds; it is not execution authority.
            await guard()
            async with engine.execution():
                task=asyncio.create_task(engine.decide(item('no'),execution_guard=guard))
                await asyncio.sleep(0)
                current[revocation]=False if revocation in ('membership','publication') else 'changed'
                assert not task.done()
            with pytest.raises(AccountAccessDenied): await task
            assert engine.decision_log()==[] and model.calls==0
        run(scenario())


def test_guarded_learning_keeps_ownership_through_provider_calls_and_next_caller_reauthorizes(tmp_path):
    from .embedded_learning_window_test import GatedTransport,store,checklist,OUTPUTS
    transport=GatedTransport()
    with store(tmp_path,transport) as engine:
        for index in range(12): checklist(engine,index,OUTPUTS[:1])
        checks=[]
        async def scenario():
            transport.renew()
            def guard():
                assert engine._lock.locked()
                checks.append('learn')
            learning=asyncio.create_task(engine.learn(execution_guard=guard))
            await asyncio.wait_for(transport.entered.wait(),5)
            assert engine._lock.locked()
            def revoked():
                checks.append('next')
                raise PermissionError('revoked while queued')
            queued=asyncio.create_task(engine.decide(item('queued'),execution_guard=revoked))
            await asyncio.sleep(0)
            assert not queued.done() and checks==['learn']
            transport.gate.set()
            await asyncio.wait_for(learning,5)
            before=engine.db.total_changes
            with pytest.raises(PermissionError,match='revoked while queued'): await queued
            assert engine.db.total_changes==before
        run(scenario())
        assert checks[0]=='learn' and checks.count('next')==1
