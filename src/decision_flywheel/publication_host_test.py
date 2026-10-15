"""Fresh host grants select the right lab without trusting browser identity claims."""
import asyncio

import httpx
import pytest
from .embedded_cyclotron import Cyclotron
from .publication_lab import (PublicationLab,PublicationActor,PublicationAccess,PublicationError,
                              HTTPPublicationClient,create_publication_host)
from .publication_lab_test import DEFINITION,BINDINGS,ITEM,OfflineModel,review_payload


def test_request_scoped_account_switching_revocation_and_unavailability_fail_closed(tmp_path):
    async def scenario():
        models={account:OfflineModel() for account in ('account-a','account-b')}
        with Cyclotron.open(tmp_path/'a',DEFINITION,models['account-a']) as a, Cyclotron.open(tmp_path/'b',DEFINITION,models['account-b']) as b:
            labs={account:PublicationLab(engine,account_id=account,bindings=BINDINGS) for account,engine in [('account-a',a),('account-b',b)]}
            selected={'account':'account-a','allowed':True,'available':True};calls=[]
            async def access(request,publication,capability):
                calls.append((selected['account'],publication,capability))
                if not selected['available']:raise ConnectionError('Synthetic central service outage; never expose tokens')
                if not selected['allowed']:return None
                actor=PublicationActor(selected['account'],'verified-person',frozenset((publication,action) for action in ('read','decide','review')))
                return PublicationAccess(labs[selected['account']],actor)
            app=create_publication_host(access)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as http:
                ca=HTTPPublicationClient(http,account_id='account-a')
                first=await ca.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'})
                saved=await ca.command('threat-intelligence','review',review_payload(first))
                assert saved['accountId']=='account-a'
                selected['account']='account-b';cb=HTTPPublicationClient(http,account_id='account-b')
                second=await cb.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'})
                assert first['decision']['decisionId']!=second['decision']['decisionId']
                assert (await cb.command('threat-intelligence','history',{}))['history'][0]['reviews']=={}
                with pytest.raises(PublicationError,match='account'):await ca.command('threat-intelligence','status',{})
                before=models['account-b'].calls
                with pytest.raises(PublicationError) as stale:
                    await ca.command('threat-intelligence','decide',{'item':{**ITEM,'id':'must-not-dispatch'},'bindingRevision':'binding-1'})
                assert stale.value.code=='account_changed' and models['account-b'].calls==before
                selected['allowed']=False
                with pytest.raises(PublicationError) as denied:await cb.command('threat-intelligence','status',{})
                assert denied.value.status==403
                selected['allowed']=True;selected['available']=False
                with pytest.raises(PublicationError) as unavailable:await cb.command('threat-intelligence','status',{})
                assert unavailable.value.status==503
                assert len(a.labels())==1 and b.labels()==[]
                assert len(calls)==8  # No cached authorization or local fallback.
    asyncio.run(scenario())


def test_client_identity_fields_and_mismatched_host_access_never_select_another_lab(tmp_path):
    async def scenario():
        model=OfflineModel()
        with Cyclotron.open(tmp_path/'engine',DEFINITION,model) as engine:
            lab=PublicationLab(engine,account_id='account-a',bindings=BINDINGS)
            actor=PublicationActor('account-b','person',frozenset({('threat-intelligence','decide')}))
            app=create_publication_host(lambda *args:PublicationAccess(lab,actor))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test',headers={'X-Cyclotron-Account':'account-a'}) as http:
                path='/publications/threat-intelligence/operations'
                body={'contractVersion':'cyclotron-publication/v1','operation':'decide','payload':{'item':ITEM,'bindingRevision':'binding-1'}}
                assert (await http.post(path,json=body)).status_code==403
                assert (await http.post(path,json={**body,'accountId':'account-a','actor':'admin'})).status_code==400
                assert model.calls==0
    asyncio.run(scenario())


def test_cancelled_authorization_dispatches_nothing_and_explicit_retry_resolves_again(tmp_path):
    async def scenario():
        model=OfflineModel();entered=asyncio.Event();release=asyncio.Event();calls=[]
        with Cyclotron.open(tmp_path/'engine',DEFINITION,model) as engine:
            lab=PublicationLab(engine,account_id='account-a',bindings=BINDINGS)
            actor=PublicationActor('account-a','person',frozenset({('threat-intelligence','decide')}))
            async def resolve(*args):
                calls.append(args[2]);entered.set();await release.wait()
                return PublicationAccess(lab,actor)
            app=create_publication_host(resolve)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as http:
                client=HTTPPublicationClient(http,account_id='account-a')
                payload={'item':ITEM,'bindingRevision':'binding-1'}
                task=asyncio.create_task(client.command('threat-intelligence','decide',payload))
                await entered.wait();task.cancel()
                with pytest.raises(asyncio.CancelledError):await task
                assert model.calls==0 and engine.decision_log()==[]
                release.set()
                result=await client.command('threat-intelligence','decide',payload)
                assert result['accountId']=='account-a' and model.calls==1 and len(calls)==2
                # A subsequent identical command reuses the native stored decision.
                assert await client.command('threat-intelligence','decide',payload)==result
                assert model.calls==1 and len(calls)==3
    asyncio.run(scenario())


def test_dynamic_host_requires_account_pin_and_valid_contract_before_resolving_access():
    async def scenario():
        calls=[]
        app=create_publication_host(lambda *args:calls.append(args))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as http:
            path='/publications/threat-intelligence/operations'
            body={'contractVersion':'cyclotron-publication/v1','operation':'status','payload':{}}
            assert (await http.post(path,json=body)).status_code==400
            headers={'X-Cyclotron-Account':'account-a'}
            assert (await http.post(path,json={**body,'operation':[]},headers=headers)).status_code==400
            assert (await http.post(path,json=body,headers={**headers,'Origin':'https://other.invalid'})).status_code==403
            assert calls==[]
    asyncio.run(scenario())
