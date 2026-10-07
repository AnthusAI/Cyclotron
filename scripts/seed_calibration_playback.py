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
        confidence=.5+self.calls/10
        return SimpleNamespace(answers={key:{'choice':'yes','probabilities':{'yes':confidence,'no':1-confidence}}
                                       for key in request['questions']},model='offline-fixture',usage={'tokens':0})


def no_optimizer(_):
    raise AssertionError('playback fixture must not run an optimizer')


def build_fixture(directory):
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
    store.save_item_list('fixture-items','Synthetic playback items')
    store.upsert_list_items('fixture-items',[{'id':f'fixture-{i}','occurred_at':f'2026-01-0{i}',
        'values':{'text':f'Synthetic item {i}; not research data.'}} for i in range(1,4)])
    model=FakeDecisionClient()
    with TestClient(create_app(store)) as client:
        sink=lambda run:GraphQLTraceSink('offline',run,transport=lambda body:client.post('/graphql',json=body).json())
        worker=WebWorker(store,directory/'runs',allow_live=True,sink_factory=sink,
            model_factory=lambda _: (JevAdapter(model),OptimizerAgent(no_optimizer)))
        try:
            run=worker.create_run('OFFLINE FIXTURE — calibration playback',{'classifier_ids':['relevance','practicality'],
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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build_fixture(args.output)))


if __name__=='__main__':
    main()
