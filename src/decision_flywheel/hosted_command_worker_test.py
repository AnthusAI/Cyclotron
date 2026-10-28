"""One fenced cloud invocation executes existing console commands without files."""

import asyncio
from uuid import uuid4

import pytest

from .cyclotron_runtime import CyclotronRuntime
from .optimizer_agent import DisabledOptimizer
from .prospectus_accounts import AccountAccessDenied, ProspectusUnavailable
from .queued_account_authorization_test import fixture as authentication
from .s3_journal import S3Journal
from .s3_journal_test import journal
from .s3_workspace_storage import S3WorkspaceStores
from .s3_workspace_storage_test import Model, fixture


def setup():
    from .hosted_command_worker import AuthorizedModel, CommandEventSink, HostedCommandWorker

    s, run = fixture()
    auth, sessions, cookie, original, state = authentication()
    actor = asyncio.run(auth.capture(sessions.get(cookie), original.account_id, run["id"], original.binding))
    model = Model()
    client = s.journal.payloads.client

    def make(domain, name):
        base = journal(client, name=name)
        return S3Journal(base.payloads, domain, name)

    def runtime(view, guard, check):
        return CyclotronRuntime(
            view,
            None,
            model_factory=lambda config: (AuthorizedModel(model, guard), DisabledOptimizer()),
            sink_factory=lambda rid: CommandEventSink(view, rid, check),
            storage_factory=lambda rid: S3WorkspaceStores(make("workspace", rid), journal_for=make),
            execution_guard=guard,
        )

    worker = HostedCommandWorker(lambda account: s, auth, runtime, allow_live=True)
    return s, run, auth, sessions, cookie, actor, state, model, worker


def test_prepare_label_reopen_and_duplicate_delivery_use_existing_runtime_without_files(monkeypatch):
    s, run, auth, sessions, cookie, actor, state, model, worker = setup()

    def forbidden(*args, **kwargs):
        raise AssertionError("hosted worker opened SQLite or local files")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("pathlib.Path.mkdir", forbidden)
    job = s.enqueue_authorized_command(run["id"], "prepare", "prepare", {}, actor)
    message = {"accountId": actor.account_id, "runId": run["id"], "jobId": job["id"]}
    assert asyncio.run(worker.process(message))["status"] == "completed"
    assert asyncio.run(worker.process(message))["status"] == "completed" and model.calls == 1
    shown = s.current_item(run["id"])
    job = s.enqueue_authorized_command(
        run["id"],
        "vote",
        "label",
        {
            "item_id": "one",
            "presentation_id": shown["prediction"]["presentation_id"],
            "labels": [{"classifier_id": "topic", "label": "science", "comment": "Exact human rationale 雪"}],
        },
        actor,
    )
    assert asyncio.run(worker.process({**message, "jobId": job["id"]}))["status"] == "completed"
    assert s.item_labels("papers", "one", 1)[0]["comment"] == "Exact human rationale 雪"
    assert model.calls == 1 and s.current_item(run["id"]) is None


@pytest.mark.parametrize("change", ["logout", "person", "binding", "membership", "outage"])
def test_revocation_before_claim_cannot_dispatch_or_open_engine(change):
    s, run, auth, sessions, cookie, actor, state, model, worker = setup()
    job = s.enqueue_authorized_command(run["id"], "once", "prepare", {}, actor)
    if change == "logout":
        sessions.revoke(cookie)
    elif change == "person":
        state["person"] = str(uuid4())
    elif change == "binding":
        state["binding"] = "changed"
    elif change == "membership":
        state["denied"] = True
    elif change == "outage":
        state["person"] = ProspectusUnavailable("offline")
    before = s.journal.read().revision
    with pytest.raises((AccountAccessDenied, ProspectusUnavailable)):
        asyncio.run(worker.process({"accountId": actor.account_id, "runId": run["id"], "jobId": job["id"]}))
    assert model.calls == 0 and s.journal.read().revision == before
    assert s.jobs(run["id"])[0]["status"] == "pending"


def test_changed_delivery_account_run_or_extra_authority_cannot_retarget_job():
    s, run, auth, sessions, cookie, actor, state, model, worker = setup()
    job = s.enqueue_authorized_command(run["id"], "once", "prepare", {}, actor)
    message = {"accountId": actor.account_id, "runId": run["id"], "jobId": job["id"]}
    for changed in ({"accountId": str(uuid4())}, {"runId": str(uuid4())}, {"personId": actor.person_id}):
        with pytest.raises((ValueError, AccountAccessDenied)):
            asyncio.run(worker.process({**message, **changed}))
    assert model.calls == 0 and s.jobs(run["id"])[0]["status"] == "pending"


def test_revocation_after_claim_cannot_open_runtime_or_publish_a_prediction():
    s, run, auth, sessions, cookie, actor, state, model, worker = setup()
    job = s.enqueue_authorized_command(run["id"], "once", "prepare", {}, actor)
    claim = s.claim_command

    def revoked(**kwargs):
        result = claim(**kwargs)
        state["denied"] = True
        return result

    s.claim_command = revoked
    with pytest.raises(AccountAccessDenied):
        asyncio.run(worker.process({"accountId": actor.account_id, "runId": run["id"], "jobId": job["id"]}))
    assert model.calls == 0 and s.current_item(run["id"]) is None
    assert s.command_recovery(job["id"])["status"] == "running"


def test_provider_outcome_is_retained_but_revocation_prevents_business_state_and_automatic_retry():
    from .hosted_command_worker import CommandReconciliationRequired

    s, run, auth, sessions, cookie, actor, state, model, worker = setup()
    job = s.enqueue_authorized_command(run["id"], "once", "prepare", {}, actor)
    provider = model.classify_many

    async def revoked(*args, **kwargs):
        result = await provider(*args, **kwargs)
        state["denied"] = True
        return result

    model.classify_many = revoked
    message = {"accountId": actor.account_id, "runId": run["id"], "jobId": job["id"]}
    with pytest.raises(AccountAccessDenied):
        asyncio.run(worker.process(message))
    assert model.calls == 1 and s.current_item(run["id"]) is None
    state["denied"] = False
    with pytest.raises(CommandReconciliationRequired):
        asyncio.run(worker.process(message))
    assert model.calls == 1 and s.command_recovery(job["id"])["automaticTakeover"] is False


def test_unknown_claim_receipt_cannot_be_recovered_by_redelivery():
    from .hosted_command_worker import CommandReconciliationRequired
    from .s3_journal import JournalUncertain

    s, run, auth, sessions, cookie, actor, state, model, worker = setup()
    job = s.enqueue_authorized_command(run["id"], "once", "prepare", {}, actor)
    s.journal.payloads.client.lose = lambda key: key == s.journal.head_key
    message = {"accountId": actor.account_id, "runId": run["id"], "jobId": job["id"]}
    with pytest.raises(JournalUncertain):
        asyncio.run(worker.process(message))
    with pytest.raises(CommandReconciliationRequired):
        asyncio.run(worker.process(message))
    assert model.calls == 0


def test_a_command_event_sink_holds_500_events_and_appends_them_as_one_batch():
    from .hosted_command_worker import CommandEventSink

    appended = []

    class View:
        def append_batch(self, run_id, rows):
            appended.append((run_id, rows))

    sink = CommandEventSink(View(), "run", lambda: None, batch_size=500)
    for n in range(500):
        sink.ingest(f"engine:{n}", {"n": n})
    assert len(appended) == 1 and [event["n"] for _, event in appended[0][1]] == list(range(500))


class Opened:
    def __init__(self, name, fail_close=False):
        self.name, self.closed, self.fail_close = name, 0, fail_close

    def close(self):
        self.closed += 1
        if self.fail_close:
            raise OSError("synthetic release failure")


class Busy(RuntimeError):
    pass


def refuse(error):
    def open_engine():
        raise error

    return open_engine


def test_a_held_engine_is_reused_for_an_equal_key_and_replaced_for_any_other():
    from .hosted_command_worker import HeldEngine

    holder, made = HeldEngine(), []

    def opener(name):
        def open_engine():
            made.append(Opened(name))
            return made[-1]

        return open_engine

    first = asyncio.run(holder.take(("run", "config-1"), opener("one")))
    assert asyncio.run(holder.take(("run", "config-1"), opener("unused"))) is first
    second = asyncio.run(holder.take(("run", "config-2"), opener("two")))
    assert [engine.name for engine in made] == ["one", "two"] and first.closed == 1 and second.closed == 0
    holder.close()
    assert second.closed == 1 and holder.opened == 2
    holder.close()  # Nothing held: nothing to release.
    assert second.closed == 1


def test_discarding_after_a_failure_releases_the_engine_and_keeps_the_original_error(capsys):
    from .hosted_command_worker import HeldEngine

    holder = HeldEngine()
    engine = asyncio.run(holder.take("key", lambda: Opened("one", fail_close=True)))
    holder.discard()
    assert engine.closed == 1 and holder.held is None
    assert capsys.readouterr().out.strip() == "held_engine_release_failed OSError"  # class only
    asyncio.run(holder.take("key", lambda: Opened("again")))
    assert holder.opened == 2  # the next job opens fresh


def test_an_engine_held_by_another_worker_is_waited_for_only_while_there_is_time():
    from .hosted_command_worker import HeldEngine

    attempts, pauses = [], []

    def open_engine():
        attempts.append(1)
        if len(attempts) < 3:
            raise Busy("owned by the worker that ran the job before")
        return Opened("free")

    async def pause(seconds):
        pauses.append(seconds)

    holder = HeldEngine(wait_while=lambda: True, sleep=pause)
    assert asyncio.run(holder.take("key", open_engine, busy=(Busy,))).name == "free"
    assert len(attempts) == 3 and len(pauses) == 2
    with pytest.raises(Busy):  # Out of time: the ownership error is raised as before.
        asyncio.run(HeldEngine(wait_while=lambda: False, sleep=pause).take("key", refuse(Busy()), busy=(Busy,)))
    with pytest.raises(Busy):  # No deadline known: never wait.
        asyncio.run(HeldEngine(sleep=pause).take("key", refuse(Busy()), busy=(Busy,)))
    with pytest.raises(ValueError):  # Only the named ownership errors are waited for.
        asyncio.run(HeldEngine(wait_while=lambda: True, sleep=pause).take("key", refuse(ValueError()), busy=(Busy,)))
    assert len(pauses) == 2


def test_between_jobs_a_held_engines_model_refuses_every_call():
    from .hosted_command_worker import AuthorizedModel, no_job_guard

    class Provider:
        calls = 0

        async def classify_many(self, *args, **kwargs):
            Provider.calls += 1

    bound = AuthorizedModel(Provider(), no_job_guard)
    with pytest.raises(PermissionError):
        asyncio.run(bound.classify_many())
    assert Provider.calls == 0
