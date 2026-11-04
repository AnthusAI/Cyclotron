"""A standing run retrains itself, over the real hosted API, queue, worker and S3 stores, with scripted models."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from . import account_sessions_test
from .embedded_cyclotron_test import Model
from .hosted_machine_publications_test import ROLE, body, client, decide, fixture
from .iteration_learning_test import Luna
from .optimizer_agent import OptimizerAgent
from .retraining import Deadlined, RetrainingState, check_run, may_start, retrain

identity = account_sessions_test.identity
SETTINGS = {"cluster": "cluster", "task_definition": "task", "subnets": ["subnet-1"]}


class SameProvider(Model):
    """Learning asks the provider the live store asks, so the answers it fits on are answers the live store can check."""

    model_identity = "hosted-publication-offline-provider"


class Ecs:
    def __init__(self):
        self.started, self.running = [], []

    def list_tasks(self, **kwargs):
        return {"taskArns": list(self.running)}

    def run_task(self, **kwargs):
        self.started.append(kwargs)


async def voted_run(http, worker, sent, count, *, start=0):
    """Decide items numbered up to ``count`` and vote on each for the output ``alpha``."""
    for n in range(start, count):
        word = "yes" if n % 2 else "no"
        item = {"id": f"hn-{n}", "values": {"text": f"{word} story {n}"}}
        job, receipt = await decide(http, worker, sent, item)
        revision = receipt.json()["source"]["revision"]
        response = await http.post(
            "/machine/publications/alpha/operations",
            json=body(
                "feedback",
                {
                    "bindingRevision": "binding-one",
                    "itemId": item["id"],
                    "itemRevision": revision,
                    "output": "alpha",
                    "label": "include" if n % 2 else "exclude",
                    "comment": "said why",
                    "editor": {"site": "alpha", "subject": "editor"},
                },
                "fb-" + uuid4().hex,
            ),
        )
        assert response.status_code == 200, response.text


def prepared(identity, monkeypatch, count):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    targets[0] = replace(targets[0], model_identity=model.model_identity)

    async def go():
        async with client(wrapped, config, account) as http:
            await voted_run(http, worker, sent, count)

    asyncio.run(go())
    return worker, store, config, account, sent, targets[0], wrapped, model


def test_votes_that_agree_with_the_decisions_never_start_a_task(identity, monkeypatch):
    worker, *_, target, _, _ = prepared(identity, monkeypatch, 0)
    ecs = Ecs()
    assert check_run(worker, target, ecs, SETTINGS)["started"] is False
    assert ecs.started == []


def test_the_whole_loop_runs_in_the_cloud_from_votes_to_a_live_new_version(identity, monkeypatch, tmp_path):
    worker, store, config, account, sent, target, wrapped, model = prepared(identity, monkeypatch, 24)
    ecs = Ecs()
    first = check_run(worker, target, ecs, SETTINGS)
    assert first["started"] and first["due"] == ["alpha"]
    environment = ecs.started[0]["overrides"]["containerOverrides"][0]["environment"]
    assert {"name": "CYCLOTRON_RETRAIN_RUN", "value": target.run_id} in environment
    # The task runs apart from the hosted worker, on its own disk.
    monkeypatch.undo()
    summary = retrain(worker, target, model=SameProvider(), optimizer=OptimizerAgent(Luna()), principal=ROLE, directory=tmp_path)
    assert summary["report"]["alpha"]["acceptance"]["accepted"]
    state = RetrainingState(worker, account, target.run_id).read().value
    assert state["pending"] and state["failures"] == 0
    # The run's queue carries the adoption, which pays for nothing.
    calls = len(model.calls)
    assert sent
    while sent:
        asyncio.run(worker.worker().process(sent.pop(0)))
    assert len(model.calls) == calls
    job = store.command_envelope(state["pending"]["jobId"])[0]
    assert job["status"] == "completed"

    settled = check_run(worker, target, ecs, SETTINGS)
    assert settled["started"] is False and settled["due"] == []
    state = RetrainingState(worker, account, target.run_id).read().value
    assert state["current"]["id"] == summary["id"]
    assert state["history"][-1]["adoption"] == "completed"

    # The next decision uses the new version without refitting it.
    async def next_decision():
        async with client(wrapped, config, account) as http:
            return await decide(http, worker, sent, {"id": "hn-new", "values": {"text": "yes a new story"}})

    _, receipt = asyncio.run(next_decision())
    assert receipt.status_code == 200, receipt.text
    assert len(model.calls) == calls + 1


def test_only_one_task_runs_at_a_time(identity, monkeypatch):
    worker, *_, target, _, _ = prepared(identity, monkeypatch, 24)
    ecs = Ecs()
    ecs.running = ["arn:task"]
    assert check_run(worker, target, ecs, SETTINGS)["started"] is False


def test_a_task_that_keeps_failing_is_retried_further_and_further_apart():
    state = {"pending": None, "started": 1000.0, "failures": 0}
    assert not may_start(state, 1000.0 + 899)
    assert may_start(state, 1000.0 + 900)
    assert not may_start({**state, "failures": 3}, 1000.0 + 900 * 7)
    assert may_start({**state, "failures": 3}, 1000.0 + 900 * 8)


def test_nothing_starts_while_an_adoption_is_waiting():
    assert not may_start({"pending": {"jobId": "x"}, "started": None, "failures": 0}, 1e9)


def test_a_hung_model_call_fails_instead_of_holding_the_task_open():
    class Hung:
        async def classify_many(self, *args, **kwargs):
            await asyncio.sleep(60)

    with pytest.raises(TimeoutError):
        asyncio.run(Deadlined(Hung(), 0.01).classify_many())


def test_a_second_round_grows_from_the_first_and_learns_only_when_new_votes_are_worth_it(identity, monkeypatch, tmp_path):
    worker, store, config, account, sent, target, wrapped, model = prepared(identity, monkeypatch, 24)
    ecs = Ecs()
    check_run(worker, target, ecs, SETTINGS)
    monkeypatch.undo()
    first = retrain(worker, target, model=SameProvider(), optimizer=OptimizerAgent(Luna()), principal=ROLE, directory=tmp_path / "1")
    while sent:
        asyncio.run(worker.worker().process(sent.pop(0)))
    assert check_run(worker, target, ecs, SETTINGS)["due"] == []

    async def more():
        async with client(wrapped, config, account) as http:
            await voted_run(http, worker, sent, 40, start=24)

    asyncio.run(more())
    second_check = check_run(worker, target, ecs, SETTINGS)
    assert second_check["due"] == ["alpha"]
    state = RetrainingState(worker, account, target.run_id)
    state.change(lambda value: value.update(started=None))
    second = retrain(worker, target, model=SameProvider(), optimizer=OptimizerAgent(Luna()), principal=ROLE, directory=tmp_path / "2")
    while sent:
        asyncio.run(worker.worker().process(sent.pop(0)))
    check_run(worker, target, ecs, SETTINGS)
    value = state.read().value
    assert value["current"]["id"] == second["id"] != first["id"]
    assert [row["adoption"] for row in value["history"]] == ["completed", "completed"]
