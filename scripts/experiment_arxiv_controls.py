#!/usr/bin/env python3
"""Compare three isolated controls on the already-frozen historical feedback."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import sqlite3

from decision_flywheel import ClassifierConfig, DecisionFlywheel, OptimizerAgent
from decision_flywheel.adapters.jev import JevAdapter, JevConfiguration
from decision_flywheel.adapters.optimizer_transport import optimizer_transport
from decision_flywheel.credential_redaction import credential_values
from decision_flywheel.control_scheduler import ControlScheduler
from decision_flywheel.replay import plan_replay
from decision_flywheel.reviewer_core import reviewer_task, reviewer_labeled_items
from decision_flywheel.reviewer_store import ReviewStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path("var/arxiv-replay-v2/reviews.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("var/arxiv-features-v1"))
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--max-requests", type=int, default=250)
    parser.add_argument("--max-optimizer-calls", type=int, default=3)
    parser.add_argument("--max-feature-trials", type=int, default=3)
    parser.add_argument("--optimizer-model", default="gpt-6-luna")
    parser.add_argument("--optimizer-transport", choices=("openai", "litellm"), default="openai")
    parser.add_argument("--decisions-provider", choices=("jev",), default="jev")
    parser.add_argument("--decisions-model", default="jev-1.13.0")
    parser.add_argument("--training-class-weighting", choices=("natural", "equal_class"), default="equal_class")
    args = parser.parse_args(argv)
    if args.max_feature_trials < 1:
        parser.error("feature trial ceiling must be positive")
    if not args.snapshot.is_file():
        parser.error("run replay preflight first to freeze the existing votes")
    with sqlite3.connect(args.snapshot.resolve().as_uri()+"?mode=ro", uri=True) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM study_metadata"))
    with ReviewStore(args.snapshot, study_seed=metadata["study_seed"],
                     rolling_audit_rate=float(metadata["rolling_audit_rate"]),
                     final_audit_rate=float(metadata["final_audit_rate"])) as store:
        rows = {row.item.id: row for row in reviewer_labeled_items(store.learning_feedback(), store.article)}
        active = store._active_actions()
        ordered = tuple(rows[key] for key in sorted(rows, key=lambda key: (active[key].created_at, key)))
        plan = plan_replay(reviewer_task(), ordered)
    evaluation = plan.evaluation(len(plan.ordered), reviewer_task().labels)
    trial_upper = 2 + args.max_feature_trials
    upper = trial_upper*(len(plan.training)+2*len(plan.development)) + 2*len(evaluation)
    protocol = {"plan": plan.manifest(reviewer_task()), "design": "rubric, examples, individual question trials; shared empty incumbent",
                "feature_trial_ceiling": args.max_feature_trials, "trial_upper_bound": trial_upper,
                "training_class_weighting": args.training_class_weighting, "promotion_metric": "balanced_brier",
                "optimizer_model": args.optimizer_model, "decisions_model": args.decisions_model,
                "optimizer_transport": args.optimizer_transport,
                "request_upper_bound": upper, "optimizer_upper_bound": 3}
    print(json.dumps({key: value for key, value in protocol.items() if key != "plan"}), flush=True)
    if not args.confirm_live:
        return 0
    if args.max_requests < upper or args.max_optimizer_calls < 3:
        parser.error("experiment exceeds request ceilings")
    args.output.mkdir(parents=True, exist_ok=True)
    runtime = args.output / "runtime.sqlite3"
    if runtime.exists():
        parser.error("this experiment already exists; do not silently rerun paid calls")
    (args.output / "protocol.json").write_text(json.dumps(protocol, indent=2)+"\n")
    adapter = JevAdapter.from_environment(configuration=JevConfiguration(model=args.decisions_model))
    transport = optimizer_transport(vars(args))
    def observe(event):
        if event["kind"] == "optimizer-response":
            print(json.dumps({"event": "optimizer-response", "model": event["model"],
                              "proposal": json.loads(event["content"])}), flush=True)
        elif event["kind"] in {"control-trial-started", "control-trial-completed", "control-cycle-completed",
                               "discovery-no-change", "fit-started", "fit-completed", "round-failed", "features-failed"}:
            print(json.dumps(event), flush=True)
        elif event["kind"] in {"feature-discovered", "feature-diagnostics"}:
            print(json.dumps(event), flush=True)
    wheel = DecisionFlywheel(runtime, ClassifierConfig(reviewer_task()), adapter, OptimizerAgent(transport),
                             max_requests=args.max_requests, training_class_weighting=args.training_class_weighting,
                             min_evaluation_per_class=2, observer=observe,
                             redact=credential_values(os.environ))
    async def run():
        before = await wheel._score(wheel.active, evaluation, plan.training, datetime.now(timezone.utc))
        result = await wheel.improve_controls(plan.training, plan.development,
            protected=tuple(row.item for row in plan.scoreboard), propensities={row.item.id: 1. for row in plan.training},
            max_feature_trials=args.max_feature_trials)
        after = await wheel._score(wheel.active, evaluation, plan.training, datetime.now(timezone.utc))
        report = {"protocol": protocol, "result": result, "audit_before": before, "audit_after": after,
                  "retained_ideas": ControlScheduler(wheel).ideas(), "jev_attempts": wheel.requests,
                  "feature_bank": wheel.feature_bank(),
                  "optimizer_attempts": transport.calls}
        (args.output / "results.json").write_text(json.dumps(report, indent=2)+"\n")
        print(json.dumps({"promoted": result["promoted"], "selected_control": result.get("selected_control"),
                          "audit_before": before, "audit_after": after, "jev_attempts": wheel.requests,
                          "optimizer_attempts": transport.calls}), flush=True)
        return int(result["reason"] == "control cycle failed")
    try:
        return asyncio.run(run())
    finally:
        wheel.close()


if __name__ == "__main__":
    raise SystemExit(main())
