"""The web application drives the existing engine and acknowledges API traces."""
import json
from dataclasses import asdict
from .web_store import WebStore
from .web_worker import WebWorker
from .web_api import create_app
from .api_event_sink import GraphQLTraceSink
from .flywheel_test import FakeModel, agent
from .reviewer_store import Article
from fastapi.testclient import TestClient


def test_prepare_then_explained_vote_records_a_real_cycle_through_graphql(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    article = asdict(Article('paper','A research paper','Real abstract','2026-10-06',('cs.AI',)))
    run = store.create_run('Test','live',{'selection_policy':{'primary':'f1','positive_class':'include'},
        'max_requests':30,'max_optimizer_calls':2,'seed':'fixture','optimize_every':20,'rubric_changes_every':2},items=[article])
    client = TestClient(create_app(store))
    factory = lambda run_id: GraphQLTraceSink('not-used',run_id,transport=lambda body:client.post('/graphql',json=body).json())
    worker = WebWorker(store,tmp_path / 'runs',sink_factory=factory,
        model_factory=lambda config:(FakeModel(),agent([])),allow_live=True)
    prepare = store.command(run['id'],'prepare-1','prepare',{})
    worker.process(store.claim_command())
    current = store.current_item(run['id'])
    assert current['item']['title'] == 'A research paper'
    assert current['prediction']['label'] == 'exclude'
    assert store.jobs(run['id'])[0]['status'] == 'completed'
    label = store.command(run['id'],'vote-1','label',{'item_id':'paper','label':'include','comment':'About knowledge access',
        'presentation_id':current['prediction']['presentation_id']})
    worker.process(store.claim_command())
    events = [r['payload'] for r in store.all_events(run['id'])]
    prediction = next(e for e in events if e['kind']=='prediction')
    feedback = next(e for e in events if e['kind']=='human-feedback')
    assert prediction['cycle_id'] == feedback['cycle_id']
    assert feedback['feedback']['edit_comment_value'] == 'About knowledge access'
    assert any(e['kind']=='cycle-completed' for e in events)
    assert store.current_item(run['id']) is None
    worker.close()


def test_live_run_creation_requires_positive_ceiling_and_explicit_selection_policy(tmp_path):
    import pytest
    store = WebStore(tmp_path / 'web.sqlite')
    worker = WebWorker(store,tmp_path / 'runs',sink_factory=lambda _:None,model_factory=lambda _:None,allow_live=True)
    with pytest.raises(ValueError):
        worker.create_run('Bad', {'max_requests':0})
    assert store.runs() == []


def test_engine_optimizer_and_decision_exchanges_are_ingested_with_full_context(tmp_path):
    import asyncio
    from .adapters.jev import JevAdapter
    from .classifier_config import ClassifierConfig
    from .flywheel import DecisionFlywheel
    from .flywheel_test import TASK, TRAIN, DEV
    from .optimizer_agent import OptimizerAgent, OptimizerReply
    store = WebStore(tmp_path / 'web.sqlite')
    run = store.create_run('Exchanges', 'recorded', {})
    with TestClient(create_app(store)) as client:
        sink = GraphQLTraceSink('unused',run['id'],transport=lambda body:client.post('/graphql',json=body).json())
        class Client:
            def system_one(self, **request):
                from types import SimpleNamespace
                return SimpleNamespace(answers={name:{'value':'include','confidence':.8,
                    'probabilities':{'include':.8,'exclude':.2}} for name in request['questions']},model='fake',usage={})
        optimizer = OptimizerAgent(lambda messages: OptimizerReply('{"rubric":"Include knowledge-base research","rationale":"Human explanation"}', 'fake'))
        wheel = DecisionFlywheel(tmp_path / 'engine.sqlite',ClassifierConfig(TASK),JevAdapter(Client()),optimizer,observer=sink)
        wheel.set_optimizer_context(['Include papers about knowledge-base management'])
        asyncio.run(wheel.improve(TRAIN,DEV,protected=(),propensities={r.item.id:1. for r in TRAIN}))
        events = [row['payload'] for row in store.all_events(run['id'])]
        request = next(e for e in events if e['kind']=='optimizer-request')
        assert 'Include papers about knowledge-base management' in request['messages'][-1]['content']
        assert any(e['kind']=='optimizer-response' and 'knowledge-base research' in e['content'] for e in events)
        assert any(e['kind']=='decision-request' and e['state'] and e['questions'] for e in events)
        assert any(e['kind']=='decision-response' and e['answers'] for e in events)
        wheel.close()


def test_failed_api_ingestion_marks_work_failed_without_a_paid_model_call(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    worker = WebWorker(store,tmp_path / 'runs',articles=[asdict(Article('paper','Title','Abstract','2026-10-06',('cs.AI',)))],
        allow_live=True,sink_factory=lambda _:lambda event:None,model_factory=lambda _: (FakeModel(),agent([])))
    run = worker.create_run('API failure', {})
    reviewer = worker.session(run['id'])
    def unavailable(event):
        raise RuntimeError('API unavailable')
    reviewer.core.observer = unavailable
    store.command(run['id'],'prepare','prepare',{})
    worker.process(store.claim_command())
    assert store.jobs(run['id'])[0]['status'] == 'failed'
    assert reviewer.core.model.calls == 0
    worker.close()


def test_restart_accepts_feedback_for_the_displayed_prediction_in_the_same_cycle(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    client = TestClient(create_app(store))
    factory = lambda run_id:GraphQLTraceSink('unused',run_id,transport=lambda body:client.post('/graphql',json=body).json())
    kwargs = dict(sink_factory=factory,model_factory=lambda _: (FakeModel(),agent([])),allow_live=True,
        articles=[asdict(Article('paper','Title','Abstract','2026-10-06',('cs.AI',)))])
    worker = WebWorker(store,tmp_path / 'runs',**kwargs)
    run = worker.create_run('Restart', {})
    store.command(run['id'],'prepare','prepare',{})
    worker.process(store.claim_command())
    current = store.current_item(run['id'])
    prediction = next(row['payload'] for row in store.all_events(run['id']) if row['payload']['kind']=='prediction')
    worker.close()
    reopened = WebWorker(store,tmp_path / 'runs',**kwargs)
    store.command(run['id'],'vote','label',{'item_id':'paper','label':'include','comment':'Knowledge access',
        'presentation_id':current['prediction']['presentation_id']})
    reopened.process(store.claim_command())
    assert store.jobs(run['id'])[0]['status'] == 'completed'
    feedback = next(row['payload'] for row in store.all_events(run['id']) if row['payload']['kind']=='human-feedback')
    assert feedback['cycle_id'] == prediction['cycle_id']
    assert reopened.sessions[run['id']].core.model.calls == 0
    reopened.close()
