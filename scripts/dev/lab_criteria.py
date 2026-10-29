"""Plan (read-only) or apply the committed lab classifier criteria to the lab catalog.

    AWS_PROFILE=cyclotron-production CYCLOTRON_LAB_ACCOUNT_ID=<account scope id> python scripts/dev/lab_criteria.py plan

Each criteria directory is one cyclotron: its classifier files and its ``definition.json``. The shared Anthus
Cyclotron is ``infra/lab/criteria`` (the default); a publication with its own cyclotron has its own directory,
named with ``--directory`` (for example ``infra/lab/criteria-distinctive``).

`plan` reads the live catalog through a get-only S3 wrapper (get_object, list_objects_v2,
head_object; a single attempt) and prints what a deploy would do. It never writes.

`apply` writes the new classifier revisions and the successor definition. It needs both
--apply and --approved-by NAME, and only Ryan's explicit approval of the change authorizes
running it. Agents run `plan` only. Applying is idempotent and refuses live drift.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "infra/lab/criteria"
BUCKET = "cyclotron-console-books-682849160744-us-east-1"
AWS_ACCOUNT = "682849160744"


def open_store(*, writable: bool):
    import boto3
    from botocore.config import Config

    from decision_flywheel.batch_storage import StorageScope
    from decision_flywheel.s3_console_storage import S3ConsoleStorage
    from decision_flywheel.s3_journal import S3Journal
    from decision_flywheel.storage_payloads import S3Payloads

    account = os.environ.get("CYCLOTRON_LAB_ACCOUNT_ID")
    if not account:
        raise SystemExit("set CYCLOTRON_LAB_ACCOUNT_ID to the lab account scope id (see the stack's PublicationTargets)")
    real = boto3.client("s3", region_name="us-east-1", config=Config(retries={"total_max_attempts": 1}))

    class GetOnly:
        meta, exceptions = real.meta, real.exceptions

        def __getattr__(self, name):
            if name in ("get_object", "list_objects_v2", "head_object"):
                return getattr(real, name)
            raise PermissionError(f"read-only wrapper refuses {name}")

    payloads = S3Payloads(real if writable else GetOnly(), BUCKET, AWS_ACCOUNT, StorageScope(account, "console"))
    journal = S3Journal(payloads, "console", "catalog", create_only_initialization=True)
    return S3ConsoleStorage(journal)


def build(store, directory=DIRECTORY):
    from decision_flywheel.classifier_criteria import load_definition, load_directory, plan_classifiers, plan_definition

    criteria = load_directory(directory, relative_to=ROOT)
    definition = load_definition(directory)
    steps = plan_classifiers(store, criteria)
    return criteria, steps, plan_definition(store, criteria, steps, definition)


def report(steps, definition_step) -> dict:
    return {
        "classifiers": [vars(step) for step in steps],
        "definition": {
            "action": definition_step.action,
            "id": definition_step.definition_id,
            "name": definition_step.name,
            "classifiers": [{"id": cid, "revision": revision} for cid, revision in definition_step.classifiers],
            "reason": definition_step.reason,
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["plan", "apply"])
    parser.add_argument("--apply", action="store_true", help="required with the apply command")
    parser.add_argument("--approved-by", help="who approved running this change (recorded in the output only)")
    parser.add_argument("--directory", type=Path, default=DIRECTORY, help="the cyclotron's criteria directory")
    args = parser.parse_args(argv)
    if args.command == "apply" and not (args.apply and args.approved_by):
        parser.error("apply needs --apply and --approved-by NAME")
    store = open_store(writable=args.command == "apply")
    criteria, steps, definition_step = build(store, ROOT / args.directory)
    print(json.dumps(report(steps, definition_step), indent=2))
    if args.command == "plan":
        drift = any(s.action == "drift" for s in steps)
        return 2 if drift else 0
    from decision_flywheel.classifier_criteria import apply_plan

    print(json.dumps({"applied": apply_plan(store, criteria, steps, definition_step), "approved_by": args.approved_by}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
