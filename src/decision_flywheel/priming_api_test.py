"""Same authenticated GraphQL application boundary for embedded and standalone hosts."""
import base64
import json
from fastapi.testclient import TestClient
from .web_api import create_app
from .web_store import WebStore


def test_file_import_api_requires_human_acceptance_and_supports_cancel_and_retry(tmp_path):
    store=WebStore(tmp_path/'db');store.save_item_list('examples','Examples')
    store.save_classifier('editor','Editorial',{'question':'Publish?','classes':[{'label':'publish'},{'label':'not publish'}]})
    client=TestClient(create_app(store))
    refs=[{'id':'editor','revision':1}]
    query='query($refs:JSON!){primingContract(listId:"examples",classifiers:$refs) primingTemplate(listId:"examples",classifiers:$refs,format:"csv")}'
    result=client.post('/graphql',json={'query':query,'variables':{'refs':refs}}).json()
    assert 'errors' not in result and 'editor.rationale' in result['data']['primingTemplate']['content']
    row={'id':'synthetic','occurred_at':'2026-10-14','values':{'text':'Synthetic content'},'labels':{'editor':{'label':'publish','rationale':'Synthetic reason'}}}
    def command(action,payload):
        return client.post('/graphql',json={'query':'mutation($action:String!,$payload:JSON!){primingCommand(action:$action,payload:$payload)}','variables':{'action':action,'payload':payload}}).json()
    payload={'identifier':'draft','list_id':'examples','refs':refs,'kind':'json','content':base64.b64encode(json.dumps([row]).encode()).decode()}
    draft=command('file',payload)['data']['primingCommand'];assert draft['status']=='preview'
    assert store.list_items('examples')==[]
    acceptance={'identifier':'draft','version':draft['version'],'labels':None}
    assert command('accept',acceptance)['data']['primingCommand']['status']=='accepted'
    assert command('accept',acceptance)['data']['primingCommand']['status']=='accepted'
    assert len(store.list_items('examples'))==1
    cancelled=command('file',{**payload,'identifier':'cancel'})['data']['primingCommand']
    assert command('cancel',{'identifier':'cancel'})['data']['primingCommand']['status']=='cancelled'
    assert 'errors' in command('accept',{**acceptance,'identifier':'cancel','version':cancelled['version']})


def test_priming_api_preserves_auth_origin_and_credential_controls(tmp_path):
    store=WebStore(tmp_path/'db');client=TestClient(create_app(store,token='test-secret',redact=('provider-secret',)))
    payload={'query':'{primingDraft(identifier:"missing")}' }
    assert client.post('/graphql',json=payload).status_code==401
    headers={'authorization':'Bearer test-secret'}
    assert client.post('/graphql',json=payload,headers={**headers,'origin':'https://evil.invalid'}).status_code==403
    mutation={'query':'mutation{primingCommand(action:"file",payload:{identifier:"provider-secret"})}'}
    response=client.post('/graphql',json=mutation,headers=headers).json()
    assert 'credential' in response['errors'][0]['message']
    assert 'provider-secret' not in json.dumps(response)
