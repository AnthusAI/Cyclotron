"""HTTP adapter for Kev's documented TypeSafe System One endpoint."""
from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
import time
from typing import Any, Protocol, Sequence

from ..models import DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities


class KevTransport(Protocol):
    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> Any: ...


@dataclass(frozen=True)
class KevConfiguration:
    model: str = "kev-latest"
    revision: str | None = None
    base_url: str = "http://127.0.0.1:8009"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def model_identity(self) -> str:
        return f"kev:{self.model}{'@' + self.revision if self.revision else ''}"


class KevAdapter:
    """POST to Kev's ``/v1/systemone`` wire contract without hidden retries."""
    name = "kev"
    capabilities = ModelCapabilities(supports_labeled_context=True, supports_probability_distributions=True)

    def __init__(self, *, transport: KevTransport | None = None,
                 configuration: KevConfiguration = KevConfiguration()):
        self.transport, self.configuration = transport, configuration

    @property
    def model_identity(self) -> str:
        return self.configuration.model_identity

    async def decide(self, task: DecisionTask, target: Item,
                     context: Sequence[LabeledItem]) -> DecisionResult:
        self.capabilities.validate_context(context)
        task.validate_target(target)
        for example in context:
            task.validate_target(example.item)
            task.validate_label(example.label)
        body = {
            "state": {"labeled_examples": [{"text": item.item.values[task.input_field], "label": item.label}
                                               for item in context],
                      "target": {"text": target.values[task.input_field]}},
            "model": self.configuration.model,
            "questions": {task.name: {"type": "choice", "instructions": task.instructions,
                                        "criteria": {label: None for label in task.labels}}},
        }
        payload = await self._response(body)
        answers = payload.get("answers")
        if not isinstance(answers, dict) or task.name not in answers:
            raise ValueError(f"Kev endpoint response is missing answer for {task.name!r}")
        answer = answers[task.name]
        if not isinstance(answer, dict) or "choice" not in answer:
            raise ValueError(f"Kev endpoint response is missing answer choice for {task.name!r}")
        result = DecisionResult(answer["choice"], answer.get("probabilities"),
                                payload.get("model") if isinstance(payload.get("model"), str) else None,
                                _numeric_usage(payload.get("usage")), _latency(payload.get("latency_ms")),
                                answer.get("confidence"))
        return task.validate_result(result)

    async def classify(self, config, target, training, *, now=None, event_sink=None):
        """Use the same full-context transport for a single learned classifier."""
        from ..classifier_config import ClassifiedAnswers
        result=await self.classify_many({'classifier':config},target,{'classifier':training},
                                        now=now,event_sink=event_sink)
        return ClassifiedAnswers(result.answers['classifier'],result.model,result.usage,result.latency_ms)

    async def classify_many(self, configurations, target, training, *, now=None,
                            event_sink=None, max_request_bytes=32000):
        """Send the scorecard once and preserve every feature's distribution."""
        from ..batched_classification import batch_request, BatchedAnswers
        request,identities=batch_request(configurations,target,training,now=now,
                                         max_request_bytes=max_request_bytes)
        questions={key:{'type':detail['type'],'instructions':detail['instructions'],
                        'criteria':{label:None for label in detail['options']}}
                   for key,detail in request['questions'].items()}
        bindings={key:{'classifier_id':identifier,'question':local,
                       'configuration_fingerprint':configurations[identifier].fingerprint}
                  for key,(identifier,local,_) in identities.items()}
        body={'state':request['state'],'questions':questions,'model':self.configuration.model}
        observe=event_sink or (lambda _:None)
        observe({'kind':'decision-request','target_id':target.id,'model':self.model_identity,
                 'state':body['state'],'questions':questions,'question_bindings':bindings})
        started=time.perf_counter()
        payload=await self._response(body)
        answers=payload.get('answers')
        if not isinstance(answers,dict):raise ValueError('Kev endpoint response is missing answers')
        model=payload.get('model') if isinstance(payload.get('model'),str) else None
        usage=_numeric_usage(payload.get('usage'))
        observe({'kind':'decision-response','target_id':target.id,'model':model,
                 'answers':answers,'usage':usage,'question_bindings':bindings})
        groups={identifier:{} for identifier in configurations}
        for key,(identifier,local,task) in identities.items():
            answer=answers.get(key)
            if not isinstance(answer,dict) or 'choice' not in answer:
                raise ValueError(f'Kev endpoint response is missing answer for {key!r}')
            groups[identifier][local]=task.validate_result(DecisionResult(
                answer['choice'],answer.get('probabilities'),model=model,confidence=answer.get('confidence')))
        return BatchedAnswers(groups,model,usage,round((time.perf_counter()-started)*1000,2))

    async def _response(self, body):
        try:
            response=await self._post(body)
        except TimeoutError as error:
            raise ValueError('Kev endpoint timed out') from error
        status=getattr(response,'status_code',None)
        if not isinstance(status,int) or not 200<=status<300:
            raise ValueError(f'Kev endpoint returned status {status}')
        try:
            payload=response.json()
        except Exception as error:
            raise ValueError('Kev endpoint returned malformed JSON') from error
        if not isinstance(payload,dict):raise ValueError('Kev endpoint returned malformed JSON')
        return payload

    async def _post(self, body: dict[str, Any]) -> Any:
        endpoint = f"{self.configuration.base_url.rstrip('/')}/v1/systemone"
        if self.transport is not None:
            return await self.transport.post(endpoint, json=body, timeout=self.configuration.timeout_seconds)
        try:
            import httpx
        except ImportError as error:  # pragma: no cover - optional dependency
            raise ImportError("Install decision-flywheel[kev] to call a Kev server.") from error
        async with httpx.AsyncClient() as client:
            return await client.post(endpoint, json=body, timeout=self.configuration.timeout_seconds)


def _numeric_usage(value: Any) -> dict[str, int | float] | None:
    if not isinstance(value, dict):
        return None
    result = {key: number for key, number in value.items()
              if isinstance(key, str) and isinstance(number, Real) and not isinstance(number, bool)}
    return result or None


def _latency(value: Any) -> float | None:
    return float(value) if isinstance(value, Real) and not isinstance(value, bool) else None
