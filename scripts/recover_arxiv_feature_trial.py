"""Recover a locally failed diagnostic fit, preserving the original experiment."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from decision_flywheel import ClassifierConfig, DecisionFlywheel, OptimizerAgent
from decision_flywheel.adapters.jev import JevAdapter, JevConfiguration
from decision_flywheel.feature_bank import FeatureBank
from decision_flywheel.flywheel import _restore
from decision_flywheel.replay import plan_replay
from decision_flywheel.reviewer_core import reviewer_task, reviewer_labeled_items
from decision_flywheel.reviewer_store import ReviewStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("var/arxiv-features-v1"))
    parser.add_argument("--snapshot", type=Path, default=Path("var/arxiv-replay-v2/reviews.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("var/arxiv-features-v1-recovered"))
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--max-requests", type=int, default=20)
    args = parser.parse_args(argv)
    original = json.loads((args.source / "results.json").read_text())
    remaining = original["protocol"]["request_upper_bound"] - original["jev_attempts"]
    failed = [trial for trial in original["result"]["trials"] if trial.get("feature_id") and trial.get("error_type")]
    if len(failed) != 1:
        parser.error("recovery requires exactly one failed individual feature trial")
    if not 0 <= args.max_requests <= remaining:
        parser.error("recovery ceiling exceeds the originally authorized remaining requests")
    print(json.dumps({"recovery_request_ceiling": args.max_requests, "optimizer_calls": 0,
                      "original_attempts": original["jev_attempts"], "original_failure": failed[0]["error_type"]}), flush=True)
    if not args.confirm_live:
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    runtime = args.output / "runtime.sqlite3"
    if runtime.exists():
        parser.error("recovery already exists; no automatic paid rerun")
    with sqlite3.connect(args.snapshot.resolve().as_uri()+"?mode=ro", uri=True) as db:
        metadata = dict(db.execute("SELECT key,value FROM study_metadata"))
    with ReviewStore(args.snapshot, study_seed=metadata["study_seed"],
                     rolling_audit_rate=float(metadata["rolling_audit_rate"]),
                     final_audit_rate=float(metadata["final_audit_rate"])) as store:
        rows = {row.item.id: row for row in reviewer_labeled_items(store.learning_feedback(), store.article)}
        votes = store._active_actions()
        ordered = tuple(rows[key] for key in sorted(rows, key=lambda key: (votes[key].created_at, key)))
        plan = plan_replay(reviewer_task(), ordered)
    if json.loads(json.dumps(plan.manifest(reviewer_task()))) != original["protocol"]["plan"]:
        parser.error("snapshot no longer matches the frozen experiment")
    with sqlite3.connect((args.source / "runtime.sqlite3").resolve().as_uri()+"?mode=ro", uri=True) as source:
        with sqlite3.connect(runtime) as destination:
            source.backup(destination)
    def no_optimizer(_):
        raise AssertionError("recovery reuses the original question; no optimizer calls allowed")
    adapter = JevAdapter.from_environment(configuration=JevConfiguration(model=original["protocol"]["decisions_model"]))
    wheel = DecisionFlywheel(runtime, ClassifierConfig(reviewer_task()), adapter, OptimizerAgent(no_optimizer),
                             max_requests=args.max_requests, training_class_weighting=original["protocol"]["training_class_weighting"],
                             min_evaluation_per_class=2)
    saved = [json.loads(row[0]) for row in wheel.db.execute("SELECT payload FROM runtime_rounds WHERE status='complete'")]
    baseline = next(row["active"] for row in saved if row["result"].get("error_type"))
    wheel._activate(_restore(baseline))
    feature = next(entry for entry in original["feature_bank"] if entry["id"] == failed[0]["feature_id"])
    proposal = {"tasks": [feature["question"]],
                "rationale": "Recover unchanged question after fixing diagnostics rounding tolerance; reuse complete request cache."}
    async def run():
        result = await wheel.improve(plan.training, plan.development, protected=tuple(r.item for r in plan.scoreboard),
            propensities={r.item.id: 1. for r in plan.training}, candidate_proposal=proposal, apply_promotion=False)
        if result.get("error_type"):
            raise RuntimeError("recovery failed; inspect the private runtime, do not silently repay")
        trials = [({**trial, **result, "original_failure": trial} if trial is failed[0] else trial)
                  for trial in original["result"]["trials"]]
        eligible = [trial for trial in trials if trial.get("improved")]
        best = min(eligible, key=lambda trial: (trial["candidate"]["balanced_brier"], trial["idea_id"])) if eligible else None
        if best:
            wheel.promote_trial(best["trial_fingerprint"], plan.training, plan.development)
        FeatureBank(wheel.db).record(feature["id"], result,
                                    result["feature_diagnostics"][feature["question"]["name"]])
        audit = await wheel._score(wheel.active, plan.evaluation(len(plan.ordered), reviewer_task().labels),
                                   plan.training, datetime.now(timezone.utc))
        report = {**original, "recovery": {"original_directory": str(args.source), "new_requests": wheel.requests,
                    "reason": proposal["rationale"]}, "jev_attempts": original["jev_attempts"]+wheel.requests,
                  "feature_bank": wheel.feature_bank(), "audit_after": audit,
                  "result": {**original["result"], "trials": trials, "selected_control": best["control"] if best else None,
                             "promoted": best is not None}}
        (args.output / "results.json").write_text(json.dumps(report, indent=2)+"\n")
        print(json.dumps({"total_jev_attempts": report["jev_attempts"], "recovery_requests": wheel.requests,
                          "selected_control": report["result"]["selected_control"], "recovered_trial": result,
                          "audit_after": audit}), flush=True)
    try:
        asyncio.run(run())
        return 0
    finally:
        wheel.close()


if __name__ == "__main__":
    raise SystemExit(main())
