"""Curated examples prime context without pretending to be operational decisions."""
import asyncio
import pytest
from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import DEFINITION,Model,item
from .models import Item,LabeledItem
from .flywheel_test import agent


def seed():return {'relevant':[LabeledItem(Item('synthetic-prime',{'text':'Synthetic reviewed reference'}),'include','trusted',{'human_feedback':'Synthetic explicit reason'})]}


def test_priming_context_is_persisted_training_only_and_used_before_first_decision(tmp_path):
    class Inspect(Model):
        async def classify_many(self,configs,target,training,**kwargs):
            assert [row.item.id for row in training['relevant']]==['synthetic-prime']
            assert configs['relevant'].example_ids==('synthetic-prime',)
            return await super().classify_many(configs,target,training,**kwargs)
    model=Inspect();c=Cyclotron.open(tmp_path/'engine',DEFINITION,model,agent([]),priming_examples=seed())
    try:
        assert c.status().alignment.labels==0
        train,dev,protected=c._partitions('relevant')
        assert train[0].context['human_feedback']=='Synthetic explicit reason'
        assert dev==protected==()
        asyncio.run(c.decide(item('operational')))
        with pytest.raises(ValueError,match='priming'):asyncio.run(c.decide(item('synthetic-prime')))
        assert model.calls==1 and c.labels()==[]
    finally:c.close()
    reopened=Cyclotron.open(tmp_path/'engine',DEFINITION,Model(),agent([]))
    try:assert reopened._partitions('relevant')[0][0].item.id=='synthetic-prime'
    finally:reopened.close()


def test_reopening_cannot_silently_replace_accepted_priming(tmp_path):
    c=Cyclotron.open(tmp_path/'engine',DEFINITION,Model(),agent([]),priming_examples=seed());c.close()
    changed={'relevant':[LabeledItem(Item('other',{'text':'Other'}),'include','trusted',{'human_feedback':'Reason'})]}
    with pytest.raises(ValueError,match='priming'):Cyclotron.open(tmp_path/'engine',DEFINITION,Model(),agent([]),priming_examples=changed)


def test_priming_requires_explicit_rationale_and_known_label(tmp_path):
    for n,row in enumerate([LabeledItem(Item('p',{'text':'p'}),'include'),LabeledItem(Item('p',{'text':'p'}),'invented',context={'human_feedback':'why'})]):
        with pytest.raises(ValueError):Cyclotron.open(tmp_path/str(n),DEFINITION,Model(),agent([]),priming_examples={'relevant':[row]})


def test_workspace_run_pins_accepted_examples_separately_and_refuses_operational_overlap(tmp_path):
    import base64,json
    from .web_store import WebStore
    from .priming import PrimingService
    from .cyclotron_runtime import CyclotronRuntime
    from .workspace_session import WorkspaceSession
    store=WebStore(tmp_path/'web.db')
    store.save_classifier('relevant','Relevance',{'question':'Include?','classes':[{'label':'include'},{'label':'exclude'}]})
    store.save_item_list('examples','Examples');store.save_item_list('live','Operational')
    row={'id':'prime','occurred_at':'2026-10-14','values':{'text':'Synthetic accepted example'},'labels':{'relevant':{'label':'include','rationale':'Synthetic explicit rationale'}}}
    service=PrimingService(store);draft=service.start_file('draft','examples',[{'id':'relevant','revision':1}],'json',base64.b64encode(json.dumps([row]).encode()).decode());service.accept('draft',draft['version'],None)
    store.upsert_list_items('live',[{'id':'operational','occurred_at':'2026-10-14','values':{'text':'Synthetic live item'}}])
    runtime=CyclotronRuntime(store,tmp_path/'runs',model_factory=lambda *_:None,sink_factory=lambda *_:None)
    run=runtime.create_run('Primed',{'classifier_ids':['relevant'],'item_list_id':'live','priming_list_id':'examples'})
    session=WorkspaceSession(store,run,tmp_path/'session',Model(),agent([]),lambda _:None)
    try:
        assert [row.item.id for row in session.partitions('relevant')[0]]==['prime']
        assert session.wheels['relevant'].initial.example_ids==('prime',)
        assert session.partitions('relevant')[1]==()
    finally:session.close()
    store.upsert_list_items('live',[{'id':'prime','occurred_at':'2026-10-14','values':{'text':'Overlapping identity'}}])
    with pytest.raises(ValueError,match='disjoint'):runtime.create_run('Leak',{'classifier_ids':['relevant'],'item_list_id':'live','priming_list_id':'examples'})
