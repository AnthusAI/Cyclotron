#!/usr/bin/env python3
"""Freeze historical votes read-only; preflight or run an explicitly bounded replay."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3

from decision_flywheel.adapters.jev import JevAdapter, JevConfiguration
from decision_flywheel.adapters.openai_optimizer import OpenAIOptimizer
from decision_flywheel.classifier_config import ClassifierConfig
from decision_flywheel.flywheel import DecisionFlywheel
from decision_flywheel.optimizer_agent import OptimizerAgent
from decision_flywheel.replay import plan_replay, run_replay
from decision_flywheel.reviewer_core import reviewer_labeled_items, reviewer_task
from decision_flywheel.reviewer_store import ReviewStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("var/reviewer.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("var/arxiv-replay-v1"))
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--seed", default="arxiv-feedback-replay-v1")
    parser.add_argument("--max-requests", type=int, default=500)
    parser.add_argument("--max-optimizer-calls", type=int, default=5)
    parser.add_argument("--optimizer-model", default="gpt-6-luna")
    parser.add_argument("--decisions-provider", choices=("jev",), default="jev")
    parser.add_argument("--decisions-model", default="jev-1.13.0")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args(argv)
    if min(args.batch_size, args.max_requests, args.max_optimizer_calls) < 1:
        parser.error("batch size and request ceilings must be positive")
    if not args.database.is_file():
        parser.error("existing rated database required")
    args.output.mkdir(parents=True, exist_ok=True)
    snapshot = args.output / "reviews.sqlite3"
    if not snapshot.exists():
        # Backup a read-only connection: the live review database is never opened for writes.
        with sqlite3.connect(args.database.resolve().as_uri()+"?mode=ro", uri=True) as source:
            with sqlite3.connect(snapshot) as destination:
                source.backup(destination)
    with sqlite3.connect(snapshot) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM study_metadata"))
    with ReviewStore(snapshot, study_seed=metadata["study_seed"],
                     rolling_audit_rate=float(metadata["rolling_audit_rate"]),
                     final_audit_rate=float(metadata["final_audit_rate"])) as store:
        feedback = store.learning_feedback()
        items = {row.item.id: row for row in reviewer_labeled_items(feedback, store.article)}
        # Current non-undone votes and comments, preserving historical arrival order.
        active = store._active_actions()
        ordered = tuple(items[key] for key in sorted(items, key=lambda key: (active[key].created_at, key)))
        plan = plan_replay(reviewer_task(), ordered, seed=args.seed, batch_size=args.batch_size)
    manifest = plan.manifest(reviewer_task())
    path = args.output / "manifest.json"
    encoded = json.dumps(manifest, indent=2)
    if path.exists() and json.loads(path.read_text()) != json.loads(encoded):
        parser.error("frozen replay plan differs; choose a new output directory")
    path.write_text(encoded+"\n")
    counts = lambda rows: {label: sum(row.label == label for row in rows) for label in reviewer_task().labels}
    print(json.dumps({"eligible_labels": len(plan.ordered), "training": counts(plan.training),
                      "development": counts(plan.development), "protected_audit_pool": counts(plan.scoreboard),
                      "final_recent_balanced_evaluation": counts(plan.evaluation(len(plan.ordered), reviewer_task().labels)),
                      "checkpoints": plan.checkpoints, "jev_uncached_upper_bound": plan.request_upper_bound,
                      "optimizer_upper_bound": len(plan.checkpoints), "output": str(args.output)}), flush=True)
    if not args.confirm_live:
        print("Preflight only: no model clients constructed or called.")
        return 0
    if plan.request_upper_bound > args.max_requests or len(plan.checkpoints) > args.max_optimizer_calls:
        parser.error("replay does not fit the approved request ceilings")
    runtime = args.output / "runtime.sqlite3"
    if runtime.exists():
        parser.error("a live replay already exists; use a new output directory rather than silently restart it")
    adapter = JevAdapter.from_environment(configuration=JevConfiguration(model=args.decisions_model))
    transport = OpenAIOptimizer.from_environment(model=args.optimizer_model, max_calls=args.max_optimizer_calls)
    def observe(event):
        kind = event["kind"]
        if kind == "optimizer-response":
            proposal = json.loads(event["content"])
            print(json.dumps({"event": kind, "model": event["model"], "proposal": proposal}), flush=True)
        elif kind in {"optimizer-request", "optimizer-failed", "proposal-validated", "fit-started", "fit-completed",
                      "candidate-evaluated", "candidate-rejected", "promoted", "waiting-for-labels", "round-failed", "features-failed"}:
            print(json.dumps({k: v for k, v in event.items() if k not in {"messages", "previous", "candidate"}}), flush=True)
    wheel = DecisionFlywheel(runtime, ClassifierConfig(reviewer_task()), adapter, OptimizerAgent(transport),
                             max_requests=args.max_requests, evaluation_weighting="equal_class",
                             min_evaluation_per_class=2, observer=observe,
                             redact=tuple(os.environ.get(k, "") for k in ("OPENAI_API_KEY", "TYPESAFE_API_KEY")))
    def checkpoint(report):
        (args.output / "results.json").write_text(json.dumps(report, indent=2)+"\n")
        point = report["checkpoints"][-1]
        print(json.dumps({"checkpoint": point["revealed_labels"], "training_labels": point["training_labels"],
                          "fitted_head": point["fitted_head"], "scoreboard": point["scoreboard"],
                          "recent_balanced_scoreboard": point["recent_balanced_scoreboard"],
                          "requests": wheel.requests}), flush=True)
    try:
        report = asyncio.run(run_replay(wheel, plan, on_checkpoint=checkpoint))
        failed = any(point.get("round", {}).get("reason") == "round failed"
                     for point in report["checkpoints"] if point.get("round"))
        print(json.dumps({"complete": not failed, "jev_attempts": wheel.requests,
                          "optimizer_attempts": transport.calls}), flush=True)
        return int(failed)
    finally:
        wheel.close()


if __name__ == "__main__":
    raise SystemExit(main())
