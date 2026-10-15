"""Loopback-only synthetic S3 console preview; never deploy this fixture.

No Google, AWS, model-provider, email or payment calls. Private fixture keys are
created in memory. The explicit preview login is not a verified Google login.
"""
import asyncio
import json
from pathlib import Path
import sqlite3
from threading import Thread
import time
from uuid import uuid4
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.responses import HTMLResponse,RedirectResponse
import uvicorn
from decision_flywheel.account_sessions import CognitoTokenVerifier
from decision_flywheel.hosted_runtime import HostedConfig,HostedRuntime
from decision_flywheel.optimizer_agent import DisabledOptimizer
from decision_flywheel.prospectus_accounts import AccountAccessDenied
from decision_flywheel.s3_journal_test import ConditionalS3
from decision_flywheel.s3_sessions_test import Cipher
from decision_flywheel.batched_classification import BatchedAnswers
from decision_flywheel.models import DecisionResult

ORIGIN='http://127.0.0.1:8916'
issuer='https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Synthetic0';client='synthetic0'
private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))|{'kid':'preview','alg':'RS256','use':'sig'}
verifier=CognitoTokenVerifier(issuer,client,jwks={'keys':[jwk]})
person_id,alpha,beta=str(uuid4()),str(uuid4()),str(uuid4())
summaries=[{'id':value,'name':name,'role':role,'entitlement':{'accountId':value,'product':'cyclotron','status':'active','capabilities':['console:read']+(['console:write'] if role=='owner' else [])}} for value,name,role in [(alpha,'Synthetic Alpha','owner'),(beta,'Synthetic Beta','viewer')]]

def central(role):
    def call(method,path,access,body=None,key=None,**kwargs):
        verifier.access(access)
        if path=='/v1/identities/me':return {'person':{'id':person_id,'createdAt':'synthetic'}}
        if path=='/v1/accounts' and method=='GET':return {'person':{'id':person_id},'accounts':summaries}
        for account in summaries:
            if path==f"/v1/accounts/{account['id']}/authorize" and body['operation'] in account['entitlement']['capabilities']:
                return {'allowed':True,'accountId':account['id'],'personId':person_id,'product':'cyclotron','role':account['role'],'operation':body['operation'],'revision':1}
        raise AccountAccessDenied('synthetic account permission refused')
    return call
class Model:
    model_identity='synthetic-preview'
    async def classify_many(self,configs,target,training,**kwargs):
        return BatchedAnswers({cid:{'decision':DecisionResult('include',{'include':.8,'exclude':.2})} for cid in configs},self.model_identity,{},1)
config=HostedConfig(issuer=issuer,client_id=client,authorization_origin='https://synthetic.auth.example.invalid',public_origin=ORIGIN,
    account='111111111111',region='us-east-1',books_bucket='synthetic-books',auth_bucket='synthetic-auth',
    session_key_arn='arn:aws:kms:us-east-1:111111111111:key/11111111-1111-4111-8111-111111111111',
    queue_url='https://sqs.us-east-1.amazonaws.com/111111111111/synthetic',allow_live=True,allow_insecure_loopback=True)
s3=ConditionalS3();cipher=Cipher();factory=lambda settings:(Model(),DisabledOptimizer())
worker=HostedRuntime(config,s3=s3,cipher=cipher,central=central('worker'),verifier=verifier,send=lambda message:None,model_factory=factory)
def send(message):
    def invocation():
        try:asyncio.run(worker.worker().process(message))
        except Exception as error:print('Synthetic worker stopped:',type(error).__name__,flush=True)
    Thread(target=invocation,daemon=True).start()
bff=HostedRuntime(config,s3=s3,cipher=cipher,central=central('bff'),verifier=verifier,send=send,model_factory=factory)
for account in summaries:
    store=bff.console(account['id'],initialize=True)
    store.save_classifier('relevance','Relevance',{'question':'Include this synthetic research item?','classes':[{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]})
    store.save_item_list('papers','Synthetic papers')
    store.upsert_list_items('papers',[{'id':'synthetic-paper','occurred_at':'2026-10-15','values':{'text':'Synthetic research content for local offline review.'}}])
    if account['role']=='owner':
        bff.workspace(account['id']).service.create_run('Synthetic S3 labeling',{'classifier_ids':['relevance'],'item_list_id':'papers','max_requests':5,'optimize_every':100})
    else:store.create_run('Beta read-only workspace','recorded',{})
app=bff.app()
# Serve the actual packaged UI, adding an unmistakable fixture notice only here.
app.router.routes=[route for route in app.router.routes if getattr(route,'path',None)!='/']
@app.get('/',response_class=HTMLResponse)
async def index():
    page=(Path(__file__).resolve().parents[1]/'trace-ui/console-dist/index.html').read_text()
    notice='<aside style="position:relative;z-index:999;background:#fff3b0;padding:12px;font:14px sans-serif">Synthetic local preview — no Google, AWS or model calls. <a href="/__synthetic/session">Use synthetic local session</a></aside>'
    return page.replace('<body>','<body>'+notice)
@app.get('/__synthetic/session')
async def preview_session():
    now=int(time.time())
    token=jwt.encode({'iss':issuer,'sub':'synthetic-preview-user','client_id':client,'token_use':'access','iat':now,'exp':now+1800},private,algorithm='RS256',headers={'kid':'preview'})
    identifier,session=bff.sessions.create(token,verifier.access(token));session.person_id=person_id;bff.sessions.save(session)
    result=RedirectResponse('/',status_code=303);result.set_cookie('cyclotron-test-session',identifier,httponly=True,samesite='lax',path='/')
    return result

def no_sql(*args,**kwargs):raise AssertionError('synthetic hosted preview attempted SQLite')
sqlite3.connect=no_sql
if __name__=='__main__':uvicorn.run(app,host='127.0.0.1',port=8916,access_log=False)
