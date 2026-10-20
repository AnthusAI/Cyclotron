"""Run inside official Lambda Python 3.13 with network disabled and read-only code."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from decision_flywheel.hosted_runtime import HostedConfig,HostedRuntime
from decision_flywheel import cloud_entry


def forbidden(*args,**kwargs):raise AssertionError('artifact reached SQLite, local persistence, credentials or network')
sqlite3.connect=forbidden;Path.mkdir=forbidden
config=HostedConfig(issuer='https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Synthetic0',client_id='synthetic0',
    authorization_origin='https://synthetic.auth.example.invalid',public_origin='https://console.example.test',
    account='111111111111',region='us-east-1',books_bucket='synthetic-books',auth_bucket='synthetic-auth',
    session_key_arn='arn:aws:kms:us-east-1:111111111111:key/11111111-1111-4111-8111-111111111111',
    queue_url='https://sqs.us-east-1.amazonaws.com/111111111111/synthetic')
class NoNetwork:
    def __getattr__(self,name):return forbidden
cloud_entry._cached_runtime=HostedRuntime(config,s3=NoNetwork(),cipher=NoNetwork(),central=forbidden,verifier=NoNetwork(),send=forbidden)
event={'version':'2.0','routeKey':'$default','rawPath':'/api/session','rawQueryString':'','headers':{'host':'synthetic.execute-api.us-east-1.amazonaws.com'},
    'requestContext':{'accountId':'111111111111','apiId':'synthetic','domainName':'synthetic.execute-api.us-east-1.amazonaws.com','stage':'$default',
        'http':{'method':'GET','path':'/api/session','protocol':'HTTP/1.1','sourceIp':'127.0.0.1','userAgent':'synthetic'}},'isBase64Encoded':False}
response=cloud_entry.bff_handler(event,SimpleNamespace())
assert response['statusCode']==200 and json.loads(response['body'])['authenticated'] is False
assert response['headers']['cache-control']=='no-store'
event['rawPath']='/graphql';event['requestContext']['http']['path']='/graphql';event['requestContext']['http']['method']='POST'
event['body']='{"query":"{runs{id}}"}';event['headers']['content-type']='application/json'
assert cloud_entry.bff_handler(event,SimpleNamespace())['statusCode']==401
print(json.dumps({'lambdaPython313Artifact':'PASS','apiGatewayV2':True,'loggedOutDenied':True,'network':'disabled','sqlite':'forbidden'}))

# Full packaged synthetic path: Gateway -> S3 BFF job -> isolated worker -> event page.
import asyncio
import base64
from dataclasses import replace
import hashlib
import io
import secrets
import time
from uuid import uuid4
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from decision_flywheel.account_sessions import CognitoTokenVerifier
from decision_flywheel.batched_classification import BatchedAnswers
from decision_flywheel.models import DecisionResult
from decision_flywheel.optimizer_agent import DisabledOptimizer
from decision_flywheel.prospectus_accounts import AccountAccessDenied

class StorageError(Exception):
    def __init__(self,code):self.response={'Error':{'Code':code}}
class SyntheticS3:
    def __init__(self):self.objects={}
    def get_object(self,**args):
        assert args['ExpectedBucketOwner']=='111111111111'
        value=self.objects.get((args['Bucket'],args['Key']))
        if value is None:raise StorageError('NoSuchKey')
        body,etag=value
        return {'Body':io.BytesIO(body),'ContentLength':len(body),'ETag':etag}
    def put_object(self,**args):
        assert args['ExpectedBucketOwner']=='111111111111'
        assert args.get('IfNoneMatch')=='*' or isinstance(args.get('IfMatch'),str)
        key=(args['Bucket'],args['Key']);old=self.objects.get(key)
        if args.get('IfNoneMatch')=='*' and old is not None:raise StorageError('PreconditionFailed')
        if args.get('IfMatch') is not None and (old is None or old[1]!=args['IfMatch']):raise StorageError('PreconditionFailed')
        body=args['Body'];etag='"'+hashlib.sha256(body).hexdigest()+'"'
        self.objects[key]=(body,etag);return {'ETag':etag}
class SyntheticCipher:
    def __init__(self):self.key=AESGCM.generate_key(256)
    def encrypt(self,kind,reference,body):
        nonce=secrets.token_bytes(12)
        return {'nonce':base64.b64encode(nonce).decode(),'body':base64.b64encode(AESGCM(self.key).encrypt(nonce,body,(kind+reference).encode())).decode()}
    def decrypt(self,kind,reference,value):
        return AESGCM(self.key).decrypt(base64.b64decode(value['nonce']),base64.b64decode(value['body']),(kind+reference).encode())
class SyntheticModel:
    model_identity='packaged-synthetic';calls=0
    async def classify_many(self,configs,target,training,**kwargs):
        self.calls+=1
        return BatchedAnswers({cid:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})} for cid in configs},self.model_identity,{},1)
private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))|{'kid':'synthetic','alg':'RS256','use':'sig'}
verifier=CognitoTokenVerifier(config.issuer,config.client_id,jwks={'keys':[jwk]})
now=int(time.time());person,account=str(uuid4()),str(uuid4())
access=jwt.encode({'iss':config.issuer,'client_id':config.client_id,'sub':'synthetic-person','token_use':'access','iat':now,'exp':now+1800,
    'auth_time':now,'origin_jti':'packaged-synthetic-lineage'},private,algorithm='RS256',headers={'kid':'synthetic'})
summary={'id':account,'name':'Packaged synthetic account','role':'owner','entitlement':{'accountId':account,'product':'cyclotron','status':'active','capabilities':['console:read','console:write']}}
central_calls=[]
def central(role):
    def call(method,path,human,body=None,key=None,**kwargs):
        verifier.access(human);central_calls.append((role,method,path))
        if path=='/v1/identities/me':return {'person':{'id':person,'createdAt':'synthetic'}}
        if role=='bff' and path=='/v1/accounts':return {'person':{'id':person},'accounts':[summary]}
        if path==f'/v1/accounts/{account}/authorize':return {'allowed':True,'accountId':account,'personId':person,'product':'cyclotron','role':'owner','operation':body['operation'],'revision':1}
        raise AccountAccessDenied('synthetic scope denied')
    return call
s3=SyntheticS3();cipher=SyntheticCipher();model=SyntheticModel();queue=[]
config=replace(config,allow_live=True)
factory=lambda settings:(model,DisabledOptimizer())
bff=HostedRuntime(config,s3=s3,cipher=cipher,central=central('bff'),verifier=verifier,send=queue.append,model_factory=factory)
worker=HostedRuntime(config,s3=s3,cipher=cipher,central=central('worker'),verifier=verifier,send=forbidden,model_factory=factory)
store=bff.console(account,initialize=True)
store.save_classifier('topic','Synthetic',{'question':'Synthetic?','classes':[{'label':'yes'},{'label':'no'}]})
store.save_item_list('papers','Synthetic papers');store.upsert_list_items('papers',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Synthetic content'}}])
cookie,session=bff.sessions.create(access,verifier.access(access));session.person_id=person;bff.sessions.save(session)
cloud_entry._cached_runtime=bff;cloud_entry._cached_adapter=None

def request(path,method='GET',body=None,query=''):
    raw={'version':'2.0','routeKey':'$default','rawPath':path,'rawQueryString':query,
        'headers':{'host':'synthetic.execute-api.us-east-1.amazonaws.com','origin':config.public_origin,'content-type':'application/json'},
        'cookies':['__Host-cyclotron-session='+cookie],
        'requestContext':{'domainName':'synthetic.execute-api.us-east-1.amazonaws.com','stage':'$default',
            'http':{'method':method,'path':path,'protocol':'HTTP/1.1','sourceIp':'127.0.0.1','userAgent':'synthetic'}},
        'body':json.dumps(body) if body else None,'isBase64Encoded':False}
    result=cloud_entry.bff_handler(raw,SimpleNamespace())
    assert result['statusCode']==200,result['statusCode']
    data=json.loads(result['body']);assert not data.get('errors'),'GraphQL operation failed'
    return data
run=request('/graphql','POST',{'query':'mutation {createRun(name:"Packaged run",mode:"live",confirmed:true,config:{classifier_ids:["topic"],item_list_id:"papers",max_requests:1,optimize_every:100}){id}}'},'account='+account)['data']['createRun']['id']
command='mutation($run:ID!,$request:String!,$kind:String!,$payload:JSON!){submitCommand(runId:$run,requestId:$request,kind:$kind,payload:$payload){id}}'
def submit(kind,payload):
    request('/graphql','POST',{'query':command,'variables':{'run':run,'request':str(uuid4()),'kind':kind,'payload':payload}},'account='+account)
    result=asyncio.run(worker.worker().process(queue[-1]));assert result['status']=='completed'
submit('prepare',{})
shown=bff.console(account).current_item(run)
submit('label',{'item_id':'one','presentation_id':shown['prediction']['presentation_id'],'labels':[{'classifier_id':'topic','label':'yes','comment':'Exact packaged human rationale'}]})
assert bff.console(account).item_labels('papers','one',1)[0]['comment']=='Exact packaged human rationale'
assert request(f'/api/runs/{run}/events',query='account='+account)['events']
assert model.calls==1
assert all((method,path)==('GET','/v1/identities/me') or method=='POST' and path.endswith('/authorize') for role,method,path in central_calls if role=='worker')
assert access.encode() not in b''.join(content for (bucket,key),(content,etag) in s3.objects.items() if bucket==config.auth_bucket)
print(json.dumps({'packagedGatewayS3QueueWorkerEvents':'PASS','modelCalls':model.calls,'rationalePreserved':True,'liveCalls':False,'sqlite':'forbidden'}))
