"""One normal cloud composition binds accounts, encrypted sessions, S3 and queue."""
import pytest
from fastapi.testclient import TestClient
from .account_sessions_test import identity,ISSUER,CLIENT
from .s3_journal_test import ConditionalS3
from .s3_sessions_test import Cipher


def test_cloud_app_is_logged_out_by_default_and_never_opens_sqlite_or_a_local_store(identity,monkeypatch):
    from .hosted_runtime import HostedConfig,HostedRuntime
    verifier,token=identity
    def forbidden(*args,**kwargs):raise AssertionError('cloud composition opened SQLite or local files')
    monkeypatch.setattr('sqlite3.connect',forbidden);monkeypatch.setattr('pathlib.Path.mkdir',forbidden)
    def central(method,path,human,body=None,key=None,**kwargs):raise AssertionError('logged-out access reached central')
    config=HostedConfig(issuer=ISSUER,client_id=CLIENT,authorization_origin='https://synthetic.auth.example.invalid',
        public_origin='https://console.example.test',account='111111111111',region='us-east-1',
        books_bucket='synthetic-books',auth_bucket='synthetic-auth',session_key_arn='arn:aws:kms:us-east-1:111111111111:key/11111111-1111-4111-8111-111111111111',
        queue_url='https://sqs.us-east-1.amazonaws.com/111111111111/synthetic',central_endpoint='https://central.example.test/api')
    runtime=HostedRuntime(config,s3=ConditionalS3(),cipher=Cipher(),central=central,verifier=verifier,send=lambda message:None)
    with TestClient(runtime.app(),base_url=config.public_origin) as client:
        assert client.get('/api/session').json()['authenticated'] is False
        assert client.post('/graphql',json={'query':'{runs{id}}'}).status_code==401
    assert runtime.config.allow_live is False


def test_cloud_config_rejects_incomplete_scopes_credentials_and_unreviewed_live_execution():
    from .hosted_runtime import HostedConfig
    with pytest.raises((TypeError,ValueError)):HostedConfig()
    with pytest.raises(ValueError):HostedConfig.from_environment({})


def complete_environment():
    return {'CYCLOTRON_ISSUER':ISSUER,'CYCLOTRON_CLIENT_ID':CLIENT,'CYCLOTRON_AUTHORIZATION_ORIGIN':'https://synthetic.auth.example.invalid',
        'CYCLOTRON_PUBLIC_ORIGIN':'https://console.example.test','CYCLOTRON_ACCOUNT':'111111111111','CYCLOTRON_REGION':'eu-west-2',
        'CYCLOTRON_BOOKS_BUCKET':'synthetic-books','CYCLOTRON_AUTH_BUCKET':'synthetic-auth',
        'CYCLOTRON_SESSION_KEY_ARN':'arn:aws:kms:eu-west-2:111111111111:key/11111111-1111-4111-8111-111111111111',
        'CYCLOTRON_QUEUE_URL':'https://sqs.eu-west-2.amazonaws.com/111111111111/synthetic','CYCLOTRON_CENTRAL_ENDPOINT':'https://central.example.test/api'}


def test_cloud_config_fails_closed_without_region_or_central_endpoint():
    from .hosted_runtime import HostedConfig
    assert HostedConfig.from_environment(complete_environment()).central_endpoint=='https://central.example.test/api'
    for missing in ('CYCLOTRON_REGION','CYCLOTRON_CENTRAL_ENDPOINT'):
        environment=complete_environment();del environment[missing]
        with pytest.raises(ValueError):HostedConfig.from_environment(environment)
    insecure=complete_environment();insecure['CYCLOTRON_CENTRAL_ENDPOINT']='http://central.example.test/api'
    with pytest.raises(ValueError):HostedConfig.from_environment(insecure)


def test_verifier_configuration_is_optional_and_read_from_the_environment():
    from .hosted_runtime import HostedConfig
    unset=HostedConfig.from_environment(complete_environment())
    assert (unset.lab_verifier_role_arn,unset.accounts_api_url)==('','')
    environment=complete_environment()|{'CYCLOTRON_LAB_VERIFIER_ROLE_ARN':'arn:aws:iam::111111111111:role/cyclotron-lab-verifier','CYCLOTRON_ACCOUNTS_API_URL':'https://central.example.test/api/'}
    configured=HostedConfig.from_environment(environment)
    assert configured.lab_verifier_role_arn.endswith(':role/cyclotron-lab-verifier') and configured.accounts_api_url=='https://central.example.test/api/'


def test_central_transport_is_signed_for_the_configured_region(identity,monkeypatch):
    from .hosted_runtime import HostedConfig,HostedRuntime
    verifier,token=identity
    signed_for=[]
    monkeypatch.setattr('decision_flywheel.hosted_runtime.SigV4Transport',lambda endpoint,region:signed_for.append((endpoint,region)))
    config=HostedConfig.from_environment(complete_environment())
    HostedRuntime(config,s3=ConditionalS3(),cipher=Cipher(),verifier=verifier,send=lambda message:None)
    assert signed_for==[('https://central.example.test/api','eu-west-2')]


def test_actual_hosted_graphql_queue_worker_and_event_page_keep_original_tenant_and_rationale(identity,monkeypatch):
    import asyncio
    from dataclasses import replace
    from uuid import uuid4
    from .hosted_runtime import HostedConfig,HostedRuntime
    from .s3_console_storage_test import seed
    from .s3_workspace_storage_test import Model
    from .optimizer_agent import DisabledOptimizer
    from .prospectus_accounts import AccountAccessDenied
    verifier,token=identity;person_id,account_id=str(uuid4()),str(uuid4());calls=[];sent=[];s3=ConditionalS3();cipher=Cipher();model=Model()
    summary={'id':account_id,'name':'Synthetic tenant','role':'owner','entitlement':{'accountId':account_id,'product':'cyclotron','status':'active','capabilities':['console:read','console:write']}}
    def central(role):
        def call(method,path,human,body=None,key=None,**kwargs):
            verifier.access(human);calls.append((role,method,path))
            if path=='/v1/identities/me':return {'person':{'id':person_id,'createdAt':'synthetic'}}
            if path=='/v1/accounts' and role=='bff':return {'person':{'id':person_id},'accounts':[summary]}
            if path==f'/v1/accounts/{account_id}/authorize':return {'allowed':True,'accountId':account_id,'personId':person_id,'product':'cyclotron','role':'owner','operation':body['operation'],'revision':1}
            raise AccountAccessDenied('not authorized')
        return call
    config=HostedConfig(issuer=ISSUER,client_id=CLIENT,authorization_origin='https://synthetic.auth.example.invalid',
        public_origin='https://console.example.test',account='111111111111',region='us-east-1',books_bucket='synthetic-books',auth_bucket='synthetic-auth',
        session_key_arn='arn:aws:kms:us-east-1:111111111111:key/11111111-1111-4111-8111-111111111111',queue_url='https://sqs.us-east-1.amazonaws.com/111111111111/synthetic',central_endpoint='https://central.example.test/api',allow_live=True)
    factory=lambda config:(model,DisabledOptimizer())
    bff=HostedRuntime(config,s3=s3,cipher=cipher,central=central('bff'),verifier=verifier,send=sent.append,model_factory=factory)
    worker=HostedRuntime(config,s3=s3,cipher=cipher,central=central('worker'),verifier=verifier,send=sent.append,model_factory=factory)
    def forbidden(*args,**kwargs):raise AssertionError('hosted request touched SQLite or a local store')
    monkeypatch.setattr('sqlite3.connect',forbidden);monkeypatch.setattr('pathlib.Path.mkdir',forbidden)
    seed(bff.console(account_id,initialize=True))
    access=token();cookie,session=bff.sessions.create(access,verifier.access(access));session.person_id=person_id;bff.sessions.save(session)
    with TestClient(bff.app(),base_url=config.public_origin) as client:
        client.cookies.set('__Host-cyclotron-session',cookie)
        def graphql(query,variables=None,account=account_id):
            result=client.post('/graphql',params={'account':account},json={'query':query,'variables':variables or {}},headers={'Origin':config.public_origin})
            assert not result.json().get('errors'),result.json()
            return result.json()['data']
        run=graphql('mutation {createRun(name:"Synthetic live",mode:"live",confirmed:true,config:{classifier_ids:["topic"],item_list_id:"papers",max_requests:1,optimize_every:100}){id}}')['createRun']['id']
        command='mutation($run:ID!,$request:String!,$kind:String!,$payload:JSON!){submitCommand(runId:$run,requestId:$request,kind:$kind,payload:$payload){id status}}'
        graphql(command,{'run':run,'request':str(uuid4()),'kind':'prepare','payload':{}})
        assert asyncio.run(worker.worker().process(sent[-1]))['status']=='completed'
        shown=graphql('query($run:ID!){currentItem(runId:$run)}',{'run':run})['currentItem']
        page=client.get(f'/api/runs/{run}/events',params={'account':account_id})
        assert page.status_code==200 and page.json()['events']
        graphql(command,{'run':run,'request':str(uuid4()),'kind':'label','payload':{'item_id':'one','presentation_id':shown['prediction']['presentation_id'],'labels':[{'classifier_id':'topic','label':'science','comment':'Exact human rationale 雪'}]}})
        assert asyncio.run(worker.worker().process(sent[-1]))['status']=='completed'
        assert bff.console(account_id).item_labels('papers','one',1)[0]['comment']=='Exact human rationale 雪'
        assert model.calls==1 and all(method=='GET' and path=='/v1/identities/me' or method=='POST' and path.endswith('/authorize') for role,method,path in calls if role=='worker')
        denied=client.get(f'/api/runs/{run}/events',params={'account':str(uuid4())})
        assert denied.status_code==403
        assert access not in str(sent)


def test_the_journal_byte_budget_defaults_to_four_mebibytes_and_can_be_raised_within_a_hard_ceiling():
    from .hosted_runtime import HostedConfig
    assert HostedConfig.from_environment(complete_environment()).max_bytes==4194304
    assert HostedConfig.from_environment(complete_environment()|{'CYCLOTRON_MAX_BYTES':'16777216'}).max_bytes==16777216
    for bad in ('4MiB','0','1048575','67108865','-1',' 16777216'):
        with pytest.raises(ValueError):HostedConfig.from_environment(complete_environment()|{'CYCLOTRON_MAX_BYTES':bad})
