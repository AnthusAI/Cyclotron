"""A deterministic numerical head with trusted-label and OOF-calibration guards."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from numbers import Real
from typing import Mapping, Sequence

from .feedback import Feature, FeedbackItem
from .models import DecisionTask


def _hash(name: str, value: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 fingerprint")
    return value


def _finite_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be numeric, not boolean")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


@dataclass(frozen=True)
class HeadRow:
    item_id: str
    feedback: FeedbackItem
    features: tuple[Feature, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id:
            raise ValueError("item_id must be a non-empty string")
        if self.feedback.item_id != self.item_id:
            raise ValueError("feedback item_id must match head row item_id")


@dataclass(frozen=True)
class OutOfFoldPredictions:
    """Full multiclass OOF logits plus per-row evidence that a row was held out."""

    classes: tuple[str, ...]
    item_ids: tuple[str, ...]
    logits: tuple[tuple[float, ...], ...]
    labels: tuple[str, ...]
    weights: tuple[float, ...]
    folds: tuple[int, ...]
    fit_ids: tuple[tuple[str, ...], ...]
    fit_labels: tuple[tuple[str, ...], ...]
    normalization_fit_ids: tuple[tuple[str, ...], ...]
    normalizers: tuple[Mapping[str, tuple[float, float, float]], ...]
    origin: str = "out_of_fold"

    def __post_init__(self) -> None:
        if self.origin != "out_of_fold":
            raise ValueError("calibration accepts out-of-fold predictions only")
        if not self.classes or len(set(self.classes)) != len(self.classes):
            raise ValueError("out-of-fold classes must be unique and non-empty")
        count = len(self.item_ids)
        fields = (self.logits, self.labels, self.weights, self.folds, self.fit_ids, self.fit_labels,
                  self.normalization_fit_ids, self.normalizers)
        if not count or any(len(field) != count for field in fields):
            raise ValueError("out-of-fold prediction fields must have the same non-zero length")
        if len(set(self.item_ids)) != count or any(not isinstance(item, str) or not item for item in self.item_ids):
            raise ValueError("out-of-fold item IDs must be unique non-empty strings")
        for item_id, logits, label, weight, fold, fitted_ids, fitted_labels, normalization_ids, normalizer in zip(
            self.item_ids, self.logits, self.labels, self.weights, self.folds, self.fit_ids, self.fit_labels,
            self.normalization_fit_ids, self.normalizers,
        ):
            if len(logits) != len(self.classes):
                raise ValueError("out-of-fold logits must cover every class")
            for value in logits:
                _finite_number("out-of-fold logits", value)
            if label not in self.classes:
                raise ValueError("out-of-fold labels must be canonical task labels")
            if not isinstance(fold, int) or isinstance(fold, bool) or fold < 0:
                raise ValueError("out-of-fold folds must be non-negative integers")
            if item_id in fitted_ids:
                raise ValueError("held out item cannot appear in its fitting partition")
            if len(set(fitted_ids)) != len(fitted_ids):
                raise ValueError("out-of-fold fitting IDs must be unique")
            if normalization_ids != fitted_ids:
                raise ValueError("out-of-fold normalizers must be fit on exactly the fitting partition")
            if not isinstance(normalizer, Mapping):
                raise ValueError("out-of-fold normalizer provenance must be a mapping")
            if set(fitted_labels) != set(self.classes):
                raise ValueError("each out-of-fold fitting partition needs full class coverage")
            if _finite_number("out-of-fold weight", weight) <= 0:
                raise ValueError("out-of-fold weights must be positive finite values")

    def probabilities(self, temperature: float) -> tuple[tuple[float, ...], ...]:
        return tuple(_softmax(logits, temperature) for logits in self.logits)


@dataclass(frozen=True)
class Calibration:
    temperature: float
    fit_on: str = "out_of_fold"

    def __post_init__(self) -> None:
        if self.fit_on != "out_of_fold":
            raise ValueError("calibration accepts out-of-fold predictions only")
        if _finite_number("temperature", self.temperature) <= 0:
            raise ValueError("temperature must be a positive finite number")


def calibrate(predictions: OutOfFoldPredictions) -> Calibration:
    """Fit one propensity-weighted multiclass temperature from structural OOF proof."""
    if not isinstance(predictions, OutOfFoldPredictions):
        raise ValueError("calibration accepts out-of-fold predictions only")

    def loss(temperature: float) -> float:
        total = 0.0
        total_weight = 0.0
        for logits, actual, weight in zip(predictions.logits, predictions.labels, predictions.weights):
            probability = _softmax(logits, temperature)[predictions.classes.index(actual)]
            total -= weight * math.log(max(probability, 1e-300))
            total_weight += weight
        return total / total_weight

    temperature = min((0.5 + step / 100 for step in range(151)), key=lambda value: (loss(value), value))
    return Calibration(temperature)


@dataclass(frozen=True)
class HeadProvenance:
    training_ids: tuple[str, ...]
    development_ids: tuple[str, ...]
    scoreboard_ids: tuple[str, ...]
    weights: tuple[float, ...]
    scorecard_fingerprint: str
    policy_fingerprint: str
    context_artifact_fingerprint: str
    source_model_provenance: str


@dataclass(frozen=True)
class LearnedHead:
    classes: tuple[str, ...]
    feature_names: tuple[str, ...]
    weights: Mapping[str, Mapping[str, float]]
    feature_normalizers: Mapping[str, tuple[float, float, float]]
    calibration: Calibration
    out_of_fold: OutOfFoldPredictions
    provenance: HeadProvenance
    refitted_scorecard_fingerprint: str

    def _normalised_values(self, values: Mapping[str, float]) -> dict[str, float]:
        if set(values) != set(self.feature_names):
            raise ValueError("prediction needs full feature coverage")
        transformed: dict[str, float] = {}
        for name in self.feature_names:
            value = _finite_number(f"prediction feature {name}", values[name])
            input_scale, mean, spread = self.feature_normalizers[name]
            normalised = (value / input_scale - mean) / spread
            if not math.isfinite(normalised):
                raise ValueError("prediction feature normalization must stay finite")
            transformed[name] = normalised
        return transformed

    def _logits(self, values: Mapping[str, float]) -> tuple[float, ...]:
        normalised = self._normalised_values(values)
        logits = tuple(self.weights[label]["intercept"] + sum(self.weights[label][name] * normalised[name]
                                                                 for name in self.feature_names)
                       for label in self.classes)
        if not all(math.isfinite(value) for value in logits):
            raise ValueError("prediction logits must stay finite")
        return logits

    def uncalibrated_probabilities(self, values: Mapping[str, float]) -> dict[str, float]:
        return dict(zip(self.classes, _softmax(self._logits(values), 1.0)))

    def probabilities(self, values: Mapping[str, float]) -> dict[str, float]:
        return dict(zip(self.classes, _softmax(self._logits(values), self.calibration.temperature)))

    def predict(self, values: Mapping[str, float]) -> str:
        probabilities = self.probabilities(values)
        return max(self.classes, key=lambda label: (probabilities[label], label))


def fit_learned_head(task: DecisionTask, rows: Sequence[HeadRow], *, declared_features: Sequence[str],
                     development_ids: Sequence[str], scoreboard_ids: Sequence[str], scorecard_fingerprint: str,
                     policy_fingerprint: str, context_artifact_fingerprint: str, source_model_provenance: str,
                     folds: int = 3) -> LearnedHead:
    """Fit numbers on trusted train rows; dev and scoreboard remain ID-only firewalls."""
    names = tuple(declared_features)
    if not names or len(set(names)) != len(names) or any(not isinstance(name, str) or not name for name in names):
        raise ValueError("declared_features must be unique non-empty names")
    if "intercept" in names:
        raise ValueError("intercept is reserved for the internal bias")
    if isinstance(folds, bool) or not isinstance(folds, int) or folds < 2:
        raise ValueError("folds must be an integer of at least two")
    _hash("scorecard_fingerprint", scorecard_fingerprint)
    _hash("policy_fingerprint", policy_fingerprint)
    _hash("context_artifact_fingerprint", context_artifact_fingerprint)
    if not isinstance(source_model_provenance, str) or not source_model_provenance:
        raise ValueError("source_model_provenance must be a non-empty generic model identifier")
    training_ids = tuple(row.item_id for row in rows)
    _validate_splits(training_ids, tuple(development_ids), tuple(scoreboard_ids))
    labels, raw_matrix, propensities = _training_values(task, rows, names)
    if len(rows) < folds * 2:
        raise ValueError("training needs at least two rows per out-of-fold split")
    if len(set(labels)) != len(task.labels) or any(labels.count(label) < 2 for label in task.labels):
        raise ValueError("each task label needs at least two training rows for out-of-fold coverage")
    normalizers, matrix = _normalise_matrix(raw_matrix, names)
    weights = _inverse_propensity_weights(propensities)
    oof = _out_of_fold(task.labels, names, training_ids, raw_matrix, labels, propensities, weights, folds)
    calibration = calibrate(oof)
    fitted = _fit(task.labels, names, matrix, labels, weights)
    provenance = HeadProvenance(training_ids, tuple(development_ids), tuple(scoreboard_ids), tuple(weights),
                                scorecard_fingerprint, policy_fingerprint, context_artifact_fingerprint,
                                source_model_provenance)
    # Include the complete text-free provenance, not merely the resulting
    # coefficients. Different task, selection, context, or source-model
    # contracts can happen to yield identical numerical parameters.
    refitted = _fingerprint({
        "calibration": calibration.temperature,
        "context_artifact": context_artifact_fingerprint,
        "development_ids": tuple(development_ids),
        "features": names,
        "normalizers": normalizers,
        "policy": policy_fingerprint,
        "scoreboard_ids": tuple(scoreboard_ids),
        "scorecard": scorecard_fingerprint,
        "source_model": source_model_provenance,
        "task": task.fingerprint,
        "training_ids": training_ids,
        "training_labels": tuple(labels),
        "training_propensities": tuple(propensities),
        "training_values": raw_matrix,
        "weights": fitted,
    })
    return LearnedHead(tuple(task.labels), names, fitted, normalizers, calibration, oof, provenance, refitted)


def _training_values(task: DecisionTask, rows: Sequence[HeadRow], names: tuple[str, ...]):
    labels: list[str] = []
    matrix: list[dict[str, float]] = []
    propensities: list[float] = []
    seen: set[str] = set()
    for row in rows:
        if row.item_id in seen:
            raise ValueError("training IDs must be unique")
        seen.add(row.item_id)
        label = row.feedback.training_label(task)
        by_name = {feature.name: feature.value for feature in row.features}
        if len(by_name) != len(row.features) or set(by_name) != set(names):
            raise ValueError("training requires full feature coverage")
        labels.append(label)
        matrix.append({name: _finite_number(f"training feature {name}", by_name[name]) for name in names})
        assert row.feedback.selection_propensity is not None
        propensities.append(_finite_number("selection_propensity", row.feedback.selection_propensity))
    return labels, matrix, propensities


def _normalise_matrix(matrix: Sequence[Mapping[str, float]], names: tuple[str, ...]):
    """Normalize without summing huge finite raw values into infinity."""
    normalizers: dict[str, tuple[float, float, float]] = {}
    for name in names:
        values = [_finite_number(f"training feature {name}", row[name]) for row in matrix]
        scale = max(max(abs(value) for value in values), 1.0)
        scaled = [value / scale for value in values]
        mean = math.fsum(scaled) / len(scaled)
        variance = math.fsum((value - mean) ** 2 for value in scaled) / len(scaled)
        spread = math.sqrt(variance) or 1.0
        if not all(math.isfinite(value) for value in (scale, mean, spread)):
            raise ValueError("feature normalization must stay finite")
        normalizers[name] = (scale, mean, spread)
    return normalizers, _transform_matrix(matrix, names, normalizers)


def _transform_matrix(matrix: Sequence[Mapping[str, float]], names: tuple[str, ...], normalizers):
    transformed = []
    for row in matrix:
        output = {}
        for name in names:
            scale, mean, spread = normalizers[name]
            value = (_finite_number(f"training feature {name}", row[name]) / scale - mean) / spread
            if not math.isfinite(value):
                raise ValueError("feature normalization must stay finite")
            output[name] = value
        transformed.append(output)
    return transformed


def _validate_splits(training: tuple[str, ...], development: tuple[str, ...], scoreboard: tuple[str, ...]) -> None:
    all_groups = (training, development, scoreboard)
    if any(len(set(group)) != len(group) for group in all_groups):
        raise ValueError("split IDs must be unique")
    if set(training) & set(development) or set(training) & set(scoreboard) or set(development) & set(scoreboard):
        raise ValueError("train, development, and scoreboard IDs must be disjoint")


def _inverse_propensity_weights(propensities: Sequence[float]) -> list[float]:
    checked = [_finite_number("selection_propensity", value) for value in propensities]
    if not checked or any(not 0.0 < value <= 1.0 for value in checked):
        raise ValueError("selection_propensity must be a finite probability in (0, 1]")
    smallest = min(checked)
    relative = [smallest / value for value in checked]
    scale = len(relative) / math.fsum(relative)
    result = [value * scale for value in relative]
    if not all(math.isfinite(value) and value > 0 for value in result):
        raise ValueError("inverse propensity weights must stay finite")
    return result


def _fit(classes: Sequence[str], names: tuple[str, ...], matrix, labels, weights, *, epochs: int = 400,
         learning_rate: float = 0.15) -> dict[str, dict[str, float]]:
    parameters = {label: {"intercept": 0.0, **{name: 0.0 for name in names}} for label in classes}
    for _ in range(epochs):
        gradients = {label: {"intercept": 0.0, **{name: 0.0 for name in names}} for label in classes}
        for row, actual, weight in zip(matrix, labels, weights):
            probabilities = _softmax(tuple(parameters[label]["intercept"] + sum(
                parameters[label][name] * row[name] for name in names) for label in classes), 1.0)
            for index, label in enumerate(classes):
                error = (1.0 if label == actual else 0.0) - probabilities[index]
                gradients[label]["intercept"] += weight * error
                for name in names:
                    gradients[label][name] += weight * error * row[name]
        for label in classes:
            for name in parameters[label]:
                parameters[label][name] += learning_rate * gradients[label][name] / len(matrix)
    return parameters


def _out_of_fold(classes, names, item_ids, matrix, labels, propensities, calibration_weights,
                 folds: int) -> OutOfFoldPredictions:
    fold_by_index: dict[int, int] = {}
    for label in classes:
        indices = [index for index, actual in enumerate(labels) if actual == label]
        for position, index in enumerate(indices):
            fold_by_index[index] = position % folds
    outputs: list[tuple[float, ...] | None] = [None] * len(matrix)
    fit_ids: list[tuple[str, ...] | None] = [None] * len(matrix)
    fit_labels: list[tuple[str, ...] | None] = [None] * len(matrix)
    fold_normalizers: list[Mapping[str, tuple[float, float, float]] | None] = [None] * len(matrix)
    for fold in range(folds):
        held = [index for index in range(len(matrix)) if fold_by_index[index] == fold]
        train = [index for index in range(len(matrix)) if fold_by_index[index] != fold]
        if not held:
            continue
        training_labels = tuple(labels[index] for index in train)
        if set(training_labels) != set(classes):
            raise ValueError("each deterministic fold must retain full fitting class coverage")
        fitted_normalizers, train_matrix = _normalise_matrix([matrix[index] for index in train], names)
        held_matrix = _transform_matrix([matrix[index] for index in held], names, fitted_normalizers)
        # Weight each fitting partition from its own propensities. A held-out
        # item's selection probability must not affect the model scoring it.
        fitted = _fit(classes, names, train_matrix, list(training_labels),
                      _inverse_propensity_weights([propensities[index] for index in train]))
        for index, held_row in zip(held, held_matrix):
            outputs[index] = tuple(fitted[label]["intercept"] + sum(fitted[label][name] * held_row[name]
                                                                       for name in names) for label in classes)
            fit_ids[index] = tuple(item_ids[position] for position in train)
            fit_labels[index] = training_labels
            fold_normalizers[index] = fitted_normalizers
    assert all(output is not None for output in outputs)
    assert all(value is not None for value in fit_ids)
    assert all(value is not None for value in fit_labels)
    assert all(value is not None for value in fold_normalizers)
    return OutOfFoldPredictions(tuple(classes), tuple(item_ids), tuple(outputs), tuple(labels), tuple(calibration_weights),
                                 tuple(fold_by_index[index] for index in range(len(matrix))),
                                 tuple(fit_ids), tuple(fit_labels), tuple(fit_ids), tuple(fold_normalizers))


def _softmax(logits: Sequence[float], temperature: float) -> tuple[float, ...]:
    temperature = _finite_number("temperature", temperature)
    if temperature <= 0:
        raise ValueError("temperature must be a positive finite number")
    highest = max(logits)
    exponentials = tuple(math.exp((value - highest) / temperature) for value in logits)
    total = math.fsum(exponentials)
    return tuple(value / total for value in exponentials)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()
