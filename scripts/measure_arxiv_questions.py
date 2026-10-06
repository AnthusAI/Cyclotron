"""Freeze the live reviewer's context, discover questions, and backfill eligible labels."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3

from decision_flywheel import ClassifierConfig, DecisionFlywheel, OptimizerAgent, select_window
from decision_flywheel.adapters.jev import JevAdapter, JevConfiguration
from decision_flywheel.adapters.openai_optimizer import OpenAIOptimizer
from decision_flywheel.reviewer_core import reviewer_task
from decision_flywheel.reviewer_flywheel import ReviewerFlywheel
from decision_flywheel.reviewer_store import ReviewStore


class Offline:
    model_identity = "preflight-only"


class NoOptimizer:
    calls = 0
    def __call__(self, _):
        raise RuntimeError("recovery must reuse the recorded optimizer response, not make another call")


def forbidden(_):
    raise AssertionError("preflight has no model transport")


def backup(source, destination):
    with sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True) as original:
        with sqlite3.connect(destination) as copied:
            original.backup(copied)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("var/reviewer.sqlite3"))
    parser.add_argument("--runtime", type=Path, default=Path("var/reviewer-runtime.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("var/arxiv-staged-backfill-v1"))
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--resume", action="store_true", help="explicitly resume cached collection; no new optimizer call")
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--max-optimizer-calls", type=int, default=1)
    parser.add_argument("--decisions-model", default="jev-1.13.0")
    parser.add_argument("--decisions-provider", choices=("jev",), default="jev")
    parser.add_argument("--optimizer-model", default="gpt-6-luna")
    args = parser.parse_args(argv)
    if args.limit < 1 or not args.database.is_file() or not args.runtime.is_file():
        parser.error("existing review/runtime databases and a positive window limit are required")
    args.output.mkdir(parents=True, exist_ok=True)
    reviews, runtime = args.output / "reviews.sqlite3", args.output / "runtime.sqlite3"
    preflight = args.output / "preflight.json"
    if not preflight.exists():
        if reviews.exists() or runtime.exists():
            parser.error("partial freeze exists; use a fresh output directory")
        backup(args.database, reviews)
        backup(args.runtime, runtime)
    elif (args.output / "started.json").exists() and args.confirm_live and not args.resume:
        parser.error("collection already started; no automatic paid rerun")
    with sqlite3.connect(reviews) as db:
        metadata = dict(db.execute("SELECT key,value FROM study_metadata"))
    with ReviewStore(reviews, study_seed=metadata["study_seed"],
                     rolling_audit_rate=float(metadata["rolling_audit_rate"]),
                     final_audit_rate=float(metadata["final_audit_rate"])) as store:
        with sqlite3.connect(runtime) as db:
            contract = json.loads(db.execute("SELECT value FROM runtime_state WHERE key='contract'").fetchone()[0])
        offline = Offline()
        offline.model_identity = contract["model"]
        dry = DecisionFlywheel(runtime, ClassifierConfig(reviewer_task()), offline, OptimizerAgent(forbidden))
        training, development, protected = ReviewerFlywheel(store, dry).partitions()
        original_version = dry.active.config.fingerprint
        dry.reconcile_feedback(training, development=development)
        if dry.active.config.fingerprint != original_version:
            dry.close()
            parser.error("saved active context is invalidated by this feedback; resolve reviewer state before measuring")
        props = {row.item.id: 1. for row in training}
        dry._validate_partitions(training, development, protected, props)
        window = select_window(training, reviewer_task().labels, args.limit)
        protocol = {"stage": "questions", "limit": args.limit, "actual_window": len(window),
                    "by_class": {label: sum(row.label == label for row in window) for label in reviewer_task().labels},
                    "context_version": original_version, "rubric_present": bool(dry.active.config.rubric),
                    "example_count": len(dry.active.config.example_ids), "existing_questions": [t.name for t in dry.active.config.tasks],
                    "window_ids": [r.item.id for r in window], "feedback_evidence": dry._evidence(window),
                    "request_upper_bound": len(window), "optimizer_upper_bound": 1,
                    "decisions_model": args.decisions_model, "optimizer_model": args.optimizer_model,
                    "ranker": "stratified OOF soft contingency; fixed context, retrospective discovery only"}
        dry.close()
        if preflight.exists() and json.loads(preflight.read_text()) != protocol:
            parser.error("arguments no longer match the frozen preflight")
        preflight.write_text(json.dumps(protocol, indent=2)+"\n")
        print(json.dumps({k: v for k, v in protocol.items() if k not in {"window_ids", "feedback_evidence"}}), flush=True)
        if not args.confirm_live:
            return 0
        if args.max_requests < len(window) or args.max_optimizer_calls < (0 if args.resume else 1):
            parser.error("explicit ceilings do not cover frozen preflight")
        if not (args.output / "started.json").exists():
            (args.output / "started.json").write_text(json.dumps({"max_requests": args.max_requests,
                                                                "max_optimizer_calls": args.max_optimizer_calls})+"\n")
        model = JevAdapter.from_environment(configuration=JevConfiguration(model=args.decisions_model))
        optimizer = NoOptimizer() if args.resume else OpenAIOptimizer.from_environment(model=args.optimizer_model, max_calls=args.max_optimizer_calls)
        def observe(event):
            if event["kind"] in {"optimization-stage-started", "question-backfill-started", "question-backfill-progress"}:
                print(json.dumps(event), flush=True)
            elif event["kind"] == "optimizer-response":
                print(json.dumps({"event": "optimizer-response", "model": event["model"],
                                  "proposal": json.loads(event["content"])}), flush=True)
        wheel = DecisionFlywheel(runtime, ClassifierConfig(reviewer_task()), model, OptimizerAgent(optimizer),
                                 max_requests=args.max_requests, observer=observe)
        before = wheel.active.fingerprint
        try:
            report = asyncio.run(wheel.optimize_stage("questions", training, development,
                protected=protected, propensities=props, limit=args.limit, retry_interrupted=args.resume))
            if wheel.active.fingerprint != before:
                raise RuntimeError("measurement must not replace the deployed classifier")
            events = [(row[0], json.loads(row[1])) for row in wheel.db.execute("SELECT id,payload FROM runtime_events")]
            start = min(index for index, event in events if event["kind"] == "optimization-stage-started"
                        and event.get("stage") == "questions" and event.get("context_version") == protocol["context_version"])
            jev_attempts = sum(index > start and event["kind"] == "features-requested" for index, event in events)
            optimizer_attempts = sum(index > start and event["kind"] == "optimizer-response" for index, event in events)
            report = {**report, "jev_attempts": jev_attempts, "optimizer_attempts": optimizer_attempts,
                      "active_unchanged": True, "protocol": protocol}
            (args.output / "results.json").write_text(json.dumps(report, indent=2)+"\n")
            print(json.dumps({"count": report.get("count"), "by_class": report.get("by_class"),
                              "jev_attempts": jev_attempts, "optimizer_attempts": optimizer_attempts,
                              "rankings": [{"name": rank["question"]["name"], "cross_validated": rank["cross_validated"]}
                                           for rank in report.get("rankings", [])]}), flush=True)
            return 0
        finally:
            wheel.close()


if __name__ == "__main__":
    raise SystemExit(main())
