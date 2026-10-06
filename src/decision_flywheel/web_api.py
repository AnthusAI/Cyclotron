"""Local GraphQL workspace; SQLite is authoritative for API trace history."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import urlsplit

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
    async def ingest_events(self, info: strawberry.Info, run_id: strawberry.ID, events: list[TraceInput]) -> list[TraceEvent]:
        data = [(e.source_id,e.payload) for e in events]
        encoded = json.dumps(data)
        if any(secret and secret in encoded for secret in info.context.get('redact', ())):
            raise ValueError('trace contains a credential; ingestion refused')
        return [event_type(r) for r in await call(info,'append_batch',str(run_id),data)]

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


def create_app(store, *, service=None, token=None, redact=()):
    def authorized(connection):
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
        return '<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Decision Flywheel</title><link rel="stylesheet" href="/assets/web.css"></head><body><div id="root"></div><script src="/assets/web.js"></script></body></html>'

    @app.get('/runs/{run_id}/timeline',response_class=HTMLResponse)
    async def timeline(run_id: str):
        run = await asyncio.to_thread(store.run,run_id)
        rows = await asyncio.to_thread(store.all_events,run_id)
        summary = await asyncio.to_thread(store.summary,run_id)
        return await asyncio.to_thread(render_trace,[r['payload'] for r in rows],
            class_config=run['config'].get('class_config'),run_comparison=(summary or {}).get('comparison'),embedded=True)

    return app
