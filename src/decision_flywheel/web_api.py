"""Local GraphQL workspace; SQLite is authoritative for API trace history."""
import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import hmac
from ipaddress import ip_address
import json
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import strawberry
from strawberry.fastapi import GraphQLRouter
from strawberry.scalars import JSON

from .trace_artifact import render_trace


@strawberry.type
class Run:
    id: strawberry.ID
    name: str
    mode: str
    status: str
    created_at: str
    config: JSON

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
    async def scorecards(self,info:strawberry.Info)->JSON:
        return await call(info,'scorecards')

    @strawberry.field
    async def scorecard_definitions(self,info:strawberry.Info)->JSON:
        return await call(info,'scorecard_definitions')

    @strawberry.field
    async def scorecard_definition_versions(self,info:strawberry.Info,scorecard_id:strawberry.ID)->JSON:
        return await call(info,'scorecard_definition_versions',str(scorecard_id))

    @strawberry.field
    async def scorecard_definition_comparison(self,info:strawberry.Info,scorecard_id:strawberry.ID,before_revision:int,after_revision:int)->JSON:
        return await call(info,'scorecard_definition_comparison',str(scorecard_id),before_revision,after_revision)

    @strawberry.field
    async def scorecard_classifiers(self,info:strawberry.Info,scorecard_id:strawberry.ID,revision:int|None=None)->JSON:
        definition=await call(info,'scorecard_definition',str(scorecard_id),revision)
        return [await call(info,'classifier',ref['id'],ref['revision']) for ref in definition['classifiers']]

    @strawberry.field
    async def scorecard_versions(self,info:strawberry.Info,scorecard_id:strawberry.ID)->JSON:
        return await call(info,'scorecard_versions',str(scorecard_id))

    @strawberry.field
    async def scorecard_checkpoints(self,info:strawberry.Info,run_id:strawberry.ID)->JSON:
        return await call(info,'scorecard_checkpoints',str(run_id))

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
        return Capabilities(live_enabled=bool(service and service.allow_live),item_count=len(service.articles) if service else 0)

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
    async def extend_scorecard(self,info:strawberry.Info,parent_run_id:strawberry.ID,classifier_ids:list[str],name:str,confirmed:bool=False)->Run:
        service=info.context.get('service')
        if not confirmed or service is None or not service.allow_live:
            raise ValueError('scorecard replay requires explicit live authority')
        run=await call(info,'extend_scorecard',str(parent_run_id),classifier_ids,name=name)
        await call(info,'append_event',run['id'],'scorecard-created',{'kind':'scorecard-version-created','scorecard_id':run['config']['scorecard_id'],'revision':run['config']['scorecard_revision'],'parent_run_id':str(parent_run_id),'learning_policy':'fresh-replay','backfill_count':run['config']['backfill_count']})
        return Run(**run)

    @strawberry.mutation
    async def activate_scorecard_version(self,info:strawberry.Info,scorecard_id:strawberry.ID,revision:int)->Run:
        run=await call(info,'activate_scorecard_version',str(scorecard_id),revision)
        await call(info,'append_event',run['id'],f'activate:{uuid4()}',{'kind':'scorecard-version-activated','scorecard_id':str(scorecard_id),'revision':revision})
        return Run(**run)

    @strawberry.mutation
    async def label_item(self, info: strawberry.Info, classifier_id: strawberry.ID, classifier_revision: int,
                         list_id: strawberry.ID, item_id: str, item_revision: int, label: str, comment: str, request_id: str) -> JSON:
        if any(secret and secret in comment for secret in info.context.get('redact', ())):
            raise ValueError('feedback contains a credential')
        return await call(info,'label_item',str(classifier_id),classifier_revision,str(list_id),item_id,item_revision,label,comment,request_id)

    @strawberry.mutation
    async def save_classifier(self, info: strawberry.Info, identifier: str, name: str, config: JSON) -> JSON:
        if any(secret and secret in json.dumps(config) for secret in info.context.get('redact', ())):
            raise ValueError('configuration contains a credential')
        return await call(info, 'save_classifier', identifier, name, config)

    @strawberry.mutation
    async def save_scorecard_definition(self,info:strawberry.Info,identifier:str,name:str,classifiers:JSON,settings:JSON)->JSON:
        if any(secret and secret in json.dumps([name,classifiers,settings]) for secret in info.context.get('redact',())):
            raise ValueError('configuration contains a credential')
        return await call(info,'save_scorecard_definition',identifier,name,classifiers,settings)

    @strawberry.mutation
    async def activate_scorecard_definition(self,info:strawberry.Info,scorecard_id:strawberry.ID,revision:int)->JSON:
        return await call(info,'activate_scorecard_definition',str(scorecard_id),revision)

    @strawberry.mutation
    async def save_item_list(self, info: strawberry.Info, identifier: str, name: str) -> JSON:
        return await call(info, 'save_item_list', identifier, name)

    @strawberry.mutation
    async def upsert_list_items(self, info: strawberry.Info, list_id: strawberry.ID, items: JSON) -> JSON:
        if any(secret and secret in json.dumps(items) for secret in info.context.get('redact', ())):
            raise ValueError('items contain a credential')
        return await call(info, 'upsert_list_items', str(list_id), items)

    @strawberry.mutation
    async def complete_run(self, info: strawberry.Info, run_id: strawberry.ID, summary: JSON) -> Run:
        if any(secret and secret in json.dumps(summary) for secret in info.context.get('redact', ())):
            raise ValueError('summary contains a credential')
        return Run(**await call(info,'complete_run',str(run_id),summary))

    @strawberry.mutation
    async def create_run(self, info: strawberry.Info, name: str, mode: str = 'recorded', config: JSON | None = None,
                         confirmed: bool = False) -> Run:
        service = info.context.get('service')
        if any(secret and secret in json.dumps(config or {}) for secret in info.context.get('redact', ())):
            raise ValueError('run configuration contains a credential')
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
        if any(secret and secret in json.dumps(config) for secret in info.context.get('redact',())):
            raise ValueError('configuration contains a credential')
        return Run(**await asyncio.to_thread(service.create_replay,name,str(source_run_id),config))

    @strawberry.mutation
    async def ingest_events(self, info: strawberry.Info, run_id: strawberry.ID, events: list[TraceInput]) -> list[TraceEvent]:
        data = [(e.source_id,e.payload) for e in events]
        encoded = json.dumps(data)
        if any(secret and secret in encoded for secret in info.context.get('redact', ())):
            raise ValueError('trace contains a credential; ingestion refused')
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
        row = await call(info,'command',str(run_id),request_id,kind,payload or {})
        return Job(id=row['id'],kind=row['kind'],status=row['status'],result=row['result'])


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def run_events(self, info: strawberry.Info, run_id: strawberry.ID, after: int = 0) -> AsyncGenerator[TraceEvent, None]:
        cursor = after
        while True:
            rows = await call(info, 'events', str(run_id), after=cursor)
            for row in rows:
                cursor = row['sequence']
                yield event_type(row)
            await asyncio.sleep(.2)


schema = strawberry.Schema(query=Query, mutation=Mutation, subscription=Subscription)


def create_app(store, *, service=None, token=None, redact=(), allow_unauthenticated_lan=False):
    def authorized(connection):
        if allow_unauthenticated_lan and connection.client:
            try:
                address = ip_address(connection.client.host)
                if address.is_private and not address.is_unspecified:
                    return True
            except ValueError:
                pass
        if not token:
            return connection.client is not None and connection.client.host in ('127.0.0.1','::1','testclient')
        supplied = connection.headers.get('authorization', '').removeprefix('Bearer ') or connection.cookies.get('flywheel-session','')
        return hmac.compare_digest(supplied, token)

    def same_origin(connection):
        origin = connection.headers.get('origin')
        return not origin or urlsplit(origin).netloc == connection.headers.get('host')

    @asynccontextmanager
    async def lifespan(app):
        if service:
            service.start()
        yield
        if service:
            await asyncio.to_thread(service.close)

    app = FastAPI(lifespan=lifespan)

    @app.middleware('http')
    async def protect(request, next_handler):
        if request.url.path.startswith(('/graphql','/runs/')):
            if not same_origin(request):
                return JSONResponse({'error':'cross-origin access refused'}, status_code=403)
            if not authorized(request):
                return JSONResponse({'error':'authentication required'}, status_code=401)
        response = await next_handler(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    async def context(request: Request = None, websocket: WebSocket = None):
        connection = request or websocket
        if not authorized(connection) or not same_origin(connection):
            raise HTTPException(403, 'access refused')
        return {'store':store,'service':service,'redact':redact}

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
        return f'<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Decision Flywheel</title><link rel="stylesheet" href="{asset_url("web.css")}"></head><body><div id="root"></div><script src="{asset_url("web.js")}"></script></body></html>'

    @app.get('/runs/{run_id}/timeline',response_class=HTMLResponse)
    async def timeline(run_id: str, classifier_id: str | None = None):
        run = await asyncio.to_thread(store.run,run_id)
        rows = await asyncio.to_thread(store.all_events,run_id)
        summary = await asyncio.to_thread(store.summary,run_id)
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
