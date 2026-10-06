"""Reusable, observable feedback-to-classifier orchestration.

The UI supplies eligible feedback and renders events. This module owns proposals,
feature collection, the existing trusted numerical fitter, comparison, promotion,
and durable restart state. Private SQLite records must not be published.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Protocol

from .classifier_config import ClassifiedAnswers, ClassifierConfig
from .classification_metrics import classification_metrics
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
    validation_status: str | None = None

    @property
    def fingerprint(self):
        head = asdict(self.head) if self.head else None
        if head and head["provenance"].get("training_class_weighting") == "natural":
            # New optional metadata must not rename an unchanged legacy head.
            head["provenance"].pop("training_class_weighting", None)
        return _hash({"config": self.config.fingerprint, "head": head,
                      "training_evidence": self.training_evidence, "development_evidence": self.development_evidence,
                      **({"validation_status":self.validation_status} if self.validation_status is not None else {})})


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
    return FittedClassifier(config, head, raw["training_evidence"], raw.get("development_evidence"),raw.get('validation_status'))


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
                 redact: Sequence[str] = (), evaluation_weighting: str = "equal_class",
                 training_class_weighting: str = "natural", min_evaluation_per_class: int = 1,
                 cache_options=None, context_validation_floor: int = 20, evaluation_policy=None):
        from .evaluation_policy import EvaluationPolicy
        self.evaluation_policy = evaluation_policy or EvaluationPolicy(recency_decay_per_class=context_validation_floor)
        if not isinstance(self.evaluation_policy, EvaluationPolicy):
            raise ValueError('evaluation_policy must be EvaluationPolicy')
        from .decision_cache import CacheOptions
        self.cache_options = cache_options if cache_options is not None else CacheOptions()
        if not isinstance(self.cache_options, CacheOptions):
            raise ValueError("cache_options must be CacheOptions")
        if evaluation_weighting not in ("natural", "equal_class") or training_class_weighting not in ("natural", "equal_class"):
            raise ValueError("unknown evaluation or training class weighting")
        if type(min_evaluation_per_class) is not int or min_evaluation_per_class < 1:
            raise ValueError("minimum evaluation coverage must be positive")
        self.evaluation_weighting = evaluation_weighting
        self.training_class_weighting = training_class_weighting
        self.min_evaluation_per_class = min_evaluation_per_class
        if type(context_validation_floor) is not int or context_validation_floor<1:
            raise ValueError('context validation floor must be positive')
        self.context_validation_floor=context_validation_floor
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
            CREATE TABLE IF NOT EXISTS runtime_answer_history (id INTEGER PRIMARY KEY, key TEXT NOT NULL, status TEXT NOT NULL, payload TEXT);
            CREATE TABLE IF NOT EXISTS runtime_rounds (key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT);
        """)
        self.initial, self.model, self.optimizer = initial, model, optimizer
        self._step_context = ContextVar("flywheel_step", default={})
        self._cycle_context = ContextVar("flywheel_cycle", default={})
        self._cycle_running = False
        self._step_running = False
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

    @property
    def optimizer_context(self):
        saved = self.db.execute("SELECT value FROM runtime_state WHERE key='optimizer_context'").fetchone()
        return json.loads(saved[0]) if saved else {"human_explanations": [], "evaluation_context_exposed": False}

    def set_optimizer_context(self, human_explanations, *, evaluation_context_exposed=False):
        """Persist explicitly supplied human guidance, separate from fit labels.

        Mark exposure when guidance comes from protected evaluation feedback.
        Clearing guidance cannot undo what a previous optimizer already saw.
        """
        if isinstance(human_explanations, (str, bytes)) or any(not isinstance(v, str) or not v.strip() for v in human_explanations):
            raise ValueError("human explanations must be non-empty strings")
        if type(evaluation_context_exposed) is not bool:
            raise ValueError("evaluation exposure must be boolean")
        previous = self.optimizer_context
        human_explanations = list(human_explanations)
        for secret in self.redact:
            human_explanations = [v.replace(secret, "[REDACTED]") for v in human_explanations]
        context = {"human_explanations": list(dict.fromkeys(human_explanations)),
                   "evaluation_context_exposed": previous["evaluation_context_exposed"] or evaluation_context_exposed}
        if context != previous:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO runtime_state VALUES ('optimizer_context', ?)", (_json(context),))
            self._emit({"kind": "optimizer-context-updated", **context})

    def _emit(self, event):
        event = {"created_at": datetime.now(timezone.utc).isoformat(), **event, **self._cycle_context.get(), **self._step_context.get()}
        text = _json(event)
        for value in self.redact:
            text = text.replace(json.dumps(value, ensure_ascii=False)[1:-1], "[REDACTED]")
        event = json.loads(text)
        with self.db:
            record = self.db.execute("INSERT INTO runtime_events(payload) VALUES (?)", (_json(event),))
        event["event_id"] = record.lastrowid
        self.observer(event)
        return event

    def history(self, limit: int = 100) -> tuple[dict, ...]:
        rows = self.db.execute("SELECT id,payload FROM runtime_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return tuple({**json.loads(payload), "event_id": event_id} for event_id, payload in reversed(rows))

    def trace_events(self, *, after_event_id=0, limit=100):
        """Ordered local events with a durable database-scoped reconnect cursor."""
        if type(after_event_id) is not int or after_event_id < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("trace cursor must be nonnegative and page size between 1 and 1000")
        rows = self.db.execute("SELECT id,payload FROM runtime_events WHERE id>? ORDER BY id LIMIT ?",
                               (after_event_id, limit)).fetchall()
        return {"events": [{**json.loads(payload), "event_id": event_id} for event_id, payload in rows],
                "cursor": rows[-1][0] if rows else after_event_id}

    async def step(self, stage, training, development, **kwargs):
        from .observability import step
        return await step(self, stage, training, development, **kwargs)

    def cycle(self, item=None, *, reason='item-processing'):
        """Scope prediction, optional feedback and triggered work under one durable cycle ID."""
        from .cycles import Cycle
        return Cycle(self, item, reason=reason)

    def preview_optimizer_request(self, stage, training, development, *, protected):
        """Preview the exact next-stage messages without collecting or emitting."""
        from .staged_optimization import stage_briefing
        briefing = stage_briefing(self, stage, training, development, protected)
        return {"messages": self.optimizer.request_messages(briefing),
                "briefing_fingerprint": briefing.fingerprint, "preview_only": True}

    def record_feedback_event(self, feedback: FeedbackItem, *, action="submitted", assignment=None):
        """Trace application-owned feedback; this never makes it a fit row."""
        if not isinstance(feedback, FeedbackItem) or action not in {"submitted", "retracted"}:
            raise ValueError("feedback event needs a FeedbackItem and a valid action")
        if assignment is not None and (not isinstance(assignment, str) or not assignment.strip()):
            raise ValueError("assignment must be a non-empty string")
        return self._emit({"kind": "human-feedback", "action": action, "assignment": assignment,
                           "feedback": asdict(feedback)})

    def hypotheses(self) -> tuple[dict, ...]:
        """Retain proposed structures and outcomes, including older saved rounds."""
        rows = self.db.execute("""SELECT payload FROM runtime_events
            WHERE json_extract(payload, '$.kind') IN
            ('round-started','proposal-validated','promoted','candidate-qualified','candidate-rejected','round-failed') ORDER BY id""").fetchall()
        found, current = {}, None
        for row in rows:
            event = json.loads(row[0])
            if event["kind"] == "round-started":
                current = None
            elif event["kind"] == "proposal-validated":
                config = event["candidate"]
                proposal = {"rubric": config["rubric"], "example_ids": config["example_ids"],
                            "dynamic_elements": config["dynamic_elements"],
                            "tasks": [{key: task[key] for key in ("name", "instructions", "labels")}
                                      for task in config["tasks"]]}
                current = _hash(proposal)
                found.setdefault(current, {"id": current, "proposal": proposal,
                                           "rationale": event["proposal"].get("rationale", ""), "attempts": []})
            elif current:
                found[current]["attempts"].append({"outcome": event["kind"],
                    "created_at": event["created_at"], "reason": event.get("reason"),
                    "candidate": event.get("candidate"), "incumbent": event.get("incumbent")})
                current = None
        return tuple(found.values())

    async def retry_hypothesis(self, hypothesis_id: str, training, development, *, protected, propensities):
        hypothesis = next((row for row in self.hypotheses() if row["id"] == hypothesis_id), None)
        if hypothesis is None:
            raise ValueError("unknown retained hypothesis")
        return await self.improve(training, development, protected=protected, propensities=propensities,
                                  candidate_proposal=hypothesis["proposal"])

    def feature_bank(self):
        """Inspect proposed/measured/deployed questions without running models."""
        from .feature_bank import FeatureBank
        active = [{"name": t.name, "instructions": t.instructions, "labels": list(t.labels)}
                  for t in self.active.config.tasks]
        return FeatureBank(self.db).entries(active_tasks=active)

    async def optimize_stage(self, stage, training, development, **kwargs):
        from .staged_optimization import optimize_stage
        return await optimize_stage(self, stage, training, development, **kwargs)

    async def improve_controls(self, training, development, *, protected, propensities, retry_interrupted=False,
                               max_feature_trials=3):
        from .control_scheduler import ControlScheduler
        return await ControlScheduler(self).run(training, development, protected=protected,
            propensities=propensities, retry_interrupted=retry_interrupted, max_feature_trials=max_feature_trials)

    def _activate(self, classifier):
        payload = _json(asdict(classifier))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runtime_state VALUES ('active', ?)", (payload,))
        self.active = classifier
        self._emit({"kind": "classifier-activated", "classifier_version": classifier.fingerprint,
                    "classifier_snapshot": asdict(classifier)})

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

    async def _answers(self, config, target, training, now, cache_options=None):
        from .decision_cache import CacheOptions, CacheMiss
        options = cache_options if cache_options is not None else self.cache_options
        if not isinstance(options, CacheOptions):
            raise ValueError("cache_options must be CacheOptions")
        request = config.request(target, training, now=now)
        serialized = _json(request)
        if len(serialized.encode()) > self.max_request_bytes:
            raise ValueError("complete decision request exceeds the configured byte safety ceiling")
        key = _hash({"model": self.model.model_identity, "request": request})
        row = self.db.execute("SELECT status,payload FROM runtime_answers WHERE key=?", (key,)).fetchone()
        self._emit({"kind": "decision-cache", "request_fingerprint": key,
                    "policy": options.policy, "status": row[0] if row else "missing",
                    "retry_failed": options.retry_failed})
        if options.policy == "cache_only" and (not row or row[0] != "complete"):
            raise CacheMiss("no complete answer exists for the exact decision request")
        if row and row[0] != "complete" and not options.retry_failed:
                raise RuntimeError("an interrupted or failed request needs explicit retry authorization")
        if row and row[0] == "complete" and options.policy != "refresh":
            raw = json.loads(row[1])
            self._emit({"kind": "features-cached", "target_id": target.id, "request_fingerprint": key,
                        "request": request, "answers": raw["answers"], "cached": True})
            return ClassifiedAnswers({name: DecisionResult(**result) for name, result in raw["answers"].items()},
                                     raw["model"], raw["usage"], raw["latency_ms"])
        if self.requests >= self.max_requests:
            from .observability import RequestBudgetExhausted
            raise RequestBudgetExhausted("decision-model request ceiling reached")
        self.requests += 1
        with self.db:
            if row:
                self.db.execute("INSERT INTO runtime_answer_history(key,status,payload) VALUES (?,?,?)", (key, *row))
            self.db.execute("INSERT OR REPLACE INTO runtime_answers VALUES (?, 'pending', NULL)", (key,))
        self._emit({"kind": "features-requested", "target_id": target.id, "request_fingerprint": key,
                    "requests": self.requests, "ceiling": self.max_requests})
        try:
            batch = await self.model.classify(config, target, training, now=now, event_sink=self._emit)
            # Validate full coverage before committing a reusable cache entry.
            self._features(config, batch)
        except Exception as error:
            with self.db:
                self.db.execute("UPDATE runtime_answers SET status='failed' WHERE key=?", (key,))
            self._emit({"kind": "features-failed", "target_id": target.id, "error_type": type(error).__name__})
            raise RuntimeError("decision feature request failed; see recorded error type") from None
        with self.db:
            self.db.execute("UPDATE runtime_answers SET status='complete',payload=? WHERE key=?",
                            (_json(asdict(batch)), key))
        self._emit({"kind": "features-completed", "target_id": target.id, "model": batch.model,
                    "request_fingerprint": key, "answers": asdict(batch)["answers"], "cached": False,
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

    async def predict(self, target: Item, training: Sequence[LabeledItem], *, now: datetime | None = None,
                      cache_options=None):
        self.reconcile_feedback(training)
        batch = await self._answers(self.active.config, target, training, now or datetime.now(timezone.utc), cache_options)
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
                    "version": self.active.fingerprint, "fitted_head": self.active.head is not None,
                    "probabilities": result.probabilities, "confidence": result.confidence,
                    "validation_status": self.active.validation_status,
                    "model": result.model, "usage": result.usage, "latency_ms": result.latency_ms})
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
                      retry_interrupted: bool = False, candidate_proposal: Mapping | None = None,
                      apply_promotion: bool = True, evaluation_time: datetime | None = None,
                      require_recall_safeguards: bool = False) -> dict:
        if evaluation_time is not None and (evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None):
            raise ValueError("evaluation time must be timezone aware")
        self._validate_partitions(training, development, protected, propensities)
        self.reconcile_feedback(training, development=development)
        round_key = _hash({"training": self._evidence(training), "development": self._evidence(development),
                           "propensities": propensities, "protected": sorted(item.id for item in protected),
                           "evaluation_weighting": self.evaluation_weighting,
                           "training_class_weighting": self.training_class_weighting,
                           "min_evaluation_per_class": self.min_evaluation_per_class,
                           "evaluation_policy": asdict(self.evaluation_policy),
                           "context_validation_floor": self.context_validation_floor,
                           "require_recall_safeguards": require_recall_safeguards,
                           "optimizer_context": self.optimizer_context,
                           **({"evaluation_time": evaluation_time.isoformat()}
                              if evaluation_time is not None and "current_datetime" in self.active.config.dynamic_elements else {}),
                           **({"apply_promotion": False, "baseline_version": self.active.fingerprint}
                              if not apply_promotion else {}),
                           **({"candidate_proposal": candidate_proposal} if candidate_proposal is not None else {})})
        saved = self.db.execute("SELECT status,payload FROM runtime_rounds WHERE key=?", (round_key,)).fetchone()
        if saved:
            if saved[0] == "complete":
                cached = json.loads(saved[1])
                if "active" in cached and apply_promotion:
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
        development_counts = {label: sum(row.label == label for row in development) for label in self.initial.task.labels}
        if min(counts.values()) < 3 or min(development_counts.values()) < self.min_evaluation_per_class:
            result = {"promoted": False, "reason": "waiting for training and development class coverage",
                      "development_counts": development_counts,
                      "minimum_training_per_class": 3, "minimum_development_per_class": self.min_evaluation_per_class}
            self._emit({"kind": "waiting-for-labels", "counts": counts, "development_count": len(development), **result})
            return result
        now = evaluation_time or datetime.now(timezone.utc)
        with self.db:
            self.db.execute("INSERT INTO runtime_rounds VALUES (?, 'pending', NULL)", (round_key,))
        self._emit({"kind": "round-started", "training_count": len(training), "development_count": len(development)})
        try:
            briefing = FeedbackBriefing.build(self.initial.task, training,
                current={**self.active.config.briefing_state(),
                         "evaluation_weighting": self.evaluation_weighting,
                         "training_class_weighting": self.training_class_weighting,
                         "prior_hypotheses": self.hypotheses(),
                         "request_budget_bytes": self.max_request_bytes,
                         "training_predictions": [event for event in self.history(1000)
                            if event["kind"] == "prediction" and event["target_id"] in {row.item.id for row in training}]},
                protected=tuple(row.item for row in development) + tuple(protected),
                human_explanations=self.optimizer_context["human_explanations"])
            if candidate_proposal is None:
                proposal = self.optimizer.propose(briefing)
            else:
                proposal = dict(candidate_proposal)
                self._emit({"kind": "hypothesis-retry", "hypothesis_id": _hash(proposal),
                            "training_count": len(training), "development_count": len(development)})
            config = self.active.config.apply(proposal, training)
            self._emit({"kind": "proposal-validated", "proposal": proposal,
                        "previous": self.active.config.briefing_state(), "candidate": config.briefing_state()})
            rows = []
            feature_observations = {task.name: [] for task in config.tasks}
            for row in training:
                batch = await self._answers(config, row.item, training, now)
                for task in config.tasks:
                    feature_observations[task.name].append((row.label, batch.answers[task.name].probabilities))
                values = self._features(config, batch)
                feedback = FeedbackItem("review-" + row.item.id, row.item.id, config.task.name,
                    final_answer_value=row.label, edit_comment_value=row.context.get("human_feedback"),
                    label_source=LABEL_SOURCE_VETTED, selection_propensity=propensities[row.item.id],
                    review_provenance="human-reviewed")
                rows.append(HeadRow(row.item.id, feedback, tuple(Feature(key, value, key.split("/", 1)[0])
                                                                for key, value in values.items())))
            from .feature_bank import probability_diagnostics
            diagnostics = {task.name: probability_diagnostics(config.task.labels, task.labels,
                           feature_observations[task.name]) for task in config.tasks}
            if diagnostics:
                self._emit({"kind": "feature-diagnostics", "diagnostics": diagnostics,
                            "training_count": len(rows), "scope": "training only"})
            self._emit({"kind": "fit-started", "training_count": len(rows), "features": list(values),
                        "rows": [{"item_id": row.item_id, "label": row.feedback.final_answer_value,
                                  "propensity": row.feedback.selection_propensity,
                                  "features": {f.name: f.value for f in row.features}} for row in rows]})
            head = fit_learned_head(config.task, rows, declared_features=tuple(values),
                development_ids=tuple(row.item.id for row in development), scoreboard_ids=tuple(item.id for item in protected),
                scorecard_fingerprint=config.fingerprint, policy_fingerprint=_hash(config.example_ids),
                context_artifact_fingerprint=config.fingerprint, source_model_provenance=self.model.model_identity,
                training_class_weighting=self.training_class_weighting)
            status=('provisional' if self.active.validation_status=='provisional' and
                    min(development_counts.values())<self.context_validation_floor else 'evaluated')
            candidate = FittedClassifier(config, head, self._evidence(training), self._evidence(development),status)
            self._emit({"kind": "fit-completed", "features": list(head.feature_names),
                        "head": asdict(head),
                        "training_count": len(rows), "calibration": "out_of_fold"})
            incumbent_metrics = await self._score(self.active, development, training, now)
            candidate_metrics = await self._score(candidate, development, training, now)
            metric = "balanced_brier" if self.evaluation_weighting == "equal_class" else "brier"
            brier_improved = candidate_metrics[metric] < incumbent_metrics[metric] - 1e-12
            recall_safe = (candidate_metrics["balanced_accuracy"] >= incumbent_metrics["balanced_accuracy"] and
                all(candidate_metrics["per_class"][label]["recall"] >= incumbent_metrics["per_class"][label]["recall"]
                    for label in self.initial.task.labels)) if require_recall_safeguards else True
            improved = brier_improved and recall_safe
            promoted = improved and apply_promotion
            result = {"promoted": promoted, "incumbent": incumbent_metrics, "candidate": candidate_metrics,
                      "improved": improved, "trial_fingerprint": round_key,
                      "brier_improved": brier_improved, "recall_safeguards_passed": recall_safe,
                      "promotion_metric": metric, "evaluation_weighting": self.evaluation_weighting,
                      "training_class_weighting": self.training_class_weighting,
                      "feature_diagnostics": diagnostics,
                      "evaluation_independent_of_optimizer_context": not self.optimizer_context["evaluation_context_exposed"],
                      "reason": (f"lower development {metric}" if improved else
                                 "candidate failed per-class recall safeguards" if not recall_safe else
                                 f"development {metric} did not improve")}
            self._emit({"kind": "candidate-evaluated", **result})
            baseline_version = self.active.fingerprint
            if promoted:
                self._activate(candidate)
            self._emit({"kind": "promoted" if promoted else "candidate-qualified" if improved else "candidate-rejected",
                        "version": self.active.fingerprint, **result})
            with self.db:
                self.db.execute("UPDATE runtime_rounds SET status='complete',payload=? WHERE key=?",
                                (_json({"result": result, "active": asdict(self.active),
                                        "fitted_candidate": asdict(candidate), "baseline_version": baseline_version,
                                        "training_evidence": self._evidence(training),
                                        "development_evidence": self._evidence(development)}), round_key))
            return result

        except Exception as error:
            from .observability import RequestBudgetExhausted
            if isinstance(error, RequestBudgetExhausted):
                self._emit({"kind": "round-paused", "reason": "request budget exhausted"})
                raise
            result = {"promoted": False, "reason": "round failed", "error_type": type(error).__name__}
            self._emit({"kind": "round-failed", **result})
            with self.db:
                self.db.execute("UPDATE runtime_rounds SET status='complete',payload=? WHERE key=?",
                                (_json({"result": result, "active": asdict(self.active)}), round_key))
            return result

    def promote_trial(self, trial_fingerprint, training, development):
        row = self.db.execute("SELECT status,payload FROM runtime_rounds WHERE key=?", (trial_fingerprint,)).fetchone()
        if not row or row[0] != "complete":
            raise ValueError("trial is not complete")
        saved = json.loads(row[1])
        if not saved["result"].get("improved") or saved.get("baseline_version") != self.active.fingerprint:
            raise ValueError("trial did not improve this incumbent")
        if saved.get("training_evidence") != self._evidence(training) or saved.get("development_evidence") != self._evidence(development):
            raise ValueError("trial feedback changed; refit and reevaluate")
        self._activate(_restore(saved["fitted_candidate"]))
        self._emit({"kind": "promoted", **saved["result"], "promoted": True,
                    "version": self.active.fingerprint, "selection": "best isolated control trial"})

    async def _score(self, classifier, development, training, now):
        available_count = len(development)
        development = self.evaluation_policy.select(development, classifier.config.task.labels)
        predictions, distributions = [], []
        for row in development:
            batch = await self._answers(classifier.config, row.item, training, now)
            if classifier.head:
                probabilities = classifier.head.probabilities(self._features(classifier.config, batch))
                label = classifier.head.predict(self._features(classifier.config, batch))
            else:
                answer = batch.answers["decision"]
                probabilities, label = answer.probabilities, answer.label
            predictions.append(label)
            distributions.append(probabilities)
        metrics = classification_metrics(classifier.config.task.labels, [row.label for row in development],
                                         predictions, distributions)
        metrics["evaluation_independent_of_optimizer_context"] = not self.optimizer_context["evaluation_context_exposed"]
        metrics['available_count'] = available_count
        metrics['sample_limit'] = self.evaluation_policy.max_samples
        metrics['sample_ids'] = [row.item.id for row in development]
        metrics['context_validation_floor']=self.context_validation_floor
        metrics['evidence_status']=('exploratory' if any(g['count']<self.context_validation_floor for g in metrics['per_class'].values())
                                    else 'development-validation; not an independent final scoreboard')
        return metrics
