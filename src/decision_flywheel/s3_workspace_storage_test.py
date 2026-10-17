"""Existing console workspace commands can reopen with S3 and frozen priming."""
import asyncio

import pytest

from .batched_classification import BatchedAnswers
from .models import DecisionResult
from .optimizer_agent import DisabledOptimizer
from .s3_console_storage_test import seed, store
from .s3_journal_test import journal
from .workspace_session import WorkspaceSession, freeze_configuration


class Model:
    model_identity='synthetic-workspace';calls=0
    async def classify_many(self,configs,target,training,**kwargs):
        self.calls+=1
        return BatchedAnswers({cid:{'decision':DecisionResult('science',{'science':.8,'sport':.1,'business':.1})} for cid in configs},self.model_identity,{},1)


def fixture():
    s=store();seed(s)
    config=freeze_configuration(s,{'classifier_ids':['topic'],'item_list_id':'papers','max_requests':1,
        'max_optimizer_calls':1,'optimize_every':100,'rubric_changes_every':2,'seed':'synthetic'})
    run=s.create_run('Hosted workspace','live',config,items=s.list_items('papers'))
    return s,run


def test_existing_prepare_feedback_and_reopen_need_no_sqlite_or_files(monkeypatch):
    from .s3_workspace_storage import S3WorkspaceStores
    s,run=fixture();client=s.journal.payloads.client;model=Model()
    def stores():return S3WorkspaceStores(journal(client,name='workspace-root'),journal_for=lambda domain,name:journal(client,name=domain+'-'+name),max_records=10000)
    def forbidden(*args,**kwargs):raise AssertionError('hosted workspace touched SQLite or local files')
    monkeypatch.setattr('sqlite3.connect',forbidden);monkeypatch.setattr('pathlib.Path.mkdir',forbidden)
    session=WorkspaceSession(s,run,None,model,DisabledOptimizer(),lambda event:None,storage=stores())
    shown=asyncio.run(session.prepare());session.close()
    assert shown['item']['id']=='one' and model.calls==1
    reopened=WorkspaceSession(s,run,None,model,DisabledOptimizer(),lambda event:None,storage=stores())
    assert asyncio.run(reopened.prepare())==shown and model.calls==1
    result=asyncio.run(reopened.feedback({'item_id':'one','presentation_id':shown['prediction']['presentation_id'],
        'labels':[{'classifier_id':'topic','label':'science','comment':'Exact human rationale 雪'}]},'vote'))
    assert result['reviewed']=='one'
    assert reopened.wheels['topic'].db is None and reopened.shared.db is None
    assert s.item_labels('papers','one',1)[0]['comment']=='Exact human rationale 雪'
    reopened.close()


def test_missing_cloud_storage_cannot_fall_back_and_reopened_budget_is_absolute(monkeypatch):
    from .s3_workspace_storage import S3WorkspaceStores
    s,run=fixture();client=s.journal.payloads.client
    def stores():return S3WorkspaceStores(journal(client,name='workspace-root'),journal_for=lambda domain,name:journal(client,name=domain+'-'+name),max_records=10000)
    with pytest.raises(ValueError):WorkspaceSession(s,run,None,Model(),DisabledOptimizer(),lambda event:None)
    first=WorkspaceSession(s,run,None,Model(),DisabledOptimizer(),lambda event:None,storage=stores())
    asyncio.run(first.prepare());first.close()
    # The charged attempt survives per-invocation composition/release.
    second=WorkspaceSession(s,run,None,Model(),DisabledOptimizer(),lambda event:None,storage=stores())
    assert second.shared.requests==1 and second.shared.max_requests==1
    second.close()


@pytest.mark.parametrize('vote',[
    {'label':'science'}, {'label':'science','rationale':None},
    {'label':'science','rationale':''}, {'label':'science','rationale':'   '},
    {'label':'science','rationale':'Exact original rationale 雪'},
])
def test_s3_workspace_uses_frozen_accepted_priming_without_retrofit_or_partition_leak(monkeypatch,vote):
    import base64
    import json

    from .priming import PrimingService
    from .s3_workspace_storage import S3WorkspaceStores
    s,run=fixture();s.save_item_list('examples','Accepted examples')
    service=PrimingService(s,storage=s)
    row={'id':'prime','occurred_at':'2026-01-01','values':{'text':'Distinct accepted example'},
         'labels':{'topic':vote}}
    draft=service.start_file('draft','examples',[{'id':'topic','revision':1}],'json',base64.b64encode(json.dumps([row]).encode()).decode())
    service.accept('draft',draft['version'],None)
    supplied=vote.get('rationale') or ''
    assert s.item_labels('examples','prime',1)[0]['comment']==supplied
    config=freeze_configuration(s,{'classifier_ids':['topic'],'item_list_id':'papers','priming_list_id':'examples','max_requests':1,
        'max_optimizer_calls':1,'optimize_every':100,'rubric_changes_every':2,'seed':'synthetic'})
    run=s.create_run('Frozen priming','live',config,items=s.list_items('papers'))
    s.label_item('topic',1,'examples','prime',1,'sport','Later correction','later')
    client=s.journal.payloads.client
    stores=S3WorkspaceStores(journal(client,name='frozen-root'),journal_for=lambda domain,name:journal(client,name=domain+'-'+name),max_records=10000)
    def forbidden(*args,**kwargs):raise AssertionError('S3 priming touched SQLite')
    monkeypatch.setattr('sqlite3.connect',forbidden)
    session=WorkspaceSession(s,run,None,Model(),DisabledOptimizer(),lambda event:None,storage=stores)
    training,development,protected=session.partitions('topic')
    assert training[0].item.id=='prime' and training[0].label=='science'
    assert training[0].context==({'human_feedback':supplied} if supplied.strip() else {})
    assert not development and all(item.id!='prime' for item in protected) and session.wheels['topic'].initial.example_ids==('prime',)
    session.close();service.close()
