"""Serve a disposable failed-comparison fixture with fake models only."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from decision_flywheel.adapters.jev import JevAdapter
from decision_flywheel.api_event_sink import GraphQLTraceSink
from decision_flywheel.classifier_config import ClassifierConfig
from decision_flywheel.flywheel import FittedClassifier
from decision_flywheel.models import DecisionTask
from decision_flywheel.web_api import create_app
from decision_flywheel.web_store import WebStore
from decision_flywheel.web_worker import WebWorker


class FakeComparisonClient:
    def __init__(self):
        self.calls = 0

    def system_one(self, *, state, questions):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError('Deliberate offline fixture failure on second request')
        label = 'yes' if int(state['target']['text']) % 2 else 'no'
        return SimpleNamespace(answers={key: {'choice': label,
            'probabilities': {'yes': .8 if label == 'yes' else .2, 'no': .2 if label == 'yes' else .8}}
            for key in questions}, model='offline-comparison-fixture', usage={'tokens': 0})


def create_offline_app(directory, *, port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('choose an unprivileged local port')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory/'workspace.sqlite3').exists():
        raise ValueError('choose a new empty fixture directory')
    store = WebStore(directory/'workspace.sqlite3')
    classifier = store.save_classifier('topic', 'Synthetic topic', {'question': 'Choose for this synthetic item',
        'classes': [{'label': 'yes', 'role': 'positive'}, {'label': 'no', 'role': 'negative'}]})
    items = [{'id': str(i), 'revision': 1, 'fingerprint': str(i), 'values': {'text': str(i)}} for i in range(4)]
    sources = []
    for name in ('Before', 'After'):
        run = store.create_run('OFFLINE FIXTURE — '+name, 'live',
            {'classifiers': [classifier], 'evaluation_protocol': 'protected-feedback-v1'}, items=items)
        for i in range(4):
            store.append_event(run['id'], str(i), {'kind': 'human-feedback', 'classifier_id': 'topic',
                'action': 'submitted', 'assignment': 'scoreboard',
                'feedback': {'item_id': str(i), 'final_answer_value': 'yes' if i % 2 else 'no'}})
        wheel = SimpleNamespace(active=FittedClassifier(ClassifierConfig(
            DecisionTask('topic', ('yes', 'no'), 'Choose for this synthetic item'))))
        store.checkpoint_scorecard(run['id'], {'topic': wheel})
        sources.append(run['id'])
    model = JevAdapter(FakeComparisonClient())
    # Seed the failure through the same acknowledged GraphQL trace path used
    # during browser recovery; no direct trace-file reconstruction.
    with TestClient(create_app(store)) as client:
        sink = lambda run: GraphQLTraceSink('offline', run,
            transport=lambda body: client.post('/graphql', json=body).json())
        worker = WebWorker(store, directory/'runs', allow_live=True, sink_factory=sink,
                           model_factory=lambda _: (model, None))
        try:
            plan = store.matched_run_preflight(*sources)
            comparison = worker.create_comparison('OFFLINE FIXTURE — interrupted comparison', *sources,
                plan['fingerprint'], max_requests=8, request_id='offline-fixture-approval')
            worker.process(store.claim_command())
            if store.jobs(comparison['id'])[0]['status'] != 'failed':
                raise AssertionError('fixture must start with one completed response and one failed attempt')
        finally:
            worker.close()
    endpoint = f'http://127.0.0.1:{port}/graphql'
    worker = WebWorker(store, directory/'runs', allow_live=True,
        sink_factory=lambda run: GraphQLTraceSink(endpoint, run), model_factory=lambda _: (model, None))
    app = create_app(store, service=worker)
    app.state.offline_worker = worker
    return app, {'run_id': comparison['id'], 'source_ids': sources,
                 'database': str(directory/'workspace.sqlite3'), 'model_calls': model.client.calls}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8785)
    args = parser.parse_args()
    import uvicorn
    app, fixture = create_offline_app(args.output, port=args.port)
    print(json.dumps({**fixture, 'url': f"http://127.0.0.1:{args.port}/#run={fixture['run_id']}&section=optimizations",
                      'models': 'FAKE ONLY — no provider credentials'}), flush=True)
    uvicorn.run(app, host='127.0.0.1', port=args.port, workers=1, access_log=False)


if __name__ == '__main__':
    main()
