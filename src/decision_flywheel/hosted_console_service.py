"""Account-scoped BFF service; persist original actor before queue publication."""
import asyncio
import hmac
from .prospectus_accounts import VerifiedAccountSession,AccountAccessDenied,ProspectusUnavailable
from .journal import uuid,encode


class HostedConsoleService:
    articles=()
    def __init__(self,account_id,store,authorization,send,*,allow_live=False,redact=()):
        uuid(account_id)
        self.account_id,self.store,self.authorization,self.send=account_id,store,authorization,send
        self.allow_live,self.redact=allow_live,tuple(redact)

    async def submit_command(self,verified,run_id,request_id,kind,payload):
        if not self.allow_live:raise ValueError('live commands require separate operator authority')
        uuid(run_id)
        if not isinstance(verified,VerifiedAccountSession) or verified.account_id!=self.account_id or not verified.session_ref:
            raise AccountAccessDenied('verified original request session required')
        session=await asyncio.to_thread(self.authorization.sessions.get_ref,verified.session_ref)
        if (session is None or session.person_id!=verified.person_id
                or not hmac.compare_digest(session.access_token,verified.access_token)):
            raise AccountAccessDenied('request session changed')
        content=encode(payload)
        if (len(content)>65536 or any(secret and secret.encode() in content for secret in (*self.redact,session.access_token))
                or not isinstance(request_id,str) or not 1<=len(request_id)<=256):raise ValueError('bounded credential-free command required')
        binding=await asyncio.to_thread(self.authorization.binding_for_run,self.account_id,run_id)
        actor=await self.authorization.capture(session,self.account_id,run_id,binding)
        await self.authorization.guard(actor)()
        job=await asyncio.to_thread(self.store.enqueue_authorized_command,run_id,request_id,kind,payload,actor)
        try:
            await asyncio.to_thread(self.send,{'accountId':self.account_id,'runId':run_id,'jobId':job['id']})
        except Exception:raise ProspectusUnavailable('queue outcome unavailable; retry the same request identity') from None
        return job

    def _runtime(self):
        from .cyclotron_runtime import CyclotronRuntime
        def unavailable(*args,**kwargs):raise ValueError('BFF cannot execute a provider or open a worker store')
        return CyclotronRuntime(self.store,None,model_factory=unavailable,sink_factory=unavailable,storage_factory=unavailable)
    def create_run(self,name,config):
        if not self.allow_live:raise ValueError('live run requires separate operator authority')
        return self._runtime().create_run(name,config)
    def create_replay(self,name,source_run_id,config):
        if not self.allow_live:raise ValueError('live replay requires separate operator authority')
        return self._runtime().create_replay(name,source_run_id,config)
