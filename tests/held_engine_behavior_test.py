"""One worker invocation keeps a standing target's engine open across its jobs; every job is still fenced."""

import asyncio

import pytest

from decision_flywheel import account_sessions_test
from decision_flywheel.embedded_cyclotron import Cyclotron
from decision_flywheel.hosted_machine_publications_test import client, decisions_body, fixture, new_item, revisions
from decision_flywheel.hosted_publications_test import PUBS
from decision_flywheel.s3_runtime_storage import S3RuntimeStores

identity = account_sessions_test.identity
BINDINGS = revisions("alpha", "beta", "gamma")


def ownership_counts(monkeypatch):
    """How many output runtimes were acquired and released."""
    counts = {"acquired": 0, "released": 0}
    acquire, close = S3RuntimeStores.acquire, S3RuntimeStores.close

    def counted_acquire(self, owner, **kwargs):
        result = acquire(self, owner, **kwargs)
        counts["acquired"] += 1
        return result

    def counted_close(self):
        held = self.authority is not None
        close(self)
        counts["released"] += held

    monkeypatch.setattr(S3RuntimeStores, "acquire", counted_acquire)
    monkeypatch.setattr(S3RuntimeStores, "close", counted_close)
    return counts


async def accept(http, sent, names):
    """Accept one decision per name; return the first job's wake-up only.

    The later wake-ups reached workers while the first job ran and found their jobs queued, so the jobs behind
    the first run in the first job's invocation.
    """
    jobs = []
    for name in names:
        posted = await http.post("/machine/decisions", json=decisions_body(BINDINGS, new_item(name), request_id="req-" + name))
        assert posted.status_code == 202, posted.text
        jobs.append(posted.json()["jobId"])
    first = sent[0]
    sent.clear()
    return first, jobs


def test_jobs_drained_in_one_invocation_reuse_one_engine_with_one_model_call_per_item(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    counts = ownership_counts(monkeypatch)
    opened = []
    real_init = Cyclotron.__init__

    def counted_init(self, *args, **kwargs):
        opened.append(1)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(Cyclotron, "__init__", counted_init)

    async def go():
        async with client(wrapped, config, account) as http:
            first, jobs = await accept(http, sent, ["kept-1", "kept-2", "kept-3"])
            result = await worker.worker().process(first)
            assert result["status"] == "completed" and result["jobId"] == jobs[-1]
            receipts = [(await http.get(f"/machine/decisions/{job}")).json() for job in jobs]
            assert [list(r["publications"]) for r in receipts] == [list(BINDINGS)] * 3  # output order kept
            assert len({r["decisionId"] for r in receipts}) == 3
            # A separate invocation opens its own engine and releases it again.
            first, _ = await accept(http, sent, ["separate-1"])
            await worker.worker().process(first)

    asyncio.run(go())
    assert len(opened) == 2
    assert counts == {"acquired": 2 * len(PUBS), "released": 2 * len(PUBS)}  # each output: once per invocation
    assert len(model.calls) == 4 and all(set(call) == set(PUBS) for call in model.calls)  # one call per item


def fence_out(store, run_id, how, worker):
    """An operator reconciles the running job, or the caller's machine authority is withdrawn."""
    if how == "reconciled":
        running = next(job for job in store.jobs(run_id) if job["status"] == "running")
        claim = store.command_recovery(running["id"])
        store.reconcile_command(
            running["id"],
            owner=claim["owner"],
            fence=claim["fence"],
            status="failed",
            reason="operator takes the job over",
            operator="test",
            apply=True,
        )
    else:
        worker.machine_principals = ()


@pytest.mark.parametrize("how", ["reconciled", "revoked"])
def test_a_fenced_out_job_releases_the_held_engine_before_any_model_call_and_the_next_job_opens_fresh(identity, monkeypatch, how):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    run_id = targets[0].run_id
    counts = ownership_counts(monkeypatch)
    decided = []
    real_decide = Cyclotron.decide

    async def decide(self, item, **kwargs):
        decided.append(item.id)
        if item.id == "fenced-2":
            fence_out(store, run_id, how, worker)  # While job 2 holds the engine kept from job 1.
        return await real_decide(self, item, **kwargs)

    monkeypatch.setattr(Cyclotron, "decide", decide)
    principals = worker.machine_principals

    async def go():
        async with client(wrapped, config, account) as http:
            first, jobs = await accept(http, sent, ["fenced-1", "fenced-2", "fenced-3"])
            with pytest.raises(Exception) as denied:
                await worker.worker().process(first)
            assert type(denied.value).__name__ in ("ValueError", "MachineDenied")
            assert decided == ["fenced-1", "fenced-2"]
            assert len(model.calls) == 1  # job 2 was fenced out before its paid call
            assert counts["released"] == counts["acquired"] == len(PUBS)  # nothing is left owned
            assert (await http.get(f"/machine/decisions/{jobs[0]}")).status_code == 200
            if how == "revoked":
                worker.machine_principals = principals
                fence_out(store, run_id, "reconciled", worker)  # The operator ends the job left running.
            result = await worker.worker().process({"accountId": account, "runId": run_id, "jobId": jobs[2]})
            assert result == {"status": "completed", "jobId": jobs[2]}
            assert (await http.get(f"/machine/decisions/{jobs[2]}")).status_code == 200

    asyncio.run(go())
    assert counts == {"acquired": 2 * len(PUBS), "released": 2 * len(PUBS)}
    assert len(model.calls) == 2


def test_a_worker_that_loses_the_next_jobs_claim_releases_its_held_engine_at_once(identity, monkeypatch):
    from uuid import uuid4

    from decision_flywheel.hosted_command_worker import CommandReconciliationRequired
    from decision_flywheel.s3_console_storage import S3ConsoleStorage

    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    counts = ownership_counts(monkeypatch)
    claims = []
    real_claim = S3ConsoleStorage.claim_command

    def claim(self, *, owner=None, job_id=None):
        claims.append(job_id)
        if len(claims) == 2:
            real_claim(self, owner=str(uuid4()), job_id=job_id)  # Another worker's delivery claims it first.
        return real_claim(self, owner=owner, job_id=job_id)

    monkeypatch.setattr(S3ConsoleStorage, "claim_command", claim)

    async def go():
        async with client(wrapped, config, account) as http:
            first, _ = await accept(http, sent, ["raced-1", "raced-2"])
            with pytest.raises(CommandReconciliationRequired):
                await worker.worker().process(first)

    asyncio.run(go())
    # The other worker's open of this engine finds it free (or waits a moment while it is released).
    assert counts == {"acquired": len(PUBS), "released": len(PUBS)}
    assert len(model.calls) == 1


def test_a_decision_job_checks_its_claim_only_where_it_matters(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    checks = []
    real_check = worker.authorization.check

    def counted(actor):
        checks.append(actor)
        return real_check(actor)

    worker.authorization.check = counted

    async def go():
        async with client(wrapped, config, account) as http:
            first, _ = await accept(http, sent, ["checked-1"])
            await worker.worker().process(first)

    asyncio.run(go())
    # Each check reads the claim fence and authorization fresh: once the claim is confirmed, before registering
    # the item, before the decision's writes, before the paid model call, before the snapshots and the flush
    # that ends the engine's writes, at the end, and before the receipt.
    assert len(checks) == 7
    assert len(model.calls) == 1
