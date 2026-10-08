"""Exercise GraphQL and cursor subscriptions without network or model keys."""
import asyncio
import pytest
pytest.importorskip('strawberry')
from fastapi.testclient import TestClient
from .web_api import create_app
from .web_store import WebStore


def test_the_app_viewport_exposes_device_safe_areas_without_disabling_zoom(tmp_path):
    response=TestClient(create_app(WebStore(tmp_path/'db'))).get('/')
    assert response.status_code==200
    assert 'viewport-fit=cover' in response.text
    assert 'user-scalable=no' not in response.text


def test_labeling_access_explains_inactive_editions_without_changing_recorded_state(tmp_path):
    store=WebStore(tmp_path/'db')
    for key in ('old','new'):
        store.save_classifier(key,key,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','Items')
    parent=store.create_run('Original','live',{'classifiers':[store.classifier('old')],'item_list_id':'list','seed':'seed'})
    added=store.extend_scorecard(parent['id'],['new'],name='Versions')
    recorded=store.create_run('Recording','recorded',{})
    client=TestClient(create_app(store))
    query='query($id:ID!){run(runId:$id){labelingAccess scorecardId config}}'
    def access(run):
        result=client.post('/graphql',json={'query':query,'variables':{'id':run['id']}}).json()
        assert 'errors' not in result
        return result['data']['run']
    assert access(parent)['labelingAccess']=={'allowed':False,'reason':'Activate this scorecard version before continuing labeling.'}
    assert access(added)['labelingAccess']=={'allowed':True,'reason':None}
    assert access(recorded)['labelingAccess']=={'allowed':False,'reason':'Recorded runs are read-only.'}
    assert access(parent)['scorecardId']==access(added)['scorecardId']==added['config']['scorecard_id']
    assert access(recorded)['scorecardId'] is None
    store.activate_scorecard_version(added['config']['scorecard_id'],1)
    assert access(parent)['labelingAccess']=={'allowed':True,'reason':None}
    assert access(parent)['config']==parent['config']
    assert store.jobs(parent['id'])==[]
    assert store.all_events(parent['id'])==[]


def test_a_live_timeline_renders_before_all_declared_classes_have_been_observed(tmp_path):
    store=WebStore(tmp_path/'db')
    definition=store.save_classifier('topic','Topic',{'question':'Choose',
        'classes':[{'label':'yes','role':'positive'},{'label':'no','role':'negative'}]})
    run=store.create_run('Partial history','live',{'classifiers':[definition]})
    client=TestClient(create_app(store))
    url=f"/runs/{run['id']}/timeline?classifier_id=topic"
    assert client.get(url).status_code==200
    store.append_event(run['id'],'one-prediction',{'event_id':1,'kind':'prediction',
        'classifier_id':'topic','target_id':'paper','label':'yes','probabilities':{'yes':.6,'no':.4}})
    assert client.get(url).status_code==200


def test_matched_preflight_query_is_read_only_and_does_not_start_service_work(tmp_path):
    from types import SimpleNamespace
    store=WebStore(tmp_path/'db');calls=[]
    store.matched_run_preflight=lambda before,after,limit=200: calls.append((before,after,limit)) or {'sample_count':4,'request_upper_bound':8}
    client=TestClient(create_app(store,service=SimpleNamespace(allow_live=False)))
    result=client.post('/graphql',json={'query':'{matchedRunPreflight(beforeRunId:"before",afterRunId:"after",limit:200)}'}).json()
    assert 'errors' not in result
    assert result['data']['matchedRunPreflight']['request_upper_bound']==8
    assert calls==[('before','after',200)]
    assert store.runs()==[]


def test_matched_execution_requires_explicit_confirmation_before_service_work(tmp_path):
    from types import SimpleNamespace
    calls=[];store=WebStore(tmp_path/'db')
    def create(*args,**kwargs):
        calls.append((args,kwargs));return store.create_run('Comparison','recorded',{'input_mode':'comparison'})
    service=SimpleNamespace(allow_live=True,create_comparison=create)
    client=TestClient(create_app(store,service=service))
    query='mutation($confirmed:Boolean!){createMatchedComparison(name:"Comparison",beforeRunId:"a",afterRunId:"b",approvedFingerprint:"plan",maxRequests:8,confirmed:$confirmed){id}}'
    result=client.post('/graphql',json={'query':query,'variables':{'confirmed':False}}).json()
    assert 'explicit' in result['errors'][0]['message']
    assert calls==[]
    result=client.post('/graphql',json={'query':query,'variables':{'confirmed':True}}).json()
    assert 'errors' not in result
    assert calls==[(('Comparison','a','b','plan'),{'max_requests':8,'limit':200})]


def test_comparison_resume_requires_confirmation_and_preserves_request_identity(tmp_path):
    from types import SimpleNamespace
    calls=[]
    def resume(*args,**kwargs):
        calls.append((args,kwargs));return {'id':'queued','kind':'matched-evaluate','status':'pending','result':None}
    service=SimpleNamespace(allow_live=True,resume_comparison=resume)
    client=TestClient(create_app(WebStore(tmp_path/'db'),service=service))
    query='mutation($confirmed:Boolean!){resumeMatchedComparison(runId:"comparison",requestId:"unique-resume",maxRequests:9,retryFailed:true,confirmed:$confirmed){id status}}'
    rejected=client.post('/graphql',json={'query':query,'variables':{'confirmed':False}}).json()
    assert 'explicit' in rejected['errors'][0]['message']
    assert calls==[]
    accepted=client.post('/graphql',json={'query':query,'variables':{'confirmed':True}}).json()
    assert 'errors' not in accepted
    assert accepted['data']['resumeMatchedComparison']['status']=='pending'
    assert calls==[(('comparison','unique-resume'),{'max_requests':9,'retry_failed':True})]


def test_classifier_history_query_exposes_old_configurations_without_creating_model_work(tmp_path):
    store=WebStore(tmp_path/'db')
    store.save_classifier('a','Old',{'question':'Old question','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_classifier('a','New',{'question':'New question','classes':[{'label':'no'},{'label':'yes'}]})
    result=TestClient(create_app(store)).post('/graphql',json={'query':'{classifierVersions(classifierId:"a")}'}).json()
    assert 'errors' not in result
    assert [row['config']['question'] for row in result['data']['classifierVersions']]==['Old question','New question']
    assert store.runs()==[]


def test_scorecard_definition_api_preserves_pinned_members_without_starting_runs(tmp_path):
    store=WebStore(tmp_path/'db')
    store.save_classifier('a','A',{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    client=TestClient(create_app(store))
    result=client.post('/graphql',json={'query':'mutation($refs:JSON!){saveScorecardDefinition(identifier:"card",name:"Card",classifiers:$refs,settings:{})}', 'variables':{'refs':[{'id':'a','revision':1}]}}).json()
    assert 'errors' not in result
    store.save_classifier('a','A new',{'question':'Choose again','classes':[{'label':'yes'},{'label':'no'}]})
    data=client.post('/graphql',json={'query':'{scorecardDefinitions scorecardDefinitionVersions(scorecardId:"card") scorecardClassifiers(scorecardId:"card") runs{id}}'}).json()['data']
    assert data['scorecardDefinitions'][0]['revision']==2
    assert data['scorecardDefinitionVersions'][0]['classifiers'][0]['revision']==1
    assert data['scorecardClassifiers'][0]['revision']==2
    assert data['runs']==[]
    old=client.post('/graphql',json={'query':'{scorecardClassifiers(scorecardId:"card",revision:1)}'}).json()
    assert 'errors' not in old
    assert old['data']['scorecardClassifiers'][0]['name']=='A'
    assert old['data']['scorecardClassifiers'][0]['config']['question']=='Choose'
    assert store.scorecard_definition('card')['revision']==2
    comparison=client.post('/graphql',json={'query':'{scorecardDefinitionComparison(scorecardId:"card",beforeRevision:2,afterRevision:1)}'}).json()
    assert 'errors' not in comparison
    result=comparison['data']['scorecardDefinitionComparison']
    assert result['before']['revision']==2
    assert result['after']['revision']==1
    assert result['changes']['members'][0]['before']['revision']==2
    assert result['classifiers'][0]['before']['name']=='A new'
    assert result['classifiers'][0]['after']['config']['question']=='Choose'
    assert store.scorecard_definition('card')['revision']==2
    assert store.runs()==[]


def test_graphql_restores_an_old_definition_without_replacing_history_or_starting_model_work(tmp_path):
    store=WebStore(tmp_path/'db')
    for cid in ('a','b'):
        store.save_classifier(cid,cid,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    original=store.save_scorecard_definition('card','Original',[{'id':'a','revision':1},{'id':'b','revision':1}],{})
    newer=store.save_scorecard_definition('card','Newer',[{'id':'b','revision':1}],{})
    client=TestClient(create_app(store))
    query='mutation($revision:Int!){activateScorecardDefinition(scorecardId:"card",revision:$revision)}'
    restored=client.post('/graphql',json={'query':query,'variables':{'revision':1}}).json()
    assert 'errors' not in restored
    assert restored['data']['activateScorecardDefinition']==original
    result=client.post('/graphql',json={'query':'{scorecardDefinitions scorecardDefinitionVersions(scorecardId:"card") scorecardClassifiers(scorecardId:"card") runs{id}}'}).json()
    assert 'errors' not in result
    assert result['data']['scorecardDefinitions']==[original]
    assert result['data']['scorecardDefinitionVersions']==[original,newer]
    assert [row['id'] for row in result['data']['scorecardClassifiers']]==['a','b']
    assert result['data']['runs']==[]
    invalid=client.post('/graphql',json={'query':query,'variables':{'revision':999}}).json()
    assert invalid['errors']
    assert store.scorecard_definition('card')==original
    assert store.scorecard_definition_versions('card')==[original,newer]
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0]==0


def test_replay_creation_requires_confirmation_before_any_service_call(tmp_path):
    from types import SimpleNamespace
    calls=[]
    service=SimpleNamespace(allow_live=True,create_replay=lambda *args:calls.append(args))
    client=TestClient(create_app(WebStore(tmp_path/'db'),service=service))
    response=client.post('/graphql',json={'query':'mutation {createReplay(name:"Replay",sourceRunId:"source",config:{},confirmed:false){id}}'}).json()
    assert response.get('errors')
    assert calls==[]


def test_paid_operating_limits_require_confirmation_and_are_logged_through_the_api(tmp_path):
    from types import SimpleNamespace
    store=WebStore(tmp_path/'web.sqlite')
    run=store.create_run('Live','live',{'max_requests':500,'max_optimizer_calls':10})
    client=TestClient(create_app(store,service=SimpleNamespace(allow_live=True)))
    query='mutation($id:ID!,$confirmed:Boolean!){updateRunLimits(runId:$id,maxRequests:10000,maxOptimizerCalls:1000,confirmed:$confirmed){id config}}'
    rejected=client.post('/graphql',json={'query':query,'variables':{'id':run['id'],'confirmed':False}}).json()
    assert rejected.get('errors')
    assert store.run(run['id'])['config']['max_optimizer_calls']==10
    accepted=client.post('/graphql',json={'query':query,'variables':{'id':run['id'],'confirmed':True}}).json()
    assert not accepted.get('errors')
    assert accepted['data']['updateRunLimits']['config']['max_optimizer_calls']==1000
    assert store.all_events(run['id'])[-1]['payload']['kind']=='operating-limits-changed'


def test_catalog_api_labels_one_item_for_two_classifiers_without_model_calls(tmp_path):
    with TestClient(create_app(WebStore(tmp_path/'web.sqlite'))) as client:
        def post(query,variables=None):
            result=client.post('/graphql',json={'query':query,'variables':variables or {}}).json()
            assert 'errors' not in result, result
            return result['data']
        for identifier,classes in [('library',['include','exclude']),('topic',['science','sport','business'])]:
            post('mutation($id:String!,$config:JSON!){saveClassifier(identifier:$id,name:$id,config:$config)}',
                 {'id':identifier,'config':{'question':'Classify this item','classes':[{'label':label} for label in classes]}})
        post('mutation {saveItemList(identifier:"papers",name:"Papers")}')
        post('mutation($items:JSON!){upsertListItems(listId:"papers",items:$items)}',
             {'items':[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Paper'}}]})
        for identifier,label in [('library','include'),('topic','science')]:
            post('mutation($id:ID!,$label:String!){labelItem(classifierId:$id,classifierRevision:1,listId:"papers",itemId:"one",itemRevision:1,label:$label,comment:"Reason",requestId:$label)}',
                 {'id':identifier,'label':label})
        result=post('{classifiers itemLists itemLabels(listId:"papers",itemId:"one",itemRevision:1)}')
        assert len(result['classifiers'])==2 and len(result['itemLabels'])==2
        assert result['itemLists'][0]['count']==1


def test_graphql_creates_run_ingests_full_exchange_and_queries_history(tmp_path):
    app = create_app(WebStore(tmp_path / 'web.sqlite'))
    with TestClient(app) as client:
        result = client.post('/graphql', json={'query':'mutation { createRun(name:"Recorded", mode:"recorded") { id name } }'}).json()
        assert 'errors' not in result
        run = result['data']['createRun']['id']
        event = {'kind':'optimizer-request','messages':[{'role':'user','content':'Full explanation'}]}
        result = client.post('/graphql',json={'query':'mutation($run:ID!,$events:[TraceInput!]!){ingestEvents(runId:$run,events:$events){sequence payload}}',
            'variables':{'run':run,'events':[{'sourceId':'one','payload':event}]}}).json()
        assert result['data']['ingestEvents'][0]['payload'] == event
        history = client.post('/graphql',json={'query':'query($run:ID!){events(runId:$run){payload} runs{id name}}','variables':{'run':run}}).json()
        assert history['data']['events'][0]['payload'] == event


def test_live_run_needs_server_authority_and_user_confirmation(tmp_path):
    with TestClient(create_app(WebStore(tmp_path / 'web.sqlite'))) as client:
        result = client.post('/graphql',json={'query':'mutation {createRun(name:"Paid",mode:"live",confirmed:true){id}}'}).json()
        assert result['errors']
        assert client.post('/graphql',json={'query':'{runs{id}}'}).json()['data']['runs'] == []


def test_cross_origin_requests_and_unauthenticated_remote_access_cannot_mutate(tmp_path):
    with TestClient(create_app(WebStore(tmp_path / 'web.sqlite'), token='private')) as client:
        query = {'query':'mutation {createRun(name:"A",mode:"recorded"){id}}'}
        assert client.post('/graphql',json=query).status_code == 401
        assert client.post('/graphql',json=query,headers={'Authorization':'Bearer private','Origin':'https://evil.example'}).status_code == 403
        assert client.post('/graphql',json=query,headers={'Authorization':'Bearer private'}).status_code == 200


def test_subscription_replays_committed_events_after_a_cursor(tmp_path):
    from .web_api import schema
    store = WebStore(tmp_path / 'web.sqlite')
    run = store.create_run('A','recorded',{})
    first = store.append_event(run['id'],'one',{'kind':'prediction'})
    second = store.append_event(run['id'],'two',{'kind':'human-feedback'})
    async def read():
        stream = await schema.subscribe('subscription($id:ID!,$after:Int!){runEvents(runId:$id,after:$after){sequence payload}}',
            variable_values={'id':run['id'],'after':first['sequence']},context_value={'store':store})
        result = await anext(stream)
        await stream.aclose()
        return result
    result = asyncio.run(read())
    assert result.data['runEvents']['sequence'] == second['sequence']


def test_websocket_transport_delivers_a_persisted_event_and_rejects_cross_origin(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    run = store.create_run('A','recorded',{})
    store.append_event(run['id'],'one',{'kind':'optimizer-response','content':'Actual reply'})
    with TestClient(create_app(store)) as client:
        with client.websocket_connect('/graphql',subprotocols=['graphql-transport-ws']) as websocket:
            websocket.send_json({'type':'connection_init'})
            assert websocket.receive_json()['type'] == 'connection_ack'
            websocket.send_json({'id':'one','type':'subscribe','payload':{
                'query':'subscription($id:ID!){runEvents(runId:$id){payload}}','variables':{'id':run['id']}}})
            event = websocket.receive_json()
            assert event['payload']['data']['runEvents']['payload']['content'] == 'Actual reply'
            websocket.send_json({'type':'complete','id':'one'})


def test_run_activity_counts_are_isolated_and_timeline_uses_embedded_chrome(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    run = store.create_run('Replay','recorded',{})
    other = store.create_run('Other','recorded',{})
    for index,kind in enumerate(('cycle-started','prediction','human-feedback','optimizer-request')):
        store.append_event(run['id'],str(index),{'kind':kind,'event_id':index+1,'label':'include',
            'created_at':'2026-10-06T21:00:00Z','cycle_id':'one','cycle_number':1,'feedback':{'final_answer_value':'include'}})
    store.append_event(other['id'],'one',{'kind':'prediction'})
    with TestClient(create_app(store)) as client:
        result = client.post('/graphql',json={'query':'query($id:ID!){run(runId:$id){counts}}','variables':{'id':run['id']}}).json()
        assert result['data']['run']['counts'] == {'cycles':1,'predictions':1,'labels':1,'optimizations':1}
        html = client.get(f"/runs/{run['id']}/timeline").text
        assert '<script id="workspace-options" type="application/json">{"embedded": true}</script>' in html
def test_explicit_local_network_access_does_not_require_a_password(tmp_path):
    with TestClient(create_app(WebStore(tmp_path / 'lan.sqlite'), allow_unauthenticated_lan=True),
                    client=('192.168.0.25', 12345)) as client:
        response = client.post('/graphql', json={'query': '{runs{id}}'})
        assert response.status_code == 200
        assert response.json()['data']['runs'] == []
        assert client.post('/graphql', headers={'Origin': 'http://unrelated.example'},
                           json={'query': '{runs{id}}'}).status_code == 403


def test_workspace_shell_versions_its_static_bundles_by_content(tmp_path):
    with TestClient(create_app(WebStore(tmp_path/'web.sqlite'))) as client:
        response=client.get('/')
    assert response.status_code == 200
    assert '/assets/web.css?v=' in response.text
    assert '/assets/web.js?v=' in response.text
