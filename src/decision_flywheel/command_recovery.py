"""Operator command: diagnose and reconcile a claimed, never-finished hosted command.

  python -m decision_flywheel.command_recovery diagnose --profile P --expect-aws-account 123456789012 \\
      --books-bucket B --cyclotron-account UUID --job UUID
  python -m decision_flywheel.command_recovery reconcile --profile P --expect-aws-account 123456789012 \\
      --books-bucket B --cyclotron-account UUID --job UUID --owner UUID --fence 1 --as failed \\
      --reason "why" [--dlq-url URL] [--apply]

`reconcile` is a dry run unless `--apply` is given. It refuses unless the recorded owner and fence match, the
job has no result, and nothing in the catalog points at a completed provider call. On apply it ends the job as
`failed` or `interrupted`, with the audit record (who, when, why) stored as the job's result in the same catalog
commit, and then deletes only the matching message from the dead-letter queue when `--dlq-url` is given. The
worker's own rule is unchanged: it never takes over a running claim.
"""

import argparse
import json
import sys

from .batch_storage import StorageScope
from .record_lake import S3RecordLake
from .s3_console_storage import RESULTS_DOMAIN, RESULTS_KIND, S3ConsoleStorage
from .s3_journal import S3Journal
from .storage_payloads import S3Payloads


def open_store(s3, books_bucket, aws_account, cyclotron_account):
    payloads = S3Payloads(s3, books_bucket, aws_account, StorageScope(cyclotron_account, "console"))
    journal = S3Journal(payloads, "console", "catalog")
    results = S3RecordLake(payloads, RESULTS_DOMAIN, RESULTS_KIND)
    return S3ConsoleStorage(journal, results=results)


def matching_dlq_messages(sqs, url, job_id, *, limit=100):
    """Receive up to `limit` messages; return those naming the job and release the rest untouched."""
    matched, seen = [], 0
    while seen < limit:
        batch = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=30, WaitTimeSeconds=0)
        messages = batch.get("Messages", [])
        if not messages:
            break
        for message in messages:
            seen += 1
            try:
                named = json.loads(message["Body"]).get("jobId") == job_id
            except ValueError:
                named = False
            if named:
                matched.append(message)
            else:
                sqs.change_message_visibility(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0)
    return matched


def purge_matching(sqs, url, job_id, *, apply):
    matched = matching_dlq_messages(sqs, url, job_id)
    if apply:
        for message in matched:
            sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
    else:
        for message in matched:
            sqs.change_message_visibility(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0)
    return len(matched)


def wake_head(sqs, url, store, account, job_id):
    """After a reconcile, wake the run's next queued job now rather than at its backoff (at most 2 minutes)."""
    run_id = store.command_recovery(job_id)["runId"]
    head = store.queue_head(run_id)
    if head is None:
        return None
    message = {"accountId": account, "runId": run_id, "jobId": head["id"]}
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps(message, separators=(",", ":")))
    return head["id"]


def run(args, *, session):
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    if identity["Account"] != args.expect_aws_account:
        print(f"Refusing: caller account is not {args.expect_aws_account}", file=sys.stderr)
        return 2
    s3 = session.client("s3")
    store = open_store(s3, args.books_bucket, identity["Account"], args.cyclotron_account)
    if args.command == "diagnose":
        print(json.dumps(store.command_recovery(args.job), indent=1))
        return 0
    try:
        audit = store.reconcile_command(
            args.job,
            owner=args.owner,
            fence=args.fence,
            status=args.as_status,
            reason=args.reason,
            operator=identity["Arn"],
            apply=args.apply,
        )
    except ValueError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    print(
        ("APPLIED" if args.apply else "DRY RUN (nothing written; pass --apply)")
        + ": "
        + json.dumps(audit["reconciliation"], indent=1)
    )
    if args.queue_url and args.apply:
        woken = wake_head(session.client("sqs"), args.queue_url, store, args.cyclotron_account, args.job)
        print("woke the next queued job: " + (woken or "none waiting"))
    if args.dlq_url:
        count = purge_matching(session.client("sqs"), args.dlq_url, args.job, apply=args.apply)
        print(f"dead-letter messages for this job: {count} " + ("deleted" if args.apply else "would be deleted"))
    return 0


def aws_session(profile, region):
    """A boto3 session whose S3 client satisfies the journal's single-attempt conditional-write rule."""
    import boto3
    from botocore.config import Config

    base = boto3.Session(profile_name=profile, region_name=region)
    s3_config = Config(signature_version="s3v4", retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=10)

    class Session:
        def client(self, name):
            return base.client(name, config=s3_config) if name == "s3" else base.client(name)

    return Session()


def parser():
    p = argparse.ArgumentParser(prog="command_recovery", description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=("diagnose", "reconcile"))
    p.add_argument("--profile")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--expect-aws-account", required=True)
    p.add_argument("--books-bucket", required=True)
    p.add_argument("--cyclotron-account", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--owner")
    p.add_argument("--fence", type=int)
    p.add_argument("--as", dest="as_status", choices=("failed", "interrupted"))
    p.add_argument("--reason")
    p.add_argument("--dlq-url")
    p.add_argument("--queue-url", help="with --apply: also wake the run's next queued job at once (needs sqs:SendMessage)")
    p.add_argument("--apply", action="store_true", help="write; the default is a dry run")
    return p


def main(argv=None, *, session=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command == "reconcile" and not (args.owner and args.fence and args.as_status and args.reason):
        p.error("reconcile needs --owner, --fence, --as and --reason")
    if session is None:
        session = aws_session(args.profile, args.region)
    return run(args, session=session)


if __name__ == "__main__":
    sys.exit(main())
