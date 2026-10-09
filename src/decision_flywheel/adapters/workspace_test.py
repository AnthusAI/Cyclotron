"""The optional workspace selects adapters without changing the core."""
import pytest


def test_workspace_factory_dispatches_kev_without_constructing_jev_or_opening_a_connection(monkeypatch):
    from .workspace import decision_adapter
    from .jev import JevAdapter
    monkeypatch.setattr(JevAdapter,'from_environment',lambda **_:pytest.fail('wrong provider'))
    adapter=decision_adapter({'decisions_provider':'kev','decisions_model':'kev-4b'})
    assert adapter.name=='kev'
    assert adapter.configuration.model=='kev-4b'
    assert callable(adapter.classify_many)


def test_old_workspace_runs_default_to_jev_and_do_not_change_their_pinned_model(monkeypatch):
    from .workspace import decision_adapter
    from .jev import JevAdapter
    observed=[]
    monkeypatch.setattr(JevAdapter,'from_environment',lambda **kwargs:observed.append(kwargs['configuration']) or 'fake')
    assert decision_adapter({'decisions_model':'jev-pinned'})=='fake'
    assert observed[0].model=='jev-pinned'


@pytest.mark.parametrize('provider',['unknown'])
def test_unsupported_workspace_providers_never_silently_fall_back_to_jev(provider,monkeypatch):
    from .workspace import decision_adapter
    from .jev import JevAdapter
    monkeypatch.setattr(JevAdapter,'from_environment',lambda **_:pytest.fail('silent fallback'))
    with pytest.raises(ValueError,match='provider|Laya'):
        decision_adapter({'decisions_provider':provider,'decisions_model':'chosen'})


def test_workspace_factory_loads_the_selected_laya_revision_without_a_jev_fallback(monkeypatch):
    from .workspace import decision_adapter
    from .laya import LayaAdapter
    from .jev import JevAdapter
    observed=[]
    monkeypatch.setattr(JevAdapter,'from_environment',lambda **_:pytest.fail('wrong provider'))
    monkeypatch.setattr(LayaAdapter,'from_default',lambda **kwargs:observed.append(kwargs['configuration']) or 'laya')
    assert decision_adapter({'decisions_provider':'laya','decisions_model':'local-pinned','decisions_revision':'abc'})=='laya'
    assert observed[0].model_id=='local-pinned' and observed[0].revision=='abc'


@pytest.mark.parametrize('provider',['kev','laya'])
def test_cyclotron_worker_records_one_joint_wire_request_through_the_graphql_api(tmp_path,provider):
    from fastapi.testclient import TestClient
    from ..web_store import WebStore
    from ..web_api import create_app
    from ..web_worker import WebWorker
    from ..api_event_sink import GraphQLTraceSink
    from ..flywheel_test import agent
    from .kev import KevAdapter,KevConfiguration
    from .kev_test import FakeTransport,Response
    from .laya import LayaAdapter
    store=WebStore(tmp_path/'db')
    for cid in ('first','second'):
        store.save_classifier(cid,cid,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':'target','occurred_at':'2026-01-01','values':{'text':'Synthetic target'}}])
    transport=FakeTransport(Response(payload={'model':'kev-test','answers':{
        'q0':{'choice':'yes','probabilities':{'yes':.8,'no':.2}},
        'q1':{'choice':'no','probabilities':{'yes':.1,'no':.9}}}}))
    class LocalModel:
        calls=0
        def system_one(self,*,state,questions):
            self.calls+=1
            self.state,self.questions=state,questions
            return {'model':'laya-test','answers':transport.response.payload['answers']}
    local=LocalModel()
    with TestClient(create_app(store)) as client:
        sink=lambda run:GraphQLTraceSink('offline',run,transport=lambda body:client.post('/graphql',json=body).json())
        worker=WebWorker(store,tmp_path/'runs',allow_live=True,sink_factory=sink,
            model_factory=lambda config:(KevAdapter(transport=transport,configuration=KevConfiguration(model=config['decisions_model']))
                if provider=='kev' else LayaAdapter(local),agent([])))
        try:
            run=worker.create_run(provider,{'classifier_ids':['first','second'],'item_list_id':'items','decisions_provider':provider})
            store.command(run['id'],'prepare','prepare',{})
            worker.process(store.claim_command())
            assert store.jobs(run['id'])[0]['status']=='completed'
            predictions=store.current_item(run['id'])['prediction']['classifiers']
            assert predictions['first']['label']=='yes' and predictions['second']['label']=='no'
            events=[row['payload'] for row in store.all_events(run['id'])]
            requests=[e for e in events if e['kind']=='decision-request']
            # One exchange is projected into each independent classifier trace;
            # the request identity ties both projections to one physical call.
            assert all(e['model'].startswith(provider+':') for e in requests)
            assert all(set(e['questions'])=={'q0','q1'} for e in requests)
            if provider=='kev':assert transport.body['model']=='kev-latest'
            assert len({e['shared_request_fingerprint'] for e in requests})==1
            assert (transport.calls if provider=='kev' else local.calls)==1
        finally:worker.close()
