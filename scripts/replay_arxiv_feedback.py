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
from decision_flywheel.cycle_replay import run_cycle_replay
from decision_flywheel.trace_artifact import read_trace, render_trace
from decision_flywheel.reviewer_core import reviewer_labeled_items, reviewer_task
from decision_flywheel.reviewer_store import ReviewStore
from decision_flywheel.selection_policy import add_selection_arguments, selection_from_arguments
from decision_flywheel.api_event_sink import GraphQLTraceSink


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_selection_arguments(parser)
    parser.add_argument("--database", type=Path, default=Path("var/reviewer.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("var/arxiv-replay-v1"))
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--seed", default="arxiv-feedback-replay-v1")
    parser.add_argument("--max-requests", type=int, default=500)
    parser.add_argument("--max-optimizer-calls", type=int, default=5)
    parser.add_argument("--optimizer-model", default="gpt-6-luna")
    parser.add_argument('--context-validation-floor',type=int,default=20)
    parser.add_argument('--evaluation-samples',type=int,default=200)
    parser.add_argument('--rubric-recency-allowance',type=float,default=2.)
    parser.add_argument('--rubric-recency-decay-per-class',type=int,default=20)
    parser.add_argument('--rubric-changes-every',type=int,default=2)
    parser.add_argument("--decisions-provider", choices=("jev",), default="jev")
    parser.add_argument("--decisions-model", default="jev-1.13.0")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument('--operational',action='store_true',help='predict before each vote, trace item cycles and separate triggered stages')
    parser.add_argument('--trace-api-url',help='GraphQL endpoint for acknowledged trace logging')
    parser.add_argument('--trace-api-run-id',help='existing web workspace run ID')
    args = parser.parse_args(argv)
    if bool(args.trace_api_url) != bool(args.trace_api_run_id):
        parser.error('trace API URL and run ID must be specified together')
    from dataclasses import asdict
    from decision_flywheel import EvaluationPolicy
    try:
        selection_policy=selection_from_arguments(args)
        evaluation_policy=EvaluationPolicy(args.evaluation_samples,args.rubric_recency_allowance,
                                           args.rubric_recency_decay_per_class)
    except ValueError as error:
        parser.error(str(error))
    if min(args.batch_size, args.max_requests, args.max_optimizer_calls,args.context_validation_floor,args.rubric_changes_every) < 1:
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
    manifest['execution'] = {
        'mode': 'operational' if args.operational else 'batch',
        'optimize_every': args.batch_size, 'retrain_every': args.batch_size,
        'stages': ['rubric', 'questions', 'examples'],
        'decisions_model': args.decisions_model, 'optimizer_model': args.optimizer_model,
        'max_requests': args.max_requests, 'max_optimizer_calls': args.max_optimizer_calls,
        'context_validation_floor':args.context_validation_floor,'cold_start_policy':'provisional-working-rubric',
        'evaluation_policy':asdict(evaluation_policy),
        'selection_policy':asdict(selection_policy) if selection_policy else None,
        'rubric_trigger':{'policy':'label-transitions','every':args.rubric_changes_every},
    }
    path = args.output / "manifest.json"
    encoded = json.dumps(manifest, indent=2)
    if path.exists() and json.loads(path.read_text()) != json.loads(encoded):
        parser.error("frozen replay plan differs; choose a new output directory")
    path.write_text(encoded+"\n")
    counts = lambda rows: {label: sum(row.label == label for row in rows) for label in reviewer_task().labels}
    transition_count=sum(left.label != right.label for left,right in zip(plan.ordered,plan.ordered[1:]))
    rubric_rounds=transition_count//args.rubric_changes_every
    cadence_rounds=len(plan.ordered)//args.batch_size
    # Rubric transitions and the other context stages are independently scheduled.
    optimizer_bound = rubric_rounds+cadence_rounds if args.operational else len(plan.checkpoints)
    requests_bound = len(plan.ordered) * (1+6*optimizer_bound) if args.operational else plan.request_upper_bound
    print(json.dumps({"operational_cycles":args.operational,"eligible_labels": len(plan.ordered), "training": counts(plan.training),
                      "development": counts(plan.development), "protected_audit_pool": counts(plan.scoreboard),
                      "final_full_audit_evaluation": counts(plan.evaluation(len(plan.ordered), reviewer_task().labels)),
                      "checkpoints": plan.checkpoints, "jev_uncached_upper_bound": requests_bound,
                      "optimizer_upper_bound": optimizer_bound, "output": str(args.output)}), flush=True)
    if not args.confirm_live:
        print("Preflight only: no model clients constructed or called.")
        return 0
    if requests_bound > args.max_requests or optimizer_bound > args.max_optimizer_calls:
        parser.error("replay does not fit the approved request ceilings")
    runtime = args.output / "runtime.sqlite3"
    if runtime.exists():
        parser.error("a live replay already exists; use a new output directory rather than silently restart it")
    adapter = JevAdapter.from_environment(configuration=JevConfiguration(model=args.decisions_model))
    transport = OpenAIOptimizer.from_environment(model=args.optimizer_model, max_calls=args.max_optimizer_calls)
    sink = GraphQLTraceSink(args.trace_api_url,args.trace_api_run_id,token=os.environ.get('FLYWHEEL_WEB_TOKEN')) if args.trace_api_url else None
    def observe(event):
        if sink:
            sink(event)
        kind = event["kind"]
        if kind == "optimizer-response":
            proposal = json.loads(event["content"])
            print(json.dumps({"event": kind, "model": event["model"], "proposal": proposal}), flush=True)
        elif kind in {"optimizer-request", "optimizer-failed", "proposal-validated", "fit-started", "fit-completed",
                      "candidate-evaluated", "candidate-rejected", "promoted", "waiting-for-labels", "round-failed", "features-failed"}:
            summary_fields = {'kind', 'event_id', 'cycle_number', 'step_stage', 'requested_model',
                              'reason', 'status', 'promoted', 'training_count', 'error_type'}
            print(json.dumps({k: v for k, v in event.items() if k in summary_fields}), flush=True)
    wheel = DecisionFlywheel(runtime, ClassifierConfig(reviewer_task()), adapter, OptimizerAgent(transport),
                             max_requests=args.max_requests, evaluation_weighting="equal_class",
                             min_evaluation_per_class=2, observer=observe,
                             context_validation_floor=args.context_validation_floor,
                             evaluation_policy=evaluation_policy,
                             selection_policy=selection_policy,
                             redact=tuple(os.environ.get(k, "") for k in ("OPENAI_API_KEY", "TYPESAFE_API_KEY")))
    def checkpoint(report):
        (args.output / "results.json").write_text(json.dumps(report, indent=2)+"\n")
        if args.operational:
            point=report['cycles'][-1]
            print(json.dumps({'cycle':point['cycle_number'],'agreement':point['metrics']['accuracy'],
                             'requests':wheel.requests,'optimizer_calls':transport.calls}),flush=True)
            return
        point = report["checkpoints"][-1]
        print(json.dumps({"checkpoint": point["revealed_labels"], "training_labels": point["training_labels"],
                          "fitted_head": point["fitted_head"], "scoreboard": point["scoreboard"],
                          "recent_balanced_scoreboard": point["recent_balanced_scoreboard"],
                          "requests": wheel.requests}), flush=True)
    try:
        if args.operational:
            report=asyncio.run(run_cycle_replay(wheel,plan,optimize_every=args.batch_size,
                retrain_every=args.batch_size,on_cycle=checkpoint,rubric_changes_every=args.rubric_changes_every))
            print(json.dumps({'complete':'stopped_reason' not in report,'cycles':len(report['cycles']),
                              'jev_attempts':wheel.requests,'optimizer_attempts':transport.calls}),flush=True)
            return int('stopped_reason' in report)
        report = asyncio.run(run_replay(wheel, plan, on_checkpoint=checkpoint))
        failed = any(point.get("round", {}).get("reason") == "round failed"
                     for point in report["checkpoints"] if point.get("round"))
        print(json.dumps({"complete": not failed, "jev_attempts": wheel.requests,
                          "optimizer_attempts": transport.calls}), flush=True)
        return int(failed)
    finally:
        if args.operational and not sink:
            events = read_trace(runtime)
            (args.output/'trace.json').write_text(json.dumps(events, indent=2)+'\n')
            (args.output/'playback.html').write_text(render_trace(events,class_config=[{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]))
        wheel.close()


if __name__ == "__main__":
    raise SystemExit(main())
