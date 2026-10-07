"""Compose independent classifier contexts into one provider-neutral request."""
from dataclasses import dataclass
import json
from typing import Mapping, Protocol
from .classifier_config import ClassifierConfig
from .models import DecisionResult, Item, LabeledItem


@dataclass(frozen=True)
class BatchedAnswers:
    answers: Mapping[str, Mapping[str, DecisionResult]]
    model: str | None
    usage: Mapping[str, object] | None
    latency_ms: float


class BatchedDecisionModel(Protocol):
    async def classify_many(self, configurations: Mapping[str, ClassifierConfig], target: Item,
                            training: Mapping[str, tuple[LabeledItem, ...]], **kwargs) -> BatchedAnswers: ...


def batch_request(configurations, target, training, *, now=None, max_request_bytes=32000):
    if not configurations or set(configurations)!=set(training):
        raise ValueError('every classifier needs its own explicit training pool')
    state = {'target':dict(target.values),'classifiers':{}}
    questions, identities = {}, {}
    for identifier,config in configurations.items():
        if not isinstance(identifier,str) or not identifier: raise ValueError('classifier identity required')
        request = config.request(target,training[identifier],now=now)
        scope = 'state.classifiers['+json.dumps(identifier)+']'
        state['classifiers'][identifier] = {key:value for key,value in request['state'].items() if key!='target'}
        state['classifiers'][identifier]['configuration_fingerprint'] = config.fingerprint
        for local_name,question in request['questions'].items():
            key = f'q{len(questions)}'
            # Rewrite only the generated suffix; user-authored rubric and task text remain unchanged.
            task = config.task if local_name=='decision' else next(t for t in config.tasks if t.name==local_name)
            instructions = task.instructions+f' Classify only state.target using only {scope} for this classifier.'
            if local_name=='decision': instructions+=f' Use {scope}.rubric as the decision criteria.'
            instructions+=f' {scope}.examples are labeled demonstrations, not targets.'
            if config.dynamic_elements: instructions+=f' Current datetime is in {scope}.current_datetime.'
            questions[key] = {**question,'instructions':instructions}
            identities[key] = (identifier,local_name,task)
    if len(json.dumps({'state':state,'questions':questions},ensure_ascii=False).encode())>max_request_bytes:
        raise ValueError('combined request exceeds context safety budget; partition classifiers explicitly')
    return {'state':state,'questions':questions}, identities
