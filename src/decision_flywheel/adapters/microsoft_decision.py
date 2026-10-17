"""Microsoft-Decision-1 on Microsoft Foundry, through the System One wire shape Jev and Kev share."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from numbers import Real
import time
from urllib.parse import urlsplit
from typing import Any, Mapping, Protocol

from ..models import DecisionResult, ModelCapabilities

_PATH = "/providers/microsoft/v1/systemone"
_PLACEMENTS = ("criteria", "state")
_ENVIRONMENT = ("AZURE_FOUNDRY_API_KEY", "AZURE_FOUNDRY_ENDPOINT", "AZURE_FOUNDRY_DEPLOYMENT")


class MicrosoftDecisionTransport(Protocol):
    async def post(self, url: str, *, json: dict[str, Any], headers: Mapping[str, str], timeout: float) -> Any: ...


@dataclass(frozen=True)
class MicrosoftDecisionConfiguration:
    deployment: str
    endpoint: str
    revision: str | None = None
    timeout_seconds: float = 60.0
    rubric_placement: str = "criteria"  # 'criteria': in the decision question's criteria; 'state': inside the state
    rubric_label: str | None = None  # criteria placement: label whose criterion carries the rubric; default first

    def __post_init__(self) -> None:
        for name in ("deployment", "endpoint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        _ = self.origin  # validates the endpoint
        if self.rubric_placement not in _PLACEMENTS:
            raise ValueError("rubric_placement must be 'criteria' or 'state'")
        if self.rubric_label is not None and (not isinstance(self.rubric_label, str) or not self.rubric_label):
            raise ValueError("rubric_label must be a non-empty string")

    @property
    def model_identity(self) -> str:
        identity = f"microsoft-decision:{self.deployment}{'@' + self.revision if self.revision else ''}"
        return identity + ":endpoint-" + sha256(self.origin.encode('utf-8')).hexdigest()

    @property
    def origin(self) -> str:
        """Scheme plus host: a Foundry project URL and the resource root reach the same decision route."""
        parts = urlsplit(self.endpoint.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("endpoint must be an http(s) URL with a host")
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname}{port}"


class MicrosoftDecisionAdapter:
    """POST to ``<endpoint>/providers/microsoft/v1/systemone`` with an api-key header and no hidden retries."""
    name = "microsoft-decision"
    capabilities = ModelCapabilities(supports_labeled_context=True, supports_probability_distributions=True)

    def __init__(self, *, configuration: MicrosoftDecisionConfiguration, api_key: str,
                 transport: MicrosoftDecisionTransport | None = None):
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("api_key must be a non-empty string")
        self.configuration, self.transport, self._api_key = configuration, transport, api_key

    def __repr__(self) -> str:
        return f"MicrosoftDecisionAdapter({self.model_identity})"

    @property
    def model_identity(self) -> str:
        return self.configuration.model_identity

    @classmethod
    def from_environment(cls, *, environ: Mapping[str, str] | None = None, transport=None, deployment: str | None = None,
                         revision: str | None = None, timeout_seconds: float = 60.0,
                         rubric_placement: str = "criteria", rubric_label: str | None = None
                         ) -> "MicrosoftDecisionAdapter":
        environ = os.environ if environ is None else environ
        needed = _ENVIRONMENT[:2] if deployment else _ENVIRONMENT
        for name in needed:
            if not environ.get(name):
                raise ValueError(f"environment variable {name} is not set")
        configuration = MicrosoftDecisionConfiguration(
            deployment or environ["AZURE_FOUNDRY_DEPLOYMENT"], environ["AZURE_FOUNDRY_ENDPOINT"], revision,
            timeout_seconds, rubric_placement, rubric_label)
        return cls(configuration=configuration, api_key=environ["AZURE_FOUNDRY_API_KEY"], transport=transport)

    async def decide(self, task, target, context) -> DecisionResult:
        self.capabilities.validate_context(context)
        task.validate_target(target)
        for example in context:
            task.validate_target(example.item)
            task.validate_label(example.label)
        body = {
            "model": self.configuration.deployment,
            "state": {"labeled_examples": [{"text": e.item.values[task.input_field], "label": e.label,
                                            **dict(e.context)} for e in context],
                      "target": {"text": target.values[task.input_field]}},
            "questions": {task.name: {"type": "choice", "instructions": task.instructions,
                                      "criteria": {label: None for label in task.labels}}},
        }
        started = time.perf_counter()
        payload = await self._response(body)
        answer = _answers(payload).get(task.name)
        if not isinstance(answer, dict) or "choice" not in answer:
            raise ValueError(f"Microsoft Decision response is missing answer for {task.name!r}")
        return task.validate_result(DecisionResult(
            answer["choice"], answer.get("probabilities"), _model(payload), _numeric_usage(payload.get("usage")),
            round((time.perf_counter() - started) * 1000, 2), answer.get("confidence")))

    async def classify(self, config, target, training, *, now=None, event_sink=None):
        from ..classifier_config import ClassifiedAnswers
        result = await self.classify_many({"classifier": config}, target, {"classifier": training},
                                          now=now, event_sink=event_sink)
        return ClassifiedAnswers(result.answers["classifier"], result.model, result.usage, result.latency_ms)

    def _rubric_label(self, labels) -> str:
        label = self.configuration.rubric_label
        if label is None:
            return labels[0]
        if label not in labels:
            raise ValueError("rubric_label must be one of the task's labels")
        return label

    async def classify_many(self, configurations, target, training, *, now=None, event_sink=None,
                            max_request_bytes=32000, share_supporting_questions=False):
        """One request; each classifier keeps its own context and answer group."""
        from ..batched_classification import batch_request, BatchedAnswers, answer_groups, question_bindings
        request, identities = batch_request(configurations, target, training, now=now,
                                            max_request_bytes=max_request_bytes,
                                            share_questions=share_supporting_questions)
        in_criteria = self.configuration.rubric_placement == "criteria"
        state, questions = request["state"], {}
        if in_criteria:
            state = {"target": state["target"], "classifiers": {
                identifier: {k: v for k, v in value.items() if k != "rubric"}
                for identifier, value in state["classifiers"].items()}}
        for key, (identifier, local_name, _) in identities.items():
            detail = request["questions"][key]
            criteria = {label: None for label in detail["options"]}
            instructions = detail["instructions"]
            if in_criteria:
                scope = "state.classifiers[" + json.dumps(identifier) + "]"
                instructions = instructions.replace(f" Use {scope}.rubric as the decision criteria.", "")
                if local_name == "decision" and configurations[identifier].rubric:
                    criteria[self._rubric_label(detail["options"])] = configurations[identifier].rubric
            questions[key] = {"type": detail["type"], "instructions": instructions, "criteria": criteria}
        bindings = question_bindings(configurations, request, identities)
        observe = event_sink or (lambda _: None)
        observe({"kind": "decision-request", "target_id": target.id, "model": self.model_identity,
                 "state": state, "questions": questions, "question_bindings": bindings})
        started = time.perf_counter()
        payload = await self._response({"model": self.configuration.deployment, "state": state,
                                        "questions": questions})
        answers, model, usage = _answers(payload), _model(payload), _numeric_usage(payload.get("usage"))
        observe({"kind": "decision-response", "target_id": target.id, "model": model, "answers": answers,
                 "usage": usage, "question_bindings": bindings})

        def answer_for(key, task):
            answer = answers.get(key)
            if not isinstance(answer, dict) or "choice" not in answer:
                raise ValueError(f"Microsoft Decision response is missing answer for {key!r}")
            return task.validate_result(DecisionResult(
                answer["choice"], answer.get("probabilities"), model=model, confidence=answer.get("confidence")))
        groups = answer_groups(configurations, request, identities, answer_for)
        return BatchedAnswers(groups, model, usage, round((time.perf_counter() - started) * 1000, 2))

    async def _response(self, body: dict[str, Any]) -> dict:
        try:
            response = await self._post(body)
        except TimeoutError:
            raise ValueError("Microsoft Decision endpoint timed out") from None
        except ValueError:
            raise
        except Exception as error:  # transport failures; never echo the message, it may carry request details
            raise ValueError(f"Microsoft Decision request failed ({type(error).__name__})") from None
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or not 200 <= status < 300:
            raise ValueError(f"Microsoft Decision endpoint returned status {status}")
        try:
            payload = response.json()
        except Exception:
            raise ValueError("Microsoft Decision endpoint returned malformed JSON") from None
        if not isinstance(payload, dict):
            raise ValueError("Microsoft Decision endpoint returned malformed JSON")
        return payload

    async def _post(self, body: dict[str, Any]) -> Any:
        url = self.configuration.origin + _PATH
        headers = {"api-key": self._api_key}
        if self.transport is not None:
            return await self.transport.post(url, json=body, headers=headers,
                                             timeout=self.configuration.timeout_seconds)
        try:
            import httpx
        except ImportError as error:  # pragma: no cover - httpx is a core dependency of most installs
            raise ImportError("Install decision-flywheel[foundry] to call Microsoft Foundry.") from error
        try:
            async with httpx.AsyncClient() as client:
                return await client.post(url, json=body, headers=headers, timeout=self.configuration.timeout_seconds)
        except httpx.TimeoutException:
            raise TimeoutError from None


def _answers(payload: dict) -> dict:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("Microsoft Decision response is missing answers")
    return answers


def _model(payload: dict) -> str | None:
    return payload["model"] if isinstance(payload.get("model"), str) else None


def _numeric_usage(value: Any) -> dict[str, int | float] | None:
    if not isinstance(value, dict):
        return None
    result = {key: number for key, number in value.items()
              if isinstance(key, str) and isinstance(number, Real) and not isinstance(number, bool)}
    return result or None
