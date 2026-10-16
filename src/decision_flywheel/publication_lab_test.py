"""The same publication contract runs locally and through a remote HTTP boundary."""
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from .batched_classification import BatchedAnswers
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .models import DecisionResult
from .publication_lab import (PublicationActor, PublicationBinding, PublicationLab,
                              EmbeddedPublicationClient, HTTPPublicationClient,
                              PublicationError, create_publication_app)

DEFINITION = CyclotronDefinition('common-reference-lab', (
    ClassifierSpec('intel', ('accept', 'reject'), 'Accept for Threat Intelligence?'),
    ClassifierSpec('pilobolus', ('accept', 'reject'), 'Accept for Pilobolus?'),
), seed='publication-contract', optimize_every=100)
BINDINGS = (PublicationBinding('threat-intelligence', ('intel',), 'binding-1'),
            PublicationBinding('pilobolus', ('pilobolus',), 'binding-1'))
ITEM = {'id': 'common-paper-v1', 'values': {'text': 'Synthetic representative research',
        'source_uri': 'https://example.invalid/paper', 'source_kind': 'research',
        'source_revision': 'fixture-v1'}}
ACTORS = {pub: PublicationActor('account-a', 'editor-'+pub, frozenset((pub, action) for action in ('decide','review','read')))
          for pub in ('threat-intelligence', 'pilobolus')}


class OfflineModel:
    model_identity = 'deterministic-publication-contract-v1'
    def __init__(self): self.calls = 0
    async def classify_many(self, configs, target, training, **kwargs):
        self.calls += 1
        return BatchedAnswers({cid: {'decision': DecisionResult('accept' if cid == 'intel' else 'reject',
            {'accept': .8 if cid == 'intel' else .2, 'reject': .2 if cid == 'intel' else .8})}
            for cid in configs}, 'offline-scripted', {}, 1)


def run(coro): return asyncio.run(coro)


@asynccontextmanager
async def clients(directory, mode, model):
    with Cyclotron.open(directory, DEFINITION, model) as engine:
        lab = PublicationLab(engine, account_id='account-a', bindings=BINDINGS)
        if mode == 'embedded':
            yield {pub: EmbeddedPublicationClient(lab, actor) for pub, actor in ACTORS.items()}, lab
        else:
            # Authenticated actor comes from host verification, never JSON/body claims.
            app = create_publication_app(lab, authorize=lambda request: ACTORS.get(request.headers.get('x-fixture-actor')))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://lab.test', headers={'x-fixture-actor':'threat-intelligence'}) as a:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://lab.test', headers={'x-fixture-actor':'pilobolus'}) as b:
                    yield {'threat-intelligence':HTTPPublicationClient(a), 'pilobolus':HTTPPublicationClient(b)}, lab


def review_payload(decision, **changes):
    result = decision['decision']['classifiers']['intel']
    return {'bindingRevision':'binding-1', 'decisionId':decision['decision']['decisionId'],
            'classifier':'intel', 'predictionVersion':result['version'], 'predictionFingerprint':result['fingerprint'],
            'requestId':'human-review-1', 'label':'reject', 'explanation':'Insufficient novel threat evidence.',
            'reasonCode':'insufficient_evidence', **changes}


@pytest.mark.parametrize('mode', ['embedded','remote'])
def test_common_items_get_independent_outputs_and_feedback_with_real_engine_execution(tmp_path, mode):
    async def scenario():
        model = OfflineModel()
        async with clients(tmp_path/'engine', mode, model) as (client, lab):
            a = await client['threat-intelligence'].command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'})
            b = await client['pilobolus'].command('pilobolus','decide',{'item':ITEM,'bindingRevision':'binding-1'})
            assert a['decision']['decisionId'] == b['decision']['decisionId']
            assert set(a['decision']['classifiers']) == {'intel'}
            assert set(b['decision']['classifiers']) == {'pilobolus'}
            assert a['decision']['classifiers']['intel']['label'] == 'accept'
            assert b['decision']['classifiers']['pilobolus']['label'] == 'reject'
            assert model.calls == 1  # Actual batched SDK execution, then durable reuse.
            payload = review_payload(a)
            saved = await client['threat-intelligence'].command('threat-intelligence','review',payload)
            assert saved == await client['threat-intelligence'].command('threat-intelligence','review',payload)
            assert saved['review']['explanation'] == payload['explanation']
            assert saved['review']['reasonCode'] == 'insufficient_evidence'
            assert saved['review']['reviewer'] == 'editor-threat-intelligence'
            history = await client['threat-intelligence'].command('threat-intelligence','history',{})
            assert history['history'][0]['item']['source_uri'] == ITEM['values']['source_uri']
            assert history['history'][0]['reviews']['intel']['label'] == 'reject'
            other = await client['pilobolus'].command('pilobolus','history',{})
            assert other['history'][0]['reviews'] == {}
            status = await client['pilobolus'].command('pilobolus','status',{})
            assert status['classifiers']['pilobolus']['alignment']['labels'] == 0
            with pytest.raises(PublicationError) as denied:
                await client['pilobolus'].command('pilobolus','review',payload)
            assert denied.value.status == 403
            assert len(lab.engine.labels()) == 1
    run(scenario())


@pytest.mark.parametrize('mode', ['embedded','remote'])
def test_restart_preserves_predictions_explanations_history_and_retry_identity(tmp_path, mode):
    async def scenario():
        model = OfflineModel()
        async with clients(tmp_path/'engine', mode, model) as (client, _):
            first = await client['threat-intelligence'].command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'})
            payload = review_payload(first)
            saved = await client['threat-intelligence'].command('threat-intelligence','review',payload)
        async with clients(tmp_path/'engine', mode, model) as (client, lab):
            assert await client['threat-intelligence'].command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'}) == first
            assert await client['threat-intelligence'].command('threat-intelligence','review',payload) == saved
            assert len(lab.engine.labels()) == 1
            history = await client['threat-intelligence'].command('threat-intelligence','history',{})
            assert history['history'][0]['reviews']['intel']['explanation'] == payload['explanation']
            assert model.calls == 1
    run(scenario())


@pytest.mark.parametrize('mode', ['embedded','remote'])
def test_stale_bindings_prediction_pins_and_changed_retry_payload_never_write_feedback(tmp_path, mode):
    async def scenario():
        async with clients(tmp_path/'engine', mode, OfflineModel()) as (client, lab):
            c = client['threat-intelligence']
            decision = await c.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'})
            for change in ({'bindingRevision':'old-binding'}, {'predictionFingerprint':'old-prediction'},
                           {'predictionVersion':999}, {'explanation':[]}, {'reviewer':'spoofed-editor'}):
                with pytest.raises(PublicationError):
                    await c.command('threat-intelligence','review',review_payload(decision, **change))
            assert lab.engine.labels() == []
            await c.command('threat-intelligence','review',review_payload(decision))
            with pytest.raises(PublicationError):
                await c.command('threat-intelligence','review',review_payload(decision, label='accept'))
            assert len(lab.engine.labels()) == 1
    run(scenario())


def test_authority_is_host_supplied_and_cross_account_or_publication_access_is_denied(tmp_path):
    async def scenario():
        model = OfflineModel()
        with Cyclotron.open(tmp_path/'engine', DEFINITION, model) as engine:
            lab = PublicationLab(engine, account_id='account-a', bindings=BINDINGS)
            for actor in (PublicationActor('other-account','editor',ACTORS['threat-intelligence'].grants),
                          ACTORS['pilobolus']):
                with pytest.raises(PublicationError) as denied:
                    await lab.command(actor,'threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'})
                assert denied.value.status == 403
            app = create_publication_app(lab, authorize=lambda request: None)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://lab.test') as http:
                response = await http.post('/publications/threat-intelligence/operations',json={'operation':'decide','payload':{'item':ITEM},'actor':'editor'})
                assert response.status_code == 403
            assert model.calls == 0
    run(scenario())


def test_binding_two_publications_to_one_feedback_output_is_not_silently_allowed(tmp_path):
    with Cyclotron.open(tmp_path/'engine', DEFINITION, OfflineModel()) as engine:
        with pytest.raises(ValueError, match='distinct publication'):
            PublicationLab(engine, account_id='account-a', bindings=(BINDINGS[0], PublicationBinding('other',('intel',),'binding-1')))


def test_remote_transport_failure_is_explicit_and_never_automatically_retried():
    async def scenario():
        calls=[]
        async def unavailable(request):
            calls.append(request)
            raise httpx.ReadTimeout('Synthetic lost acknowledgement',request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable),base_url='http://lab.test') as http:
            with pytest.raises(httpx.ReadTimeout):
                await HTTPPublicationClient(http).command('threat-intelligence','status',{})
        assert len(calls) == 1
    run(scenario())

@pytest.mark.parametrize('mode',['embedded','remote'])
def test_corrections_keep_scoped_audit_history_and_resumable_cursors(tmp_path,mode):
    async def scenario():
        async with clients(tmp_path/'engine',mode,OfflineModel()) as (client,_):
            c=client['threat-intelligence']
            decision=await c.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'})
            await c.command('threat-intelligence','review',review_payload(decision))
            before=await c.command('threat-intelligence','events',{'after':0,'limit':100})
            await c.command('threat-intelligence','review',review_payload(decision,requestId='correction-2',label='accept',explanation='Corrected after reading the full evidence.'))
            after=await c.command('threat-intelligence','events',{'after':before['cursor'],'limit':100})
            reviews=[event['review'] for event in before['events']+after['events'] if event['kind']=='review']
            assert [review['label'] for review in reviews]==['reject','accept']
            assert reviews[-1]['kind']=='correction'
            assert reviews[0]['explanation']=='Insufficient novel threat evidence.'
            sibling=await client['pilobolus'].command('pilobolus','events',{'after':0,'limit':100})
            assert all(event['kind']!='review' for event in sibling['events'])
            assert all(set(event['decision']['classifiers'])=={'pilobolus'} for event in sibling['events'] if event['kind']=='decision')
    run(scenario())


def test_a_lost_http_review_acknowledgement_reuses_the_actual_committed_label(tmp_path):
    async def scenario():
        with Cyclotron.open(tmp_path/'engine',DEFINITION,OfflineModel()) as engine:
            lab=PublicationLab(engine,account_id='account-a',bindings=BINDINGS)
            inner=httpx.ASGITransport(app=create_publication_app(lab,authorize=lambda _:ACTORS['threat-intelligence']))
            class LostAcknowledgement(httpx.AsyncBaseTransport):
                def __init__(self):self.lost=False;self.reviews=0
                async def handle_async_request(self,request):
                    response=await inner.handle_async_request(request)
                    if b'"operation":"review"' in request.content:
                        self.reviews+=1
                        if not self.lost:
                            self.lost=True
                            await response.aread();await response.aclose()
                            raise httpx.ReadTimeout('Synthetic response lost after commit',request=request)
                    return response
                async def aclose(self):await inner.aclose()
            transport=LostAcknowledgement()
            async with httpx.AsyncClient(transport=transport,base_url='http://offline.test') as http:
                client=HTTPPublicationClient(http)
                decision=await client.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'})
                payload=review_payload(decision)
                with pytest.raises(httpx.ReadTimeout):await client.command('threat-intelligence','review',payload)
                assert transport.reviews==1 and len(engine.labels())==1
                saved=await client.command('threat-intelligence','review',payload)
                assert saved['review']['label']=='reject'
                assert len(engine.labels())==1 and transport.reviews==2
                events=await client.command('threat-intelligence','events',{'after':0,'limit':100})
                assert sum(event['kind']=='review' for event in events['events'])==1
    run(scenario())


def test_http_boundary_rejects_cross_origin_malformed_operations_and_oversized_inputs(tmp_path):
    async def scenario():
        model=OfflineModel()
        with Cyclotron.open(tmp_path/'engine',DEFINITION,model) as engine:
            lab=PublicationLab(engine,account_id='account-a',bindings=BINDINGS)
            app=create_publication_app(lab,authorize=lambda _:ACTORS['threat-intelligence'])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as http:
                path='/publications/threat-intelligence/operations'
                body={'contractVersion':'cyclotron-publication/v1','operation':'status','payload':{}}
                assert (await http.post(path,json=body,headers={'Origin':'https://untrusted.invalid'})).status_code==403
                assert (await http.post(path,json={**body,'operation':{}})).status_code==400
                assert (await http.post(path,content=b' '*2_000_001)).status_code==413
                assert model.calls==0
    run(scenario())


def test_remote_uncertain_outcomes_remain_reconciliation_errors_without_automatic_retry():
    async def scenario():
        calls=[]
        async def response(request):
            calls.append(request)
            return httpx.Response(409,json={'error':'Pending command needs reconciliation','code':'reconciliation_required'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(response),base_url='http://offline.test') as http:
            with pytest.raises(PublicationError,match='reconciliation') as error:
                await HTTPPublicationClient(http).command('threat-intelligence','steer',{})
            assert error.value.code=='reconciliation_required' and len(calls)==1
    run(scenario())


@pytest.mark.parametrize('mode', ['embedded','remote'])
@pytest.mark.parametrize('explanation', ['omitted',None,'','  ','  Human reason  ','  First line\nSecond line  '])
def test_optional_review_explanation_uses_existing_native_normalization_and_survives_reopen(tmp_path, mode, explanation):
    async def scenario():
        directory=tmp_path/'engine';model=OfflineModel()
        async with clients(directory,mode,model) as (client,lab):
            c=client['threat-intelligence']
            decision=await c.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'})
            payload=review_payload(decision)
            if explanation=='omitted':payload.pop('explanation')
            else:payload['explanation']=explanation
            saved=await c.command('threat-intelligence','review',payload)
            expected=(explanation.strip() or None) if isinstance(explanation,str) and explanation!='omitted' else None
            assert saved['review']['label']=='reject'
            assert saved['review']['explanation']==expected
            assert await c.command('threat-intelligence','review',payload)==saved
            if expected is None:
                for absent in (None,'','  '):
                    assert await c.command('threat-intelligence','review',{**payload,'explanation':absent})==saved
            assert len(lab.engine.labels())==1
        async with clients(directory,mode,model) as (client,lab):
            c=client['threat-intelligence']
            assert await c.command('threat-intelligence','review',payload)==saved
            history=await c.command('threat-intelligence','history',{})
            assert history['history'][0]['reviews']['intel']['explanation']==expected
            assert len(lab.engine.labels())==1 and model.calls==1
            with pytest.raises(PublicationError):
                await c.command('threat-intelligence','review',{**payload,'explanation':'Changed human reason'})
            assert len(lab.engine.labels())==1
    run(scenario())


@pytest.mark.parametrize('mode', ['embedded','remote'])
def test_optional_explanation_still_requires_configured_label_and_bounded_text(tmp_path, mode):
    async def scenario():
        async with clients(tmp_path/'engine',mode,OfflineModel()) as (client,lab):
            c=client['threat-intelligence']
            decision=await c.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1'})
            for value in (False,0,[],{},'x'*4001):
                with pytest.raises(PublicationError):
                    await c.command('threat-intelligence','review',review_payload(decision,explanation=value))
            for value in (None,'','  ','unknown'):
                with pytest.raises(PublicationError):
                    await c.command('threat-intelligence','review',review_payload(decision,label=value,explanation=''))
            payload=review_payload(decision,explanation='');payload.pop('label')
            with pytest.raises(PublicationError):await c.command('threat-intelligence','review',payload)
            assert lab.engine.labels()==[]
    run(scenario())
