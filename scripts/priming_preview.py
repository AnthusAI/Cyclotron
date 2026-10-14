"""Local synthetic preview, with no provider or cloud requests."""
import base64
import json
from pathlib import Path
import uvicorn
from decision_flywheel.priming import PrimingService
from decision_flywheel.web_api import create_app
from decision_flywheel.web_store import WebStore

store=WebStore(Path('var/priming-preview.sqlite3'))
schema={'type':'object','properties':{'title':{'type':'string'},'abstract':{'type':'string'}},'required':['title','abstract'],'additionalProperties':False}
for identifier,name,labels in [('editorial','Editorial relevance',['publish','not publish']),('scope','Threat-intelligence scope',['in scope','out of scope'])]:
    store.save_classifier(identifier,name,{'question':name+'?', 'input_field':'abstract','classes':[{'label':value} for value in labels],'item_schema':schema})
store.save_item_list('synthetic-examples','Synthetic preview examples')
service=PrimingService(store)
row={'id':'synthetic-paper-001','occurred_at':'2026-10-14','values':{'title':'Synthetic example — research-paper priming','abstract':'This is synthetic test content for inspecting the optional import workflow. Ryan’s real examples have not been supplied.'},'labels':{'editorial':{'label':'publish','rationale':'Synthetic test rationale; not an editorial decision.'},'scope':{'label':'in scope','rationale':'Synthetic test rationale; not a scope decision.'}},'provenance':{'source':'synthetic local fixture'}}
service.start_file('synthetic-preview','synthetic-examples',[{'id':'editorial','revision':1},{'id':'scope','revision':1}],'json',base64.b64encode(json.dumps([row]).encode()).decode())
uvicorn.run(create_app(store,priming=service),host='127.0.0.1',port=8796)
