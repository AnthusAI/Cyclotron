"""Thin article-review application client for the reusable DecisionFlywheel."""
from __future__ import annotations

import asyncio

from .flywheel import DecisionFlywheel, development_assignment
from .reviewer_core import reviewer_item, reviewer_labeled_items
from .reviewer_predictor import ReviewerPrediction
from .reviewer_store import ReviewStore


class ReviewerFlywheel:
    def __init__(self, store: ReviewStore, core: DecisionFlywheel):
        self.store, self.core = store, core

    def partitions(self):
        eligible = reviewer_labeled_items(self.store.learning_feedback(), self.store.article)
        development = tuple(row for row in eligible if development_assignment(self.store.study_seed, row.item.id))
        training = tuple(row for row in eligible if not development_assignment(self.store.study_seed, row.item.id))
        training_ids = {row.item.id for row in training}
        development_ids = {row.item.id for row in development}
        protected = tuple(reviewer_item(article) for article in self.store.articles()
                          if article.id not in training_ids | development_ids and
                          (self.store.assignment_for(article.id) != "train" or
                           development_assignment(self.store.study_seed, article.id)))
        return training, development, protected

    def predict(self, article):
        training, development, _ = self.partitions()
        self.core.reconcile_feedback(training, development=development)
        result = asyncio.run(self.core.predict(reviewer_item(article), training))
        return ReviewerPrediction(result.label, result.confidence if result.confidence is not None else .5,
            "jev:flywheel-head" if self.core.active.head else "jev:flywheel-warmup",
            self.core.active.fingerprint, len(training))

    def improve(self, *, retry_interrupted: bool = False):
        training, development, protected = self.partitions()
        # This demo reviews every displayed item. A future sampled reviewer must
        # pass its actual recorded review propensities instead of this full-review policy.
        return asyncio.run(self.core.improve_controls(training, development, protected=protected,
                                            propensities={row.item.id: 1.0 for row in training},
                                            retry_interrupted=retry_interrupted))

    def reconcile(self):
        training, development, _ = self.partitions()
        self.core.reconcile_feedback(training, development=development)

    def hypotheses(self):
        return self.core.hypotheses()

    def retry_hypothesis(self, hypothesis_id):
        training, development, protected = self.partitions()
        return asyncio.run(self.core.retry_hypothesis(hypothesis_id, training, development,
                          protected=protected, propensities={row.item.id: 1. for row in training}))

    def status(self):
        training, development, _ = self.partitions()
        active = self.core.active
        events = self.history()
        outcome = next((event for event in reversed(events) if event["kind"] in
                       {"promoted", "candidate-rejected", "round-failed", "round-interrupted", "waiting-for-labels",
                        "classifier-invalidated", "control-cycle-completed"}), None)
        def definition(task):
            return {"name": task.name, "instructions": task.instructions, "labels": list(task.labels)}
        return {"version": active.fingerprint, "rubric": active.config.rubric,
                "tasks": [task.name for task in active.config.tasks],
                "main_decision": definition(active.config.task),
                "task_definitions": [definition(task) for task in active.config.tasks],
                "dynamic_elements": list(active.config.dynamic_elements),
                "evaluation_weighting": self.core.evaluation_weighting,
                "training_class_weighting": self.core.training_class_weighting,
                "minimum_development_per_class": self.core.min_evaluation_per_class,
                "optimizer_requests_recorded": sum(event["kind"] == "optimizer-request" for event in events),
                "optimizer_responses_recorded": sum(event["kind"] == "optimizer-response" for event in events),
                "example_ids": list(active.config.example_ids), "fitted_head": active.head is not None,
                "features": list(active.head.feature_names) if active.head else [],
                "training_count": len(training), "development_count": len(development),
                "by_label": {label: sum(row.label == label for row in training)
                             for label in active.config.task.labels},
                "requests": self.core.requests, "ceiling": self.core.max_requests, "latest": outcome}

    def history(self):
        return self.core.history(1000)
