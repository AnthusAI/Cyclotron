"""Thin article-review application client for the reusable DecisionFlywheel."""
from __future__ import annotations

import asyncio

from .flywheel import DecisionFlywheel, development_assignment
from .reviewer_core import reviewer_item, reviewer_labeled_items
from .reviewer_predictor import ReviewerPrediction
from .reviewer_store import ReviewStore
from .feedback import FeedbackItem, LABEL_SOURCE_VETTED


class ReviewerFlywheel:
    def __init__(self, store: ReviewStore, core: DecisionFlywheel, *, stage="rubric", retrospective_limit=200,
                 min_stage_evaluation_per_class=20):
        self.store, self.core = store, core
        self.stage, self.retrospective_limit = stage, retrospective_limit
        self.min_stage_evaluation_per_class = min_stage_evaluation_per_class
        self.current_cycle = None

    def partitions(self):
        eligible = reviewer_labeled_items(self.store.learning_feedback(), self.store.article)
        votes = self.store._active_actions()
        eligible = tuple(sorted(eligible, key=lambda row: (votes[row.item.id].created_at, row.item.id)))
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
        self.finish_cycle()
        self.current_cycle = self.core.cycle(reviewer_item(article))
        self.current_cycle.__enter__()
        training, development, _ = self.partitions()
        self.core.reconcile_feedback(training, development=development)
        result = asyncio.run(self.core.predict(reviewer_item(article), training))
        return ReviewerPrediction(result.label, result.confidence if result.confidence is not None else .5,
            "jev:flywheel-head" if self.core.active.head else "jev:flywheel-warmup",
            self.core.active.fingerprint, len(training))

    def finish_cycle(self):
        if self.current_cycle:
            self.current_cycle.__exit__(None,None,None)
            self.current_cycle = None

    def feedback_trigger(self, every):
        count = self.core.db.execute("SELECT COUNT(*) FROM runtime_events WHERE json_extract(payload,'$.kind')='human-feedback' AND json_extract(payload,'$.action')='submitted' AND json_extract(payload,'$.cycle_id') IS NOT NULL").fetchone()[0]
        due = count > 0 and count % every == 0
        if self.current_cycle:
            self.current_cycle.check_trigger(self.stage,due=due,
                reason='feedback cadence reached' if due else 'feedback cadence not reached',
                details={'feedback_count':count,'threshold':every})
        return due

    def improve(self, *, retry_interrupted: bool = False, stage=None, trigger="manual"):
        if self.current_cycle:
            return self._improve(retry_interrupted=retry_interrupted, stage=stage, trigger=trigger)
        with self.core.cycle(None, reason='reviewer-maintenance') as cycle:
            self.current_cycle = cycle
            try:
                return self._improve(retry_interrupted=retry_interrupted, stage=stage, trigger=trigger)
            finally:
                self.current_cycle = None

    def _improve(self, *, retry_interrupted: bool = False, stage=None, trigger="manual"):
        if self.current_cycle and trigger != 'feedback-cadence':
            self.current_cycle.check_trigger(stage or self.stage,due=True,reason=trigger)
        self.sync_optimizer_context()
        training, development, protected = self.partitions()
        selected_stage = stage or self.stage
        if retry_interrupted and stage is None:
            selected_stage = next((event["stage"] for event in reversed(self.history())
                                   if event["kind"] in {"optimization-stage-started", "optimization-stage-failed"}), self.stage)
            selected_stage = next((event["step_stage"] for event in reversed(self.history())
                                  if event["kind"] in {"step-paused", "step-failed", "step-started"}), selected_stage)
        # This demo reviews every displayed item. A future sampled reviewer must
        # pass its actual recorded review propensities instead of this full-review policy.
        try:
            async def advance():
                traced = await self.core.step(selected_stage, training, development, protected=protected,
                                            propensities={row.item.id: 1.0 for row in training},
                                            retry_interrupted=retry_interrupted, limit=self.retrospective_limit,
                                            min_development_per_class=self.min_stage_evaluation_per_class,
                                            trigger="reviewer-retry" if retry_interrupted else trigger)
                result = traced.get("result", {"stage": selected_stage, "promoted": False,
                                               "reason": traced.get("reason"), "status": traced["status"]})
                if selected_stage == "questions" and traced["status"] == "completed":
                    fitted = await self.core.step("classifier", training, development, protected=protected,
                        propensities={row.item.id: 1. for row in training}, retry_interrupted=retry_interrupted,
                        min_development_per_class=self.min_stage_evaluation_per_class, trigger="reviewer-question-handoff",
                        parent_step_id=traced["step_id"])
                    result["classifier_training"] = fitted.get("result", {"status": fitted["status"], "reason": fitted.get("reason")})
                return result
            return asyncio.run(advance())
        except Exception as error:
            result = {"stage": selected_stage, "promoted": False, "error_type": getattr(error, "error_type", type(error).__name__),
                      "reason": "stage failed; saved state retained, explicit retry required"}
            self.core._emit({"kind": "optimization-stage-failed", **result})
            return result

    def sync_optimizer_context(self):
        """Use the user's explanations as guidance, never as extra fit labels.

        This reviewer intentionally includes protected-role comments. Their
        reuse is visible and invalidates independent evaluation claims.
        """
        training, _, _ = self.partitions()
        training_ids = {row.item.id for row in training}
        comments = [(item_id, event.comment) for item_id, event in self.store._active_actions().items()
                    if event.action == "vote" and event.comment and event.comment.strip()]
        self.core.set_optimizer_context(tuple(comment for _, comment in comments),
            evaluation_context_exposed=any(item_id not in training_ids for item_id, _ in comments))
        return self.core.optimizer_context

    def reconcile(self):
        training, development, _ = self.partitions()
        self.core.reconcile_feedback(training, development=development)

    def record_review_event(self, event):
        action = "submitted"
        if event.action == "undo":
            action = "retracted"
            event = next(e for e in self.store.events_for(event.article_id) if e.id == event.undoes_event_id)
        if event.action != "vote":
            return None
        presentation = next((p for p in self.store.presentations_for(event.article_id) if p.id == event.presentation_id), None)
        feedback = FeedbackItem(event.id, event.article_id, self.core.initial.task.name,
            initial_answer_value=presentation.predicted_label if presentation else None,
            final_answer_value=event.label, edit_comment_value=event.comment,
            label_source=LABEL_SOURCE_VETTED, selection_propensity=1., review_provenance="human-reviewed")
        assignment = self.store.assignment_for(event.article_id)
        if assignment == "train" and development_assignment(self.store.study_seed, event.article_id):
            assignment = "development"
        return self.core.record_feedback_event(feedback, action=action, assignment=assignment)

    def hypotheses(self):
        return self.core.hypotheses()

    def feature_bank(self):
        return self.core.feature_bank()

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
                        "classifier-invalidated", "control-cycle-completed", "optimization-stage-completed",
                        "question-ranking-completed", "optimization-stage-failed", "classifier-training-completed",
                        "step-paused", "step-failed"}), None)
        def definition(task):
            return {"name": task.name, "instructions": task.instructions, "labels": list(task.labels)}
        return {"version": active.fingerprint, "rubric": active.config.rubric,
                "optimizer_context": self.core.optimizer_context,
                "optimization_stage": self.stage, "retrospective_limit": self.retrospective_limit,
                "stage_evaluation_floor": self.min_stage_evaluation_per_class,
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
                "feature_bank": self.feature_bank(),
                "training_count": len(training), "development_count": len(development),
                "by_label": {label: sum(row.label == label for row in training)
                             for label in active.config.task.labels},
                "requests": self.core.requests, "ceiling": self.core.max_requests, "latest": outcome}

    def history(self):
        return self.core.history(1000)
