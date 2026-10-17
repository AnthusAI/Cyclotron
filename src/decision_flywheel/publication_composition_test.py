"""Catalog-backed provenance freezes the inputs actually shown to each publication."""
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from .embedded_cyclotron import Cyclotron
from .publication_lab import PublicationLab,PublicationBinding,EmbeddedPublicationClient,HTTPPublicationClient,create_publication_app,PublicationError
from .publication_records import PublicationRecords
from .publication_lab_test import DEFINITION,BINDINGS,ACTORS,ITEM,OfflineModel,review_payload
from .web_store import WebStore


def catalog(tmp_path,revision=1):
    store=WebStore(tmp_path/'catalog.sqlite3')
    for key in ('sources','bindings','records'):store.save_item_list(key,key)
    store.upsert_list_items('sources',[{'id':ITEM['id'],'values':ITEM['values'],'occurred_at':'2026-01-01T00:00:00Z'}]) if revision==1 else None
    for binding in BINDINGS:
        store.upsert_list_items('bindings',[{'id':binding.publication_id,'values':{'accountId':'account-a','publicationId':binding.publication_id,'revision':f'binding-{revision}','outputs':list(binding.outputs)},'occurred_at':f'2026-01-0{revision}T00:00:00Z'}])
    records=PublicationRecords(store,account_id='account-a',source_lists={'sources'},binding_list='bindings',record_list='records')
    return store,records


@asynccontextmanager
async def host(tmp_path,mode,records,model,revision=1):
    with Cyclotron.open(tmp_path/'engine',DEFINITION,model) as engine:
        bindings=tuple(PublicationBinding(row.publication_id,row.outputs,f'binding-{revision}') for row in BINDINGS)
        lab=PublicationLab(engine,account_id='account-a',bindings=bindings,records=records)
        if mode=='embedded':yield EmbeddedPublicationClient(lab,ACTORS['threat-intelligence']),engine
        else:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_publication_app(lab,authorize=lambda _:ACTORS['threat-intelligence'])),base_url='http://offline.test') as http:
                yield HTTPPublicationClient(http),engine


def request(records,source_revision=1,binding_revision=1):
    return {'sourceRef':records.reference('sources',ITEM['id'],source_revision).to_json(),'bindingRef':records.reference('bindings','threat-intelligence',binding_revision).to_json()}


@pytest.mark.parametrize('mode',['embedded','remote'])
def test_historical_source_values_survive_sdk_latest_item_replacement_and_restart(tmp_path,mode):
    async def scenario():
        store,records=catalog(tmp_path);model=OfflineModel()
        async with host(tmp_path,mode,records,model) as (client,engine):
            first=await client.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1','context':request(records)})
            changed={**ITEM,'values':{**ITEM['values'],'text':'Updated source abstract'}}
            store.upsert_list_items('sources',[{**changed,'occurred_at':'2026-01-02T00:00:00Z'}])
            second=await client.command('threat-intelligence','decide',{'item':changed,'bindingRevision':'binding-1','context':request(records,2)})
            assert first['context']['contextId']!=second['context']['contextId']
            with pytest.raises(PublicationError):
                await client.command('threat-intelligence','review',{**review_payload(first),'contextId':first['context']['contextId']})
            assert engine.labels()==[]
        # Reopen both authoritative native stores; no in-memory provenance cache.
        records=PublicationRecords(WebStore(tmp_path/'catalog.sqlite3'),account_id='account-a',source_lists={'sources'},binding_list='bindings',record_list='records')
        async with host(tmp_path,mode,records,model) as (client,_):
            history=await client.command('threat-intelligence','history',{})
            assert [row['item']['text'] for row in history['history']]==[ITEM['values']['text'],'Updated source abstract']
            assert history['history'][0]['historicalContext'] is True
            assert history['history'][0]['context']['sourceRef']['revision']==1
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['embedded','remote'])
def test_binding_history_and_replay_keep_original_context_and_human_explanations(tmp_path,mode):
    async def scenario():
        store,records=catalog(tmp_path);model=OfflineModel()
        async with host(tmp_path,mode,records,model) as (client,engine):
            first=await client.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1','context':request(records)})
            await client.command('threat-intelligence','review',{**review_payload(first),'contextId':first['context']['contextId']})
            changed={**ITEM,'values':{**ITEM['values'],'text':'A changed source must not silently rewrite labeled training data.'}}
            store.upsert_list_items('sources',[{**changed,'occurred_at':'2026-01-02T00:00:00Z'}])
            with pytest.raises(PublicationError):
                await client.command('threat-intelligence','decide',{'item':changed,'bindingRevision':'binding-1','context':request(records,source_revision=2)})
            assert engine.labels()[0]['item'].values==ITEM['values'] and model.calls==1
        _,records=catalog(tmp_path,revision=2)
        async with host(tmp_path,mode,records,model,revision=2) as (client,engine):
            second=await client.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-2','context':request(records,binding_revision=2)})
            assert first['decision']['decisionId']==second['decision']['decisionId']
            payload={**review_payload(second,bindingRevision='binding-2',label='accept',explanation='Explicitly corrected under the new publication binding.'),'contextId':second['context']['contextId']}
            await client.command('threat-intelligence','review',payload)
            assert len(engine.labels())==1
            replay=await client.command('threat-intelligence','replay',{})
            assert [row['binding']['values']['revision'] for row in replay['records']]==['binding-1','binding-2']
            assert [row['review']['label'] for row in replay['records']]==['reject','accept']
            assert replay['records'][0]['review']['explanation']=='Insufficient novel threat evidence.'
            assert replay['records'][0]['source']['values']==ITEM['values']
            assert replay['records'][0]['prediction']==first['decision']
            history=await client.command('threat-intelligence','history',{})
            assert [row['context']['binding']['values']['revision'] for row in history['history']]==['binding-1','binding-2']
            assert history['history'][0]['reviews']['intel']['label']=='reject'
            assert history['history'][0]['historicalContext'] is True
            with Cyclotron.open(tmp_path/'replayed-engine',DEFINITION,OfflineModel()) as restored:
                from .models import Item
                labels=[{'item':Item(row['source']['id'],row['source']['values']),
                    'classifier':row['review']['classifier'],'label':row['review']['label'],
                    'explanation':row['review']['explanation'],'reason_code':row['review']['reasonCode'],
                    'reviewer':row['review']['reviewer'],'review_id':row['review']['reviewId'],
                    'selected_by':row['review']['selectedBy']} for row in replay['records']]
                assert await restored.replay(labels)==2
                active=restored.labels()
                assert len(active)==1 and active[0]['classifier']=='intel' and active[0]['label']=='accept'
                assert active[0]['explanation']=='Explicitly corrected under the new publication binding.'
    asyncio.run(scenario())


def test_outputs_with_historical_publication_feedback_cannot_be_silently_reassigned(tmp_path):
    _,records=catalog(tmp_path)
    async def scenario():
        async with host(tmp_path,'embedded',records,OfflineModel()) as (client,engine):
            await client.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1','context':request(records)})
            with pytest.raises(ValueError,match='historical output ownership'):
                PublicationLab(engine,account_id='account-a',bindings=(PublicationBinding('different-publication',('intel',),'binding-2'),),records=records)
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['embedded','remote'])
def test_output_scoped_steering_runs_native_evaluation_without_changing_sibling_classifier(tmp_path,mode):
    import json
    from .batched_classification import BatchedAnswers
    from .models import Item,DecisionResult,LabeledItem
    from .publication_lab import PublicationActor
    from .publication_steering import PublicationSteering,NativeOutputEvaluator
    class NativeModel(OfflineModel):
        model_identity='offline-native-publication-steering-v1'
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1;answers={}
            for cid,config in configs.items():
                outputs={'decision':DecisionResult('reject',{'accept':.2,'reject':.8})}
                for task in config.tasks:
                    positive=target.values['text'].startswith('yes')
                    outputs[task.name]=DecisionResult('yes' if positive else 'no',{'yes':.98 if positive else .02,'no':.02 if positive else .98})
                answers[cid]=outputs
            return BatchedAnswers(answers,'offline-native',{},1)
    training=tuple(LabeledItem(Item(f'train-{i}',{'text':('yes' if i%2 else 'no')+f' training {i}'}),'accept' if i%2 else 'reject') for i in range(8))
    development=tuple(LabeledItem(Item(f'dev-{i}',{'text':('yes' if i%2 else 'no')+f' development {i}'}),'accept' if i%2 else 'reject') for i in range(4))
    async def scenario():
        _,records=catalog(tmp_path);model=NativeModel()
        actor=PublicationActor('account-a','editor-a',frozenset({('threat-intelligence','read'),('threat-intelligence','steer')}))
        with Cyclotron.open(tmp_path/'engine',DEFINITION,model) as engine:
            evaluator=NativeOutputEvaluator('intel',engine.wheels['intel'],partitions=lambda:{'training':training,'development':development,'protected':(),'propensities':{row.item.id:1. for row in training}})
            gateway=PublicationSteering(records,{'intel':evaluator},owner_lock=engine._lock,allow_evaluation=True)
            lab=PublicationLab(engine,account_id='account-a',bindings=BINDINGS,records=records,steering=gateway)
            payload={'bindingRevision':'binding-1','bindingRef':records.reference('bindings','threat-intelligence',1).to_json(),'classifier':'intel','requestId':'native-evaluation','expectedFingerprint':engine.wheels['intel'].active.fingerprint,'action':'evaluate','proposal':{'rubric':'Evaluate practical evidence','tasks':[{'name':'practical','instructions':'Is this practical?','labels':['yes','no']}]},'explanation':'Human-requested scoped trial.'}
            sibling_before=engine.wheels['pilobolus'].active.fingerprint
            async def exercise(client):
                with pytest.raises(PublicationError):await client.command('threat-intelligence','steer',{**payload,'classifier':'pilobolus'})
                result=await client.command('threat-intelligence','steer',payload)
                assert result['steering']['result']['trial_fingerprint']
                assert result['steering']['result']['promoted'] is False
                assert 'pilobolus' not in json.dumps(result['steering'])
                calls=model.calls
                assert await client.command('threat-intelligence','steer',payload)==result
                assert model.calls==calls and model.calls>0
                assert engine.wheels['pilobolus'].active.fingerprint==sibling_before
                assert engine.labels()==[]
            if mode=='embedded':await exercise(EmbeddedPublicationClient(lab,actor))
            else:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_publication_app(lab,authorize=lambda _:actor)),base_url='http://offline.test') as http:
                    await exercise(HTTPPublicationClient(http))
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['embedded','remote'])
def test_uncomposed_canonical_retraction_blocks_replay_instead_of_resurrecting_a_label(tmp_path,mode):
    async def scenario():
        _,records=catalog(tmp_path)
        async with host(tmp_path,mode,records,OfflineModel()) as (client,engine):
            first=await client.command('threat-intelligence','decide',{'item':ITEM,'bindingRevision':'binding-1','context':request(records)})
            await client.command('threat-intelligence','review',{**review_payload(first),'contextId':first['context']['contextId']})
            await engine.undo_review(first['decision']['decisionId'],classifier='intel',review_id='external-retraction')
            assert engine.labels()==[]
            with pytest.raises(PublicationError,match='reconcil|refused'):
                await client.command('threat-intelligence','replay',{})
            history=await client.command('threat-intelligence','history',{})
            assert history['history'][0]['feedbackReconciliationRequired'] is True
            assert history['history'][0]['historicalContext'] is True
    asyncio.run(scenario())
