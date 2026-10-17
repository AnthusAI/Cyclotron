"""Freeze held-out sets from an embedded cyclotron's audit reviews and score them.

``HeldOutEvaluation`` is the engine behind the cyclotron's held-out methods
(``freeze_held_out_set``, ``held_out_sets``, ``held_out_reports``,
``held_out_request_bound`` and ``evaluate_held_out``). It reads the engine's
stores and wheels and keeps its sets and reports in the root store. The caller
owns execution: ``evaluate`` runs inside the engine's execution scope.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .classifier_config import ClassifiedAnswers
from .embedded_records import Review, ReviewSelection, _json
from .flywheel import DecisionFlywheel, FittedClassifier
from .held_out import (
    AFTER_DESCRIPTION,
    BEFORE_DESCRIPTION,
    DEFAULT_MINIMUM_PER_LABEL,
    REPORTS_KEPT_PER_CLASSIFIER,
    Endpoint,
    HeldOutItem,
    HeldOutSet,
    build_report,
)
from .models import DecisionResult, Item


class HeldOutEvaluation:
    def __init__(self, engine):
        self.engine = engine

    def only_or_named_classifier(self, classifier: str | None) -> str:
        specs = self.engine.specs
        if classifier is None:
            if len(specs) != 1:
                raise ValueError("name the classifier; this cyclotron has several")
            return next(iter(specs))
        if classifier not in specs:
            raise ValueError("classifier not in this cyclotron")
        return classifier

    def active_audit_reviews(self, classifier: str) -> dict[str, tuple[Review, ReviewSelection]]:
        """The latest active review of each audit-share item, with how its decision was sent to review."""
        engine = self.engine
        audit_item_ids = {item.id for item in engine._partitions(classifier)[2]}
        latest_by_item: dict[str, tuple[Review, ReviewSelection]] = {}
        for decision_id, item_id in engine.root_storage.decision_items():
            if item_id not in audit_item_ids:
                continue
            review = engine._active_review(decision_id, classifier)
            if review is None or review.selected_by == "reviewer":
                continue
            latest_by_item[item_id] = (review, engine.decision(decision_id).review.for_output(classifier))
        return latest_by_item

    def legacy_sets(self, classifier: str) -> list[dict[str, Any]]:
        """Sets saved before each set had its own record: one list per classifier, oldest first."""
        row = self.engine.root_storage.meta(f"held_out_sets:{classifier}")
        return json.loads(row[0]) if row else []

    def set_ids(self, classifier: str) -> list[str]:
        """Every frozen set's identity, oldest first: legacy sets, then the sets indexed one record each."""
        row = self.engine.root_storage.meta(f"held_out_set_ids:{classifier}")
        indexed = json.loads(row[0]) if row else []
        legacy = [stored["id"] for stored in self.legacy_sets(classifier)]
        return [*legacy, *(set_id for set_id in indexed if set_id not in legacy)]

    def stored_set(self, classifier: str, set_id: str) -> dict[str, Any] | None:
        row = self.engine.root_storage.meta(f"held_out_set:{classifier}:{set_id}")
        if row is not None:
            return json.loads(row[0])
        return next((stored for stored in self.legacy_sets(classifier) if stored["id"] == set_id), None)

    def freeze(self, classifier: str | None = None) -> dict[str, Any]:
        """Freeze the labeled audit items so far into a held-out set that never changes. No model call.

        Returns the set's summary. Freezing again with no new audit labels
        returns the same set; with new labels, it adds a newer set.
        """
        engine = self.engine
        classifier = self.only_or_named_classifier(classifier)
        engine._sync()
        values = engine._item_values()
        candidates = [
            HeldOutItem(item_id, values[item_id], review.label, selection.propensity, selection.reason)
            for item_id, (review, selection) in self.active_audit_reviews(classifier).items()
        ]
        held_out_set = HeldOutSet.freeze(engine.definition.id, classifier, candidates)
        labels = engine.specs[classifier].task.labels
        existing = self.stored_set(classifier, held_out_set.set_id)
        if existing is not None:
            return HeldOutSet.from_json(existing).summary_json(labels)
        set_ids = self.set_ids(classifier)
        with engine.root_storage.transaction():
            engine.root_storage.put_meta(f"held_out_set:{classifier}:{held_out_set.set_id}", _json(held_out_set.to_json()))
            engine.root_storage.put_meta(f"held_out_set_ids:{classifier}", _json([*set_ids, held_out_set.set_id]))
        return held_out_set.summary_json(labels)

    def sets(self, classifier: str | None = None) -> list[dict[str, Any]]:
        """Summaries of the frozen held-out sets, oldest first. No model call."""
        classifier = self.only_or_named_classifier(classifier)
        labels = self.engine.specs[classifier].task.labels
        return [
            HeldOutSet.from_json(self.stored_set(classifier, set_id)).summary_json(labels) for set_id in self.set_ids(classifier)
        ]

    def reports(self, classifier: str | None = None) -> list[dict[str, Any]]:
        """The latest cyclotron-held-out-report/v1 documents, oldest first. No model call."""
        classifier = self.only_or_named_classifier(classifier)
        row = self.engine.root_storage.meta(f"held_out_reports:{classifier}")
        return json.loads(row[0]) if row else []

    def request_bound(self, classifier: str | None = None, *, set_id: str | None = None) -> int:
        """The most new decision-model requests ``evaluate`` can make: two per held-out item."""
        classifier = self.only_or_named_classifier(classifier)
        return 2 * len(self.held_out_set(classifier, set_id).items)

    def held_out_set(self, classifier: str, set_id: str | None):
        set_ids = self.set_ids(classifier)
        if not set_ids:
            raise ValueError("freeze a held-out set first")
        if set_id is not None and set_id not in set_ids:
            raise ValueError(f"unknown held-out set {set_id}")
        return HeldOutSet.from_json(self.stored_set(classifier, set_id or set_ids[-1]))

    async def evaluate(
        self, classifier: str | None = None, *, set_id: str | None = None, minimum_per_label: int | None = None
    ) -> dict[str, Any]:
        """Score the store's first classifier and its active one on a frozen held-out set.

        Makes at most ``request_bound()`` decision-model requests; repeating an
        evaluation answers the unchanged endpoint from the request cache.
        Nothing is learned, fitted or promoted. Returns the report and keeps it
        for ``reports``.
        """
        engine = self.engine
        wheels, shared = engine.wheels, engine.shared
        classifier = self.only_or_named_classifier(classifier)
        engine._sync()
        held_out_set = self.held_out_set(classifier, set_id)
        audit_item_ids = {item.id for item in engine._partitions(classifier)[2]}
        excluded_item_ids = [row.item_id for row in held_out_set.items if row.item_id not in audit_item_ids]
        scored_items = [row for row in held_out_set.items if row.item_id not in excluded_item_ids]
        current_reviews = self.active_audit_reviews(classifier)
        labels_changed_since_frozen = sum(
            1
            for row in held_out_set.items
            if row.item_id in current_reviews and current_reviews[row.item_id][0].label != row.label
        )
        request_bound = 2 * len(scored_items)
        remaining_requests = shared.max_requests - shared.requests
        if request_bound > remaining_requests:
            raise ValueError(
                f"this evaluation may need {request_bound} decision-model requests and "
                f"{remaining_requests} remain in this open; reopen with a larger max_requests"
            )
        before_configs = {cid: wheel.initial for cid, wheel in wheels.items()}
        before_training = {cid: tuple(engine.priming_examples.get(cid, ())) for cid in wheels}
        after_configs = {cid: wheel.active.config for cid, wheel in wheels.items()}
        after_training = {cid: engine._partitions(cid)[0] for cid in wheels}
        shared.bind_context(after_configs, after_training)
        active = wheels[classifier].active
        if active.head and active.head.provenance.source_model_provenance != wheels[classifier].model_context(
            active.config, after_training[classifier]
        ):
            raise ValueError("the ML model was fit on an older shared context; decide an item to refit it first")
        clock = datetime.fromisoformat(held_out_set.frozen_at)
        engine._batches = []
        prepared_requests = 0
        predictions = {}
        try:
            for endpoint_name, configs, training, head in (
                ("before", before_configs, before_training, None),
                ("after", after_configs, after_training, active.head),
            ):
                predictions[endpoint_name] = {}
                for row in scored_items:
                    payload, _ = await shared.prepare(configs, Item(row.item_id, row.values), training, now=clock)
                    prepared_requests += 1
                    raw = payload["result"]
                    answers = ClassifiedAnswers(
                        {name: DecisionResult(**answer) for name, answer in raw["answers"][classifier].items()},
                        raw["model"],
                        None,
                        raw["latency_ms"],
                    )
                    predictions[endpoint_name][row.item_id] = (
                        head.predict(DecisionFlywheel._features(configs[classifier], answers))
                        if head
                        else answers.answers["decision"].label
                    )
        finally:
            paid_batches, engine._batches = engine._batches, []
            shared.bind_context(after_configs, after_training)
        spec = engine.specs[classifier]
        versions = engine.versions[classifier]
        report = build_report(
            held_out_set,
            labels=spec.task.labels,
            positive_label=spec.positive_label,
            before=Endpoint(
                1, 0, FittedClassifier(wheels[classifier].initial).fingerprint, BEFORE_DESCRIPTION, predictions["before"]
            ),
            after=Endpoint(versions.version, versions.refits, active.fingerprint, AFTER_DESCRIPTION, predictions["after"]),
            excluded_item_ids=excluded_item_ids,
            labels_changed_since_frozen=labels_changed_since_frozen,
            cost={
                "newRequests": len(paid_batches),
                "cachedRequests": prepared_requests - len(paid_batches),
                "promptTokens": sum(batch[0] for batch in paid_batches),
                "outputTokens": sum(batch[2] for batch in paid_batches),
            },
            minimum_per_label=DEFAULT_MINIMUM_PER_LABEL if minimum_per_label is None else minimum_per_label,
        )
        kept_reports = [*self.reports(classifier), report][-REPORTS_KEPT_PER_CLASSIFIER:]
        with engine.root_storage.transaction():
            engine.root_storage.put_meta(f"held_out_reports:{classifier}", _json(kept_reports))
        return report
