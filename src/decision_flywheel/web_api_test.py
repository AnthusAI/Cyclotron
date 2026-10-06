"""Exercise GraphQL and cursor subscriptions without network or model keys."""
import asyncio
import pytest
pytest.importorskip('strawberry')
from fastapi.testclient import TestClient
from .web_api import create_app
from .web_store import WebStore


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
