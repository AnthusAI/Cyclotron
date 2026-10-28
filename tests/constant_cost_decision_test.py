"""A decision reads the same storage whether it is the tenth or the hundredth: no history scans."""

import asyncio
import collections

from decision_flywheel import account_sessions_test
from decision_flywheel.embedded_cyclotron import Cyclotron
from decision_flywheel.embedded_cyclotron_test import DEFINITION, Model, item
from decision_flywheel.hosted_machine_publications_test import client, decisions_body, drain, fixture, new_item, revisions
from decision_flywheel.s3_embedded_storage import S3CyclotronStores
from decision_flywheel.s3_journal_test import ConditionalS3, journal

identity = account_sessions_test.identity


def _engine(client, model):
    stores = S3CyclotronStores(
        journal(client, name="root"),
        journal_for=lambda domain, name: journal(client, name=domain + "-" + name),
    )
    return Cyclotron.open(None, DEFINITION, model, storage=stores, max_requests=None)


def _reads_of_one_decision(client, model, identifier):
    """Open a fresh engine and decide one item, as the hosted worker does per job."""
    before = len(client.calls)
    with _engine(client, model) as engine:
        asyncio.run(engine.decide(item(identifier)))
    return collections.Counter(
        "event-record" if "/records/runtime/" in request["Key"] else "other"
        for kind, request in client.calls[before:]
        if kind == "get"
    )


def test_a_decision_after_a_long_history_reads_no_more_than_one_after_a_short_one():
    client, model = ConditionalS3(), Model()
    for number in range(10):
        _reads_of_one_decision(client, model, f"early-{number}")
    early = _reads_of_one_decision(client, model, "probe-early")
    for number in range(60):
        _reads_of_one_decision(client, model, f"later-{number}")
    late = _reads_of_one_decision(client, model, "probe-late")
    assert late == early


def test_a_decision_never_reads_the_event_history():
    client, model = ConditionalS3(), Model()
    for number in range(20):
        _reads_of_one_decision(client, model, f"history-{number}")
    assert _reads_of_one_decision(client, model, "probe")["event-record"] == 0


def test_a_hosted_decision_for_several_publications_reads_no_event_history_after_many_decisions(identity, monkeypatch, capsys):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def decide_all(name):
        async with client(wrapped, config, account) as http:
            body = decisions_body(revisions("alpha", "beta", "gamma"), new_item(name), request_id="req-" + name)
            assert (await http.post("/machine/decisions", json=body)).status_code == 202
            await drain(worker, sent)

    async def go():
        for number in range(15):
            await decide_all(f"history-{number}")
        capsys.readouterr()
        await decide_all("probe")

    asyncio.run(go())
    assert "runtime_history_read" not in capsys.readouterr().out
