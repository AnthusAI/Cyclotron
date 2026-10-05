"""Reusable, observable feedback-to-classifier orchestration.

The UI supplies eligible feedback and renders events. This module owns proposals,
feature collection, the existing trusted numerical fitter, comparison, promotion,
and durable restart state. Private SQLite records must not be published.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Protocol

from .classifier_config import ClassifiedAnswers, ClassifierConfig
from .context import _normalized_text
from .feedback import Feature, FeedbackItem, LABEL_SOURCE_VETTED
from .head import (Calibration, HeadProvenance, HeadRow, LearnedHead, OutOfFoldPredictions,
                   fit_learned_head)
from .models import DecisionResult, DecisionTask, Item, LabeledItem
from .optimizer_agent import FeedbackBriefing, OptimizerAgent


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def development_assignment(seed: str, item_id: str, *, rate: float = .25) -> bool:
    """A label-independent permanent development role within eligible records."""
    if not isinstance(seed, str) or not seed or not isinstance(item_id, str) or not item_id:
        raise ValueError("assignment seed and item ID must be non-empty strings")
    if isinstance(rate, bool) or not isinstance(rate, (float, int)) or not 0 <= rate < 1:
        raise ValueError("development rate must be in [0,1)")
    value = int(hashlib.sha256(f"{seed}:development:{item_id}".encode()).hexdigest(), 16) / 2**256
    return value < rate


class FeatureModel(Protocol):
    model_identity: str
    async def classify(self, config: ClassifierConfig, target: Item,
                       training: Sequence[LabeledItem], *, now: datetime | None = None,
                       event_sink: Callable[[dict], None] | None = None) -> ClassifiedAnswers: ...


@dataclass(frozen=True)
class FittedClassifier:
    config: ClassifierConfig
    head: LearnedHead | None = None
    training_evidence: Mapping[str, str] | None = None
    development_evidence: Mapping[str, str] | None = None

    @property
    def fingerprint(self):
        return _hash({"config": self.config.fingerprint, "head": asdict(self.head) if self.head else None,
                      "training_evidence": self.training_evidence, "development_evidence": self.development_evidence})


def _restore(raw):
    config = raw["config"]
    config = ClassifierConfig(DecisionTask(**config["task"]), config["rubric"], tuple(config["example_ids"]),
                              tuple(DecisionTask(**task) for task in config["tasks"]),
                              tuple(config["dynamic_elements"]), config["parent_fingerprint"])
    head = raw["head"]
    if head is not None:
        oof = head["out_of_fold"]
        for name in ("classes", "item_ids", "labels", "weights", "folds", "normalizers"):
            oof[name] = tuple(oof[name])
        for name in ("logits", "fit_ids", "fit_labels", "normalization_fit_ids"):
            oof[name] = tuple(tuple(row) for row in oof[name])
        provenance = head["provenance"]
        for name in ("training_ids", "development_ids", "scoreboard_ids", "weights"):
            provenance[name] = tuple(provenance[name])
        head = LearnedHead(tuple(head["classes"]), tuple(head["feature_names"]), head["weights"],
                           {name: tuple(value) for name, value in head["feature_normalizers"].items()},
                           Calibration(**head["calibration"]), OutOfFoldPredictions(**oof),
                           HeadProvenance(**provenance), head["refitted_scorecard_fingerprint"])
    return FittedClassifier(config, head, raw["training_evidence"], raw.get("development_evidence"))


class DecisionFlywheel:
    """Small application-facing API: predict, improve, reconcile, history, close.

    All reviews supplied for fitting need explicit selection propensities. Audit
    labels are not accepted here. The caller supplies their records as protected
    ID/text firewalls, without labels. max_requests is a per-open authorization
    ceiling; each reserved attempt is also durably recorded before the call.
    """

    def __init__(self, database: str | Path, initial: ClassifierConfig, model: FeatureModel,
                 optimizer: OptimizerAgent, *, observer: Callable[[dict], None] | None = None,
                 max_requests: int = 100, max_request_bytes: int = 32000,
                 redact: Sequence[str] = ()):
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("max_requests must be positive")
        if type(max_request_bytes) is not int or max_request_bytes < 1:
            raise ValueError("max_request_bytes must be positive")
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_answers (key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT);
            CREATE TABLE IF NOT EXISTS runtime_rounds (key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT);
        """)
        self.initial, self.model, self.optimizer = initial, model, optimizer
        self.observer = observer or (lambda event: None)
        self.redact = tuple(value for value in redact if value)
        self.max_requests, self.max_request_bytes, self.requests = max_requests, max_request_bytes, 0
        expected = _json({"task": initial.task.fingerprint, "model": model.model_identity})
        saved = self.db.execute("SELECT value FROM runtime_state WHERE key='contract'").fetchone()
        if saved and saved[0] != expected:
            self.db.close()
            raise ValueError("runtime task/model contract does not match the saved study")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO runtime_state VALUES ('contract', ?)", (expected,))
        active = self.db.execute("SELECT value FROM runtime_state WHERE key='active'").fetchone()
        self.active = _restore(json.loads(active[0])) if active else FittedClassifier(initial)
        prior_observer = optimizer.observer
        def observe(event):
            clean = self._emit(event)
            prior_observer(clean)
        # The library persists actual optimizer messages before the UI callback.
        self.optimizer_observer = prior_observer
        optimizer.observer = observe

    def close(self):
        self.optimizer.observer = self.optimizer_observer
        self.db.close()

    def _emit(self, event):
        event = {"created_at": datetime.now(timezone.utc).isoformat(), **event}
        text = _json(event)
        for value in self.redact:
            text = text.replace(json.dumps(value, ensure_ascii=False)[1:-1], "[REDACTED]")
        event = json.loads(text)
        with self.db:
            self.db.execute("INSERT INTO runtime_events(payload) VALUES (?)", (_json(event),))
        self.observer(event)
        return event

    def history(self, limit: int = 100) -> tuple[dict, ...]:
        rows = self.db.execute("SELECT payload FROM runtime_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return tuple(json.loads(row[0]) for row in reversed(rows))

    def _activate(self, classifier):
        payload = _json(asdict(classifier))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runtime_state VALUES ('active', ?)", (payload,))
        self.active = classifier

    @staticmethod
    def _evidence(training):
        return {row.item.id: _hash({"values": dict(row.item.values), "label": row.label,
                                  "context": dict(row.context)}) for row in training}

    def reconcile_feedback(self, training: Sequence[LabeledItem], *, development: Sequence[LabeledItem] | None = None):
        """Retain valid fits when new votes arrive; invalidate on undo/correction."""
        current = self._evidence(training)
        original = self.active.training_evidence or {}
        development_changed = (development is not None and any(
            self._evidence(development).get(key) != value for key, value in (self.active.development_evidence or {}).items()))
        if any(current.get(key) != value for key, value in original.items()) or development_changed:
            before = self.active.fingerprint
            # Rubric and tasks were inferred from invalidated feedback too.
            self._activate(FittedClassifier(self.initial))
            self._emit({"kind": "classifier-invalidated", "previous_version": before,
                        "reason": "training or development feedback was removed or corrected"})

    async def _answers(self, config, target, training, now):
        request = config.request(target, training, now=now)
        serialized = _json(request)
        if len(serialized.encode()) > self.max_request_bytes:
            raise ValueError("complete decision request exceeds the configured byte safety ceiling")
        key = _hash({"model": self.model.model_identity, "request": request})
        row = self.db.execute("SELECT status,payload FROM runtime_answers WHERE key=?", (key,)).fetchone()
        if row:
            if row[0] != "complete":
                raise RuntimeError("an interrupted or failed request needs explicit retry authorization")
            raw = json.loads(row[1])
            self._emit({"kind": "features-cached", "target_id": target.id, "request_fingerprint": key})
            return ClassifiedAnswers({name: DecisionResult(**result) for name, result in raw["answers"].items()},
                                     raw["model"], raw["usage"], raw["latency_ms"])
        if self.requests >= self.max_requests:
            raise RuntimeError("decision-model request ceiling reached")
        self.requests += 1
        with self.db:
            self.db.execute("INSERT INTO runtime_answers VALUES (?, 'pending', NULL)", (key,))
        self._emit({"kind": "features-requested", "target_id": target.id, "request_fingerprint": key,
                    "requests": self.requests, "ceiling": self.max_requests})
        try:
            batch = await self.model.classify(config, target, training, now=now, event_sink=self._emit)
            # Validate full coverage before committing a reusable cache entry.
            self._features(config, batch)
        except Exception as error:
            self._emit({"kind": "features-failed", "target_id": target.id, "error_type": type(error).__name__})
            raise RuntimeError("decision feature request failed; see recorded error type") from None
        with self.db:
            self.db.execute("UPDATE runtime_answers SET status='complete',payload=? WHERE key=?",
                            (_json(asdict(batch)), key))
        self._emit({"kind": "features-completed", "target_id": target.id, "model": batch.model,
                    "usage": batch.usage, "latency_ms": batch.latency_ms})
        return batch

    @staticmethod
    def _features(config, batch):
        tasks = {"decision": config.task, **{task.name: task for task in config.tasks}}
        if set(batch.answers) != set(tasks):
            raise ValueError("decision response needs every classification task")
        values = {}
        for name, task in tasks.items():
            answer = task.validate_result(batch.answers[name])
            if answer.probabilities is None:
                raise ValueError("learned features require real probability distributions")
            for label in task.labels[:-1]:
                values[f"{name}/{label}"] = answer.probabilities[label]
        return values

    async def predict(self, target: Item, training: Sequence[LabeledItem], *, now: datetime | None = None):
        self.reconcile_feedback(training)
        batch = await self._answers(self.active.config, target, training, now or datetime.now(timezone.utc))
        if self.active.head:
            values = self._features(self.active.config, batch)
            probabilities = self.active.head.probabilities(values)
            label = self.active.head.predict(values)
            result = DecisionResult(label, probabilities, batch.model, batch.usage, batch.latency_ms,
                                    probabilities[label])
        else:
            main = batch.answers["decision"]
            result = DecisionResult(main.label, main.probabilities, batch.model, batch.usage, batch.latency_ms,
                                    main.probabilities[main.label] if main.probabilities else main.confidence)
        self._emit({"kind": "prediction", "target_id": target.id, "label": result.label,
                    "version": self.active.fingerprint, "fitted_head": self.active.head is not None})
        return result

    def _validate_partitions(self, training, development, protected, propensities):
        task = self.initial.task
        groups = [[row.item for row in training], [row.item for row in development], list(protected)]
        ids, texts = set(), set()
        for group in groups:
            own_ids, own_texts = set(), set()
            for item in group:
                text = _normalized_text(item, task)
                if item.id in ids or text in texts or item.id in own_ids or text in own_texts:
                    raise ValueError("training, development and protected records must be disjoint by ID and text")
                own_ids.add(item.id)
                own_texts.add(text)
            ids.update(own_ids)
            texts.update(own_texts)
        for row in (*training, *development):
            if row.source != "trusted":
                raise ValueError("only trusted human labels may be used")
            task.validate_label(row.label)
        for row in training:
            p = propensities.get(row.item.id)
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 < p <= 1:
                raise ValueError("each training label requires a recorded selection propensity")

    async def improve(self, training: Sequence[LabeledItem], development: Sequence[LabeledItem], *,
                      protected: Sequence[Item], propensities: Mapping[str, float],
                      retry_interrupted: bool = False) -> dict:
        self._validate_partitions(training, development, protected, propensities)
        self.reconcile_feedback(training, development=development)
        round_key = _hash({"training": self._evidence(training), "development": self._evidence(development),
                           "propensities": propensities, "protected": sorted(item.id for item in protected)})
        saved = self.db.execute("SELECT status,payload FROM runtime_rounds WHERE key=?", (round_key,)).fetchone()
        if saved:
            if saved[0] == "complete":
                cached = json.loads(saved[1])
                if "active" in cached:
                    self._activate(_restore(cached["active"]))
                return cached.get("result", cached)
            if not retry_interrupted:
                result = {"promoted": False, "reason": "interrupted round requires explicit retry authorization"}
                self._emit({"kind": "round-interrupted", **result})
                return result
            # Only this round is authorized again. Failed/pending decision
            # requests retain their separate no-silent-repayment guard.
            with self.db:
                self.db.execute("DELETE FROM runtime_rounds WHERE key=? AND status='pending'", (round_key,))
            self._emit({"kind": "round-retry-authorized", "round_fingerprint": round_key})
        counts = {label: sum(row.label == label for row in training) for label in self.initial.task.labels}
        if min(counts.values()) < 3 or len(development) < 2:
            result = {"promoted": False, "reason": "waiting for at least three training votes per label and two development votes"}
            self._emit({"kind": "waiting-for-labels", "counts": counts, "development_count": len(development), **result})
            return result
        now = datetime.now(timezone.utc)
        with self.db:
            self.db.execute("INSERT INTO runtime_rounds VALUES (?, 'pending', NULL)", (round_key,))
        self._emit({"kind": "round-started", "training_count": len(training), "development_count": len(development)})
        try:
            briefing = FeedbackBriefing.build(self.initial.task, training,
                current={**self.active.config.briefing_state(),
                         "request_budget_bytes": self.max_request_bytes,
                         "training_predictions": [event for event in self.history(1000)
                            if event["kind"] == "prediction" and event["target_id"] in {row.item.id for row in training}]},
                protected=tuple(row.item for row in development) + tuple(protected))
            proposal = self.optimizer.propose(briefing)
            config = self.active.config.apply(proposal, training)
            self._emit({"kind": "proposal-validated", "proposal": proposal,
                        "previous": self.active.config.briefing_state(), "candidate": config.briefing_state()})
            rows = []
            for row in training:
                batch = await self._answers(config, row.item, training, now)
                values = self._features(config, batch)
                feedback = FeedbackItem("review-" + row.item.id, row.item.id, config.task.name,
                    final_answer_value=row.label, edit_comment_value=row.context.get("human_feedback"),
                    label_source=LABEL_SOURCE_VETTED, selection_propensity=propensities[row.item.id],
                    review_provenance="human-reviewed")
                rows.append(HeadRow(row.item.id, feedback, tuple(Feature(key, value, key.split("/", 1)[0])
                                                                for key, value in values.items())))
            self._emit({"kind": "fit-started", "training_count": len(rows), "features": list(values)})
            head = fit_learned_head(config.task, rows, declared_features=tuple(values),
                development_ids=tuple(row.item.id for row in development), scoreboard_ids=tuple(item.id for item in protected),
                scorecard_fingerprint=config.fingerprint, policy_fingerprint=_hash(config.example_ids),
                context_artifact_fingerprint=config.fingerprint, source_model_provenance=self.model.model_identity)
            candidate = FittedClassifier(config, head, self._evidence(training), self._evidence(development))
            self._emit({"kind": "fit-completed", "features": list(head.feature_names),
                        "training_count": len(rows), "calibration": "out_of_fold"})
            incumbent_metrics = await self._score(self.active, development, training, now)
            candidate_metrics = await self._score(candidate, development, training, now)
            promoted = candidate_metrics["brier"] < incumbent_metrics["brier"] - 1e-12
            result = {"promoted": promoted, "incumbent": incumbent_metrics, "candidate": candidate_metrics,
                      "reason": "lower development Brier" if promoted else "development Brier did not improve"}
            self._emit({"kind": "candidate-evaluated", **result})
            if promoted:
                self._activate(candidate)
            self._emit({"kind": "promoted" if promoted else "candidate-rejected", "version": self.active.fingerprint, **result})
            with self.db:
                self.db.execute("UPDATE runtime_rounds SET status='complete',payload=? WHERE key=?",
                                (_json({"result": result, "active": asdict(self.active)}), round_key))
            return result
        except Exception as error:
            result = {"promoted": False, "reason": "round failed", "error_type": type(error).__name__}
            self._emit({"kind": "round-failed", **result})
            with self.db:
                self.db.execute("UPDATE runtime_rounds SET status='complete',payload=? WHERE key=?",
                                (_json({"result": result, "active": asdict(self.active)}), round_key))
            return result

    async def _score(self, classifier, development, training, now):
        correct, brier = 0, 0.0
        for row in development:
            batch = await self._answers(classifier.config, row.item, training, now)
            if classifier.head:
                probabilities = classifier.head.probabilities(self._features(classifier.config, batch))
                label = classifier.head.predict(self._features(classifier.config, batch))
            else:
                answer = batch.answers["decision"]
                probabilities, label = answer.probabilities, answer.label
            correct += label == row.label
            brier += sum((probabilities[key] - (key == row.label)) ** 2 for key in classifier.config.task.labels)
        return {"count": len(development), "accuracy": correct / len(development), "brier": brier / len(development)}
