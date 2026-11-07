"""A standing run retrains itself, over the real hosted API, queue, worker and S3 stores, with scripted models."""

import asyncio
import sys
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from . import account_sessions_test
from .embedded_cyclotron_test import Model
from .hosted_machine_publications_test import ROLE, body, client, decide, fixture
from .hosted_publications import PublicationCommandRuntime
from .iteration_learning_test import Luna
from .optimizer_agent import OptimizerAgent
from .provider_credentials_test import KEY
from .retraining import (
    Deadlined,
    RetrainingState,
    check_run,
    job_decisions,
    look_up_older_decisions,
    may_start,
    optimizer_transport,
    refresh_votes,
    retrain,
)

identity = account_sessions_test.identity
SETTINGS = {"cluster": "cluster", "task_definition": "task", "subnets": ["subnet-1"], "principal": ROLE}


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
    assert not sent  # The sweep starts it: nothing here needs the queue's send permission.
    asyncio.run(worker.worker().sweep())
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


def test_the_optimizer_is_keyed_from_openais_own_secret_never_the_decision_models(monkeypatch):
    read = []
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **kwargs: SimpleNamespace(**kwargs)))

    class Secrets:
        def get_secret_value(self, SecretId):
            read.append(SecretId)
            return {"SecretString": KEY}

    transport = optimizer_transport(lambda: Secrets())
    assert read == ["cyclotron/providers/openai-optimizer"]
    assert transport.client.api_key == KEY


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
    first = retrain(
        worker, target, model=SameProvider(), optimizer=OptimizerAgent(Luna()), principal=ROLE, directory=tmp_path / "1"
    )
    asyncio.run(worker.worker().sweep())
    assert check_run(worker, target, ecs, SETTINGS)["due"] == []

    async def more():
        async with client(wrapped, config, account) as http:
            await voted_run(http, worker, sent, 40, start=24)

    asyncio.run(more())
    second_check = check_run(worker, target, ecs, SETTINGS)
    assert second_check["due"] == ["alpha"]
    state = RetrainingState(worker, account, target.run_id)
    state.change(lambda value: value.update(started=None))
    second = retrain(
        worker, target, model=SameProvider(), optimizer=OptimizerAgent(Luna()), principal=ROLE, directory=tmp_path / "2"
    )
    asyncio.run(worker.worker().sweep())
    check_run(worker, target, ecs, SETTINGS)
    value = state.read().value
    assert value["current"]["id"] == second["id"] != first["id"]
    assert [row["adoption"] for row in value["history"]] == ["completed", "completed"]


def learned_and_queued(identity, monkeypatch, tmp_path):
    parts = prepared(identity, monkeypatch, 24)
    worker, _, _, _, _, target, *_ = parts
    check_run(worker, target, Ecs(), SETTINGS)
    monkeypatch.undo()
    retrain(worker, target, model=SameProvider(), optimizer=OptimizerAgent(Luna()), principal=ROLE, directory=tmp_path)
    return parts


def test_a_voted_item_sent_again_after_adoption_is_decided_and_does_not_hold_the_queue(identity, monkeypatch, tmp_path):
    worker, store, config, account, sent, target, wrapped, model = learned_and_queued(identity, monkeypatch, tmp_path)
    asyncio.run(worker.worker().sweep())

    async def resend():
        async with client(wrapped, config, account) as http:
            return await decide(http, worker, sent, {"id": "hn-1", "values": {"text": "yes story 1"}})

    _, receipt = asyncio.run(resend())
    assert receipt.status_code == 200, receipt.text
    assert not any(job["status"] == "running" for job in store.jobs(target.run_id))


def test_an_adoption_that_cannot_finish_ends_with_a_receipt_and_frees_the_run(identity, monkeypatch, tmp_path, capsys):
    worker, store, _, account, _, target, *_ = learned_and_queued(identity, monkeypatch, tmp_path)
    from . import hosted_publications

    def broken(*args, **kwargs):
        raise hosted_publications.IterationStale("another iteration was adopted")

    monkeypatch.setattr(hosted_publications, "read_iteration", broken)
    asyncio.run(worker.worker().sweep())
    assert not any(job["status"] == "running" for job in store.jobs(target.run_id))
    assert "iteration_adoption_failed IterationStale" in capsys.readouterr().out
    check_run(worker, target, Ecs(), SETTINGS, now=1e12)
    state = RetrainingState(worker, account, target.run_id).read().value
    assert state["pending"] is None and state["failures"] == 1 and state["started"] == 1e12


def test_a_task_lost_before_queueing_its_result_is_queued_by_the_next_check_once(identity, monkeypatch, tmp_path):
    worker, store, _, account, _, target, *_ = learned_and_queued(identity, monkeypatch, tmp_path)
    state = RetrainingState(worker, account, target.run_id)
    job = state.read().value["pending"]["jobId"]
    state.change(lambda value: value["pending"].update(jobId=None))
    check_run(worker, target, Ecs(), SETTINGS)
    assert state.read().value["pending"]["jobId"] == job
    assert sum(row["id"] == job for row in store.jobs(target.run_id)) == 1


def test_the_next_iteration_starts_from_the_live_store_even_if_the_state_is_behind(identity, monkeypatch, tmp_path):
    worker, store, _, account, _, target, *_ = learned_and_queued(identity, monkeypatch, tmp_path)
    asyncio.run(worker.worker().sweep())
    from .retraining import live_store

    state = RetrainingState(worker, account, target.run_id).read().value
    _, parent, live = live_store(worker, account, store.run(target.run_id)["config"])
    assert parent == state["pending"]["iteration"]["id"]
    assert live["alpha"]["head"] is not None


def test_votes_on_decisions_made_before_filing_by_item_still_start_a_task(identity, monkeypatch):
    monkeypatch.setattr(PublicationCommandRuntime, "record_decided", lambda *args: None)
    worker, _, _, account, _, target, _, _ = prepared(identity, monkeypatch, 24)
    ecs = Ecs()
    check = check_run(worker, target, ecs, SETTINGS)
    assert check["started"] and check["due"] == ["alpha"]
    rows = RetrainingState(worker, account, target.run_id).read().value["votes"]["alpha"]
    assert all(row["decided"] and row["lookedUp"] for row in rows.values())


def test_no_task_starts_until_the_lookup_of_older_decisions_has_finished(identity, monkeypatch):
    monkeypatch.setattr(PublicationCommandRuntime, "record_decided", lambda *args: None)
    worker, *_, target, _, _ = prepared(identity, monkeypatch, 24)
    ecs = Ecs()
    waiting = check_run(worker, target, ecs, SETTINGS, deadline=float("-inf"))
    assert waiting["lookingUpOlderDecisions"] and not waiting["started"] and ecs.started == []
    assert check_run(worker, target, ecs, SETTINGS)["started"]


def decision_job(item, label, *, revision=1):
    decision = {"classifiers": {"alpha": {"label": label, "confidence": 0.9}}}
    response = {"publications": {"alpha": {"decision": decision, "source": {"revision": revision}}}}
    return {"id": f"{item}-{label}", "status": "completed", "kind": "predict", "payload": {"item": {"id": item}}}, response


class Jobs:
    """A run's job records, oldest first, counting how often they are walked."""

    def __init__(self, *jobs):
        self.jobs, self.walks = [job for job, _ in jobs], 0
        self.responses = {job["id"]: {"response": response} for job, response in jobs}

    def run_job_count(self, run_id):
        self.walks += 1
        return len(self.jobs)

    def run_job_at(self, run_id, position, *, results):
        return self.jobs[position - 1]

    def job_result(self, job_id):
        return self.responses[job_id]


class Journal:
    def __init__(self, votes):
        self.value = {"votes": {"alpha": votes}, "lookup": None}

    def read(self):
        return SimpleNamespace(value=self.value)

    def change(self, apply):
        apply(self.value)


def awaiting(*items, revision=1):
    return {item: {"revision": revision, "label": "include", "decided": None, "lookedUp": False} for item in items}


def test_an_items_first_decision_is_the_one_that_stands(monkeypatch):
    monkeypatch.setattr("decision_flywheel.retraining.is_decision", lambda kind: True)
    state = Journal(awaiting("a"))
    look_up_older_decisions(Jobs(decision_job("a", "exclude"), decision_job("a", "include")), "run", state)
    assert state.value["votes"]["alpha"]["a"]["decided"]["label"] == "exclude"


def test_a_decision_of_another_revision_does_not_answer_for_the_vote(monkeypatch):
    monkeypatch.setattr("decision_flywheel.retraining.is_decision", lambda kind: True)
    state = Journal(awaiting("a", revision=2))
    look_up_older_decisions(Jobs(decision_job("a", "exclude", revision=1)), "run", state)
    row = state.value["votes"]["alpha"]["a"]
    assert row["decided"] is None and row["lookedUp"]


def test_an_item_with_no_decision_on_record_is_looked_up_only_once():
    jobs, state = Jobs(), Journal(awaiting("gone"))
    look_up_older_decisions(jobs, "run", state)
    look_up_older_decisions(jobs, "run", state)
    assert jobs.walks == 1


def test_a_lookup_that_runs_out_of_time_carries_on_where_it_stopped(monkeypatch):
    monkeypatch.setattr("decision_flywheel.retraining.is_decision", lambda kind: True)
    jobs, state = Jobs(decision_job("a", "exclude"), decision_job("b", "exclude")), Journal(awaiting("a", "b"))
    ticks = iter([0, 1])
    look_up_older_decisions(jobs, "run", state, deadline=1, clock=lambda: next(ticks))
    assert state.value["lookup"] == {"items": ["a", "b"], "position": 1}
    assert state.value["votes"]["alpha"]["b"]["decided"] and not state.value["votes"]["alpha"]["a"]["decided"]
    look_up_older_decisions(jobs, "run", state)
    assert state.value["lookup"] is None and jobs.walks == 1
    assert state.value["votes"]["alpha"]["a"]["decided"]


def test_a_changed_vote_keeps_the_decision_that_was_looked_up():
    class Store:
        def events(self, run_id, *, after, limit):
            row = {
                "sequence": 1,
                "payload": {"kind": "publication-machine-feedback", "output": "alpha", "itemId": "a", "itemRevision": 1},
            }
            return [row] if after == 0 else []

        def item_labels(self, list_id, item_id, revision):
            return [{"classifier_id": "alpha", "sequence": 2, "label": "exclude", "comment": None}]

        def item_results(self, list_id, item_id, revision):
            return []

    decided = {"label": "include", "confidence": 0.9}
    state = {"cursor": 0, "votes": {"alpha": {"a": {"revision": 1, "label": "include", "decided": decided, "lookedUp": True}}}}
    row = refresh_votes(Store(), "run", "list", state)["votes"]["alpha"]["a"]
    assert row["label"] == "exclude" and row["decided"] == decided and row["lookedUp"]


def test_a_single_publication_decision_is_read_like_a_shared_one():
    decision = {"classifiers": {"alpha": {"label": "include"}}}
    assert job_decisions({"decision": decision, "source": {"revision": 3}}) == (
        3,
        {"alpha": {"label": "include", "confidence": None}},
    )
