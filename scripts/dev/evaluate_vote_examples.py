"""Compare a standing run's decisions with and without one site's votes as examples. Writes nothing to the lab.

    AWS_PROFILE=cyclotron-production CYCLOTRON_LAB_ACCOUNT_ID=<account scope id> python scripts/dev/evaluate_vote_examples.py \
        --run <run id> --output <classifier id> [--sibling <classifier id>] --report <path> [--live]

The lab is read through a get-only S3 wrapper. Decisions are made in two temporary local stores, one holding the run's
accepted priming and one adding the site's example votes. Without --live it prints the split and makes no model call.
With --live it makes paid decision-model calls, each item twice: the control checks that this harness reproduces the
recorded decisions and gives the siblings a baseline (their votes may be on items decided on an earlier run). The
report holds item ids, labels and counts, never article text or explanations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/dev"))


def read_only_store():
    from lab_criteria import AWS_ACCOUNT, BUCKET, open_store

    from decision_flywheel.record_lake import S3RecordLake
    from decision_flywheel.s3_console_storage import RESULTS_DOMAIN, RESULTS_KIND

    store = open_store(writable=False)
    store.results = S3RecordLake(store.journal.payloads, RESULTS_DOMAIN, RESULTS_KIND)
    return store, BUCKET, AWS_ACCOUNT


def machine_votes(store, run_id, list_id, output):
    """Each item's current vote for one output, through the rule that keeps the latest label only."""
    from decision_flywheel.vote_examples import Vote

    revisions, after = {}, 0
    while True:
        rows = store.events(run_id, after=after, limit=1000)
        if not rows:
            break
        for row in rows:
            after = max(after, row["id"] if "id" in row else row.get("cursor", after))
            payload = row.get("payload") or {}
            if payload.get("kind") == "publication-machine-feedback" and payload["output"] == output:
                revisions[payload["itemId"]] = payload["itemRevision"]
        if len(rows) < 1000:
            break
    votes = []
    for item_id, revision in revisions.items():
        for label in store.item_labels(list_id, item_id, revision):
            if label["classifier_id"] == output:
                values = store.item_revision(list_id, item_id, revision)
                votes.append(Vote(output, item_id, values.get("values", values), label["label"], label["comment"] or ""))
    return votes


def recorded_decisions(store, run_id, item_ids):
    """Each item's newest recorded labels by output."""
    from decision_flywheel.decision_kinds import is_decision

    found = {}
    for job in store.iter_jobs(run_id, results=False):
        item_id = (job["payload"].get("item") or {}).get("id")
        if item_id not in item_ids or item_id in found or job["status"] != "completed" or not is_decision(job["kind"]):
            continue
        response = store.job_result(job["id"]).get("response") or {}
        labels = {}
        for row in (response.get("publications") or {}).values():
            for output, result in (row.get("decision") or {}).get("classifiers", {}).items():
                labels[output] = result["label"]
        found[item_id] = labels
    return found


def live_priming(store, bucket, aws_account, cyclotron_id):
    """The run's accepted priming, minus retracted examples, from its own root store."""
    from decision_flywheel.batch_storage import StorageScope
    from decision_flywheel.priming_context import decode_priming as decode
    from decision_flywheel.s3_journal import S3Journal
    from decision_flywheel.storage_payloads import S3Payloads

    account = os.environ["CYCLOTRON_LAB_ACCOUNT_ID"]
    payloads = S3Payloads(store.journal.payloads.client, bucket, aws_account, StorageScope(account, "console"))
    name = "publication-" + sha256(cyclotron_id.encode()).hexdigest()
    # The root's committed head, read without taking ownership of the run's store.
    meta = S3Journal(payloads, "workspace", name, create_only_initialization=True).read().value["data"]["meta"]
    if "priming_examples" not in meta:
        raise SystemExit("the run has no accepted priming")
    if json.loads(meta.get("priming_retractions") or "[]"):
        raise SystemExit("the run has retracted priming; teach this script to remove it before trusting a comparison")
    return decode(json.loads(meta["priming_examples"]))


def definition_of(config):
    """The same definition the worker opens for this run."""
    from decision_flywheel.embedded_cyclotron import ClassifierSpec, CyclotronDefinition

    specs, rubrics = [], {}
    for row in config["classifiers"]:
        definition = row["config"]
        positive = next((c["label"] for c in definition["classes"] if c.get("role") == "positive"), None)
        specs.append(
            ClassifierSpec(
                row["id"],
                tuple(c["label"] for c in definition["classes"]),
                definition["question"],
                input_field=definition.get("input_field", "text"),
                positive_label=positive,
            )
        )
        rubrics[row["id"]] = definition.get("seed_rubric", "")
    return CyclotronDefinition(config["cyclotron_id"], tuple(specs), seed=config["seed"]), rubrics


async def decide_all(directory, definition, rubrics, model, priming, targets):
    """Decide each target in one fresh local store. A failed call is recorded and the rest go on."""
    from decision_flywheel.embedded_cyclotron import Cyclotron

    engine = Cyclotron.open(
        directory,
        definition,
        model,
        None,
        seed_rubrics=rubrics,
        priming_examples=priming,
        max_requests=len(targets),
    )
    labels, failed = {}, {}
    try:
        for item in targets:
            try:
                decision = await engine.decide(item)
                labels[item.id] = {output: row.label for output, row in decision.results.items()}
            except Exception as error:
                failed[item.id] = type(error).__name__
    finally:
        engine.close()
    return labels, failed


def flips(ids, before, after):
    """By output, how many items got a different label, among items decided both ways."""
    changed, compared = Counter(), Counter()
    for item_id in ids:
        for output, label in (before.get(item_id) or {}).items():
            if output in (after.get(item_id) or {}):
                compared[output] += 1
                changed[output] += label != after[item_id][output]
    return {output: {"compared": compared[output], "changed": changed[output]} for output in compared}


class Counted:
    """The decision model, counting its calls and the usage it reports."""

    def __init__(self, model):
        self.model, self.calls, self.usage = model, 0, Counter()

    def __getattr__(self, name):
        return getattr(self.model, name)

    async def classify_many(self, *args, **kwargs):
        self.calls += 1
        answers = await self.model.classify_many(*args, **kwargs)
        for key, value in (answers.usage or {}).items():
            self.usage[key] += value
        return answers

    def spent(self):
        return {"calls": self.calls, "usage": dict(self.usage)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sibling", action="append", default=[])
    parser.add_argument("--report", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    from decision_flywheel.adapters.jev import JevAdapter, JevConfiguration
    from decision_flywheel.models import Item
    from decision_flywheel.vote_examples import agreement, labeled_examples, split_votes

    store, bucket, aws_account = read_only_store()
    config = store.run(args.run)["config"]
    list_id = config["item_list_id"]
    votes = machine_votes(store, args.run, list_id, args.output)
    examples, evaluation = split_votes(votes, config["seed"])
    example_ids = {vote.item_id for vote in examples}
    siblings = {output: machine_votes(store, args.run, list_id, output) for output in args.sibling}
    overlap = {output: sum(v.item_id in example_ids for v in rows) for output, rows in siblings.items()}
    siblings = {output: [v for v in rows if v.item_id not in example_ids] for output, rows in siblings.items()}
    primed = live_priming(store, bucket, aws_account, config["cyclotron_id"])
    plan = {
        "run": args.run,
        "output": args.output,
        "votes": dict(Counter(v.label for v in votes)),
        "examples": dict(Counter(v.label for v in examples)),
        "evaluation": dict(Counter(v.label for v in evaluation)),
        "siblings": {output: dict(Counter(v.label for v in rows)) for output, rows in siblings.items()},
        "siblingVotesSkippedBecauseTheyAreExamples": overlap,
        "primed": {output: len(rows) for output, rows in primed.items()},
        "model": config["decisions_model"],
    }
    print(json.dumps(plan, indent=1))
    if not args.live:
        return

    targets = {v.item_id: Item(v.item_id, dict(v.values)) for v in evaluation}
    sibling_targets = {
        v.item_id: Item(v.item_id, dict(v.values)) for rows in siblings.values() for v in rows if v.item_id not in targets
    }
    everyone = {**targets, **sibling_targets}
    recorded = recorded_decisions(store, args.run, set(everyone))
    definition, rubrics = definition_of(config)
    candidate_priming = {**primed, args.output: (*primed.get(args.output, ()), *labeled_examples(examples))}
    jev = JevAdapter.from_environment(configuration=JevConfiguration(model=config["decisions_model"]))
    control_model, candidate_model = Counted(jev), Counted(jev)

    async def run():
        with tempfile.TemporaryDirectory() as directory:
            control = await decide_all(
                Path(directory) / "control", definition, rubrics, control_model, primed, list(everyone.values())
            )
            candidate = await decide_all(
                Path(directory) / "candidate", definition, rubrics, candidate_model, candidate_priming, list(everyone.values())
            )
        return control, candidate

    (control, control_failed), (candidate, candidate_failed) = asyncio.run(run())
    labels = lambda decided, output: {i: row[output] for i, row in decided.items() if output in row}  # noqa: E731
    positive = {row.id: row.positive_label for row in definition.classifiers}
    report = {
        **plan,
        "control": control_model.spent(),
        "candidate": candidate_model.spent(),
        "failed": {"control": control_failed, "candidate": candidate_failed},
        "evaluation_scores": {
            name: agreement(evaluation, labels(decided, args.output), positive=positive[args.output])
            for name, decided in (("recorded", recorded), ("control", control), ("candidate", candidate))
        },
        "sibling_scores": {
            output: {
                name: agreement(rows, labels(decided, output), positive=positive[output])
                for name, decided in (("recorded", recorded), ("control", control), ("candidate", candidate))
            }
            for output, rows in siblings.items()
        },
        "harness_reproduces_recorded": flips(targets, recorded, control),
        "control_to_candidate": flips(everyone, control, candidate),
        "items": {i: {"recorded": recorded.get(i), "control": control.get(i), "candidate": candidate.get(i)} for i in everyone},
    }
    Path(args.report).write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k != "items"}, indent=1))


if __name__ == "__main__":
    main()
