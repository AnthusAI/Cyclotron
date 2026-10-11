"""Jev's TypeSafe System One adapter with an injectable synchronous client."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from dataclasses import dataclass
from hashlib import sha256
from numbers import Real
import time
from typing import Any, Callable, Sequence

from ..models import DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities
from ..classifier_config import ClassifiedAnswers, ClassifierConfig

_RUBRIC_HINT = " Use state.rubric as the human's decision criteria."


@dataclass(frozen=True)
class JevConfiguration:
    """Stable cache/checkpoint identity; retries belong to the outer runner."""

    model: str = "jev-latest"
    base_url: str | None = None
    timeout_seconds: float = 30.0
    max_retries: int = 0
    rubric_label: str | None = None  # label whose criterion carries the rubric; default: first label

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.rubric_label is not None and (not isinstance(self.rubric_label, str) or not self.rubric_label):
            raise ValueError("rubric_label must be a non-empty string")
        if self.max_retries != 0:
            raise ValueError("Jev adapter retries must be disabled; the outer runner counts attempts")

    @property
    def model_identity(self) -> str:
        identity = f"jev:{self.model}"
        if self.base_url is not None:
            endpoint = self.base_url.rstrip('/')
            identity += ":endpoint-" + sha256(endpoint.encode('utf-8')).hexdigest()
        return identity


class JevAdapter:
    """Use Jev's documented ``system_one(state, questions)`` contract.

    ``client`` is deliberately injected for tests and applications.  ``from_environment``
    is the only convenience factory and constructs the SDK lazily.
    """

    name = "jev"
    capabilities = ModelCapabilities(supports_labeled_context=True, supports_probability_distributions=True)

    def __init__(self, client: Any, *, configuration: JevConfiguration = JevConfiguration()):
        self.client = client
        self.configuration = configuration

    @property
    def model_identity(self) -> str:
        return self.configuration.model_identity

    @classmethod
    def from_environment(cls, *, configuration: JevConfiguration = JevConfiguration()) -> "JevAdapter":
        """Load a gitignored dotenv file, if installed, without exposing its values."""
        try:
            from dotenv import load_dotenv
        except ImportError as error:  # pragma: no cover - optional dependency
            raise ImportError("Install decision-flywheel[jev] to construct a Jev SDK client.") from error
        try:
            from typesafe_sdk import TypeSafeClient
            from typesafe_sdk._core.retry import RetryPolicy
        except ImportError as error:  # pragma: no cover - optional dependency
            raise ImportError("Install decision-flywheel[jev] to construct a Jev SDK client.") from error
        load_dotenv(override=False)
        client = TypeSafeClient(model=configuration.model, base_url=configuration.base_url,
                                timeout=configuration.timeout_seconds,
                                retry=RetryPolicy(max_retries=0))
        return cls(client, configuration=configuration)

    def _rubric_label(self, labels: Sequence[str]) -> str:
        """Configured label, else the task's first label (no positive class is visible here)."""
        label = self.configuration.rubric_label
        if label is None:
            return labels[0]
        if label not in labels:
            raise ValueError("rubric_label must be one of the task's labels")
        return label

    def _jev_questions(self, config: ClassifierConfig, questions: dict, names: dict) -> dict:
        """Build Jev questions; the rubric rides in the decision question's criteria."""
        built = {}
        for key, detail in questions.items():
            criteria = {label: None for label in detail["options"]}
            if names[key] == "decision" and config.rubric:
                criteria[self._rubric_label(detail["options"])] = config.rubric
            built[key] = {"type": detail["type"], "instructions": detail["instructions"], "criteria": criteria}
        return built

    async def decide(self, task: DecisionTask, target: Item,
                     context: Sequence[LabeledItem]) -> DecisionResult:
        self.capabilities.validate_context(context)
        task.validate_target(target)
        for example in context:
            task.validate_target(example.item)
            task.validate_label(example.label)
        state = {
            "labeled_examples": [
                {"text": example.item.values[task.input_field], "label": example.label, **dict(example.context)}
                for example in context
            ],
            "target": {"text": target.values[task.input_field]},
        }
        question = {"type": "choice", "instructions": task.instructions,
                    "criteria": {label: None for label in task.labels}}
        started = time.perf_counter()
        response = await asyncio.to_thread(
            self.client.system_one, state=state, questions={task.name: question}
        )
        answer = _mapping(_mapping(getattr(response, "answers", {})).get(task.name))
        result = DecisionResult(
            answer["choice"], answer.get("probabilities"), _string_or_none(getattr(response, "model", None)),
            _numeric_usage(getattr(response, "usage", None)), round((time.perf_counter() - started) * 1000, 2),
            answer.get("confidence"),
        )
        return task.validate_result(result)

    async def classify(self, config: ClassifierConfig, target: Item,
                       training: Sequence[LabeledItem], *, now: datetime | None = None,
                       event_sink: Callable[[dict], None] | None = None) -> ClassifiedAnswers:
        """Ask the main decision and every discovered element in one SDK request."""
        request = config.request(target, training, now=now)
        state = {key: value for key, value in request["state"].items() if key != "rubric"}
        raw_questions = {name: {**detail, "instructions": detail["instructions"].replace(_RUBRIC_HINT, "")}
                         for name, detail in request["questions"].items()}
        questions = self._jev_questions(config, raw_questions, {name: name for name in raw_questions})
        observe = event_sink or (lambda event: None)
        observe({"kind": "decision-request", "target_id": target.id, "model": self.model_identity,
                 "state": state, "questions": questions})
        started = time.perf_counter()
        response = await asyncio.to_thread(self.client.system_one, state=state, questions=questions)
        raw = _mapping(getattr(response, "answers", {}))
        observe({"kind": "decision-response", "target_id": target.id,
                 "answers": {name: _mapping(value) for name, value in raw.items()},
                 "model": _string_or_none(getattr(response, "model", None)),
                 "usage": _numeric_usage(getattr(response, "usage", None))})
        model = _string_or_none(getattr(response, "model", None))
        tasks = {"decision": config.task, **{task.name: task for task in config.tasks}}
        answers = {}
        for name, task in tasks.items():
            answer = _mapping(raw.get(name))
            answers[name] = task.validate_result(DecisionResult(
                answer["choice"], answer.get("probabilities"), model=model,
                confidence=answer.get("confidence")))
        return ClassifiedAnswers(answers, model, _numeric_usage(getattr(response, "usage", None)),
                                 round((time.perf_counter() - started) * 1000, 2))

    async def classify_many(self, configurations, target, training, *, now=None, event_sink=None,
                            max_request_bytes=32000):
        """One transport request; each classifier keeps its own context and answer group."""
        from ..batched_classification import batch_request, BatchedAnswers
        request, identities = batch_request(configurations,target,training,now=now,max_request_bytes=max_request_bytes)
        state = {'target':request['state']['target'],'classifiers':{
            identifier:{k:v for k,v in value.items() if k!='rubric'} for identifier,value in request['state']['classifiers'].items()}}
        questions = {}
        for key,(identifier,local_name,_) in identities.items():
            scope = 'state.classifiers['+json.dumps(identifier)+']'
            detail = {**request['questions'][key]}
            detail['instructions'] = detail['instructions'].replace(f' Use {scope}.rubric as the decision criteria.','')
            questions.update(self._jev_questions(configurations[identifier],{key:detail},{key:local_name}))
        observe = event_sink or (lambda event:None)
        bindings = {key:{'classifier_id':identifier,'question':local_name,'configuration_fingerprint':configurations[identifier].fingerprint}
                    for key,(identifier,local_name,_) in identities.items()}
        observe({'kind':'decision-request','target_id':target.id,'model':self.model_identity,
                 'state':state,'questions':questions,'question_bindings':bindings})
        started = time.perf_counter()
        response = await asyncio.to_thread(self.client.system_one,state=state,questions=questions)
        raw = _mapping(getattr(response,'answers',{}))
        model, usage = _string_or_none(getattr(response,'model',None)), _numeric_usage(getattr(response,'usage',None))
        observe({'kind':'decision-response','target_id':target.id,'answers':{key:_mapping(value) for key,value in raw.items()},
                 'model':model,'usage':usage,'question_bindings':bindings})
        groups = {identifier:{} for identifier in configurations}
        for key,(identifier,local_name,task) in identities.items():
            answer = _mapping(raw.get(key))
            groups[identifier][local_name] = task.validate_result(DecisionResult(answer['choice'],answer.get('probabilities'),model=model,confidence=answer.get('confidence')))
        return BatchedAnswers(groups,model,usage,round((time.perf_counter()-started)*1000,2))


def _mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        raise ValueError("Jev response is missing a structured answer")
    return value.get("root", value)


def _numeric_usage(value: Any) -> dict[str, int | float] | None:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        return None
    usage = {key: amount for key, amount in value.items()
             if isinstance(key, str) and isinstance(amount, Real) and not isinstance(amount, bool)}
    return usage or None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
