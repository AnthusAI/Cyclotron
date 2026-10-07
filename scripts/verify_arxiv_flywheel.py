#!/usr/bin/env python3
"""Explicit bounded live wiring check, never a replacement for a held-out study."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from decision_flywheel.adapters.jev import JevAdapter, JevConfiguration
from decision_flywheel.adapters.openai_optimizer import OpenAIOptimizer
from decision_flywheel.classifier_config import ClassifierConfig
from decision_flywheel.flywheel import DecisionFlywheel
from decision_flywheel.optimizer_agent import OptimizerAgent
from decision_flywheel.reviewer_core import reviewer_item, reviewer_task
from decision_flywheel.reviewer_flywheel import ReviewerFlywheel
from decision_flywheel.reviewer_store import ReviewStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("var/reviewer.sqlite3"))
    parser.add_argument("--runtime", type=Path, default=Path("var/reviewer-runtime.sqlite3"))
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--max-optimizer-calls", type=int, default=2)
    parser.add_argument("--development-limit", type=int, default=6)
    parser.add_argument("--optimizer-model", default="gpt-6-luna")
    parser.add_argument("--decisions-provider", choices=("jev",), default="jev")
    parser.add_argument("--decisions-model", default="jev-1.13.0")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error("explicit --confirm-live is required")
    if min(args.max_requests, args.max_optimizer_calls, args.development_limit) < 1:
        parser.error("ceilings and development limit must be positive")
    if not args.database.is_file():
        parser.error("existing rated database required")
    adapter = JevAdapter.from_environment(configuration=JevConfiguration(model=args.decisions_model))
    transport = OpenAIOptimizer.from_environment(model=args.optimizer_model, max_calls=args.max_optimizer_calls)
    def observe(event):
        if event["kind"] in {"optimizer-request", "optimizer-response", "fit-started", "fit-completed",
                             "candidate-evaluated", "promoted", "candidate-rejected", "round-failed", "features-failed"}:
            print(event["kind"], flush=True)
        elif event["kind"] == "features-requested":
            print(f"Jev attempt {event['requests']}/{event['ceiling']}", flush=True)
    with ReviewStore(args.database, study_seed="arxiv-review-v1") as store:
        original_feedback = store.learning_feedback()
        wheel = DecisionFlywheel(args.runtime, ClassifierConfig(reviewer_task()), adapter, OptimizerAgent(transport),
            observer=observe, max_requests=args.max_requests,
            redact=tuple(os.environ.get(key, "") for key in ("OPENAI_API_KEY", "TYPESAFE_API_KEY")))
        try:
            client = ReviewerFlywheel(store, wheel)
            training, all_development, protected = client.partitions()
            # Fixed ID order, not label or observed-result selection. Unused dev
            # items remain protected from optimizer and fitting.
            development = all_development[:args.development_limit]
            protected = (*protected, *(row.item for row in all_development[args.development_limit:]))
            upper = len(training) + 2 * len(development) + 1
            print(json.dumps({"training": len(training), "development": len(development),
                              "uncached_jev_upper_bound_including_prediction": upper,
                              "jev_ceiling": args.max_requests, "optimizer_ceiling": args.max_optimizer_calls}), flush=True)
            if upper > args.max_requests:
                parser.error("the uncached check does not fit the approved ceiling; reduce the fixed development limit")
            result = asyncio.run(wheel.improve(training, development, protected=protected,
                                               propensities={row.item.id: 1.0 for row in training}))
            article = store.next_unreviewed()
            prediction = None
            if article and result.get("reason") != "round failed":
                prediction = asyncio.run(wheel.predict(reviewer_item(article), training))
            if store.learning_feedback() != original_feedback:
                raise RuntimeError("human feedback changed during the check")
            summary = {"verification": "bounded live wiring check; not a held-out performance study",
                       "round": result, "jev_attempts": wheel.requests, "optimizer_attempts": transport.calls,
                       "active_fitted_head": wheel.active.head is not None,
                       "active_version": wheel.active.fingerprint,
                       "prediction_label": prediction.label if prediction else None,
                       "feedback_unchanged": True}
            summary_path = args.runtime.with_suffix(".verification.json")
            summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(summary), flush=True)
            return 1 if result.get("reason") == "round failed" else 0
        finally:
            wheel.close()


if __name__ == "__main__":
    raise SystemExit(main())
