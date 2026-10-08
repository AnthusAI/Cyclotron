"""Browser recovery fixtures exercise API-owned traces without provider access."""
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from decision_flywheel.web_store import WebStore


def fixture_script():
    path = Path(__file__).parents[1]/'scripts/seed_comparison_recovery.py'
    spec = importlib.util.spec_from_file_location('comparison_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failed_comparison_fixture_resumes_only_when_failed_call_retry_is_approved(tmp_path, monkeypatch):
    module = fixture_script()
    def forbidden(*args, **kwargs):
        raise AssertionError('offline fixture cannot construct a paid client')
    monkeypatch.setattr(module.JevAdapter, 'from_environment', forbidden)
    import httpx
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    app, fixture = module.create_offline_app(tmp_path, port=8785)
    store = WebStore(tmp_path/'workspace.sqlite3')
    assert fixture['model_calls'] == 2
    assert store.jobs(fixture['run_id'])[0]['status'] == 'failed'
    frozen = store.matched_evaluation_inputs(fixture['run_id'])
    sources = {run: store.all_events(run) for run in fixture['source_ids']}
    with TestClient(app) as client:
        app.state.offline_worker.sink_factory = lambda run: module.GraphQLTraceSink('offline', run,
            transport=lambda body: client.post('/graphql', json=body).json())
        query = 'mutation($run:ID!,$retry:Boolean!,$confirmed:Boolean!,$request:String!){resumeMatchedComparison(runId:$run,requestId:$request,maxRequests:9,confirmed:$confirmed,retryFailed:$retry){id}}'
        def resume(request, confirmed, retry):
            return client.post('/graphql', json={'query': query, 'variables': {
                'run': fixture['run_id'], 'retry': retry, 'confirmed': confirmed, 'request': request}}).json()
        rejected = resume('unapproved', False, True)
        assert 'errors' in rejected and len(store.jobs(fixture['run_id'])) == 1
        result = resume('cached-only', True, False)
        assert 'errors' not in result
        # The live fake worker owns command execution; wait only on committed state.
        import time
        deadline = time.monotonic()+5
        while store.jobs(fixture['run_id'])[0]['status'] in ('pending', 'running') and time.monotonic()<deadline:
            time.sleep(.01)
        assert store.jobs(fixture['run_id'])[0]['status'] == 'failed'
        model, _ = app.state.offline_worker.model_factory({})
        assert model.client.calls == 2
        result = resume('retry-failed', True, True)
        assert 'errors' not in result
        deadline = time.monotonic()+5
        while store.jobs(fixture['run_id'])[0]['status'] in ('pending', 'running') and time.monotonic()<deadline:
            time.sleep(.01)
        job = store.jobs(fixture['run_id'])[0]
        assert job['status'] == 'completed', job
        assert job['result']['requests'] == 9
        assert job['result']['new_requests'] == 7
    assert store.matched_evaluation_inputs(fixture['run_id']) == frozen
    assert {run: store.all_events(run) for run in sources} == sources
    assert len(store.jobs(fixture['run_id'])) == 3


def test_comparison_fixture_refuses_existing_workspaces_before_touching_them(tmp_path):
    path = tmp_path/'workspace.sqlite3'
    path.write_bytes(b'not a disposable workspace')
    with pytest.raises(ValueError, match='new empty fixture directory'):
        fixture_script().create_offline_app(tmp_path, port=8785)
    assert path.read_bytes() == b'not a disposable workspace'
