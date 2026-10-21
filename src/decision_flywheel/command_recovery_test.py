"""The operator reconcile command ends a wedged claimed command safely, audited, and never guesses."""

import json
from uuid import uuid4

import pytest

from .command_recovery import main, open_store, purge_matching
from .s3_console_storage import insert
from .s3_journal_test import ConditionalS3

ITEM = {"publication": "anth-us", "bindingRevision": "1", "item": {"id": "hn-1"}}
REASON = "wrong provider key; no prediction produced"


def wedged(client=None):
    client = client or ConditionalS3()
    s = open_store(client, "synthetic-bucket", "111111111111", "account-a")
    s.initialize()
    run = s.create_run("Live", "live", {})
    job = s.command(run["id"], "req-1", "publication", ITEM)
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner, job_id=job["id"])
    return client, s, run, job, owner, claimed["claim_fence"]


def reconcile(s, job, owner, fence, **changes):
    args = dict(owner=owner, fence=fence, status="failed", reason=REASON, operator="arn:aws:iam::1:user/op", apply=True)
    return s.reconcile_command(job["id"], **{**args, **changes})


def test_dry_run_checks_everything_and_writes_nothing():
    _, s, run, job, owner, fence = wedged()
    before = s.journal.read().revision
    audit = reconcile(s, job, owner, fence, apply=False)["reconciliation"]
    assert audit["to"] == "failed" and audit["reason"] == REASON
    assert s.journal.read().revision == before and s.command_recovery(job["id"])["status"] == "running"


def test_apply_ends_the_job_with_an_audit_record_and_unblocks_the_run():
    _, s, run, job, owner, fence = wedged()
    with pytest.raises(ValueError, match="in progress"):
        s.command(run["id"], "req-2", "publication", ITEM)
    reconcile(s, job, owner, fence)
    done = next(j for j in s.jobs(run["id"]) if j["id"] == job["id"])
    audit = done["result"]["reconciliation"]
    assert done["status"] == "failed"
    assert audit["operator"] == "arn:aws:iam::1:user/op" and audit["reason"] == REASON and audit["at"]
    assert (audit["owner"], audit["fence"], audit["from"], audit["to"]) == (owner, fence, "running", "failed")
    assert s.command(run["id"], "req-2", "publication", ITEM)["status"] == "pending"


def test_a_different_owner_or_fence_is_refused():
    _, s, run, job, owner, fence = wedged()
    with pytest.raises(ValueError, match="owner and fence"):
        reconcile(s, job, str(uuid4()), fence)
    with pytest.raises(ValueError, match="owner and fence"):
        reconcile(s, job, owner, fence + 1)
    assert s.command_recovery(job["id"])["status"] == "running"


def test_only_a_running_job_can_be_reconciled_and_only_once():
    _, s, run, job, owner, fence = wedged()
    reconcile(s, job, owner, fence)
    with pytest.raises(ValueError, match="running"):
        reconcile(s, job, owner, fence)
    with pytest.raises(ValueError, match="unknown"):
        s.reconcile_command(str(uuid4()), owner=owner, fence=fence, status="failed", reason=REASON, operator="op")


def test_bad_target_or_missing_reason_is_refused():
    _, s, run, job, owner, fence = wedged()
    with pytest.raises(ValueError):
        reconcile(s, job, owner, fence, status="completed")
    with pytest.raises(ValueError):
        reconcile(s, job, owner, fence, reason="x")
    with pytest.raises(ValueError):
        reconcile(s, job, owner, fence, operator="")


@pytest.mark.parametrize("evidence", ["prediction", "retained", "event"])
def test_recorded_evidence_of_a_provider_outcome_refuses_even_a_dry_run(evidence):
    _, s, run, job, owner, fence = wedged()
    rows = {
        "prediction": ("web_items", dict(id="hn-1", run_id=run["id"], payload="{}", prediction="{}", reviewed=0)),
        "retained": ("item_predictions", dict(run_id=run["id"], list_id="l", item_id="hn-1", item_revision=1, payload="{}")),
        "event": ("web_events", dict(sequence=0, run_id=run["id"], source_id="s", payload=json.dumps({"job": job["id"]}))),
    }
    table, row = rows[evidence]
    if evidence == "retained":
        s.save_item_list("l", "L")
        s.upsert_list_items("l", [{"id": "hn-1", "occurred_at": "2026-01-01", "values": {"text": "x"}}])
    s._mutate(lambda value: insert(value, table, row))
    for apply in (False, True):
        with pytest.raises(ValueError, match="provider outcome may exist"):
            reconcile(s, job, owner, fence, apply=apply)
    assert s.command_recovery(job["id"])["status"] == "running"


def test_a_job_with_a_recorded_result_is_refused():
    _, s, run, job, owner, fence = wedged()

    def put(value):
        next(r for r in value["snapshot"]["tables"]["web_jobs"] if r["id"] == job["id"])["result"] = json.dumps({"response": 1})

    s._mutate(put)
    with pytest.raises(ValueError, match="recorded result"):
        reconcile(s, job, owner, fence)


class Queue:
    def __init__(self, jobs):
        self.messages = {f"h{i}": json.dumps({"jobId": j}) for i, j in enumerate(jobs)}
        self.held, self.deleted = set(), []

    def receive_message(self, QueueUrl, **kw):
        fresh = [h for h in self.messages if h not in self.held and h not in self.deleted][:10]
        self.held.update(fresh)
        return {"Messages": [{"ReceiptHandle": h, "Body": self.messages[h]} for h in fresh]}

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        self.held.discard(ReceiptHandle)

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


def test_dead_letter_purge_deletes_only_the_matching_message_and_only_when_applied():
    q = Queue(["other", "job-1", "another"])
    assert purge_matching(q, "url", "job-1", apply=False) == 1 and q.deleted == []
    q.held.clear()
    assert purge_matching(q, "url", "job-1", apply=True) == 1 and q.deleted == ["h1"]


class Session:
    def __init__(self, client, account="111111111111", queue=None):
        self.client_, self.account, self.queue = client, account, queue

    def client(self, name, **kw):
        if name == "sts":
            return type("Sts", (), {"get_caller_identity": lambda _: {"Account": self.account, "Arn": "arn:aws:iam::1:user/op"}})()
        return {"s3": self.client_, "sqs": self.queue}[name]


def cli(job, owner, fence, session, *extra):
    return main(["reconcile", "--expect-aws-account", "111111111111", "--books-bucket", "synthetic-bucket",
                 "--cyclotron-account", "account-a", "--job", job["id"], "--owner", owner, "--fence", str(fence),
                 "--as", "failed", "--reason", REASON, "--dlq-url", "q", *extra], session=session)


def test_cli_defaults_to_dry_run_then_applies_and_purges(capsys):
    client, s, run, job, owner, fence = wedged()
    queue = Queue([job["id"], "unrelated"])
    session = Session(client, queue=queue)
    assert cli(job, owner, fence, session) == 0
    assert "DRY RUN" in capsys.readouterr().out and queue.deleted == []
    assert s.command_recovery(job["id"])["status"] == "running"
    queue.held.clear()
    assert cli(job, owner, fence, session, "--apply") == 0
    assert "APPLIED" in capsys.readouterr().out and queue.deleted == ["h0"]
    assert s.command_recovery(job["id"])["status"] == "failed"


def test_cli_refuses_in_the_wrong_aws_account_and_on_a_fence_mismatch(capsys):
    client, s, run, job, owner, fence = wedged()
    assert cli(job, owner, fence, Session(client, account="999999999999"), "--apply") == 2
    assert cli(job, owner, fence + 1, Session(client, queue=Queue([])), "--apply") == 1
    assert s.command_recovery(job["id"])["status"] == "running"
