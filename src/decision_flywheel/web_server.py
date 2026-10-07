"""Run the local GraphQL app or import an existing recording through its API."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path

from .api_event_sink import GraphQLTraceSink
from .trace_artifact import read_trace
from .trace_server import bind_address
from .web_store import WebStore


def import_recording(endpoint, database, name, *, config=None, token=None):
    import httpx
    headers = {'Authorization':f'Bearer {token}'} if token else {}
    def transport(body):
        response = httpx.post(endpoint,json=body,headers=headers,timeout=30,trust_env=False)
        response.raise_for_status()
        result = response.json()
        if result.get('errors'):
            raise RuntimeError('recording API import failed')
        return result
    events = read_trace(database)
    result = transport({'query':'mutation($name:String!,$config:JSON!){createRun(name:$name,mode:"recorded",config:$config){id}}',
                        'variables':{'name':name,'config':config or {}}})
    run_id = result['data']['createRun']['id']
    for offset in range(0,len(events),200):
        transport({'query':'mutation($run:ID!,$events:[TraceInput!]!){ingestEvents(runId:$run,events:$events){sequence}}',
            'variables':{'run':run_id,'events':[{'sourceId':f"engine:{event['event_id']}",'payload':event} for event in events[offset:offset+200]]}})
    transport({'query':'mutation($run:ID!,$summary:JSON!){completeRun(runId:$run,summary:$summary){id}}',
        'variables':{'run':run_id,'summary':{'comparison':config if config and 'before' in config else None,
            'source':'imported recording; no new model calls'}}})
    return run_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    serve = sub.add_parser('serve')
    serve.add_argument('--database',type=Path,default=Path('var/web/workspace.sqlite3'))
    serve.add_argument('--articles',type=Path)
    serve.add_argument('--host',default='127.0.0.1')
    serve.add_argument('--port',type=int,default=8782)
    serve.add_argument('--allow-live',action='store_true',help='enable explicit live-run creation; does not itself make model calls')
    serve.add_argument('--allow-unauthenticated-lan',action='store_true',
        help='allow devices on the trusted local network to use the workspace without signing in')
    load = sub.add_parser('import-recording')
    load.add_argument('--endpoint',default='http://127.0.0.1:8782/graphql')
    load.add_argument('--database',type=Path,required=True)
    load.add_argument('--name',required=True)
    load.add_argument('--config',type=Path)
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(override=False)
    token = os.environ.get('FLYWHEEL_WEB_TOKEN')
    if args.command == 'import-recording':
        run_id = import_recording(args.endpoint,args.database,args.name,
            config=json.loads(args.config.read_text()) if args.config else None,token=token)
        print(json.dumps({'imported_run_id':run_id}))
        return 0
    try:
        host = bind_address(args.host)
    except ValueError as error:
        parser.error(str(error))
    if host != '127.0.0.1' and not token and not args.allow_unauthenticated_lan:
        parser.error('LAN access requires FLYWHEEL_WEB_TOKEN in the environment')
    from .adapters.jev import JevAdapter,JevConfiguration
    from .adapters.openai_optimizer import OpenAIOptimizer
    from .optimizer_agent import OptimizerAgent
    from .reviewer import load_articles_jsonl
    from .web_api import create_app
    from .web_worker import WebWorker
    import uvicorn
    store = WebStore(args.database)
    articles = tuple(asdict(article) for article in load_articles_jsonl(args.articles)) if args.articles else ()
    redact = tuple(os.environ.get(key,'') for key in ('FLYWHEEL_WEB_TOKEN','TYPESAFE_API_KEY','OPENAI_API_KEY'))
    endpoint = f'http://{host}:{args.port}/graphql'
    def model_factory(config):
        return (JevAdapter.from_environment(configuration=JevConfiguration(model=config['decisions_model'])),
                OptimizerAgent(OpenAIOptimizer.from_environment(model=config['optimizer_model'],max_calls=config['max_optimizer_calls'])))
    service = WebWorker(store,args.database.parent / 'runs',articles=articles,allow_live=args.allow_live,
        sink_factory=lambda run_id:GraphQLTraceSink(endpoint,run_id,token=token),model_factory=model_factory,redact=redact)
    app = create_app(store,service=service,token=token,redact=redact,
        allow_unauthenticated_lan=args.allow_unauthenticated_lan)
    print(f'Workspace: http://{host}:{args.port}/',flush=True)
    uvicorn.run(app,host=host,port=args.port,workers=1,access_log=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
