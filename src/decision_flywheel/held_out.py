"""The before-and-after measurement: one fixed held-out set, two classifier endpoints.

A held-out set is frozen from reviews the cyclotron selected for items in its
audit share. Those items never train, tune or prime a classifier, so both
endpoints meet them for the first time. Once frozen, a set never changes: its
items, their values and their labels are copied into it, and later labels go
into a new set.

"Before" is the classifier as its store was created (seed rubric and priming
examples, no ML model). "After" is the classifier active now. Both are scored
on every item of the same set with the same request clock, so the only thing
that differs between the two columns is what the cyclotron learned.

Each item counts with the inverse of the probability that it was sent to
review, so once the review rate tapers the numbers still stand for every
decision in the audit share and not only the uncertain ones that were
reviewed. The paired change is tested with an exact two-sided sign test on the
items one endpoint got right and the other got wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from math import comb
from pathlib import Path
from typing import Any, Mapping, Sequence

SET_SCHEMA = "cyclotron-held-out-set/v1"
REPORT_SCHEMA = "cyclotron-held-out-report/v1"
REPORT_SCHEMA_PATH = Path(__file__).with_name("schemas") / "cyclotron-held-out-report.v1.schema.json"
HEADLINE_METRIC = "balancedAccuracy"
HEADLINE_NAME = "Balanced accuracy"
HEADLINE_DEFINITION = ("How often it agrees with reviewers on each label, averaged over labels, "
                       "so a rare label counts as much as a common one.")
SELECTION_DESCRIPTION = ("Reviews the cyclotron selected for items in its audit share. "
                         "Those items never train, tune or prime a classifier.")
BEFORE_DESCRIPTION = "As the store was created: the seed rubric and priming examples, before any review."
AFTER_DESCRIPTION = "The classifier active now."
DEFAULT_MINIMUM_PER_LABEL = 10
SIGNIFICANCE_LEVEL = .05
VERDICTS = ("improved", "worse", "no-clear-change", "too-few-labels", "nothing-learned-yet")
REPORTS_KEPT_PER_CLASSIFIER = 10


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class HeldOutItem:
    item_id: str
    values: Mapping[str, Any]
    label: str
    selection_propensity: float
    selection_reason: str | None

    @property
    def weight(self) -> float:
        return 1. / self.selection_propensity

    def to_json(self) -> dict[str, Any]:
        return {"itemId": self.item_id, "values": dict(self.values), "label": self.label,
                "selectionPropensity": self.selection_propensity, "selectionReason": self.selection_reason}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "HeldOutItem":
        return cls(value["itemId"], value["values"], value["label"], value["selectionPropensity"],
                   value["selectionReason"])


@dataclass(frozen=True)
class HeldOutSet:
    """A frozen list of labeled audit items for one classifier."""
    set_id: str
    cyclotron_id: str
    classifier: str
    frozen_at: str
    items: tuple[HeldOutItem, ...]
    schema: str = field(default=SET_SCHEMA)

    @classmethod
    def freeze(cls, cyclotron_id: str, classifier: str, items: Sequence[HeldOutItem], *,
               frozen_at: datetime | None = None) -> "HeldOutSet":
        ordered_items = tuple(sorted(items, key=lambda held_out_item: held_out_item.item_id))
        if not ordered_items:
            raise ValueError("a held-out set needs at least one labeled audit item")
        if len({held_out_item.item_id for held_out_item in ordered_items}) != len(ordered_items):
            raise ValueError("a held-out set lists each item once")
        if any(not 0 < held_out_item.selection_propensity <= 1 for held_out_item in ordered_items):
            raise ValueError("selection propensities must be in (0, 1]")
        content_fingerprint = hashlib.sha256(_canonical_json(
            {"cyclotron": cyclotron_id, "classifier": classifier,
             "items": [held_out_item.to_json() for held_out_item in ordered_items]}).encode()).hexdigest()
        return cls(f"held-out-{content_fingerprint[:16]}", cyclotron_id, classifier,
                   (frozen_at or datetime.now(timezone.utc)).isoformat(), ordered_items)

    def label_counts(self, labels: Sequence[str]) -> dict[str, int]:
        return {label: sum(held_out_item.label == label for held_out_item in self.items) for label in labels}

    def to_json(self) -> dict[str, Any]:
        return {"schema": self.schema, "id": self.set_id, "cyclotron": self.cyclotron_id,
                "classifier": self.classifier, "frozenAt": self.frozen_at,
                "items": [held_out_item.to_json() for held_out_item in self.items]}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "HeldOutSet":
        if value.get("schema") != SET_SCHEMA:
            raise ValueError("not a cyclotron-held-out-set/v1 record")
        return cls(value["id"], value["cyclotron"], value["classifier"], value["frozenAt"],
                   tuple(HeldOutItem.from_json(row) for row in value["items"]))

    def summary_json(self, labels: Sequence[str]) -> dict[str, Any]:
        return {"id": self.set_id, "frozenAt": self.frozen_at, "items": len(self.items),
                "labelCounts": self.label_counts(labels),
                "weighted": any(held_out_item.selection_propensity != 1 for held_out_item in self.items),
                "selection": SELECTION_DESCRIPTION}


@dataclass(frozen=True)
class Endpoint:
    version: int
    refits: int
    fingerprint: str
    description: str
    predictions: Mapping[str, str]


def weighted_metrics(labels: Sequence[str], positive_label: str | None,
                     scored_items: Sequence[HeldOutItem], predictions: Mapping[str, str]) -> dict[str, Any]:
    """Accuracy, balanced accuracy, precision and recall, each item weighted by its inverse propensity."""
    def ratio(numerator, denominator):
        return numerator / denominator if denominator else None

    total_weight = sum(held_out_item.weight for held_out_item in scored_items)
    correct_weight = sum(held_out_item.weight for held_out_item in scored_items
                         if predictions[held_out_item.item_id] == held_out_item.label)
    recall_by_label, precision_by_label = {}, {}
    for label in labels:
        actual_weight = sum(held_out_item.weight for held_out_item in scored_items if held_out_item.label == label)
        predicted_weight = sum(held_out_item.weight for held_out_item in scored_items
                               if predictions[held_out_item.item_id] == label)
        agreeing_weight = sum(held_out_item.weight for held_out_item in scored_items
                              if held_out_item.label == label and predictions[held_out_item.item_id] == label)
        recall_by_label[label] = ratio(agreeing_weight, actual_weight)
        precision_by_label[label] = ratio(agreeing_weight, predicted_weight)
    measured_recalls = [recall for recall in recall_by_label.values() if recall is not None]
    if positive_label is not None:
        precision, recall = precision_by_label[positive_label], recall_by_label[positive_label]
    else:
        measured_precisions = [value for value in precision_by_label.values() if value is not None]
        precision = sum(measured_precisions) / len(measured_precisions) if measured_precisions else None
        recall = sum(measured_recalls) / len(measured_recalls) if measured_recalls else None
    return {"balancedAccuracy": sum(measured_recalls) / len(measured_recalls) if measured_recalls else None,
            "accuracy": ratio(correct_weight, total_weight), "precision": precision, "recall": recall,
            "recallByLabel": recall_by_label}


def sign_test_p_value(gained: int, lost: int) -> float:
    """Exact two-sided sign test on discordant pairs; 1.0 when there are none."""
    discordant = gained + lost
    if discordant == 0:
        return 1.
    tail = sum(comb(discordant, count) for count in range(min(gained, lost) + 1)) / 2 ** discordant
    return min(1., 2 * tail)


def _points(before: float | None, after: float | None) -> float | None:
    return None if before is None or after is None else round((after - before) * 100, 1)


def _verdict(*, same_endpoint: bool, smallest_label_count: int, minimum_per_label: int,
             gained: int, lost: int, p_value: float) -> str:
    if same_endpoint:
        return "nothing-learned-yet"
    if smallest_label_count < minimum_per_label:
        return "too-few-labels"
    if p_value < SIGNIFICANCE_LEVEL and gained != lost:
        return "improved" if gained > lost else "worse"
    return "no-clear-change"


def _summary(verdict: str, headline_before, headline_after, change_points, gained, lost, p_value,
             minimum_per_label, smallest_label_count) -> str:
    def percent(value):
        return "not measured" if value is None else f"{round(value * 100)}%"
    movement = (f"{HEADLINE_NAME} went from {percent(headline_before)} to {percent(headline_after)}"
                + ("" if change_points is None else f" ({change_points:+.1f} points)") + ".")
    shown_p_value = "p < 0.001" if p_value < .001 else f"p = {p_value:.3f}"
    pairs = f"It newly got {gained} right and newly got {lost} wrong (sign test {shown_p_value})."
    if verdict == "nothing-learned-yet":
        return "The active classifier is still the one the store was created with, so there is nothing to compare yet."
    if verdict == "too-few-labels":
        return (f"{movement} {pairs} Too few labels to claim a change: the rarest label has "
                f"{smallest_label_count}, and a claim needs {minimum_per_label} of each.")
    if verdict == "improved":
        return f"{movement} {pairs} The improvement is clear on this held-out set."
    if verdict == "worse":
        return f"{movement} {pairs} The active classifier is clearly worse on this held-out set."
    return f"{movement} {pairs} The change is not clear on this held-out set."


def build_report(held_out_set: HeldOutSet, *, labels: Sequence[str], positive_label: str | None,
                 before: Endpoint, after: Endpoint, excluded_item_ids: Sequence[str] = (),
                 labels_changed_since_frozen: int = 0, cost: Mapping[str, int] | None = None,
                 minimum_per_label: int = DEFAULT_MINIMUM_PER_LABEL,
                 evaluated_at: datetime | None = None) -> dict[str, Any]:
    """The cyclotron-held-out-report/v1 document for one evaluation."""
    if type(minimum_per_label) is not int or minimum_per_label < 1:
        raise ValueError("minimum_per_label must be a positive integer")
    excluded = set(excluded_item_ids)
    scored_items = [held_out_item for held_out_item in held_out_set.items if held_out_item.item_id not in excluded]
    if not scored_items:
        raise ValueError("every item of this held-out set has since been used for training; freeze a new set")
    for endpoint in (before, after):
        missing = {held_out_item.item_id for held_out_item in scored_items} - set(endpoint.predictions)
        if missing:
            raise ValueError("each endpoint needs a prediction for every held-out item")
    before_metrics = weighted_metrics(labels, positive_label, scored_items, before.predictions)
    after_metrics = weighted_metrics(labels, positive_label, scored_items, after.predictions)
    outcomes = {"gained": 0, "lost": 0, "bothRight": 0, "bothWrong": 0}
    item_rows = []
    for held_out_item in scored_items:
        before_right = before.predictions[held_out_item.item_id] == held_out_item.label
        after_right = after.predictions[held_out_item.item_id] == held_out_item.label
        outcome = ("bothRight" if before_right and after_right else "gained" if after_right
                   else "lost" if before_right else "bothWrong")
        outcomes[outcome] += 1
        item_rows.append({"itemId": held_out_item.item_id, "label": held_out_item.label,
                          "weight": held_out_item.weight, "before": before.predictions[held_out_item.item_id],
                          "after": after.predictions[held_out_item.item_id], "outcome": outcome})
    scored_label_counts = {label: sum(held_out_item.label == label for held_out_item in scored_items)
                           for label in labels}
    smallest_label_count = min(scored_label_counts.values())
    p_value = sign_test_p_value(outcomes["gained"], outcomes["lost"])
    verdict = _verdict(same_endpoint=before.fingerprint == after.fingerprint,
                       smallest_label_count=smallest_label_count, minimum_per_label=minimum_per_label,
                       gained=outcomes["gained"], lost=outcomes["lost"], p_value=p_value)
    headline_before, headline_after = before_metrics[HEADLINE_METRIC], after_metrics[HEADLINE_METRIC]
    change_points = _points(headline_before, headline_after)

    def endpoint_json(endpoint: Endpoint, metrics):
        return {"version": endpoint.version, "refits": endpoint.refits, "fingerprint": endpoint.fingerprint,
                "description": endpoint.description, "metrics": metrics}

    return {
        "schema": REPORT_SCHEMA,
        "cyclotron": {"id": held_out_set.cyclotron_id, "classifier": held_out_set.classifier},
        "evaluatedAt": (evaluated_at or datetime.now(timezone.utc)).isoformat(),
        "positiveLabel": positive_label,
        "heldOutSet": {**held_out_set.summary_json(labels), "scored": len(scored_items),
                       "scoredLabelCounts": scored_label_counts,
                       "excludedSinceFrozen": len(held_out_set.items) - len(scored_items),
                       "labelsChangedSinceFrozen": labels_changed_since_frozen},
        "headline": {"metric": HEADLINE_METRIC, "name": HEADLINE_NAME, "definition": HEADLINE_DEFINITION,
                     "before": headline_before, "after": headline_after, "changePoints": change_points},
        "before": endpoint_json(before, before_metrics),
        "after": endpoint_json(after, after_metrics),
        "change": {**outcomes, "pValue": p_value, "significanceLevel": SIGNIFICANCE_LEVEL,
                   "minimumPerLabel": minimum_per_label, "verdict": verdict,
                   "summary": _summary(verdict, headline_before, headline_after, change_points,
                                       outcomes["gained"], outcomes["lost"], p_value, minimum_per_label,
                                       smallest_label_count)},
        "cost": {"newRequests": 0, "cachedRequests": 0, "promptTokens": 0, "outputTokens": 0, **(cost or {})},
        "items": item_rows,
    }


def load_report_schema() -> dict[str, Any]:
    return json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
