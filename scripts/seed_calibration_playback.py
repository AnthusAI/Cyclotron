"""Create a clearly synthetic, no-network workspace for playback acceptance."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from decision_flywheel.adapters.jev import JevAdapter
from decision_flywheel.api_event_sink import GraphQLTraceSink
from decision_flywheel.optimizer_agent import OptimizerAgent
from decision_flywheel.web_api import create_app
from decision_flywheel.web_store import WebStore
from decision_flywheel.web_worker import WebWorker


class FakeDecisionClient:
    calls=0

    def system_one(self,**request):
        self.calls+=1
        confidence=min(.9,.5+self.calls/10)
        return SimpleNamespace(answers={key:{'choice':'yes','probabilities':{'yes':confidence,'no':1-confidence}}
                                       for key in request['questions']},model='offline-fixture',usage={'tokens':0})


def no_optimizer(_):
    raise AssertionError('playback fixture must not run an optimizer')


def build_fixture(directory, *, pending_item=False):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    # Refuse to seed an existing workspace: never merge synthetic labels into
    # a real run, nor quietly duplicate fixtures when an operator repeats this.
    if (directory/'workspace.sqlite3').exists():
        raise ValueError('choose a new empty fixture directory')
    store=WebStore(directory/'workspace.sqlite3')
    for identifier in ('relevance','practicality'):
        store.save_classifier(identifier,identifier.title(),{'question':'Does this synthetic item qualify?',
            'classes':[{'label':'yes','role':'positive'},{'label':'no','role':'negative'}]})
    store.save_scorecard_definition('fixture-scorecard','Synthetic scorecard',
        [{'id':identifier,'revision':1} for identifier in ('relevance','practicality')],{})
    store.save_item_list('fixture-items','Synthetic playback items')
    store.upsert_list_items('fixture-items',[{'id':f'fixture-{i}','occurred_at':f'2026-01-0{i}',
        'values':{'text':f'Synthetic item {i}; not research data.'}} for i in range(1,5 if pending_item else 4)])
    model=FakeDecisionClient()
    with TestClient(create_app(store)) as client:
        sink=lambda run:GraphQLTraceSink('offline',run,transport=lambda body:client.post('/graphql',json=body).json())
        worker=WebWorker(store,directory/'runs',allow_live=True,sink_factory=sink,
            model_factory=lambda _: (JevAdapter(model),OptimizerAgent(no_optimizer)))
        try:
            run=worker.create_run('OFFLINE FIXTURE — calibration playback',{'scorecard_id':'fixture-scorecard',
                'item_list_id':'fixture-items','seed':'calibration-playback-v1','optimize_every':100,'rubric_changes_every':100})
            store.command(run['id'],'prepare-1','prepare',{})
            worker.process(store.claim_command())
            for i in range(1,4):
                shown=store.current_item(run['id'])
                store.command(run['id'],f'vote-{i}','label',{'item_id':shown['item']['id'],
                    'presentation_id':shown['prediction']['presentation_id'],
                    'labels':[{'classifier_id':'relevance','label':'yes' if i==1 else 'no','comment':f'Synthetic explanation {i}'},
                              {'classifier_id':'practicality','label':'no' if i==1 else 'yes','comment':f'Synthetic opposite preference {i}'}]})
                worker.process(store.claim_command())
                while command:=store.claim_command():
                    worker.process(command)
            return {'run_id':run['id'],'database':str(directory/'workspace.sqlite3'),'model_calls':model.calls}
        finally:
            worker.close()


def create_offline_app(directory, *, port, pending_item=False):
    """Serve interactive/replay acceptance through the real API, with no paid adapters."""
    if type(port) is not int or not 1024<=port<=65535:
        raise ValueError('choose an unprivileged local port')
    fixture=build_fixture(directory,pending_item=pending_item)
    directory=Path(directory)
    store=WebStore(directory/'workspace.sqlite3')
    client=FakeDecisionClient()
    endpoint=f'http://127.0.0.1:{port}/graphql'
    worker=WebWorker(store,directory/'runs',allow_live=True,
        sink_factory=lambda run:GraphQLTraceSink(endpoint,run),
        model_factory=lambda _: (JevAdapter(client),OptimizerAgent(no_optimizer)))
    app=create_app(store,service=worker)
    app.state.offline_worker=worker
    return app,fixture


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--serve',action='store_true',help='Serve an isolated fake-model browser workspace')
    parser.add_argument('--pending-item',action='store_true',help='Keep a fourth item ready for labeling after three reviews')
    parser.add_argument('--port',type=int,default=8785)
    args=parser.parse_args()
    if args.serve:
        import uvicorn
        app,fixture=create_offline_app(args.output,port=args.port,pending_item=args.pending_item)
        print(json.dumps({**fixture,'url':f"http://127.0.0.1:{args.port}/#run={fixture['run_id']}&view=timeline&section=optimizations",
                          'models':'FAKE ONLY — synthetic labels, no provider credentials'}),flush=True)
        uvicorn.run(app,host='127.0.0.1',port=args.port,workers=1,access_log=False)
    else:
        print(json.dumps(build_fixture(args.output,pending_item=args.pending_item)))


if __name__=='__main__':
    main()
