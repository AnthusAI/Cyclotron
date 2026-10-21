"""Normal Lambda/BFF composition; all storage and identity adapters are explicit.

No credentials, memberships or SQLite database are configured on import. Cloud
entry points remain inert until reviewed outputs populate HostedConfig.
"""
import asyncio
from copy import copy
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from .account_sessions import OAuthConfig,CognitoTokenVerifier
from .account_routes import HostedAccounts
from .batch_storage import StorageScope
from .google_identity import GoogleIdentityClient
from .prospectus_accounts import AccountsClient,AccountWorkspace,SigV4Transport
from .queued_account_authorization import QueuedAccountAuthorization
from .s3_journal import S3Journal,encode,uuid
from .storage_payloads import S3Payloads
from .s3_sessions import S3OAuthSessions,KMSSessionCipher
from .s3_console_storage import S3ConsoleStorage
from .s3_workspace_storage import S3WorkspaceStores
from .hosted_console_service import HostedConsoleService
from .hosted_command_worker import HostedCommandWorker,AuthorizedModel,CommandEventSink
from .cyclotron_runtime import CyclotronRuntime
from .web_api import create_app


@dataclass(frozen=True)
class HostedConfig:
    issuer:str
    client_id:str
    authorization_origin:str
    public_origin:str
    account:str
    region:str
    books_bucket:str
    auth_bucket:str
    session_key_arn:str
    queue_url:str
    central_endpoint:str
    allow_live:bool=False
    allow_insecure_loopback:bool=False
    max_bytes:int=4194304
    max_records:int=10000
    max_commits:int=1000000

    def __post_init__(self):
        OAuthConfig(self.issuer,self.client_id,self.authorization_origin,self.public_origin,allow_insecure_loopback=self.allow_insecure_loopback)
        if not re.fullmatch(r'[0-9]{12}',self.account) or not re.fullmatch(r'[a-z]+-[a-z]+-[0-9]',self.region):raise ValueError('fixed AWS account and region required')
        if (self.books_bucket==self.auth_bucket or any(not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',bucket) for bucket in (self.books_bucket,self.auth_bucket))):raise ValueError('distinct fixed books and auth buckets required')
        if not re.fullmatch(r'arn:aws:kms:'+re.escape(self.region)+':'+self.account+r':key/[a-f0-9-]{36}',self.session_key_arn):raise ValueError('exact regional session key ARN required')
        if not re.fullmatch(r'https://sqs\.'+re.escape(self.region)+r'\.amazonaws\.com/'+self.account+r'/[A-Za-z0-9_-]+',self.queue_url):raise ValueError('fixed account queue URL required')
        if not self.central_endpoint.startswith('https://'):raise ValueError('https central endpoint required')
        if type(self.allow_live) is not bool or any(type(v) is not int or v<1 for v in (self.max_bytes,self.max_records,self.max_commits)):raise ValueError('explicit bounded host configuration required')

    @classmethod
    def from_environment(cls,environment):
        names=('issuer','client_id','authorization_origin','public_origin','account','region','books_bucket','auth_bucket','session_key_arn','queue_url','central_endpoint')
        try:values={name:environment['CYCLOTRON_'+name.upper()] for name in names}
        except KeyError:raise ValueError('reviewed cloud configuration is incomplete') from None
        enabled = environment.get('CYCLOTRON_ALLOW_LIVE', 'false')
        if enabled not in ('true', 'false'):
            raise ValueError('CYCLOTRON_ALLOW_LIVE must be explicitly true or false')
        values['allow_live'] = enabled == 'true'
        return cls(**values)


class HostedRuntime:
    def __init__(self,config,*,s3,kms=None,cipher=None,sqs=None,send=None,central=None,verifier=None,model_factory=None,open_cyclotron=None,publication_targets=None):
        self.config,self.s3=config,s3
        self.open_cyclotron=open_cyclotron
        if config.allow_live and model_factory is None:raise ValueError('explicit authorized provider factory required for live execution')
        self.model_factory=model_factory
        self.publication_targets = publication_targets
        if publication_targets is not None:
            from .hosted_publications import configured_targets
            configured_targets(self)
        self.central=central or SigV4Transport(config.central_endpoint,config.region)
        self.accounts=AccountsClient(self.central);self.identities=GoogleIdentityClient(self.central)
        self.verifier=verifier or CognitoTokenVerifier(config.issuer,config.client_id)
        self.sessions=S3OAuthSessions(self.verifier,journal_for=self._auth_journal,
            cipher=cipher or KMSSessionCipher(kms,config.session_key_arn,application='cyclotron-console'))
        self.authorization=QueuedAccountAuthorization(self.sessions,self.verifier,self.identities,self.accounts,self.binding)
        if send is None:
            if sqs is None:raise ValueError('explicit queue client required')
            def send(message):return sqs.send_message(QueueUrl=config.queue_url,MessageBody=json.dumps(message,separators=(',',':')))
        self.send=send

    def _journal(self,bucket,scope,domain,name):
        payloads=S3Payloads(self.s3,bucket,self.config.account,scope,max_bytes=self.config.max_bytes)
        return S3Journal(payloads,domain,name,max_bytes=self.config.max_bytes,max_commits=self.config.max_commits,create_only_initialization=True)
    def _auth_journal(self,kind,reference):
        return self._journal(self.config.auth_bucket,StorageScope('console-auth','sessions'),'oauth',kind+'-'+reference)
    def journal(self,account_id,domain,name):
        uuid(account_id)
        return self._journal(self.config.books_bucket,StorageScope(account_id,'console'),domain,name)
    def console(self,account_id,*,initialize=False):
        store=S3ConsoleStorage(self.journal(account_id,'console','catalog'),max_records=self.config.max_records)
        if initialize:store.initialize()
        return store
    def binding(self,account_id,run_id):
        uuid(run_id)
        run=self.console(account_id).run(run_id)
        return sha256(encode({'account':account_id,'workspace':'console','run':run_id,'config':run['config']})).hexdigest()
    def workspace(self,account_id):
        from .priming import PrimingService
        store=self.console(account_id,initialize=True)
        # Pure file priming is supported. No transformer/provider is inferred.
        priming=PrimingService(store,storage=store,client=_NoTransformerClient())
        service=HostedConsoleService(account_id,store,self.authorization,self.send,allow_live=self.config.allow_live,
            command_policy=lambda run_id,kind:self._publication_command_policy(account_id,run_id,kind),
            data_command=self._data_command if self.publication_targets is not None else None)
        held_out=None
        if self.open_cyclotron is not None:
            from .held_out_service import HeldOutService
            def open_account_cyclotron(cyclotron_id,max_requests=None):
                return self.open_cyclotron(account_id,cyclotron_id,max_requests)
            held_out=HeldOutService(open_account_cyclotron,allow_live=self.config.allow_live)
        return AccountWorkspace(store,service,account_id,priming=priming,held_out=held_out)
    def app(self):
        oauth=OAuthConfig(self.config.issuer,self.config.client_id,self.config.authorization_origin,self.config.public_origin,allow_insecure_loopback=self.config.allow_insecure_loopback)
        self.gateway=HostedAccounts(oauth,self.accounts,self.workspace,verifier=self.verifier,sessions=self.sessions,google_identity=self.identities)
        app=create_app(None,account_access=self.gateway.access,public_origin=self.config.public_origin,event_transport="poll")
        self.gateway.install(app)
        from .hosted_publication_bootstrap import install_bootstrap_route
        install_bootstrap_route(app, self)
        if self.publication_targets is not None:
            from .hosted_publications import install_publication_routes, configured_targets
            configured_targets(self)
            install_publication_routes(app, self)
        return app
    def _runtime(self,account_id,view,guard,check):
        owned={}
        def check_owned():
            check()
            if 'stores' in owned:owned['stores'].refresh()
        async def guarded():await asyncio.to_thread(check_owned)
        def storage(run_id):
            bundle=S3WorkspaceStores(self.journal(account_id,'workspace',run_id),
                journal_for=lambda domain,name:self.journal(account_id,domain,name),max_records=self.config.max_records,execution_guard=check)
            owned['stores']=bundle
            return bundle
        def model(config):
            provider,optimizer=self.model_factory(config)
            if getattr(optimizer,'enabled',True):
                from .s3_optimizer_transport import require_durable_optimizer
                require_durable_optimizer(optimizer)
                optimizer=copy(optimizer);optimizer.complete=optimizer.complete.with_execution_guard(check_owned)
            return AuthorizedModel(provider,guarded),optimizer
        return CyclotronRuntime(view,None,model_factory=model,sink_factory=lambda rid:CommandEventSink(view,rid,check_owned),
            storage_factory=storage,execution_guard=guarded)
    def _data_command(self, kind, payload):
        from .hosted_publications import is_priming_command
        return is_priming_command(kind, payload)

    def _publication_command_policy(self, account_id, run_id, kind):
        if self.publication_targets is None:
            return
        from .hosted_publications import configured_targets
        reserved = any(t.account_id == account_id and t.run_id == run_id for t in configured_targets(self))
        if (reserved and kind != 'publication') or (kind == 'publication' and not reserved):
            raise ValueError('configured publication runs require their scoped prediction path; feedback is not activated')

    def worker(self):
        # The worker's own runtime credentials/central transport must be the
        # reviewed execution principal; the central pair is never caller input.
        class Worker:
            async def process(inner,message):
                def publication(view, guard, check, actor):
                    from .hosted_publications import PublicationCommandRuntime
                    return PublicationCommandRuntime(self, view, guard, check, actor)
                return await HostedCommandWorker(self.console,self.authorization,
                    lambda view,guard,check:self._runtime(message['accountId'],view,guard,check),allow_live=self.config.allow_live,
                    publication_runtime_for_command=publication if self.publication_targets is not None else None,
                    command_policy=lambda actor,kind:self._publication_command_policy(actor.account_id,actor.run_id,kind),
                    data_command=self._data_command if self.publication_targets is not None else None).process(message)
        return Worker()


class _NoTransformerClient:
    def stream(self,*args,**kwargs):raise ValueError('hosted transformer requires separate scoped configuration')
