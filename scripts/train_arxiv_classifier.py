"""Bounded development-only classifier training from frozen human feedback."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3

from decision_flywheel import ClassifierConfig, DecisionFlywheel, OptimizerAgent
from decision_flywheel.adapters.jev import JevAdapter, JevConfiguration
from decision_flywheel.classifier_training import train_classifier
from decision_flywheel.reviewer_core import reviewer_task
from decision_flywheel.reviewer_flywheel import ReviewerFlywheel
from decision_flywheel.reviewer_store import ReviewStore
from measure_arxiv_questions import backup


def forbidden(_):
    raise AssertionError("classifier training cannot invoke the optimizer")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="frozen backfill directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--max-requests", type=int, default=174)
    parser.add_argument("--exploratory-min-per-class", type=int, default=2)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "started.json").exists():
        parser.error("collection already started; no automatic paid rerun")
    for name in ("reviews.sqlite3", "runtime.sqlite3"):
        if not (args.output / name).exists():
            backup(args.source / name, args.output / name)
    with sqlite3.connect(args.output / "reviews.sqlite3") as db:
        metadata = dict(db.execute("SELECT key,value FROM study_metadata"))
    with sqlite3.connect(args.output / "runtime.sqlite3") as db:
        contract = json.loads(db.execute("SELECT value FROM runtime_state WHERE key='contract'").fetchone()[0])
    offline = type("Offline", (), {"model_identity": contract["model"]})()
    store = ReviewStore(args.output / "reviews.sqlite3", study_seed=metadata["study_seed"],
        rolling_audit_rate=float(metadata["rolling_audit_rate"]), final_audit_rate=float(metadata["final_audit_rate"]))
    wheel = DecisionFlywheel(args.output / "runtime.sqlite3", ClassifierConfig(reviewer_task()), offline, OptimizerAgent(forbidden))
    training, development, protected = ReviewerFlywheel(store, wheel).partitions()
    before = wheel.active.fingerprint
    wheel.reconcile_feedback(training, development=development)
    if before != wheel.active.fingerprint:
        parser.error("saved classifier invalidated by current feedback")
    protocol = {"training_counts": {l: sum(r.label == l for r in training) for l in reviewer_task().labels},
        "development_counts": {l: sum(r.label == l for r in development) for l in reviewer_task().labels},
        "request_upper_bound": 2 * (len(training) + len(development)), "optimizer_calls": 0,
        "final_audit_used": False, "automatic_deployment": False, "classifier_version": before}
    wheel.close()
    print(json.dumps(protocol), flush=True)
    (args.output / "preflight.json").write_text(json.dumps(protocol, indent=2)+"\n")
    if not args.confirm_live:
        store.close()
        return
    if args.max_requests < protocol["request_upper_bound"]:
        parser.error("ceiling does not cover preflight")
    (args.output / "started.json").write_text(json.dumps({"max_requests": args.max_requests})+"\n")
    model = JevAdapter.from_environment(configuration=JevConfiguration(model="jev-1.13.0"))
    def observe(event):
        if event["kind"] in {"fit-started", "fit-completed", "candidate-evaluated"}:
            print(json.dumps(event), flush=True)
    wheel = DecisionFlywheel(args.output / "runtime.sqlite3", ClassifierConfig(reviewer_task()), model,
        OptimizerAgent(forbidden), max_requests=args.max_requests, observer=observe)
    try:
        result = asyncio.run(train_classifier(wheel, training, development, protected=protected,
            propensities={r.item.id: 1. for r in training}, min_development_per_class=args.exploratory_min_per_class,
            apply_promotion=False))
        assert wheel.active.fingerprint == before
        result.update(protocol=protocol, new_requests=wheel.requests, scope="exploratory development; not final accuracy")
        (args.output / "results.json").write_text(json.dumps(result, indent=2)+"\n")
        print(json.dumps({"new_requests": wheel.requests, "selected": result["selected"], "output": str(args.output)}), flush=True)
    finally:
        wheel.close()
        store.close()


if __name__ == "__main__":
    main()
