"""Create a clearly synthetic, no-network workspace for playback acceptance."""
import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from decision_flywheel.adapters.jev import JevAdapter
from decision_flywheel.api_event_sink import GraphQLTraceSink
from decision_flywheel.optimizer_agent import OptimizerAgent, OptimizerReply
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


def scripted_optimizer(messages):
    """Scripted structural proposals, not an external completion service."""
    context=json.loads(messages[-1]['content'])
    control=context['current']['control_under_test']
    proposals={'rubric':'Accept synthetic items whose human feedback says yes.',
        'example_ids':[row['id'] for row in context['feedback'][:2]],
        'tasks':[{'name':'synthetic_signal','instructions':'Does this synthetic item qualify?',
                  'labels':['yes','no']}]}
    return OptimizerReply(json.dumps({'rationale':'Scripted offline acceptance proposal',control:proposals[control]}),
        'scripted-offline-optimizer',usage={'tokens':0},
        tool_calls=({'name':'fixture_proposal','arguments':{'control':control}},))


def build_fixture(directory, *, pending_item=False, optimizer_activity=False):
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
    reviews=40 if optimizer_activity else 3
    store.upsert_list_items('fixture-items',[{'id':f'fixture-{i}',
        'occurred_at':(date(2026,1,1)+timedelta(days=i-1)).isoformat(),
        'values':{'text':f'Synthetic item {i}; not research data.'}}
        for i in range(1,reviews+1+int(pending_item))])
    model=FakeDecisionClient()
    with TestClient(create_app(store)) as client:
        sink=lambda run:GraphQLTraceSink('offline',run,transport=lambda body:client.post('/graphql',json=body).json())
        worker=WebWorker(store,directory/'runs',allow_live=True,sink_factory=sink,
            model_factory=lambda _: (JevAdapter(model),OptimizerAgent(scripted_optimizer if optimizer_activity else no_optimizer)))
        try:
            run=worker.create_run('OFFLINE FIXTURE — calibration playback',{'scorecard_id':'fixture-scorecard',
                'item_list_id':'fixture-items','seed':'calibration-playback-v1',
                'optimize_every':10 if optimizer_activity else 100,
                'rubric_changes_every':20 if optimizer_activity else 100,
                'max_requests':10000,'max_optimizer_calls':100})
            store.command(run['id'],'prepare-1','prepare',{})
            worker.process(store.claim_command())
            for i in range(1,reviews+1):
                shown=store.current_item(run['id'])
                positive=i%2==1 if optimizer_activity else i==1
                store.command(run['id'],f'vote-{i}','label',{'item_id':shown['item']['id'],
                    'presentation_id':shown['prediction']['presentation_id'],
                    'labels':[{'classifier_id':'relevance','label':'yes' if positive else 'no','comment':f'Synthetic explanation {i}'},
                              {'classifier_id':'practicality','label':'no' if positive else 'yes','comment':f'Synthetic opposite preference {i}'}]})
                worker.process(store.claim_command())
                while command:=store.claim_command():
                    worker.process(command)
            return {'run_id':run['id'],'database':str(directory/'workspace.sqlite3'),'model_calls':model.calls}
        finally:
            worker.close()


def create_offline_app(directory, *, port, pending_item=False, optimizer_activity=False):
    """Serve interactive/replay acceptance through the real API, with no paid adapters."""
    if type(port) is not int or not 1024<=port<=65535:
        raise ValueError('choose an unprivileged local port')
    fixture=build_fixture(directory,pending_item=pending_item,optimizer_activity=optimizer_activity)
    directory=Path(directory)
    store=WebStore(directory/'workspace.sqlite3')
    client=FakeDecisionClient()
    endpoint=f'http://127.0.0.1:{port}/graphql'
    worker=WebWorker(store,directory/'runs',allow_live=True,
        sink_factory=lambda run:GraphQLTraceSink(endpoint,run),
        model_factory=lambda _: (JevAdapter(client),OptimizerAgent(scripted_optimizer if optimizer_activity else no_optimizer)))
    app=create_app(store,service=worker)
    app.state.offline_worker=worker
    return app,fixture


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--serve',action='store_true',help='Serve an isolated fake-model browser workspace')
    parser.add_argument('--pending-item',action='store_true',help='Keep one additional item ready for labeling after the seeded reviews')
    parser.add_argument('--optimizer-activity',action='store_true',help='Seed forty synthetic reviews with scripted optimization through the real worker')
    parser.add_argument('--port',type=int,default=8785)
    args=parser.parse_args()
    if args.serve:
        import uvicorn
        app,fixture=create_offline_app(args.output,port=args.port,pending_item=args.pending_item,optimizer_activity=args.optimizer_activity)
        print(json.dumps({**fixture,'url':f"http://127.0.0.1:{args.port}/#run={fixture['run_id']}&view=timeline&section=optimizations",
                          'models':'FAKE ONLY — synthetic labels, no provider credentials'}),flush=True)
        uvicorn.run(app,host='127.0.0.1',port=args.port,workers=1,access_log=False)
    else:
        print(json.dumps(build_fixture(args.output,pending_item=args.pending_item,optimizer_activity=args.optimizer_activity)))


if __name__=='__main__':
    main()
