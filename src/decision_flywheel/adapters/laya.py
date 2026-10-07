"""Optional local Laya adapter."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Sequence

from ..models import DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities


@dataclass(frozen=True)
class LayaConfiguration:
    model_id: str = "convaiinnovations/laya"
    revision: str | None = None

    @property
    def model_identity(self) -> str:
        return f"laya:{self.model_id}{'@' + self.revision if self.revision else ''}"


class LayaAdapter:
    """Wrap a Laya-compatible local model exposing synchronous ``system_one``."""
    name = "laya"
    capabilities = ModelCapabilities(
        supports_labeled_context=True, supports_probability_distributions=True
    )

    def __init__(self, model: Any, *, configuration: LayaConfiguration = LayaConfiguration(),
                 model_name: str | None = None):
        self.model, self.configuration = model, configuration
        self.model_name = model_name or configuration.model_identity

    @property
    def model_identity(self) -> str:
        return self.configuration.model_identity

    @classmethod
    def from_default(cls, *, configuration: LayaConfiguration = LayaConfiguration()):
        try:
            import laya
        except ImportError as error:  # pragma: no cover - optional dependency
            raise ImportError("Install decision-flywheel[laya] to load the upstream Laya model.") from error
        return cls(laya.load(configuration.model_id, revision=configuration.revision), configuration=configuration,
                   model_name=f"{configuration.model_identity}:sdk-{getattr(laya, '__version__', 'unknown')}")

    async def decide(self, task: DecisionTask, target: Item,
                     context: Sequence[LabeledItem]) -> DecisionResult:
        task.validate_target(target)
        for example in context:
            task.validate_target(example.item)
            task.validate_label(example.label)
        text = target.values[task.input_field]
        state = text if not context else {
            "target": {"text": text}, "labeled_examples": [
                {"text": e.item.values[task.input_field], "label": e.label, **dict(e.context)} for e in context]}
        question = {"type": "choice", "instructions": task.instructions,
                    "criteria": {label: label for label in task.labels}}
        started = time.perf_counter()
        response = await self._response(state, {task.name: question})
        answers = response.get("answers", response); answer = answers[task.name]
        label = answer.get("choice", answer.get("value")); task.validate_label(label)
        result = DecisionResult(label, answer.get("probabilities"), self.model_name,
                                response.get("usage"), response.get("latency_ms", round((time.perf_counter()-started)*1000, 2)),
                                answer.get("confidence"))
        return task.validate_result(result)

    async def classify(self, config, target, training, *, now=None, event_sink=None):
        from ..classifier_config import ClassifiedAnswers
        result = await self.classify_many({"classifier": config}, target, {"classifier": training},
                                          now=now, event_sink=event_sink)
        return ClassifiedAnswers(result.answers["classifier"], result.model, result.usage, result.latency_ms)

    async def classify_many(self, configurations, target, training, *, now=None,
                            event_sink=None, max_request_bytes=32000):
        """Transport scoped context, without promising in-context effectiveness."""
        from ..batched_classification import batch_request, BatchedAnswers
        request, identities = batch_request(configurations, target, training, now=now,
                                             max_request_bytes=max_request_bytes)
        questions = {key: {"type": value["type"], "instructions": value["instructions"],
                           "criteria": {label: None for label in value["options"]}}
                     for key, value in request["questions"].items()}
        bindings = {key: {"classifier_id": identifier, "question": local,
                          "configuration_fingerprint": configurations[identifier].fingerprint}
                    for key, (identifier, local, _) in identities.items()}
        observe = event_sink or (lambda _: None)
        observe({"kind": "decision-request", "target_id": target.id, "model": self.model_identity,
                 "state": request["state"], "questions": questions, "question_bindings": bindings})
        started = time.perf_counter()
        def record_response(payload):
            observe({"kind": "decision-response", "target_id": target.id,
                     "model": payload.get("model", self.model_name), "answers": payload.get("answers"),
                     "usage": payload.get("usage"), "question_bindings": bindings})
        response = await self._response(request["state"], questions, on_response=record_response)
        answers = response.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("Laya response is missing answers")
        model = response.get("model", self.model_name)
        usage = response.get("usage")
        groups = {identifier: {} for identifier in configurations}
        for key, (identifier, local, task) in identities.items():
            answer = answers.get(key)
            if not isinstance(answer, dict) or not isinstance(answer.get("choice"), str):
                raise ValueError(f"Laya response is missing choice for {key!r}")
            groups[identifier][local] = task.validate_result(DecisionResult(
                answer["choice"], answer.get("probabilities"), model=model,
                confidence=answer.get("confidence")))
        return BatchedAnswers(groups, model, usage, round((time.perf_counter()-started)*1000, 2))

    async def _response(self, state, questions, *, on_response=None):
        self._guard_against_source_truncation(state, questions)
        response = await asyncio.to_thread(self.model.system_one, state=state, questions=questions)
        if not isinstance(response, dict):
            raise ValueError("Laya response must be a mapping")
        if on_response is not None:
            on_response(response)
        usage = response.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError("Laya usage must be a mapping")
        if usage.get("truncated") or usage.get("state_tokens_dropped") or usage.get("truncated_questions"):
            raise ValueError("Laya reported truncated decision context; result is not usable")
        return response

    def _guard_against_source_truncation(self, state, questions) -> None:
        """Use Laya's own tokenizer/config when available before synchronous inference.

        Agent.system_one silently slices state beyond its state room.  The public
        direct SDK exposes ``tok`` and ``cfg``; injected lightweight fakes need
        not emulate those internals, but real upstream agents do.
        """
        tokenizer, cfg = getattr(self.model, "tok", None), getattr(self.model, "cfg", None)
        if tokenizer is None or not isinstance(cfg, dict):
            return
        text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        encoded = tokenizer(text, add_special_tokens=False)
        ids = encoded.get("input_ids") if isinstance(encoded, dict) else None
        if not isinstance(ids, list):
            raise ValueError("Laya tokenizer did not return input_ids for truncation guard")
        max_len = cfg.get("max_len", 512)
        head_max_len = cfg.get("head_max_len", 192)
        if not isinstance(max_len, int) or not isinstance(head_max_len, int):
            raise ValueError("Laya model config lacks integer token limits")
        # Upstream's predict_long uses this same maximum one-window state room.
        state_room = max_len - head_max_len - 8
        if len(ids) > state_room:
            raise ValueError(f"Laya request would truncate state: {len(ids)} tokens exceeds {state_room}")
        # Upstream build_head cuts instructions to the option budget and caps
        # each option at 48 tokens. Require the full question to fit instead.
        for name, question in questions.items():
            head = tokenizer(f"choice question: {question['instructions']}", add_special_tokens=False)["input_ids"]
            option_lengths = []
            for label, description in question["criteria"].items():
                option = label if description in (None, "") else f"{label}: {description}"
                tokens = tokenizer(" " + option, add_special_tokens=False)["input_ids"]
                if len(tokens) > 48:
                    raise ValueError(f"Laya request would truncate question {name!r} option")
                option_lengths.append(len(tokens) + 1)  # each option's mask marker
            if len(head) + sum(option_lengths) > head_max_len or head_max_len - sum(option_lengths) < 16:
                raise ValueError(f"Laya request would truncate question {name!r}; use a larger head budget")
