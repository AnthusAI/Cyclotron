"""A run accepts every command and runs them one at a time, in the order they were accepted."""

import asyncio
from uuid import uuid4

import httpx
import pytest

from . import account_sessions_test
from .command_recovery import open_store
from .hosted_command_worker_test import setup as worker_setup
from .hosted_machine_publications_test import body, client, fixture, new_item
from .hosted_publications_test import ITEM
from .hosted_publications_test import setup as publication_setup
from .s3_journal_test import ConditionalS3
from .storage_errors import CommandNotReady

identity = account_sessions_test.identity
PAYLOAD = {"publication": "anth-us", "bindingRevision": "1", "item": {"id": "hn-1"}}


def store_with_run():
    s = open_store(ConditionalS3(), "synthetic-bucket", "111111111111", "account-a")
    s.initialize()
    return s, s.create_run("Live", "live", {})


def test_a_long_queue_is_all_accepted_and_all_completes_in_acceptance_order():
    s, run = store_with_run()
    accepted = [s.command(run["id"], f"req-{n:04d}", "publication", PAYLOAD)["id"] for n in range(200)]
    assert len(set(accepted)) == 200 and s.queue_state(accepted[199])["position"] == 199
    finished = []
    while job := s.claim_command(owner=str(uuid4())):
        s.finish_command(job["id"], "completed", {}, owner=job["claim_owner"], fence=job["claim_fence"])
        finished.append(job["id"])
    assert finished == accepted


def test_posting_the_same_request_again_returns_the_same_job_even_while_others_wait():
    s, run = store_with_run()
    first = s.command(run["id"], "req-1", "publication", PAYLOAD)
    s.command(run["id"], "req-2", "publication", PAYLOAD)
    assert s.command(run["id"], "req-1", "publication", PAYLOAD)["id"] == first["id"]
    assert len(s.jobs(run["id"])) == 2


def test_only_the_head_of_a_run_may_start_and_only_after_the_running_job_finishes():
    s, run = store_with_run()
    other = s.create_run("Other", "live", {})
    first, second = (s.command(run["id"], f"req-{n}", "publication", PAYLOAD) for n in (1, 2))
    elsewhere = s.command(other["id"], "req-1", "publication", PAYLOAD)
    assert [s.queue_state(j["id"])["position"] for j in (first, second)] == [0, 1]
    with pytest.raises(CommandNotReady):
        s.claim_command(owner=str(uuid4()), job_id=second["id"])
    head = s.claim_command(owner=str(uuid4()), job_id=first["id"])
    with pytest.raises(CommandNotReady):  # the head is running: the next job still waits
        s.claim_command(owner=str(uuid4()), job_id=second["id"])
    assert s.queue_state(second["id"]) == {"status": "pending", "position": 1, "queued": True, "ready": False}
    assert s.claim_command(owner=str(uuid4()), job_id=elsewhere["id"])["id"] == elsewhere["id"]  # other runs are independent
    s.finish_command(first["id"], "completed", {}, owner=head["claim_owner"], fence=head["claim_fence"])
    assert s.claim_command(owner=str(uuid4()), job_id=second["id"])["id"] == second["id"]


def test_a_crash_mid_job_blocks_the_queue_visibly_and_an_operator_reconcile_releases_it(monkeypatch):
    s, run = store_with_run()
    first = s.command(run["id"], "req-1", "publication", PAYLOAD)
    second = s.command(run["id"], "req-2", "publication", PAYLOAD)
    claim = s.claim_command(owner=str(uuid4()), job_id=first["id"])  # the worker dies here: never finished
    assert s.command_recovery(first["id"])["stuck"] is False
    monkeypatch.setattr("decision_flywheel.s3_console_storage.STUCK_HEAD_MINUTES", -1)
    seen = s.command_recovery(first["id"])
    assert seen["stuck"] is True and seen["blockedJobs"] == 1 and seen["claimedAt"]
    assert [row["jobId"] for row in seen["runQueue"]] == [first["id"], second["id"]]
    s.reconcile_command(
        first["id"],
        owner=claim["claim_owner"],
        fence=claim["claim_fence"],
        status="interrupted",
        reason="worker crashed mid job",
        operator="arn:aws:iam::1:user/op",
        apply=True,
    )
    assert s.claim_command(owner=str(uuid4()), job_id=second["id"])["id"] == second["id"]


def test_a_waiting_job_is_settled_as_queued_without_a_send_and_runs_when_the_head_finishes():
    s, run, auth, sessions, cookie, actor, state, model, worker = worker_setup()
    first = s.enqueue_authorized_command(run["id"], "first", "prepare", {}, actor)
    second = s.enqueue_authorized_command(run["id"], "second", "prepare", {}, actor)
    refs = {"accountId": actor.account_id, "runId": run["id"]}
    assert not hasattr(worker, "wake")  # the worker has no way to write to its own queue
    assert asyncio.run(worker.deliver({**refs, "jobId": second["id"]})) == {"status": "queued", "jobId": second["id"]}
    assert s.queue_state(second["id"])["queued"] and model.calls == 0
    assert asyncio.run(worker.deliver({**refs, "jobId": first["id"]}))["jobId"] == second["id"]  # drained behind it
    assert [j["status"] for j in s.jobs(run["id"])] == ["completed", "completed"]


def test_a_retired_resend_with_an_attempt_count_still_delivers_its_job():
    s, run, auth, sessions, cookie, actor, state, model, worker = worker_setup()
    job = s.enqueue_authorized_command(run["id"], "only", "prepare", {}, actor)
    message = {"accountId": actor.account_id, "runId": run["id"], "jobId": job["id"], "attempt": 7}
    assert asyncio.run(worker.deliver(message))["status"] == "completed"


def test_one_delivery_drains_the_whole_run_in_acceptance_order_when_time_allows():
    s, run, auth, sessions, cookie, actor, state, model, worker = worker_setup()
    jobs = [s.enqueue_authorized_command(run["id"], f"job-{n}", "prepare", {}, actor) for n in range(4)]
    refs = {"accountId": actor.account_id, "runId": run["id"]}
    assert asyncio.run(worker.deliver({**refs, "jobId": jobs[0]["id"]}, lambda: 900))["jobId"] == jobs[-1]["id"]
    assert [j["status"] for j in s.jobs(run["id"])] == ["completed"] * 4
    started = [s.command_recovery(j["id"])["claimedAt"] for j in jobs]
    assert started == sorted(started)


def test_a_delivery_starts_no_further_job_without_room_for_twice_the_last_one():
    s, run, auth, sessions, cookie, actor, state, model, worker = worker_setup()
    ticks = iter([0.0, 80.0])  # the first job takes 80 seconds
    worker.clock = lambda: next(ticks)
    first = s.enqueue_authorized_command(run["id"], "first", "prepare", {}, actor)
    second = s.enqueue_authorized_command(run["id"], "second", "prepare", {}, actor)
    refs = {"accountId": actor.account_id, "runId": run["id"]}
    assert asyncio.run(worker.deliver({**refs, "jobId": first["id"]}, lambda: 159))["jobId"] == first["id"]
    assert s.queue_state(second["id"])["ready"]  # left for the sweep


def test_the_sweep_starts_each_runs_waiting_head_and_leaves_a_running_run_alone():
    s, run, auth, sessions, cookie, actor, state, model, worker = worker_setup()
    first = s.enqueue_authorized_command(run["id"], "first", "prepare", {}, actor)
    s.enqueue_authorized_command(run["id"], "second", "prepare", {}, actor)
    assert s.ready_heads() == [{"id": first["id"], "run_id": run["id"]}]
    assert asyncio.run(worker.sweep(actor.account_id)) == ["completed"]
    assert [j["status"] for j in s.jobs(run["id"])] == ["completed", "completed"] and s.ready_heads() == []
    third = s.enqueue_authorized_command(run["id"], "third", "prepare", {}, actor)
    s.claim_command(owner=str(uuid4()), job_id=third["id"])
    s.enqueue_authorized_command(run["id"], "fourth", "prepare", {}, actor)
    assert s.ready_heads() == [] and asyncio.run(worker.sweep(actor.account_id)) == []


def test_a_failing_head_in_the_sweep_is_reported_by_class_only(capsys):
    s, run, auth, sessions, cookie, actor, state, model, worker = worker_setup()
    s.enqueue_authorized_command(run["id"], "first", "prepare", {}, actor)
    state["denied"] = True
    assert asyncio.run(worker.sweep(actor.account_id)) == ["error"]
    assert "queue_sweep_failed AccountAccessDenied" in capsys.readouterr().out


def test_five_decisions_posted_at_once_are_all_accepted_queued_and_decided_in_posting_order(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            posts = [
                await http.post(
                    "/machine/publications/alpha/operations",
                    json=body("decide", {"bindingRevision": "binding-one", "item": new_item(f"hn-{n}")}, f"req-queue-{n:04d}"),
                )
                for n in range(5)
            ]
            assert [p.status_code for p in posts] == [202] * 5
            jobs = [p.json()["jobId"] for p in posts]
            waiting = await http.get(f"/machine/publications/alpha/commands/{jobs[3]}")
            assert waiting.status_code == 202 and waiting.json()["queued"] is True and waiting.json()["queuePosition"] == 3
            # Deliver the BFF's wake-ups newest first: the queue, not delivery order, decides who runs.
            assert len(sent) == 5
            while sent:
                await worker.worker().process(sent.pop())
            assert sent == []  # the worker sent nothing back to its own queue
            receipts = [await http.get(f"/machine/publications/alpha/commands/{job}") for job in jobs]
            assert [r.status_code for r in receipts] == [200] * 5
            return jobs

    jobs = asyncio.run(go())
    started = [store.command_recovery(job)["claimedAt"] for job in jobs]
    assert started == sorted(started) and len(model.calls) == 5


def test_a_busy_run_still_accepts_a_human_command_and_reports_where_it_waits(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = publication_setup(
        identity, monkeypatch
    )

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.public_origin,
            cookies={"__Host-cyclotron-session": cookie},
            headers={"Origin": config.public_origin, "X-CSRF-Token": session.csrf_token, "X-Cyclotron-Account": account},
        ) as http:
            posts = [
                await http.post(
                    f"/api/publications/{name}/operations",
                    json={
                        "contractVersion": "cyclotron-publication/v1",
                        "operation": "decide",
                        "payload": {"bindingRevision": "binding-one", "item": ITEM},
                    },
                )
                for name in ("alpha", "beta")
            ]
            assert [p.status_code for p in posts] == [202, 202]
            receipt = await http.get(f"/api/publications/beta/commands/{posts[1].json()['jobId']}")
            assert receipt.status_code == 202 and receipt.json()["queued"] is True and receipt.json()["queuePosition"] == 1

    asyncio.run(go())


def test_uncertain_storage_or_an_account_outage_is_503_with_retry_after_and_never_a_busy_refusal(identity, monkeypatch):
    from .prospectus_accounts import ProspectusUnavailable

    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def unavailable(*args, **kwargs):
        raise ProspectusUnavailable("down")

    monkeypatch.setattr("decision_flywheel.hosted_console_service.HostedConsoleService.submit_machine_command", unavailable)

    async def go():
        async with client(wrapped, config, account) as http:
            response = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "binding-one", "item": new_item()}, "req-down-0001"),
            )
            assert response.status_code == 503 and response.headers["Retry-After"] == "5"

    asyncio.run(go())


def test_auto_priming_waits_for_a_busy_run_to_go_idle_before_it_applies(monkeypatch):
    from types import SimpleNamespace

    from . import hosted_curation_routes

    busy = iter([True, True, False])
    polls = []

    class Store:
        def run_has_active_work(self, run_id):
            polls.append(run_id)
            return next(busy)

    class Reached(Exception):
        pass

    def run_config():
        raise Reached

    monkeypatch.setattr(hosted_curation_routes, "PRIME_POLL_SECONDS", 0)
    request = SimpleNamespace(
        runtime=SimpleNamespace(console=lambda account: Store()),
        account="a",
        target=SimpleNamespace(run_id="run-1"),
        run_config=run_config,
    )
    with pytest.raises(Reached):  # the first step after the wait
        asyncio.run(hosted_curation_routes.apply_priming(request))
    assert polls == ["run-1"] * 3


def test_a_reconcile_with_a_queue_url_wakes_the_next_job_at_once():
    from .command_recovery import wake_head

    s, run = store_with_run()
    first = s.command(run["id"], "req-1", "publication", PAYLOAD)
    second = s.command(run["id"], "req-2", "publication", PAYLOAD)
    claim = s.claim_command(owner=str(uuid4()), job_id=first["id"])
    sent = []

    class Sqs:
        def send_message(self, **kwargs):
            sent.append(kwargs)

    assert wake_head(Sqs(), "https://queue", s, "account-a", first["id"]) is None  # head still running
    s.finish_command(first["id"], "failed", {}, owner=claim["claim_owner"], fence=claim["claim_fence"])
    assert wake_head(Sqs(), "https://queue", s, "account-a", first["id"]) == second["id"]
    assert sent[0]["QueueUrl"] == "https://queue" and second["id"] in sent[0]["MessageBody"]


def test_a_long_article_is_queued_with_only_a_reference_in_the_catalog_and_runs_with_its_full_text():
    import json

    def canonical_json_size(value):
        return len(json.dumps(value, separators=(",", ":")))

    s, run = store_with_run()
    body = "paragraph " * 40_000  # about 400 KB
    payload = {**PAYLOAD, "item": {"id": "hn-long", "values": {"title": "T", "body": body, "text": "T\n\n" + body}}}
    before = canonical_json_size(s._read())
    job = s.command(run["id"], "req-long", "publication", payload)
    assert canonical_json_size(s._read()) - before < 4_000  # the catalog grew by a row and a reference, not the text
    assert s.command(run["id"], "req-long", "publication", payload)["id"] == job["id"]  # the same request is the same job
    with pytest.raises(ValueError):
        s.command(run["id"], "req-long", "publication", {**payload, "item": {"id": "hn-long", "values": {"title": "U"}}})
    stored = next(r for r in s._read()["snapshot"]["tables"]["web_jobs"] if r["id"] == job["id"])
    assert body not in stored["payload"] and "valuesRef" in stored["payload"]


def test_a_quick_job_after_a_slow_one_does_not_license_a_third_without_room_for_the_slow_one_twice():
    s, run, auth, sessions, cookie, actor, state, model, worker = worker_setup()
    jobs = [s.enqueue_authorized_command(run["id"], f"job-{n}", "prepare", {}, actor) for n in range(3)]
    process, now = worker.process, [0.0]

    async def timed(message):  # the first job takes 80 seconds, the second 10
        result = await process(message)
        now[0] += 80.0 if message["jobId"] == jobs[0]["id"] else 10.0
        return result

    worker.process, worker.clock = timed, lambda: now[0]
    refs = {"accountId": actor.account_id, "runId": run["id"]}
    remaining = lambda: 250 - now[0]  # noqa: E731
    assert asyncio.run(worker.deliver({**refs, "jobId": jobs[0]["id"]}, remaining))["jobId"] == jobs[1]["id"]
    # 160 s are left after the quick job: room for twice it (20 s), but not twice the slow one (160 s).
    assert s.queue_state(jobs[2]["id"])["ready"]


def test_validating_a_catalog_with_thousands_of_claimed_jobs_stays_linear():
    import time

    s, run = store_with_run()
    value = s._read()
    for n in range(4000):
        job = {"_rowid": n + 1, "id": str(uuid4()), "run_id": run["id"], "request_id": f"r{n}", "kind": "publication"}
        value["snapshot"]["tables"]["web_jobs"].append({**job, "payload": "{}", "status": "completed", "result": None})
        value["claims"][job["id"]] = {"owner": str(uuid4()), "fence": 1}
        value["cold"]["hot"]["web_jobs"][job["id"]] = n + 1
    value["cold"]["rowids"]["web_jobs"] = value["cold"]["ordinals"]["web_jobs"][run["id"]] = 4000
    started = time.monotonic()
    s._validate(value)
    assert time.monotonic() - started < 2.0  # one scan per claim over 4000 jobs (16 million row visits) took far longer
