"""A scorecard application runtime owns the shared-request session details."""
import asyncio

from .batched_classification import BatchedAnswers
from .flywheel_test import agent
from .models import DecisionResult
from .scorecard_runtime import ScorecardRuntime
from .web_store import WebStore
from .web_worker import WebWorker
from .workspace_session import freeze_configuration


def test_scorecard_runtime_prepares_all_classifier_outputs_in_one_shared_request(tmp_path):
    store = WebStore(tmp_path / "workspace.sqlite3")
    for identifier in ("relevance", "quality"):
        store.save_classifier(identifier, identifier.title(), {
            "question": "Classify this item",
            "classes": [{"label": "include"}, {"label": "exclude"}],
        })
    store.save_item_list("papers", "Papers")
    store.upsert_list_items("papers", [{
        "id": "paper", "occurred_at": "2026-10-07", "values": {"text": "A paper"},
    }])
    config = freeze_configuration(store, {
        "classifier_ids": ["relevance", "quality"], "item_list_id": "papers",
        "max_requests": 5, "max_optimizer_calls": 2, "optimize_every": 20,
        "rubric_changes_every": 2, "seed": "fixture",
    })
    run = store.create_run("Scorecard", "live", config, items=store.list_items("papers"))

    class Model:
        model_identity = "fake"
        calls = 0

        async def classify_many(self, configs, target, training, **_kwargs):
            self.calls += 1
            return BatchedAnswers({
                identifier: {"decision": DecisionResult("include", {"include": .8, "exclude": .2})}
                for identifier in configs
            }, "fake", {}, 1)

    model = Model()
    runtime = ScorecardRuntime(store, tmp_path / "runs", model_factory=lambda _config: (model, agent([])),
                               sink_factory=lambda _run_id: lambda _event: None)
    session = runtime.open_session(run["id"], run["config"], store.items(run["id"]), None)
    try:
        command = asyncio.run(runtime.execute(session, "prepare", {}, None, run["config"]))
        assert model.calls == 1
        assert set(command.result["prediction"]["classifiers"]) == {"relevance", "quality"}
        assert command.updates == ()
    finally:
        session.close()


def test_worker_routes_a_frozen_scorecard_through_the_scorecard_runtime(tmp_path):
    store = WebStore(tmp_path / "workspace.sqlite3")
    store.save_classifier("topic", "Topic", {
        "question": "Classify this item", "classes": [{"label": "yes"}, {"label": "no"}],
    })
    store.save_item_list("papers", "Papers")
    store.upsert_list_items("papers", [{
        "id": "paper", "occurred_at": "2026-10-07", "values": {"text": "A paper"},
    }])

    class Model:
        model_identity = "fake"
        calls = 0

        async def classify_many(self, configs, target, training, **_kwargs):
            self.calls += 1
            return BatchedAnswers({
                identifier: {"decision": DecisionResult("yes", {"yes": .8, "no": .2})}
                for identifier in configs
            }, "fake", {}, 1)

    model = Model()
    worker = WebWorker(store, tmp_path / "runs", allow_live=True,
                       model_factory=lambda _config: (model, agent([])),
                       sink_factory=lambda _run_id: lambda _event: None)
    run = worker.create_run("Scorecard", {
        "scorecard_id": _scorecard(store), "item_list_id": "papers",
    })
    store.command(run["id"], "prepare", "prepare", {})
    worker.process(store.claim_command())
    try:
        assert type(worker.sessions[run["id"]]).__name__ == "ScorecardSession"
        assert model.calls == 1
        assert store.current_item(run["id"])["prediction"]["classifiers"]["topic"]["label"] == "yes"
    finally:
        worker.close()


def _scorecard(store):
    return store.save_scorecard_definition("scorecard", "Scorecard", [{"id": "topic", "revision": 1}], {})["id"]


def test_scorecard_provider_is_inherited_pinned_and_delivered_to_the_model_factory(tmp_path):
    from types import SimpleNamespace
    store=WebStore(tmp_path/'db')
    store.save_classifier('topic','Topic',{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':'item','occurred_at':'2026-01-01','values':{'text':'Synthetic item'}}])
    definition=store.save_scorecard_definition('scorecard','Scorecard',[{'id':'topic','revision':1}],
        {'decisions_provider':'kev','decisions_model':'kev-4b'})
    observed=[]
    runtime=ScorecardRuntime(store,tmp_path/'runs',model_factory=lambda config:observed.append(config) or (SimpleNamespace(model_identity='fake'),agent([])),sink_factory=lambda _:lambda event:None)
    run=runtime.create_run('Pinned',{'scorecard_id':definition['id'],'item_list_id':'items'})
    assert run['config']['decisions_provider']=='kev'
    assert run['config']['decisions_model']=='kev-4b'
    # Configuration remains inspectable without constructing any provider.
    assert observed==[]
    session=runtime.open_session(run['id'],run['config'],store.items(run['id']),None)
    session.close()
    assert observed[0]['decisions_provider']=='kev'


def test_explicit_provider_override_uses_its_default_model_without_inheriting_another_provider_model(tmp_path):
    store=WebStore(tmp_path/'db')
    store.save_classifier('topic','Topic',{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':'item','occurred_at':'2026-01-01','values':{'text':'Synthetic item'}}])
    store.save_scorecard_definition('scorecard','Scorecard',[{'id':'topic','revision':1}],
        {'decisions_provider':'jev','decisions_model':'jev-1.13.0'})
    runtime=ScorecardRuntime(store,tmp_path/'runs',model_factory=lambda _:None,sink_factory=lambda _:None)
    run=runtime.create_run('Kev',{'scorecard_id':'scorecard','item_list_id':'items','decisions_provider':'kev'})
    assert run['config']['decisions_provider']=='kev'
    assert run['config']['decisions_model']=='kev-latest'
