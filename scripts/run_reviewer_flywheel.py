#!/usr/bin/env python3
"""Run the reusable Decision Flywheel against eligible article-review feedback.

This is the sole paid entry point for the reviewer integration.  It requires a
human ``--confirm-live`` flag, uses the existing generic example-list optimizer,
and writes a text-free report the reviewer can display.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from decision_flywheel.adapters.jev import JevAdapter, JevConfiguration
from decision_flywheel.artifacts import ArtifactValidationError, create_artifact, load_compatible_fixed_incumbent
from decision_flywheel.example_list import improve_example_list
from decision_flywheel.events import JsonlEventStream
from decision_flywheel.run_ledger import FlywheelRound, JsonlRunLedger, TrialActivity, feedback_fingerprint
from decision_flywheel.reviewer_core import reviewer_labeled_items, reviewer_task
from decision_flywheel.reviewer_store import ReviewStore


def _report(improvement, labels, comments: int, hard_examples: int, *, artifact_path: Path, pool_revision: str,
            artifact_hash: str) -> dict[str, object]:
    optimization = improvement.optimization
    planned_trials = dict(improvement.round.trials)
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": optimization.model_fingerprint,
        "training_feedback": {"total": len(labels), "comments": comments,
                              "hard_jev_corrections": hard_examples,
                              "by_label": {label: sum(row.label == label for row in labels)
                                           for label in ("include", "exclude")}},
        "development": {"count": len(improvement.round.development),
                        "ids": [row.item.id for row in improvement.round.development]},
        "round": {"incumbent_source": improvement.round.incumbent_source,
                  "candidate_count": len(improvement.round.candidates),
                  "development_count": len(improvement.round.development)},
        "calls": {"attempted": optimization.model_calls_attempted,
                  "succeeded": optimization.model_calls_succeeded,
                  "ceiling": optimization.max_model_calls,
                  "checkpoint_entries": optimization.checkpoint_entries},
        "objective": optimization.objective,
        "trials": [
            {"name": trial.trial_name, "status": trial.status, "objective": trial.objective,
             "failure_reasons": list(trial.failure_reasons),
             "decision_count": len(trial.decisions),
             "from_cache": sum(decision.from_checkpoint for decision in trial.decisions),
             # IDs and counts make the optimizer's actual work inspectable while
             # retaining the report's no-article-text contract.
             "example_ids": list(planned_trials[trial.trial_name].example_ids),
             "reserve_ids": list(planned_trials[trial.trial_name].reserve_ids),
             "example_count": len(planned_trials[trial.trial_name].example_ids),
             "reserve_count": len(planned_trials[trial.trial_name].reserve_ids)}
            for trial in optimization.trials
        ],
        "winner": improvement.winner_trial,
        "promoted": improvement.promoted,
        "reason": improvement.reason,
        "scores": improvement.scores,
        "artifact": {"path": str(artifact_path), "pool_revision": pool_revision, "hash": artifact_hash},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one measured Jev Decision Flywheel round for article review")
    parser.add_argument("--database", type=Path, default=Path("var/reviewer.sqlite3"))
    parser.add_argument("--report", type=Path, default=Path("var/reviewer-flywheel.json"))
    parser.add_argument("--checkpoint", type=Path, default=Path("var/reviewer-flywheel-cache.json"))
    parser.add_argument("--artifact", type=Path, default=Path("var/reviewer-flywheel-artifact.json"))
    parser.add_argument("--ledger", type=Path, default=Path("var/reviewer-flywheel-runs.jsonl"),
                        help="append-only text-free measured-round ledger")
    parser.add_argument("--events", type=Path, default=Path("var/reviewer-flywheel-events.jsonl"),
                        help="append-only text-free live optimizer events")
    parser.add_argument("--max-requests", type=int, default=24)
    parser.add_argument("--model", default="jev-latest")
    parser.add_argument("--confirm-live", action="store_true",
                        help="required: authorizes this bounded paid Jev optimization round")
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error("refusing a paid run without --confirm-live")
    if args.max_requests < 1:
        parser.error("--max-requests must be positive")
    with ReviewStore(args.database, study_seed="arxiv-review-v1") as store:
        feedback = store.learning_feedback()
        labels = reviewer_labeled_items(feedback, store.article)
        hard_demo_ids = store.hard_learning_example_ids()
    counts = {label: sum(row.label == label for row in labels) for label in ("include", "exclude")}
    if min(counts.values()) < 3:
        parser.error("need at least three eligible Include and three eligible Exclude labels before optimization")
    try:
        checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8")) if args.checkpoint.exists() else {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("optimizer checkpoint is unreadable") from error
    if not isinstance(checkpoint, dict):
        raise ValueError("optimizer checkpoint must be a JSON object")
    adapter = JevAdapter.from_environment(configuration=JevConfiguration(model=args.model))
    event_stream = JsonlEventStream(args.events)

    def observe(event) -> None:
        event_stream.append(event)
        if event.event_type in {"trial-started", "trial-completed", "trial-incomplete", "round-completed"}:
            scope = event.trial_name or "round"
            print(f"[{scope}] {event.event_type}: {event.calls_attempted} attempted, "
                  f"{event.calls_succeeded} succeeded")

    incumbent = None
    if args.artifact.exists():
        try:
            incumbent = load_compatible_fixed_incumbent(
                args.artifact.read_text(encoding="utf-8"), reviewer_task(), labels)
        except (OSError, ArtifactValidationError) as error:
            parser.error(f"cannot reuse the previous frozen policy as this round's incumbent: {error}")
    improvement = asyncio.run(improve_example_list(
        reviewer_task(), labels, adapter, max_model_calls=args.max_requests, per_label=2, dev_max=6,
        incumbent=incumbent, hard_demo_ids=hard_demo_ids, checkpoint=checkpoint,
        model_fingerprint=adapter.model_identity, event_sink=observe,
    ))
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    args.checkpoint.write_text(json.dumps(checkpoint, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if improvement.optimization.winner is None:
        raise RuntimeError("an incomplete search cannot produce a live artifact")
    pool_revision = f"reviewer-{improvement.optimization.candidate_pool_fingerprint[:16]}"
    artifact = create_artifact(reviewer_task(), improvement.round.candidates, pool_revision, improvement.optimization)
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(artifact + "\n", encoding="utf-8")
    artifact_hash = json.loads(artifact)["artifact_hash"]
    report = _report(improvement, labels, sum(row.comment is not None for row in feedback), len(hard_demo_ids),
                     artifact_path=args.artifact, pool_revision=pool_revision, artifact_hash=artifact_hash)
    outcome = ("promoted" if improvement.promoted else "incomplete"
               if improvement.optimization.winner is None else "incumbent-retained")
    ledger_round = FlywheelRound(
        task_fingerprint=reviewer_task().fingerprint,
        input_fingerprint=feedback_fingerprint(reviewer_task(), labels),
        active_policy_fingerprint=improvement.winner.fingerprint,
        model_fingerprint=improvement.optimization.model_fingerprint,
        feedback_count=len(labels), candidate_count=len(improvement.round.candidates),
        development_count=len(improvement.round.development), objective_name=improvement.optimization.objective,
        winner=improvement.winner_trial, promoted=improvement.promoted, outcome=outcome,
        calls_attempted=improvement.optimization.model_calls_attempted,
        calls_succeeded=improvement.optimization.model_calls_succeeded,
        trials=tuple(TrialActivity(trial.trial_name, trial.status, len(trial.decisions),
                                   sum(decision.from_checkpoint for decision in trial.decisions), trial.objective)
                     for trial in improvement.optimization.trials),
    )
    recorded = JsonlRunLedger(args.ledger).append(ledger_round)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote measured Jev flywheel report to {args.report}")
    print(f"{report['reason']} ({report['calls']['attempted']} calls, {report['development']['count']} development items)")
    print("Recorded a new measured round in the run ledger." if recorded else "Measured round already present in the run ledger.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
