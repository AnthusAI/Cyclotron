"""The decision-ready doorbell: shape, silence when unconfigured, and never harming the job."""

import json
from uuid import uuid4

import pytest

from .decision_events import DecisionEventNotifier
from .machine_access import MachineActor

BUS = "arn:aws:events:us-east-1:111111111111:event-bus/papyrus"
ROLE = "arn:aws:iam::222222222222:role/synthetic-invoker"


class FakeEvents:
    def __init__(self, response=None, error=None):
        self.calls, self.response, self.error = [], response or {"FailedEntryCount": 0, "Entries": []}, error

    def put_events(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def machine_job(kind="publication", payload=None):
    account, run = str(uuid4()), str(uuid4())
    actor = MachineActor(ROLE, "papyrus-monitor", account, run, "binding")
    payload = payload or {"bindings": {"gamma": "r1", "alpha": "r1"}, "item": {"id": "hn-1", "values": {"text": "x"}}}
    return {"id": str(uuid4()), "run_id": run, "kind": kind, "request_id": "machine:req-0001", "payload": payload}, actor


def test_a_completed_machine_decision_sends_one_doorbell_with_the_agreed_shape():
    client, logs = FakeEvents(), []
    job, actor = machine_job()
    DecisionEventNotifier(client, BUS, log=logs.append).notify(job, actor, "completed")
    assert len(client.calls) == 1 and logs == []
    (entry,) = client.calls[0]["Entries"]
    assert {k: v for k, v in entry.items() if k != "Detail"} == {
        "EventBusName": BUS,
        "Source": "cyclotron.lab",
        "DetailType": "Decision Ready",
    }
    assert json.loads(entry["Detail"]) == {
        "contractVersion": "cyclotron-publication/v1",
        "accountId": actor.account_id,
        "runId": job["run_id"],
        "jobId": job["id"],
        "requestId": "req-0001",
        "itemId": "hn-1",
        "publications": ["alpha", "gamma"],
        "status": "complete",
    }


def test_a_failed_machine_decision_sends_status_failed_without_any_decision_content():
    client = FakeEvents()
    job, actor = machine_job()
    DecisionEventNotifier(client, BUS).notify(job, actor, "failed")
    detail = json.loads(client.calls[0]["Entries"][0]["Detail"])
    assert detail["status"] == "failed" and '"text"' not in json.dumps(detail)


def test_nothing_is_sent_and_no_client_is_created_when_the_bus_is_unconfigured():
    def forbidden():
        raise AssertionError("client created")

    for environment in ({}, {"CYCLOTRON_DECISION_EVENT_BUS_ARN": ""}, {"CYCLOTRON_DECISION_EVENT_BUS_ARN": "  "}):
        assert DecisionEventNotifier.from_environment(environment, forbidden) is None
    client = FakeEvents()
    notifier = DecisionEventNotifier.from_environment({"CYCLOTRON_DECISION_EVENT_BUS_ARN": BUS}, lambda: client)
    assert notifier.bus_arn == BUS and notifier.client is client


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("prepare", {}),
        ("vote", {"label": "x"}),
        ("publication", {"publication": "alpha", "bindingRevision": "r1", "item": {"id": "a", "values": {}}}),
    ],
)
def test_non_decision_commands_send_nothing(kind, payload):
    client = FakeEvents()
    job, actor = machine_job(kind, payload)
    DecisionEventNotifier(client, BUS).notify(job, actor, "completed")
    assert client.calls == []


def test_a_human_actor_or_a_non_terminal_status_sends_nothing():
    client = FakeEvents()
    job, actor = machine_job()
    notifier = DecisionEventNotifier(client, BUS)
    notifier.notify(job, object(), "completed")
    notifier.notify(job, actor, "running")
    assert client.calls == []


def test_a_client_error_is_swallowed_and_logged_by_name_only():
    logs = []
    job, actor = machine_job()
    broken = FakeEvents(error=RuntimeError("secret-token-123"))
    DecisionEventNotifier(broken, BUS, log=logs.append).notify(job, actor, "completed")
    assert len(logs) == 1 and "RuntimeError" in logs[0] and "secret-token-123" not in logs[0]


def test_failed_entries_are_logged_and_do_not_raise():
    logs = []
    job, actor = machine_job()
    client = FakeEvents(response={"FailedEntryCount": 1, "Entries": [{"ErrorCode": "x"}]})
    DecisionEventNotifier(client, BUS, log=logs.append).notify(job, actor, "completed")
    assert json.loads(logs[0]) == {"event": "decision_event_failed_entries", "failedEntryCount": 1}
