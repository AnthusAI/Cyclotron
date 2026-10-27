"""Existing workspace API with explicit native or account-scoped storage."""
import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import hmac
from ipaddress import ip_address
import json
import time
from pathlib import Path
from typing import AsyncGenerator, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import strawberry
from strawberry.extensions import MaskErrors, SchemaExtension
from strawberry.fastapi import GraphQLRouter
from strawberry.scalars import JSON

from .trace_artifact import render_trace


LOOPBACK_HOSTS = ('127.0.0.1', '::1')
DEFAULT_MAXIMUM_EVENT_SUBSCRIBERS = 64
PRIMING_PAYLOAD_KEYS = {
    'file': {'identifier', 'list_id', 'refs', 'kind', 'content'},
    'transform': {'identifier', 'list_id', 'refs', 'transformer_id', 'inputs'},
    'poll': {'identifier'},
    'feedback': {'identifier', 'version', 'request_id', 'reason', 'inputs'},
    'cancel': {'identifier'},
    'accept': {'identifier', 'version', 'labels'},
}


def refuse_credentials(info, subject, *values):
    """Refuse caller input that contains any configured secret before it is persisted."""
    encoded = json.dumps(values, default=str)
    if any(secret and secret in encoded for secret in info.context.get('redact', ())):
        raise ValueError(f'{subject} contains a credential')


def require_known_payload_keys(payload, allowed_keys, subject):
    if not isinstance(payload, Mapping):
        raise ValueError(f'{subject} payload must be an object')
    if set(payload) - set(allowed_keys):
        raise ValueError(f'{subject} payload has an unknown field')


def error_message_is_safe_to_expose(error) -> bool:
    """Only deliberate ValueError and account-denial messages reach clients; everything else is masked."""
    original = getattr(error, 'original_error', None)
    if isinstance(original, ValueError):
        return True
    try:
        from .prospectus_accounts import AccountAccessDenied, ProspectusUnavailable
    except ImportError:
        return False
    return isinstance(original, (AccountAccessDenied, ProspectusUnavailable))


class EventSubscriberLimit:
    def __init__(self, maximum):
        self.maximum, self.active = maximum, 0

    def acquire(self):
        if self.active >= self.maximum:
            raise ValueError('too many live event subscribers; try again later')
        self.active += 1

    def release(self):
        self.active -= 1


@strawberry.type
class Run:
    id: strawberry.ID
    name: str
    mode: str
    status: str
    created_at: str
    config: JSON

    @strawberry.field
    async def cyclotron_id(self,info:strawberry.Info)->strawberry.ID|None:
        identifier=await asyncio.to_thread(info.context['store'].run_cyclotron_id,str(self.id))
        return strawberry.ID(identifier) if identifier is not None else None

    @strawberry.field
    async def labeling_access(self,info:strawberry.Info)->JSON:
        return await asyncio.to_thread(info.context['store'].labeling_access,str(self.id))

    @strawberry.field
    async def event_cursor(self,info:strawberry.Info)->int:
        return await asyncio.to_thread(info.context['store'].event_cursor,str(self.id))

    @strawberry.field
    async def summary(self, info: strawberry.Info) -> JSON | None:
        return await asyncio.to_thread(info.context['store'].summary,str(self.id))

    @strawberry.field
    async def counts(self, info: strawberry.Info) -> JSON:
        return await asyncio.to_thread(info.context['store'].counts,str(self.id))


@strawberry.type
class TraceEvent:
    sequence: int
    source_id: str
    payload: JSON


@strawberry.input
class TraceInput:
    source_id: str
    payload: JSON


@strawberry.type
class Job:
    id: strawberry.ID
    kind: str
    status: str
    result: JSON | None


@strawberry.type
class Capabilities:
    live_enabled: bool
    item_count: int


def event_type(row):
    return TraceEvent(sequence=row['sequence'],source_id=row['source_id'],payload=row['payload'])


async def call(info, method, *args, **kwargs):
    return await asyncio.to_thread(getattr(info.context['store'], method), *args, **kwargs)


@strawberry.type
class Query:
    @strawberry.field
    async def priming_contract(self, info: strawberry.Info, list_id: strawberry.ID, classifiers: JSON) -> JSON:
        return await asyncio.to_thread(info.context['priming_service']().contract, str(list_id), classifiers)

    @strawberry.field
    async def priming_template(self, info: strawberry.Info, list_id: strawberry.ID, classifiers: JSON, format: str) -> JSON:
        return await asyncio.to_thread(info.context['priming_service']().template, str(list_id), classifiers, format)

    @strawberry.field
    async def priming_draft(self, info: strawberry.Info, identifier: str) -> JSON:
        return await asyncio.to_thread(info.context['priming_service']().get, identifier)

    @strawberry.field
    async def held_out_overview(self, info: strawberry.Info, cyclotron_id: strawberry.ID,
                                classifier: str | None = None) -> JSON:
        return await info.context['held_out_service']().overview(str(cyclotron_id), classifier)


    @strawberry.field
    async def cyclotrons(self,info:strawberry.Info)->JSON:
        return await call(info,'cyclotrons')

    @strawberry.field
    async def cyclotron_definitions(self,info:strawberry.Info)->JSON:
        return await call(info,'cyclotron_definitions')

    @strawberry.field
    async def cyclotron_definition_versions(self,info:strawberry.Info,cyclotron_id:strawberry.ID)->JSON:
        return await call(info,'cyclotron_definition_versions',str(cyclotron_id))

    @strawberry.field
    async def cyclotron_definition_comparison(self,info:strawberry.Info,cyclotron_id:strawberry.ID,before_revision:int,after_revision:int)->JSON:
        return await call(info,'cyclotron_definition_comparison',str(cyclotron_id),before_revision,after_revision)

    @strawberry.field
    async def cyclotron_classifiers(self,info:strawberry.Info,cyclotron_id:strawberry.ID,revision:int|None=None)->JSON:
        definition=await call(info,'cyclotron_definition',str(cyclotron_id),revision)
        return [await call(info,'classifier',ref['id'],ref['revision']) for ref in definition['classifiers']]

    @strawberry.field
    async def cyclotron_versions(self,info:strawberry.Info,cyclotron_id:strawberry.ID)->JSON:
        return await call(info,'cyclotron_versions',str(cyclotron_id))

    @strawberry.field
    async def cyclotron_checkpoints(self,info:strawberry.Info,run_id:strawberry.ID)->JSON:
        return await call(info,'cyclotron_checkpoints',str(run_id))

    @strawberry.field
    async def matched_evaluation_target(self,info:strawberry.Info,run_id:strawberry.ID,event_id:int)->JSON|None:
        return await call(info,'matched_evaluation_target',str(run_id),event_id)

    @strawberry.field
    async def matched_run_preflight(self,info:strawberry.Info,before_run_id:strawberry.ID,after_run_id:strawberry.ID,limit:int=200)->JSON:
        return await call(info,'matched_run_preflight',str(before_run_id),str(after_run_id),limit=limit)

    @strawberry.field
    async def item_results(self, info: strawberry.Info, list_id: strawberry.ID, item_id: str, item_revision: int) -> JSON:
        return await call(info, 'item_results', str(list_id), item_id, item_revision)

    @strawberry.field
    async def item_labels(self, info: strawberry.Info, list_id: strawberry.ID, item_id: str, item_revision: int) -> JSON:
        return await call(info, 'item_labels', str(list_id), item_id, item_revision)

    @strawberry.field
    async def classifiers(self, info: strawberry.Info) -> JSON:
        return await call(info, 'classifiers')

    @strawberry.field
    async def classifier_versions(self, info: strawberry.Info, classifier_id: strawberry.ID) -> JSON:
        return await call(info, 'classifier_versions', str(classifier_id))

    @strawberry.field
    async def item_lists(self, info: strawberry.Info) -> JSON:
        return await call(info, 'item_lists')

    @strawberry.field
    async def list_items(self, info: strawberry.Info, list_id: strawberry.ID, after: int = 0, limit: int = 200) -> JSON:
        return await call(info, 'list_items', str(list_id), after=after, limit=limit)

    @strawberry.field
    async def capabilities(self, info: strawberry.Info) -> Capabilities:
        service = info.context.get('service')
        live_enabled = info.context['allow_live'] if 'allow_live' in info.context else bool(service and service.allow_live)
        return Capabilities(live_enabled=live_enabled,item_count=len(service.articles) if service else 0)

    @strawberry.field
    async def runs(self, info: strawberry.Info) -> list[Run]:
        return [Run(**r) for r in await call(info, 'runs')]

    @strawberry.field
    async def run(self, info: strawberry.Info, run_id: strawberry.ID) -> Run:
        return Run(**await call(info, 'run', str(run_id)))

    @strawberry.field
    async def events(self, info: strawberry.Info, run_id: strawberry.ID, after: int = 0, limit: int = 1000) -> list[TraceEvent]:
        return [event_type(r) for r in await call(info, 'events', str(run_id), after=after, limit=limit)]

    @strawberry.field
    async def current_item(self, info: strawberry.Info, run_id: strawberry.ID) -> JSON | None:
        return await call(info, 'current_item', str(run_id))

    @strawberry.field
    async def jobs(self, info: strawberry.Info, run_id: strawberry.ID) -> list[Job]:
        return [Job(id=r['id'],kind=r['kind'],status=r['status'],result=r['result']) for r in await call(info, 'jobs', str(run_id))]


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def priming_command(self, info: strawberry.Info, action: str, payload: JSON) -> JSON:
        priming = info.context['priming_service']()
        payload = priming.safe(payload)
        actions = {'file': priming.start_file, 'transform': priming.start_transform,
                   'poll': priming.poll, 'feedback': priming.feedback, 'cancel': priming.cancel, 'accept': priming.accept}
        if action not in actions or not isinstance(payload, dict):
            raise ValueError('unknown priming command')
        require_known_payload_keys(payload, PRIMING_PAYLOAD_KEYS[action], 'priming command')
        return await asyncio.to_thread(actions[action], **payload)

    @strawberry.mutation
    async def held_out_command(self, info: strawberry.Info, action: str, payload: JSON) -> JSON:
        return await info.context['held_out_service']().command(action, payload)


    @strawberry.mutation
    async def extend_cyclotron(self,info:strawberry.Info,parent_run_id:strawberry.ID,classifier_ids:list[str],name:str,confirmed:bool=False)->Run:
        service=info.context.get('service')
        if not confirmed or service is None or not service.allow_live:
            raise ValueError('cyclotron replay requires explicit live authority')
        refuse_credentials(info,'cyclotron',name)
        run=await call(info,'extend_cyclotron',str(parent_run_id),classifier_ids,name=name)
        await call(info,'append_event',run['id'],'cyclotron-created',{'kind':'cyclotron-version-created','cyclotron_id':run['config']['cyclotron_id'],'revision':run['config']['cyclotron_revision'],'parent_run_id':str(parent_run_id),'learning_policy':'fresh-replay','backfill_count':run['config']['backfill_count']})
        return Run(**run)

    @strawberry.mutation
    async def promote_supporting_question(self,info:strawberry.Info,parent_run_id:strawberry.ID,source_classifier_id:str,
                                          question:str,classifier_id:str,classifier_name:str,name:str,
                                          positive_label:str|None=None,confirmed:bool=False)->Run:
        service=info.context.get('service')
        if not confirmed or service is None or not service.allow_live:
            raise ValueError('cyclotron replay requires explicit live authority')
        refuse_credentials(info,'supporting question',name,question,classifier_name)
        run=await call(info,'promote_supporting_question',str(parent_run_id),source_classifier_id,question,
                       classifier_id=classifier_id,classifier_name=classifier_name,name=name,positive_label=positive_label)
        await call(info,'append_event',run['id'],'cyclotron-created',{'kind':'cyclotron-version-created','cyclotron_id':run['config']['cyclotron_id'],'revision':run['config']['cyclotron_revision'],'parent_run_id':str(parent_run_id),'learning_policy':'fresh-replay','backfill_count':run['config']['backfill_count'],
            'promoted_supporting_question':{'source_classifier_id':source_classifier_id,'question':question,'classifier_id':classifier_id}})
        return Run(**run)

    @strawberry.mutation
    async def activate_cyclotron_version(self,info:strawberry.Info,cyclotron_id:strawberry.ID,revision:int)->Run:
        run=await call(info,'activate_cyclotron_version',str(cyclotron_id),revision)
        await call(info,'append_event',run['id'],f'activate:{uuid4()}',{'kind':'cyclotron-version-activated','cyclotron_id':str(cyclotron_id),'revision':revision})
        return Run(**run)

    @strawberry.mutation
    async def label_item(self, info: strawberry.Info, classifier_id: strawberry.ID, classifier_revision: int,
                         list_id: strawberry.ID, item_id: str, item_revision: int, label: str, comment: str, request_id: str) -> JSON:
        refuse_credentials(info, 'feedback', comment)
        return await call(info,'label_item',str(classifier_id),classifier_revision,str(list_id),item_id,item_revision,label,comment,request_id)

    @strawberry.mutation
    async def save_classifier(self, info: strawberry.Info, identifier: str, name: str, config: JSON) -> JSON:
        refuse_credentials(info, 'configuration', name, config)
        return await call(info, 'save_classifier', identifier, name, config)

    @strawberry.mutation
    async def save_cyclotron_definition(self,info:strawberry.Info,identifier:str,name:str,classifiers:JSON,settings:JSON)->JSON:
        refuse_credentials(info,'configuration',name,classifiers,settings)
        return await call(info,'save_cyclotron_definition',identifier,name,classifiers,settings)

    @strawberry.mutation
    async def activate_cyclotron_definition(self,info:strawberry.Info,cyclotron_id:strawberry.ID,revision:int)->JSON:
        return await call(info,'activate_cyclotron_definition',str(cyclotron_id),revision)

    @strawberry.mutation
    async def save_item_list(self, info: strawberry.Info, identifier: str, name: str) -> JSON:
        refuse_credentials(info, 'item list', name)
        return await call(info, 'save_item_list', identifier, name)

    @strawberry.mutation
    async def upsert_list_items(self, info: strawberry.Info, list_id: strawberry.ID, items: JSON) -> JSON:
        refuse_credentials(info, 'items', items)
        return await call(info, 'upsert_list_items', str(list_id), items)

    @strawberry.mutation
    async def complete_run(self, info: strawberry.Info, run_id: strawberry.ID, summary: JSON) -> Run:
        refuse_credentials(info, 'summary', summary)
        return Run(**await call(info,'complete_run',str(run_id),summary))

    @strawberry.mutation
    async def create_run(self, info: strawberry.Info, name: str, mode: str = 'recorded', config: JSON | None = None,
                         confirmed: bool = False) -> Run:
        service = info.context.get('service')
        refuse_credentials(info, 'run configuration', name, config or {})
        if mode == 'live':
            if not confirmed or service is None or not service.allow_live:
                raise ValueError('live run requires server authority and explicit confirmation')
            result = await asyncio.to_thread(service.create_run, name, config or {})
        else:
            result = await call(info, 'create_run', name, mode, config or {})
        return Run(**result)

    @strawberry.mutation
    async def resume_matched_comparison(self,info:strawberry.Info,run_id:strawberry.ID,request_id:str,
                                       max_requests:int,retry_failed:bool=False,confirmed:bool=False)->Job:
        service=info.context.get('service')
        if not confirmed or service is None or not service.allow_live:
            raise ValueError('matched comparison resume requires explicit live-call authority')
        row=await asyncio.to_thread(service.resume_comparison,str(run_id),request_id,
                                   max_requests=max_requests,retry_failed=retry_failed)
        return Job(id=row['id'],kind=row['kind'],status=row['status'],result=row['result'])

    @strawberry.mutation
    async def create_matched_comparison(self,info:strawberry.Info,name:str,before_run_id:strawberry.ID,
                                       after_run_id:strawberry.ID,approved_fingerprint:str,max_requests:int,
                                       limit:int=200,confirmed:bool=False,request_id:str|None=None)->Run:
        service=info.context.get('service')
        if not confirmed or service is None or not service.allow_live:
            raise ValueError('matched comparison requires explicit live-call authority')
        identity={} if request_id is None else {'request_id':request_id}
        return Run(**await asyncio.to_thread(service.create_comparison,name,str(before_run_id),str(after_run_id),
                                            approved_fingerprint,max_requests=max_requests,limit=limit,**identity))

    @strawberry.mutation
    async def create_replay(self,info:strawberry.Info,name:str,source_run_id:strawberry.ID,config:JSON,confirmed:bool=False)->Run:
        service=info.context.get('service')
        if not confirmed or service is None or not service.allow_live:
            raise ValueError('replay execution requires explicit live-call authority')
        refuse_credentials(info,'configuration',name,config)
        return Run(**await asyncio.to_thread(service.create_replay,name,str(source_run_id),config))

    @strawberry.mutation
    async def ingest_events(self, info: strawberry.Info, run_id: strawberry.ID, events: list[TraceInput]) -> list[TraceEvent]:
        data = [(e.source_id,e.payload) for e in events]
        refuse_credentials(info, 'trace', data)
        return [event_type(r) for r in await call(info,'append_batch',str(run_id),data)]

    @strawberry.mutation
    async def update_run_limits(self,info:strawberry.Info,run_id:strawberry.ID,max_requests:int,max_optimizer_calls:int,confirmed:bool=False)->Run:
        service=info.context.get('service')
        if not confirmed or service is None or not service.allow_live:
            raise ValueError('changing paid limits requires explicit confirmation and a live server')
        before=await call(info,'run',str(run_id))
        result=await call(info,'update_run_limits',str(run_id),max_requests,max_optimizer_calls)
        await call(info,'append_event',str(run_id),f'limits:{uuid4()}',
            {'kind':'operating-limits-changed','before':{key:before['config'].get(key) for key in ('max_requests','max_optimizer_calls')},
             'after':{'max_requests':max_requests,'max_optimizer_calls':max_optimizer_calls}})
        return Run(**result)

    @strawberry.mutation
    async def resume_feedback_command(self, info: strawberry.Info, run_id: strawberry.ID,
                                      job_id: strawberry.ID) -> Job:
        if not info.context.get('service'):
            raise ValueError('labeling worker is not available')
        row=await call(info,'resume_feedback_command',str(run_id),str(job_id))
        return Job(id=row['id'],kind=row['kind'],status=row['status'],result=row['result'])

    @strawberry.mutation
    async def submit_command(self, info: strawberry.Info, run_id: strawberry.ID, request_id: str,
                             kind: str, payload: JSON | None = None) -> Job:
        if not info.context.get('service'):
            raise ValueError('labeling worker is not available')
        refuse_credentials(info, 'command', request_id, kind, payload)
        if info.context.get('account_access'):
            service=info.context['service']
            if not hasattr(service,'submit_command'):raise ValueError('hosted command composition unavailable')
            row=await service.submit_command(info.context.get('verified_session'),str(run_id),request_id,kind,payload or {})
        else:
            row = await call(info,'command',str(run_id),request_id,kind,payload or {})
        return Job(id=row['id'],kind=row['kind'],status=row['status'],result=row['result'])


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def run_events(self, info: strawberry.Info, run_id: strawberry.ID, after: int = 0) -> AsyncGenerator[TraceEvent, None]:
        cursor = after
        subscribers = info.context['event_subscribers']
        subscribers.acquire()
        try:
            while True:
                rows = await call(info, 'events', str(run_id), after=cursor)
                for row in rows:
                    cursor = row['sequence']
                    yield event_type(row)
                await asyncio.sleep(.2)
        finally:
            subscribers.release()


def agreed_live_authority(*services):
    """The one paid-call flag every configured service must share; disagreement is a configuration error."""
    flags = {bool(service.allow_live) for service in services if service is not None and hasattr(service, 'allow_live')}
    if len(flags) > 1:
        raise ValueError('live-call authority must agree across the worker, held-out and priming services')
    return flags.pop() if flags else False


class AccountAuthorization(SchemaExtension):
    async def on_execute(self):
        context = self.execution_context.context
        access = context.get('account_access')
        if access:
            operation = 'console:write' if self.execution_context.operation_type.value == 'mutation' else 'console:read'
            workspace = await access(context['connection'], operation)
            if self.execution_context.operation_type.value == 'subscription' and hasattr(workspace.store, 'release_request_snapshot'):
                workspace.store.release_request_snapshot()
            context.update(store=workspace.store, service=workspace.service, account_id=workspace.account_id,
                           verified_session=getattr(workspace,"request_session",None))
            if getattr(workspace,"priming",None) is not None:
                context['priming_service']=lambda:workspace.priming
            if getattr(workspace,"held_out",None) is not None:
                context['held_out_service']=lambda:workspace.held_out
            context['allow_live']=agreed_live_authority(workspace.service,getattr(workspace,"held_out",None),getattr(workspace,"priming",None))
        yield

    async def on_stream_result(self, result):
        context = self.execution_context.context
        if context.get('account_access') and self.execution_context.operation_type.value == 'subscription':
            from .prospectus_accounts import AccountAccessDenied
            workspace = await context['account_access'](context['connection'], 'console:read')
            if workspace.account_id != context['account_id']:
                raise AccountAccessDenied('account selection changed; reconnect required')
        yield


schema = strawberry.Schema(query=Query, mutation=Mutation, subscription=Subscription,
    extensions=[AccountAuthorization, lambda: MaskErrors(should_mask_error=lambda error: not error_message_is_safe_to_expose(error))])


def create_app(store, *, service=None, token=None, redact=(), allow_unauthenticated_lan=False, account_access=None, priming=None, held_out=None, public_origin=None, event_transport="websocket",
               trusted_loopback_hosts=None, maximum_event_subscribers=DEFAULT_MAXIMUM_EVENT_SUBSCRIBERS):
    if event_transport not in ("websocket","poll"):raise ValueError("explicit supported event transport required")
    if account_access and priming is not None:
        raise ValueError('hosted account access cannot use a global priming service')
    if account_access and held_out is not None:
        raise ValueError('hosted account access cannot use a global held-out service')
    shared_live_authority = agreed_live_authority(service, held_out, priming)
    loopback_hosts = tuple(LOOPBACK_HOSTS if trusted_loopback_hosts is None else trusted_loopback_hosts)
    event_subscribers = EventSubscriberLimit(maximum_event_subscribers)
    def held_out_service():
        if held_out is None:
            raise ValueError('held-out reports are not configured for this workspace')
        return held_out
    configured_priming=priming
    def priming_service():
        nonlocal configured_priming
        # A local default is lazy so unrelated console routes do not require
        # optional priming dependencies. Hosted composition stays fail-closed:
        # a global service cannot select another account's catalog or transformer.
        if account_access:
            raise ValueError('hosted priming requires an account-scoped service composition')
        if configured_priming is None:
            from .priming import PrimingService
            configured_priming=PrimingService(store,redact=redact)
        return configured_priming
    if account_access and (token or allow_unauthenticated_lan or service):
        raise ValueError('hosted account access cannot use shared-token/LAN authority or a global worker')
    if account_access and not hasattr(SchemaExtension, 'on_stream_result'):
        raise ValueError('hosted accounts require streaming authorization support; install the accounts extra')
    def authorized(connection):
        if allow_unauthenticated_lan and connection.client:
            try:
                address = ip_address(connection.client.host)
                if address.is_private and not address.is_unspecified:
                    return True
            except ValueError:
                pass
        if not token:
            return connection.client is not None and connection.client.host in loopback_hosts
        supplied = connection.headers.get('authorization', '').removeprefix('Bearer ') or connection.cookies.get('flywheel-session','')
        return hmac.compare_digest(supplied, token)

    def same_origin(connection):
        origin = connection.headers.get('origin')
        return not origin or (origin == public_origin if public_origin is not None else urlsplit(origin).netloc == connection.headers.get('host'))

    @asynccontextmanager
    async def lifespan(app):
        if service:
            service.start()
        yield
        if service:
            await asyncio.to_thread(service.close)
        if priming is None and configured_priming is not None:
            configured_priming.close()

    app = FastAPI(lifespan=lifespan)
    # Private per-request proof of successful preflight, never a cached grant.
    preflight_sentinel = object()

    @app.middleware('http')
    async def protect(request, next_handler):
        started = time.monotonic()
        response = await protect_request(request, next_handler)
        if account_access:
            from .prospectus_accounts import log_console_phase
            from .refusal_reason import logs_reason, refusal_reason
            route = getattr(request.scope.get('route'), 'path', None) or 'unmatched'
            fields = {}
            if logs_reason(request.url.path, response.status_code):
                payload = b''.join([chunk async for chunk in response.body_iterator])
                rebuilt = Response(payload, response.status_code)
                rebuilt.raw_headers = response.raw_headers
                response = rebuilt
                reason = refusal_reason(response.status_code, payload)
                if reason:
                    fields['reason'] = reason
            log_console_phase('request', started, route=route, method=request.method, status=response.status_code, **fields)
        return response

    async def protect_request(request, next_handler):
        if request.url.path.startswith(('/graphql','/runs/')):
            if not same_origin(request):
                return JSONResponse({'error':'cross-origin access refused'}, status_code=403)
            from .prospectus_accounts import ProspectusUnavailable
            try:
                authenticated = await account_access.authenticated(request) if account_access else authorized(request)
            except ProspectusUnavailable:
                return JSONResponse({'error':'account service unavailable; try again'}, status_code=503,
                    headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Vary':'Cookie'})
            if not authenticated:
                return JSONResponse({'error':'authentication required'}, status_code=401)
            if account_access:
                request.state._cyclotron_preflight = preflight_sentinel
        response = await next_handler(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        if account_access:
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['Vary'] = 'Cookie'
        return response

    async def context(request: Request = None, websocket: WebSocket = None):
        connection = request or websocket
        checked_http = request is not None and getattr(request.state, '_cyclotron_preflight', None) is preflight_sentinel
        authenticated = checked_http or (await account_access.authenticated(connection) if account_access else authorized(connection))
        if not authenticated or not same_origin(connection):
            raise HTTPException(403, 'access refused')
        return {'store':None if account_access else store,'service':service,'redact':redact,
            'account_access':account_access,'connection':connection,'priming_service':priming_service,
            'held_out_service':held_out_service,'allow_live':shared_live_authority,
            'event_subscribers':event_subscribers}

    app.include_router(GraphQLRouter(schema, context_getter=context, graphql_ide=None,
        allow_queries_via_get=False, subscription_protocols=('graphql-transport-ws',)), prefix='/graphql')
    assets = Path(__file__).parent / 'vendor' / 'trace-ui'

    def asset_url(name):
        """Give each rebuilt bundle a new URL without depending on browser cache state."""
        fingerprint=sha256((assets / name).read_bytes()).hexdigest()[:16]
        return f'/assets/{name}?v={fingerprint}'

    app.mount('/assets',StaticFiles(directory=assets),name='assets')

    @app.post('/session')
    async def session(request: Request):
        if account_access:
            raise HTTPException(403,'shared token login is unavailable')
        if not same_origin(request):
            raise HTTPException(403,'access refused')
        body = await request.json()
        if not token or not isinstance(body.get('token'),str) or not hmac.compare_digest(body['token'],token):
            raise HTTPException(401,'access refused')
        response = JSONResponse({'authenticated':True})
        response.set_cookie('flywheel-session',token,httponly=True,samesite='strict')
        return response

    @app.get('/',response_class=HTMLResponse)
    async def index():
        account_mode = (' data-account-mode="hosted" data-event-transport="'+event_transport+'"') if account_access else ''
        return f'<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Decision Flywheel</title><link rel="stylesheet" href="{asset_url("web.css")}"></head><body><div id="root"{account_mode}></div><script src="{asset_url("web.js")}"></script></body></html>'

    @app.get('/runs/{run_id}/timeline',response_class=HTMLResponse)
    async def timeline(run_id: str, request: Request, classifier_id: str | None = None):
        timeline_store = store
        if account_access:
            from .prospectus_accounts import AccountAccessDenied, ProspectusUnavailable
            try:
                workspace = await account_access(request, 'console:read')
                timeline_store = workspace.store
            except (AccountAccessDenied, ProspectusUnavailable):
                raise HTTPException(403,'account access refused') from None
        try:
            run = await asyncio.to_thread(timeline_store.run,run_id)
        except ValueError:
            raise HTTPException(404,'unknown run') from None
        rows = await asyncio.to_thread(timeline_store.all_events,run_id)
        summary = await asyncio.to_thread(timeline_store.summary,run_id)
        classes=run['config'].get('class_config')
        if run['config'].get('classifiers'):
            classifiers=run['config']['classifiers']
            classifier_id=classifier_id or classifiers[0]['id']
            classifier=next((c for c in classifiers if c['id']==classifier_id),None)
            if classifier is None:raise HTTPException(404,'unknown classifier in run')
            rows=[row for row in rows if row['payload'].get('classifier_id')==classifier_id]
            classes=classifier['config']['classes']
        return await asyncio.to_thread(render_trace,[r['payload'] for r in rows],
            class_config=classes,run_comparison=(summary or {}).get('comparison'),embedded=True)

    return app
