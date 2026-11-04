"""Run the native optimizer on one site's votes in a temporary local store. Writes nothing to the lab.

    AWS_PROFILE=cyclotron-production CYCLOTRON_LAB_ACCOUNT_ID=<account scope id> python scripts/dev/optimize_vote_elements.py \
        --run <run id> --output <classifier id> --report <path> --dotenv <path to .env> \
        [--optimizer-model gpt-6-luna] [--max-optimizer-calls 20] [--max-decision-calls 2000]

Runs the rubric stage, then the questions stage with its classifier fit, on the site's votes. The evaluation votes from
``split_votes`` are protected throughout: the optimizer never sees them, and each stage's result is scored on them.
Makes paid decision-model and optimizer calls, each under its ceiling. The report holds the proposals, labels, counts
and scores, never article text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/dev"))

import evaluate_vote_examples as harness  # noqa: E402


async def decided_labels(wheel, rows, training, now):
    """What the active version says about each item: its fitted head when it has one, else the decision answer."""
    labels = {}
    active = wheel.active
    for row in rows:
        batch = await wheel._answers(active.config, row.item, training, now)
        if active.head:
            labels[row.item.id] = active.head.predict(wheel._features(active.config, batch))
        else:
            labels[row.item.id] = batch.answers["decision"].label
    return labels


def version_of(wheel):
    config = wheel.active.config
    return {
        "fingerprint": wheel.active.fingerprint,
        "rubric": config.rubric,
        "questions": [{"name": t.name, "instructions": t.instructions, "labels": list(t.labels)} for t in config.tasks],
        "fittedHead": wheel.active.head is not None,
        "examples": len(config.example_ids),
    }


async def optimize(args, config, votes, primed):
    from decision_flywheel.adapters.jev import JevAdapter, JevConfiguration
    from decision_flywheel.adapters.optimizer_transport import optimizer_transport
    from decision_flywheel.embedded_cyclotron import Cyclotron
    from decision_flywheel.flywheel import development_assignment
    from decision_flywheel.models import Item
    from decision_flywheel.optimizer_agent import OptimizerAgent
    from decision_flywheel.staged_optimization import optimize_stage
    from decision_flywheel.vote_examples import agreement, labeled_examples, split_votes

    examples, evaluation = split_votes(votes, config["seed"])
    rows = labeled_examples(examples)
    dev_seed = config["seed"] + ":optimizer-development"
    development = tuple(row for row in rows if development_assignment(dev_seed, row.item.id))
    held = {row.item.id for row in development}
    training = (*primed.get(args.output, ()), *(row for row in rows if row.item.id not in held))
    scored = labeled_examples(evaluation)
    protected = tuple(Item(row.item.id, dict(row.item.values)) for row in scored)
    propensities = {row.item.id: 1.0 for row in training}
    definition, rubrics = harness.definition_of(config)
    positive = next(c.positive_label for c in definition.classifiers if c.id == args.output)
    model = harness.Counted(JevAdapter.from_environment(configuration=JevConfiguration(model=config["decisions_model"])))
    transport = optimizer_transport({"optimizer_model": args.optimizer_model, "max_optimizer_calls": args.max_optimizer_calls})
    report = {
        "split": {
            "training": len(training),
            "development": len(development),
            "evaluation": len(evaluation),
            "optimizerModel": args.optimizer_model,
        },
        "stages": [],
    }

    def save():
        report["decisionCalls"] = model.spent()
        report["optimizerCalls"] = getattr(transport, "calls", None)
        Path(args.report).write_text(json.dumps(report, indent=1, default=str))

    with tempfile.TemporaryDirectory() as directory:
        engine = Cyclotron.open(
            Path(directory) / "store",
            definition,
            model,
            OptimizerAgent(transport),
            seed_rubrics=rubrics,
            priming_examples=primed,
            max_requests=args.max_decision_calls,
        )
        try:
            wheel = engine.wheels[args.output]
            pools = {cid: tuple(primed.get(cid, ())) for cid in engine.wheels}
            pools[args.output] = training

            def bind():
                engine.shared.bind_context({cid: w.active.config for cid, w in engine.wheels.items()}, pools)

            async with engine.execution():
                wheel.set_optimizer_context([r.context["human_feedback"] for r in training if r.context.get("human_feedback")])
                now = datetime.now(timezone.utc)
                bind()
                baseline = await decided_labels(wheel, scored, training, now)
                report["baseline"] = {
                    "version": version_of(wheel),
                    "score": agreement(evaluation, baseline, positive=positive),
                }
                save()
                for stage in ("rubric", "questions"):
                    bind()
                    result = await optimize_stage(
                        wheel,
                        stage,
                        training,
                        development,
                        protected=protected,
                        propensities=propensities,
                        min_development_per_class=1,
                    )
                    bind()
                    labels = await decided_labels(wheel, scored, training, now)
                    report["stages"].append(
                        {
                            "stage": stage,
                            "promoted": result.get("promoted"),
                            "reason": result.get("reason"),
                            "proposal": result.get("proposal"),
                            "elementRankings": result.get("rankings"),
                            "classifierTraining": result.get("classifier_training"),
                            "version": version_of(wheel),
                            "score": agreement(evaluation, labels, positive=positive),
                            "labels": {v.item_id: {"vote": v.label, "said": labels.get(v.item_id)} for v in evaluation},
                        }
                    )
                    save()
        finally:
            engine.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--dotenv", required=True)
    parser.add_argument("--optimizer-model", default="gpt-6-luna")
    parser.add_argument("--max-optimizer-calls", type=int, default=20)
    parser.add_argument("--max-decision-calls", type=int, default=2000)
    args = parser.parse_args()

    from dotenv import load_dotenv

    # The named file wins: an older key exported in the shell must not be used in its place.
    load_dotenv(args.dotenv, override=True)
    store, bucket, aws_account = harness.read_only_store()
    config = store.run(args.run)["config"]
    votes = harness.machine_votes(store, args.run, config["item_list_id"], args.output)
    primed = harness.live_priming(store, bucket, aws_account, config["cyclotron_id"])
    report = asyncio.run(optimize(args, config, votes, primed))
    print(json.dumps({key: value for key, value in report.items() if key != "stages"}, indent=1, default=str)[:3000])
    for row in report["stages"]:
        print(json.dumps({key: value for key, value in row.items() if key != "labels"}, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main()
