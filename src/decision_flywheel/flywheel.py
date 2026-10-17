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
from functools import wraps
from typing import Protocol

from .classifier_config import ClassifiedAnswers, ClassifierConfig
from .classification_metrics import classification_metrics
from .context import _normalized_text
from .feedback import Feature, FeedbackItem, LABEL_SOURCE_VETTED
from .head import Calibration, HeadProvenance, LearnedHead, OutOfFoldPredictions
from .models import DecisionResult, DecisionTask, Item, LabeledItem
from .optimizer_agent import FeedbackBriefing, OptimizerAgent


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def track_answer_dependencies(function):
    """Nested operations retain exactly the responses they consumed."""
    @wraps(function)
    async def tracked(wheel, *args, **kwargs):
        token = wheel._answer_scopes.set((*wheel._answer_scopes.get(), {}))
        try:
            return await function(wheel, *args, **kwargs)
        finally:
            wheel._answer_scopes.reset(token)
    return tracked


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
    answer_dependencies: tuple[Mapping, ...] = ()

    @property
    def fingerprint(self):
        head = asdict(self.head) if self.head else None
        if head and head["provenance"].get("training_class_weighting") == "natural":
            # New optional metadata must not rename an unchanged legacy head.
            head["provenance"].pop("training_class_weighting", None)
        return _hash({"config": self.config.fingerprint, "head": head,
                      "training_evidence": self.training_evidence, "development_evidence": self.development_evidence,
                      **({"answer_dependencies": self.answer_dependencies} if self.answer_dependencies else {}),
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
                           HeadProvenance(**provenance), head["refitted_cyclotron_fingerprint"])
    return FittedClassifier(config, head, raw["training_evidence"], raw.get("development_evidence"),
                            raw.get('validation_status'), tuple(raw.get('answer_dependencies', ())))


STORE_BUSY_TIMEOUT_SECONDS = 30


class DecisionFlywheel:
    """Small application-facing API: predict, improve, reconcile, history, close.

    All reviews supplied for fitting need explicit selection propensities. Audit
    labels are not accepted here. The caller supplies their records as protected
    ID/text firewalls, without labels. max_requests is a per-open authorization
    ceiling; each reserved attempt is also durably recorded before the call.
    """

    def __init__(self, database: str | Path, initial: ClassifierConfig, model: FeatureModel,
                 optimizer: OptimizerAgent | None = None, *, observer: Callable[[dict], None] | None = None,
                 max_requests: int = 100, max_request_bytes: int = 32000, calibration_method: str = 'auto',
                 redact: Sequence[str] = (), evaluation_weighting: str = "equal_class",
                 training_class_weighting: str = "natural", min_evaluation_per_class: int = 1,
                 cache_options=None, context_validation_floor: int = 20, evaluation_policy=None, selection_policy=None,
                 idea_screening=None, storage=None):
        from .selection_policy import SelectionPolicy
        if selection_policy is not None and not isinstance(selection_policy, SelectionPolicy):
            raise ValueError('selection_policy must be SelectionPolicy')
        if selection_policy is not None and selection_policy.positive_class is not None and selection_policy.positive_class not in initial.task.labels:
            raise ValueError('selection positive class must be a task label')
        self.selection_policy = selection_policy
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
        if calibration_method not in ('auto','temperature','isotonic'):
            raise ValueError('calibration_method must be auto, temperature, or isotonic')
        if type(min_evaluation_per_class) is not int or min_evaluation_per_class < 1:
            raise ValueError("minimum evaluation coverage must be positive")
        self.evaluation_weighting = evaluation_weighting
        self.training_class_weighting = training_class_weighting
        self.calibration_method = calibration_method
        self.min_evaluation_per_class = min_evaluation_per_class
        if type(context_validation_floor) is not int or context_validation_floor<1:
            raise ValueError('context validation floor must be positive')
        self.context_validation_floor=context_validation_floor
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("max_requests must be positive")
        if type(max_request_bytes) is not int or max_request_bytes < 1:
            raise ValueError("max_request_bytes must be positive")
        if getattr(storage,'requires_durable_optimizer',False):
            from .s3_optimizer_transport import require_durable_optimizer
            require_durable_optimizer(optimizer)
        if storage is None:
            from .runtime_storage import SQLiteRuntimeStateStorage
            self.runtime_storage = SQLiteRuntimeStateStorage(database, timeout=STORE_BUSY_TIMEOUT_SECONDS)
            # Native compatibility for existing embedded snapshots and local tools.
            # Runtime algorithms use domain repositories; this is not a cloud mode.
            self.db = self.runtime_storage.db
            from .pending_storage import SQLitePendingStorage
            self.pending_storage = SQLitePendingStorage(self.db)
            from .runtime_cache_storage import SQLiteRuntimeCacheStorage
            self.runtime_cache = SQLiteRuntimeCacheStorage(self.db)
            from .learning_storage import SQLiteLearningStorage, SQLiteFeatureStorage, SQLiteControlStorage
            from .hypothesis_storage import SQLiteHypothesisStorage
            self.learning_storage = SQLiteLearningStorage(self.db)
            self.feature_storage = SQLiteFeatureStorage(self.db)
            self.control_storage = SQLiteControlStorage(self.db)
            self.hypothesis_storage = SQLiteHypothesisStorage(self.db, controls=self.control_storage)
            from .shadow_storage import SQLiteShadowStorage
            self.shadow_storage = SQLiteShadowStorage(self.db)
        else:
            # The injected composition must provide every domain. Never fill a
            # missing cloud repository with a local database.
            names=('runtime_storage','pending_storage','runtime_cache','learning_storage',
                   'feature_storage','control_storage','hypothesis_storage','shadow_storage')
            repositories={name:getattr(storage,name) for name in names}
            for name,repository in repositories.items():setattr(self,name,repository)
            self.db=None  # Native compatibility handle is unavailable in cloud mode.
        if optimizer is None:
            from .optimizer_agent import DisabledOptimizer
            optimizer = DisabledOptimizer()
        self.initial, self.model, self.optimizer = initial, model, optimizer
        self.version_baseline = initial
        self._step_context = ContextVar("flywheel_step", default={})
        self._answer_scopes = ContextVar('answer_dependencies', default=())
        # A multi-output cyclotron installs a sibling check; one output has none.
        self.sibling_check = None
        self._cycle_context = ContextVar("flywheel_cycle", default={})
        self._cycle_running = False
        self._step_running = False
        self.observer = observer or (lambda event: None)
        self.redact = tuple(value for value in redact if value)
        self.max_requests, self.max_request_bytes, self.requests = max_requests, max_request_bytes, 0
        expected = _json({"task": initial.task.fingerprint, "model": model.model_identity})
        try:
            self.runtime_storage.bind_contract(expected)
        except ValueError:
            self.runtime_storage.close()
            raise
        active = self.runtime_storage.state('active')
        self.active = _restore(json.loads(active)) if active else FittedClassifier(initial)
        prior_observer = optimizer.observer
        def observe(event):
            clean = self._emit(event)
            prior_observer(clean)
        # The library persists actual optimizer messages before the UI callback.
        self.optimizer_observer = prior_observer
        optimizer.observer = observe
        saved_policy = self.runtime_storage.state('selection_policy')
        if self.selection_policy is None and saved_policy:
            self.selection_policy = SelectionPolicy(**json.loads(saved_policy))
        implicit_default = self.selection_policy is None
        if implicit_default:
            # Default objective is accuracy; a policy saved by an earlier run (above) always wins.
            # The implicit default is not persisted or announced; only explicit policies are.
            self.selection_policy = SelectionPolicy(primary='accuracy')
        if not implicit_default:
            encoded = _json(asdict(self.selection_policy))
            if not saved_policy or saved_policy != encoded:
                self.runtime_storage.put_state('selection_policy', encoded)
                self._emit({'kind':'selection-policy-configured', 'selection_policy':asdict(self.selection_policy),
                            'previous':json.loads(saved_policy) if saved_policy else None})

        from .idea_screening import IdeaScreeningConfig
        if idea_screening is not None and not isinstance(idea_screening, IdeaScreeningConfig):
            raise ValueError('idea_screening must be IdeaScreeningConfig')
        saved_screening = self.runtime_storage.state('idea_screening')
        self.idea_screening = idea_screening or (IdeaScreeningConfig.from_json(json.loads(saved_screening))
                                                 if saved_screening else IdeaScreeningConfig())
        self._screening_unsupported_logged = False
        if idea_screening is not None:
            encoded = _json(idea_screening.as_json())
            if not saved_screening or saved_screening != encoded:
                self.runtime_storage.put_state('idea_screening', encoded)
                self._emit({'kind': 'idea-screening-configured', 'idea_screening': idea_screening.as_json(),
                            'previous': json.loads(saved_screening) if saved_screening else None})

    def screening_enabled(self):
        """Opt-in idea screening, supported only when accuracy is the primary objective (else legacy path)."""
        if not self.idea_screening.enabled:
            return False
        if self.selection_policy is not None and self.selection_policy.primary != 'accuracy':
            if not self._screening_unsupported_logged:
                self._screening_unsupported_logged = True
                from .idea_screening import _wrong_objective_event
                _wrong_objective_event(self)
            return False
        return True

    def shadow_enabled(self):
        """Forward shadow evaluation (Stage 2b): opt-in, accuracy objective only."""
        return self.screening_enabled() and self.idea_screening.mode == 'shadow'

    def close(self):
        self.optimizer.observer = self.optimizer_observer
        self.runtime_storage.close()

    @property
    def optimizer_context(self):
        saved = self.runtime_storage.state('optimizer_context')
        return json.loads(saved) if saved else {"human_explanations": [], "evaluation_context_exposed": False}

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
            self.runtime_storage.put_state('optimizer_context', _json(context))
            self._emit({"kind": "optimizer-context-updated", **context})

    def _emit(self, event):
        event = {"created_at": datetime.now(timezone.utc).isoformat(), **event, **self._cycle_context.get(), **self._step_context.get()}
        text = _json(event)
        for value in self.redact:
            text = text.replace(json.dumps(value, ensure_ascii=False)[1:-1], "[REDACTED]")
        event = json.loads(text)
        event["event_id"] = self.runtime_storage.append_event(_json(event))
        self.observer(event)
        return event

    def history(self, limit: int = 100) -> tuple[dict, ...]:
        return self.runtime_storage.history(limit)

    def events_of(self, kinds: tuple[str, ...]) -> tuple[dict, ...]:
        """Every event of the named kinds in order, with no recency window."""
        return self.runtime_storage.events_of(kinds)

    def full_history(self) -> tuple[dict, ...]:
        """Every event in order, with no recency window."""
        return self.runtime_storage.history(-1)

    def active_labels(self) -> tuple[dict, ...]:
        """The submitted feedback events still in force, in arrival order."""
        return self.runtime_storage.active_labels()

    def trace_events(self, *, after_event_id=0, limit=100):
        """Ordered events with the same durable database-scoped cursor."""
        return self.runtime_storage.trace_events(after_event_id=after_event_id,limit=limit)

    async def step(self, stage, training, development, **kwargs):
        from .observability import step
        return await step(self, stage, training, development, **kwargs)

    def cycle(self, item=None, *, reason='item-processing'):
        """Scope prediction, optional feedback and triggered work under one durable cycle ID."""
        from .cycles import Cycle
        return Cycle(self, item, reason=reason)

    def resume_cycle(self, item, *, include_failed=False):
        """Resume an unfinished operational cycle without rerunning paid work."""
        from .cycles import Cycle
        return Cycle.resume(self,item,include_failed=include_failed)

    def preview_optimizer_request(self, stage, training, development, *, protected):
        """Preview the exact next-stage messages without collecting or emitting."""
        from .staged_optimization import stage_briefing
        briefing = stage_briefing(self, stage, training, development, protected)
        return {"messages": self.optimizer.request_messages(briefing),
                "briefing_fingerprint": briefing.fingerprint, "preview_only": True}

    def record_feedback_event(self, feedback: FeedbackItem, *, action="submitted", assignment=None, annotations=None):
        """Trace application-owned feedback; this never makes it a fit row.

        ``annotations`` adds application facts about the review, such as who
        selected it, a reason code, or the decision it answers. Values are
        strings or None and cannot replace the event's own fields.
        """
        if not isinstance(feedback, FeedbackItem) or action not in {"submitted", "retracted"}:
            raise ValueError("feedback event needs a FeedbackItem and a valid action")
        if assignment is not None and (not isinstance(assignment, str) or not assignment.strip()):
            raise ValueError("assignment must be a non-empty string")
        annotations = dict(annotations or {})
        if set(annotations) & {"kind", "action", "assignment", "feedback", "created_at", "event_id"} or any(
                not isinstance(key, str) or (value is not None and not isinstance(value, str))
                for key, value in annotations.items()):
            raise ValueError("feedback annotations must be new string fields with string or None values")
        return self._emit({"kind": "human-feedback", "action": action, "assignment": assignment,
                           "feedback": asdict(feedback), **annotations})

    def hypotheses(self) -> tuple[dict, ...]:
        """Retain proposed structures and outcomes, including older saved rounds."""
        rows = self.runtime_storage.event_payloads((
            'round-started','proposal-validated','promoted','candidate-qualified','candidate-rejected','round-failed'))
        found, current = {}, None
        for payload in rows:
            event = json.loads(payload)
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
        return FeatureBank(storage=self.feature_storage).entries(active_tasks=active)

    async def optimize_stage(self, stage, training, development, **kwargs):
        from .staged_optimization import optimize_stage
        return await optimize_stage(self, stage, training, development, **kwargs)

    async def improve_controls(self, training, development, *, protected, propensities, retry_interrupted=False,
                               max_feature_trials=3):
        from .control_scheduler import ControlScheduler
        return await ControlScheduler(self).run(training, development, protected=protected,
            propensities=propensities, retry_interrupted=retry_interrupted, max_feature_trials=max_feature_trials)

    def _activate(self, classifier, *, change_summary=None):
        if classifier.head and classifier.head.provenance.context_artifact_fingerprint != classifier.config.fingerprint:
            raise ValueError('learned head does not match the classifier context')
        if classifier.head and (not classifier.answer_dependencies or
                                not self.answer_dependencies_current(classifier.answer_dependencies)):
            raise ValueError('decision answers changed; refit the learned head')
        payload = _json(asdict(classifier))
        self.runtime_storage.put_state('active', payload)
        self.active = classifier
        from .classifier_snapshot import compact_snapshot
        self._emit({"kind": "classifier-activated", "classifier_version": classifier.fingerprint,
                    "classifier_snapshot": compact_snapshot(classifier),
                    **({"change_summary": change_summary} if change_summary else {})})

    def extend_example_list(self, example_ids, *, change_summary):
        """Append human-accepted examples to the fixed example list as a new version.

        The decision context changes, so a learned head fitted to the previous
        context no longer applies: the new version starts unfitted and the next
        learning pass refits it. A later invalidation resets to an initial
        configuration that keeps these examples; versions still count from the
        configuration the store was created with.
        """
        from dataclasses import replace
        additional_example_ids = tuple(example_ids)
        active_config = self.active.config
        if not additional_example_ids or any(key in active_config.example_ids for key in additional_example_ids):
            raise ValueError("added examples must be new to the active example list")
        self.initial = replace(self.initial, example_ids=self.initial.example_ids + tuple(
            key for key in additional_example_ids if key not in self.initial.example_ids))
        extended_config = replace(active_config, example_ids=active_config.example_ids + additional_example_ids,
                                  parent_fingerprint=active_config.fingerprint)
        self._activate(FittedClassifier(extended_config), change_summary=change_summary)

    SHARED_QUESTIONS_NOTE = ("Other classifiers in this cyclotron ask these supporting questions. A question with the "
                             "same name, instructions and labels is asked once and its answer feeds every classifier "
                             "that lists it; propose one verbatim to share it.")

    def sibling_briefing(self):
        """Optimizer context naming sibling supporting questions; empty for a single output."""
        check = self.sibling_check
        questions = check.sibling_questions() if check is not None and check.sharing() else {}
        return {"sibling_supporting_questions": {"note": self.SHARED_QUESTIONS_NOTE, "by_classifier": questions}} if questions else {}

    async def sibling_effects(self, config, now):
        """Each sibling output's development effect of activating ``config`` (None without siblings)."""
        if self.sibling_check is None:
            return None
        effects = await self.sibling_check.evaluate(config, now)
        if effects['context_changed']:
            self._emit({'kind': 'sibling-effects-measured', 'candidate_configuration': config.briefing_state(), **effects})
        return effects

    def model_context(self, config, training):
        identity = getattr(self.model, 'feature_context_identity', None)
        return identity(config, training) if identity else self.model.model_identity

    def _answer_key(self, config, target, training, now):
        identity = getattr(self.model, 'cache_identity', None)
        model = identity(config, target, training, now=now) if identity else self.model.model_identity
        return _hash({'model': model, 'request': config.request(target, training, now=now)})

    def answer_dependency(self, config, target, training, now):
        """Reference the exact consumed response, without copying article text."""
        key = self._answer_key(config, target, training, now)
        row = self.runtime_cache.answer(key)
        if not row or row[0] != 'complete':
            raise ValueError('a fit requires a complete recorded decision response')
        dependency = {'request_fingerprint': key, 'response_fingerprint': _hash(json.loads(row[1]))}
        snapshot = getattr(self.model, 'answer_dependency', None)
        if snapshot:
            dependency['provider_dependency'] = snapshot(config, target, training, now=now)
        return dependency

    def answer_dependencies_current(self, dependencies):
        """Check consumed response revisions locally; never call a provider."""
        check = getattr(self.model, 'answer_dependency_current', None)
        for dependency in dependencies:
            row = self.runtime_cache.answer(dependency['request_fingerprint'])
            if not row or row[0] != 'complete' or _hash(json.loads(row[1])) != dependency['response_fingerprint']:
                return False
            provider = dependency.get('provider_dependency')
            if provider is not None and (check is None or not check(provider)):
                return False
        return True

    def _record_answer_dependencies(self, dependencies):
        for scope in self._answer_scopes.get():
            for dependency in dependencies:
                scope[_hash(dependency)] = dependency

    def collected_answer_dependencies(self):
        scopes = self._answer_scopes.get()
        return list(scopes[-1].values()) if scopes else []

    def cached_artifact(self, table, column, key):
        """Reuse valid derived work; retain obsolete revisions for inspection."""
        if (table, column) not in {('runtime_rounds', 'key'), ('classifier_training', 'id'),
                                 ('optimization_stages', 'id'), ('question_measurements', 'id'),
                                 ('example_measurements', 'id')}:
            raise ValueError('unknown derived cache')
        kinds={'classifier_training':'classifier','optimization_stages':'rubric',
               'question_measurements':'questions','example_measurements':'examples'}
        def read(identifier):
            return (self.runtime_cache.round(identifier) if table=='runtime_rounds' else
                    self.learning_storage.artifact(kinds[table],identifier))
        while True:
            row = read(key)
            if not row or row[0] != 'complete':
                return key, row
            payload = json.loads(row[1])
            dependencies = payload.get('answer_dependencies')
            if dependencies is not None and self.answer_dependencies_current(dependencies):
                self._record_answer_dependencies(dependencies)
                return key, row
            previous = key
            key = _hash({'previous_artifact': previous, 'response_revision': 1})
            exists = read(key)
            if not exists:
                self._emit({'kind': 'derived-cache-invalidated', 'artifact_type': table,
                            'previous_artifact': previous, 'artifact_fingerprint': key,
                            'reason': 'consumed decision responses changed or dependencies were not recorded'})

    def reconcile_model_context(self, training):
        """Discard a stale numerical head without discarding learned criteria."""
        if not self.active.head:
            return False
        current = self.model_context(self.active.config, training)
        previous = self.active.head.provenance.source_model_provenance
        dependencies_recorded = bool(self.active.answer_dependencies)
        answers_current = dependencies_recorded and self.answer_dependencies_current(self.active.answer_dependencies)
        if current == previous and answers_current:
            return False
        from dataclasses import replace
        previous_snapshot = asdict(self.active)
        self._activate(replace(self.active, head=None, answer_dependencies=()))
        self._emit({'kind':'head-invalidated','reason':('decision feature context changed' if current != previous
                    else 'consumed decision response changed' if dependencies_recorded
                    else 'decision response dependencies were not recorded'),
                    'previous_classifier_snapshot': previous_snapshot,
                    'classifier_snapshot': asdict(self.active),
                    'previous_model_context':previous,'model_context':current})
        return True

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
        capabilities = getattr(self.model, 'capabilities', None)
        if capabilities is not None:
            capabilities.validate_context(request['state']['examples'])
        serialized = _json(request)
        if len(serialized.encode()) > self.max_request_bytes:
            raise ValueError("complete decision request exceeds the configured byte safety ceiling")
        identity = getattr(self.model, 'cache_identity', None)
        key = self._answer_key(config, target, training, now)
        row = self.runtime_cache.answer(key)
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
            self._record_answer_dependencies([self.answer_dependency(config, target, training, now)])
            return ClassifiedAnswers({name: DecisionResult(**result) for name, result in raw["answers"].items()},
                                     raw["model"], raw["usage"], raw["latency_ms"])
        if self.requests >= self.max_requests:
            from .observability import RequestBudgetExhausted
            raise RequestBudgetExhausted("decision-model request ceiling reached")
        self.requests += 1
        self.runtime_cache.begin_answer(key, row)
        self._emit({"kind": "features-requested", "target_id": target.id, "request_fingerprint": key,
                    "requests": self.requests, "ceiling": self.max_requests})
        try:
            collect = getattr(self.model, 'classify_with_cache_options', None)
            batch = (await collect(config, target, training, now=now, event_sink=self._emit, cache_options=options)
                     if collect else await self.model.classify(config, target, training, now=now, event_sink=self._emit))
            # Validate full coverage before committing a reusable cache entry.
            self._features(config, batch)
        except Exception as error:
            self.runtime_cache.fail_answer(key)
            self._emit({"kind": "features-failed", "target_id": target.id, "error_type": type(error).__name__})
            raise RuntimeError("decision feature request failed; see recorded error type") from None
        current_key = (_hash({"model": identity(config, target, training, now=now), "request": request})
                       if identity else key)
        self.runtime_cache.complete_answer(key, _json(asdict(batch)), previous=row, current_key=current_key)
        key = current_key
        self._emit({"kind": "features-completed", "target_id": target.id, "model": batch.model,
                    "request_fingerprint": key, "answers": asdict(batch)["answers"], "cached": False,
                    "usage": batch.usage, "latency_ms": batch.latency_ms})
        self._record_answer_dependencies([self.answer_dependency(config, target, training, now)])
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
        self.reconcile_model_context(training)
        batch = await self._answers(self.active.config, target, training, now or datetime.now(timezone.utc), cache_options)
        # A refresh can replace a consumed training response during this call.
        self.reconcile_model_context(training)
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
                    "decision_model_label": batch.answers['decision'].label,
                    "decision_model_probabilities": batch.answers['decision'].probabilities,
                    "ml_features": values if self.active.head else None,
                    "uncalibrated_probabilities": self.active.head.uncalibrated_probabilities(values) if self.active.head else None,
                    "calibration_temperature": self.active.head.calibration.temperature if self.active.head else None,
                    "calibration_provenance": {"method":self.active.head.calibration.method,"fit_on":self.active.head.calibration.fit_on,
                                               "selection_reason":self.active.head.calibration.selection_reason,
                                               "isotonic_knots":self.active.head.calibration.isotonic_knots,
                                               "training_ids":list(self.active.head.provenance.training_ids)} if self.active.head else None,
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

    @track_answer_dependencies
    async def improve(self, training: Sequence[LabeledItem], development: Sequence[LabeledItem], *,
                      protected: Sequence[Item], propensities: Mapping[str, float],
                      retry_interrupted: bool = False, candidate_proposal: Mapping | None = None,
                      apply_promotion: bool = True, evaluation_time: datetime | None = None,
                      require_recall_safeguards: bool = False) -> dict:
        if evaluation_time is not None and (evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None):
            raise ValueError("evaluation time must be timezone aware")
        self._validate_partitions(training, development, protected, propensities)
        self.reconcile_feedback(training, development=development)
        self.reconcile_model_context(training)
        round_key = _hash({"training": self._evidence(training), "development": self._evidence(development),
                           "model_context": self.model_context(self.active.config, training),
                           "propensities": propensities, "protected": sorted(item.id for item in protected),
                           "evaluation_weighting": self.evaluation_weighting,
                           "training_class_weighting": self.training_class_weighting,
                           "min_evaluation_per_class": self.min_evaluation_per_class,
                           "evaluation_policy": asdict(self.evaluation_policy),
                           "selection_policy": asdict(self.selection_policy) if self.selection_policy else None,
                           "context_validation_floor": self.context_validation_floor,
                           "require_recall_safeguards": require_recall_safeguards,
                           "optimizer_context": self.optimizer_context,
                           **({"initial_answers": {row.item.id: row.initial_answer_value for row in training
                                                   if row.initial_answer_value is not None}}
                              if any(row.initial_answer_value is not None for row in training) else {}),
                           **({"evaluation_time": evaluation_time.isoformat()}
                              if evaluation_time is not None and "current_datetime" in self.active.config.dynamic_elements else {}),
                           **({"apply_promotion": False, "baseline_version": self.active.fingerprint}
                              if not apply_promotion else {}),
                           **({"candidate_proposal": candidate_proposal} if candidate_proposal is not None else {}),
                           **({"siblings": self.sibling_check.evidence()} if self.sibling_check is not None else {})})
        round_key, saved = self.cached_artifact('runtime_rounds', 'key', round_key)
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
            self.runtime_cache.reset_pending_round(round_key)
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
        self.runtime_cache.begin_round(round_key)
        self._emit({"kind": "round-started", "training_count": len(training), "development_count": len(development)})
        try:
            briefing = FeedbackBriefing.build(self.initial.task, training,
                current={**self.active.config.briefing_state(),
                         "evaluation_weighting": self.evaluation_weighting,
                         "selection_policy": asdict(self.selection_policy) if self.selection_policy else None,
                         "training_class_weighting": self.training_class_weighting,
                         "prior_hypotheses": self.hypotheses(),
                         "request_budget_bytes": self.max_request_bytes,
                         **self.sibling_briefing(),
                         "training_predictions": [event for event in self.history(1000)
                            if event["kind"] == "prediction" and event["target_id"] in {row.item.id for row in training}]},
                protected=tuple(row.item for row in development) + tuple(protected),
                human_explanations=self.optimizer_context["human_explanations"])
            if candidate_proposal is None:
                from .optimizer_agent import ask_optimizer
                proposal = await ask_optimizer(self.optimizer, 'propose', briefing)
            else:
                proposal = dict(candidate_proposal)
                self._emit({"kind": "hypothesis-retry", "hypothesis_id": _hash(proposal),
                            "training_count": len(training), "development_count": len(development)})
            config = self.active.config.apply(proposal, training)
            shared_owners = self.sibling_check.shared_owners(config) if self.sibling_check is not None else {}
            self._emit({"kind": "proposal-validated", "proposal": proposal,
                        "previous": self.active.config.briefing_state(), "candidate": config.briefing_state(),
                        **({"shared_supporting_questions": shared_owners} if shared_owners else {})})
            status=('provisional' if self.active.validation_status=='provisional' and
                    min(development_counts.values())<self.context_validation_floor else 'evaluated')
            from .candidate_fitting import fit_candidate
            candidate, diagnostics = await fit_candidate(self, config, training, development,
                protected=protected, propensities=propensities, validation_status=status, now=now)
            self.reconcile_model_context(training)
            incumbent_metrics = await self._score(self.active, development, training, now)
            candidate_metrics = await self._score(candidate, development, training, now)
            metric = "balanced_brier" if self.evaluation_weighting == "equal_class" else "brier"
            brier_improved = candidate_metrics[metric] < incumbent_metrics[metric] - 1e-12
            recall_safe = (candidate_metrics["balanced_accuracy"] >= incumbent_metrics["balanced_accuracy"] and
                all(candidate_metrics["per_class"][label]["recall"] >= incumbent_metrics["per_class"][label]["recall"]
                    for label in self.initial.task.labels)) if require_recall_safeguards else True
            selection = self.selection_policy.compare(incumbent_metrics, candidate_metrics) if self.selection_policy else None
            improved = selection['improved'] if selection else brier_improved and recall_safe
            # A context change is part of every sibling's shared request.
            siblings = await self.sibling_effects(candidate.config, now) if improved else None
            sibling_refused = bool(siblings and siblings['refused'])
            improved = improved and not sibling_refused
            promoted = improved and apply_promotion
            result = {"promoted": promoted, "incumbent": incumbent_metrics, "candidate": candidate_metrics,
                      "improved": improved, "trial_fingerprint": round_key,
                      "brier_improved": brier_improved, "recall_safeguards_passed": recall_safe,
                      "selection": selection,
                      "selection_policy": asdict(self.selection_policy) if self.selection_policy else None,
                      "promotion_metric": self.selection_policy.primary if selection else metric, "evaluation_weighting": self.evaluation_weighting,
                      "training_class_weighting": self.training_class_weighting,
                      "feature_diagnostics": diagnostics,
                      "evaluation_independent_of_optimizer_context": not self.optimizer_context["evaluation_context_exposed"],
                      "reason": siblings['reason'] if sibling_refused else
                                selection['reason'] if selection else (f"lower development {metric}" if improved else
                                 "candidate failed per-class recall safeguards" if not recall_safe else
                                 f"development {metric} did not improve")}
            if siblings is not None:
                result["sibling_effects"] = siblings
            if shared_owners:
                result["shared_supporting_questions"] = shared_owners
            self._emit({"kind": "candidate-evaluated", **result})
            baseline_version = self.active.fingerprint
            if promoted:
                self._activate(candidate)
            self._emit({"kind": "promoted" if promoted else "candidate-qualified" if improved else "candidate-rejected",
                        "version": self.active.fingerprint, **result})
            self.runtime_cache.complete_round(round_key,
                                _json({"result": result, "active": asdict(self.active),
                                        "fitted_candidate": asdict(candidate), "baseline_version": baseline_version,
                                        "candidate_model_context": self.model_context(candidate.config, training),
                                        "answer_dependencies": self.collected_answer_dependencies(),
                                        "training_evidence": self._evidence(training),
                                        "development_evidence": self._evidence(development)}))
            return result

        except Exception as error:
            from .observability import RequestBudgetExhausted
            if isinstance(error, RequestBudgetExhausted):
                self._emit({"kind": "round-paused", "reason": "request budget exhausted"})
                raise
            result = {"promoted": False, "reason": "round failed", "error_type": type(error).__name__}
            self._emit({"kind": "round-failed", **result})
            self.runtime_cache.complete_round(round_key,
                                _json({"result": result, "active": asdict(self.active),
                                        "answer_dependencies": self.collected_answer_dependencies()}))
            return result
        except BaseException as error:
            # Superseded or cancelled: close the round in the trace; the pass removes its pending row.
            from .observability import _stopped
            self._emit({"kind": "round-stopped", "status": _stopped(error), "promoted": False})
            raise

    def promote_trial(self, trial_fingerprint, training, development):
        row = self.runtime_cache.round(trial_fingerprint)
        if not row or row[0] != "complete":
            raise ValueError("trial is not complete")
        saved = json.loads(row[1])
        if saved.get('answer_dependencies') is not None and not self.answer_dependencies_current(saved['answer_dependencies']):
            raise ValueError('decision answers changed; refit and reevaluate the trial')
        selection = saved['result'].get('selection')
        previous_policy = saved['result'].get('selection_policy', selection.get('policy') if isinstance(selection, dict) else None)
        if previous_policy != (asdict(self.selection_policy) if self.selection_policy else None):
            raise ValueError('selection policy changed; reevaluate the trial')
        if not saved["result"].get("improved") or saved.get("baseline_version") != self.active.fingerprint:
            raise ValueError("trial did not improve this incumbent")
        if saved.get("training_evidence") != self._evidence(training) or saved.get("development_evidence") != self._evidence(development):
            raise ValueError("trial feedback changed; refit and reevaluate")
        candidate = _restore(saved["fitted_candidate"])
        measured_context = saved.get("candidate_model_context") or (
            candidate.head.provenance.source_model_provenance if candidate.head else None)
        if measured_context != self.model_context(candidate.config, training):
            raise ValueError('decision feature context changed; refit and reevaluate the trial')
        if not self.answer_dependencies_current(candidate.answer_dependencies):
            raise ValueError('decision answers changed; refit and reevaluate the trial')
        self._activate(candidate)
        self._emit({"kind": "promoted", **saved["result"], "promoted": True,
                    "version": self.active.fingerprint})

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
