"""Compose independent classifier contexts into one provider-neutral request."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .classifier_config import ClassifierConfig
from .models import DecisionResult, Item, LabeledItem


@dataclass(frozen=True)
class BatchedAnswers:
    answers: Mapping[str, Mapping[str, DecisionResult]]
    model: str | None
    usage: Mapping[str, object] | None
    latency_ms: float


class BatchedDecisionModel(Protocol):
    async def classify_many(
        self,
        configurations: Mapping[str, ClassifierConfig],
        target: Item,
        training: Mapping[str, tuple[LabeledItem, ...]],
        **kwargs,
    ) -> BatchedAnswers: ...


def _signature(task):
    return (task.name, task.instructions, tuple(task.labels))


def shared_questions(configurations):
    """Supporting questions identical (name, instructions, labels) in several classifiers, with their owners."""
    owners = {}
    for identifier, config in configurations.items():
        for task in config.tasks:
            owners.setdefault(_signature(task), []).append(identifier)
    return {signature: ids for signature, ids in owners.items() if len(ids) > 1}


def batch_request(configurations, target, training, *, now=None, share_questions=False, with_composition=False):
    """Compose one request, fitted to ``DECISION_REQUEST_BYTES`` (see ``request_composition``). With ``share_questions``, a supporting question several classifiers ask is asked once.

    Without it (the default, and every store created before sharing existed)
    each classifier's copy is asked separately in its own scope, exactly as
    before.

    The returned identities map each question key to its first owner as
    ``(classifier, local_name, task)``. Shared keys also appear in
    ``request['owners']`` with every ``[classifier, local_name]`` they answer;
    use :func:`answer_groups` to fan answers out to every owner.
    """
    if not configurations or set(configurations) != set(training):
        raise ValueError("every classifier needs its own explicit training pool")
    from .request_composition import altered, fit, interleave, keep_order

    questions, identities, owners, examples = {}, {}, {}, {}
    shared, asked = (shared_questions(configurations) if share_questions else {}), {}
    for identifier, config in configurations.items():
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("classifier identity required")
        examples[identifier] = config.eligible_examples(target, training[identifier])
        config.state(target.values, (), now=now)  # validate the instant before composing
        scope = "state.classifiers[" + json.dumps(identifier) + "]"
        for local_name, question in config.questions().items():
            task = config.task if local_name == "decision" else next(t for t in config.tasks if t.name == local_name)
            signature = _signature(task) if local_name != "decision" else None
            if signature in shared:
                if signature in asked:
                    owners[asked[signature]].append([identifier, local_name])
                    continue
                key = f"q{len(questions)}"
                asked[signature] = key
                owners[key] = [[identifier, local_name]]
                # One answer serves several classifiers, so it uses no single classifier's context.
                instructions = task.instructions + (
                    " Classify only state.target. This supporting question is shared by"
                    " classifiers " + json.dumps(shared[signature]) + "; answer it from state.target alone, not from"
                    " any classifier's rubric or examples."
                )
                questions[key] = {**question, "instructions": instructions}
                identities[key] = (identifier, local_name, task)
                continue
            key = f"q{len(questions)}"
            # Rewrite only the generated suffix; user-authored rubric and task text remain unchanged.
            instructions = task.instructions + f" Classify only state.target using only {scope} for this classifier."
            if local_name == "decision":
                instructions += f" Use {scope}.rubric as the decision criteria."
            instructions += f" {scope}.examples are labeled demonstrations, not targets."
            if config.dynamic_elements:
                instructions += f" Current datetime is in {scope}.current_datetime."
            questions[key] = {**question, "instructions": instructions}
            identities[key] = (identifier, local_name, task)

    def build(kept, values):
        state = {"target": dict(values), "classifiers": {}}
        for identifier, config in configurations.items():
            shown = [example for key, example, _, _ in examples[identifier] if (identifier, key) in kept]
            scoped = {k: v for k, v in config.state(values, shown, now=now).items() if k != "target"}
            state["classifiers"][identifier] = {**scoped, "configuration_fingerprint": config.fingerprint}
        request = {"state": state, "questions": questions}
        if owners:
            request["owners"] = owners
        return request

    priority = interleave(
        {
            identifier: keep_order(
                [(key, label, recency) for key, _, label, recency in rows], configurations[identifier].task.labels
            )
            for identifier, rows in examples.items()
        }
    )
    body_field = next(iter(configurations.values())).task.input_field
    available = sum(len(rows) for rows in examples.values())
    request, composition, kept = fit(build, priority, dict(target.values), body_field, available)
    composition["classifiers"] = {
        identifier: {
            "examples_available": len(rows),
            "examples_shown": sum((identifier, key) in kept for key, _, _, _ in rows),
        }
        for identifier, rows in examples.items()
    }
    if altered(composition):
        request = {**request, "composition": composition}
    return (request, identities, composition) if with_composition else (request, identities)


def question_bindings(configurations, request, identities):
    """Trace bindings for each asked question, naming every owner of a shared one."""
    bindings = {}
    for key, (identifier, local_name, _) in identities.items():
        binding = {
            "classifier_id": identifier,
            "question": local_name,
            "configuration_fingerprint": configurations[identifier].fingerprint,
        }
        if key in request.get("owners", {}):
            binding["owners"] = [{"classifier_id": owner, "question": name} for owner, name in request["owners"][key]]
        bindings[key] = binding
    return bindings


def answer_groups(configurations, request, identities, answer):
    """Per-classifier answer groups; ``answer(key, task)`` is called once per asked question."""
    groups = {identifier: {} for identifier in configurations}
    for key, (identifier, local_name, task) in identities.items():
        result = answer(key, task)
        for owner, name in request.get("owners", {}).get(key, [[identifier, local_name]]):
            groups[owner][name] = result
    return groups


def output_shares(request, identities):
    """Each classifier's estimated share of one joint request, by request bytes.

    A classifier's own context and questions count for it alone; the target
    is split evenly across classifiers and a shared supporting question across
    its owners. Shares sum to one. Providers bill the request as a whole, so
    this is an estimate for reporting, not a measured cost.
    """
    size = lambda value: len(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())
    classifiers = list(request["state"]["classifiers"])
    weights = {cid: float(size(request["state"]["classifiers"][cid])) for cid in classifiers}
    for cid in classifiers:
        weights[cid] += size(request["state"]["target"]) / len(classifiers)
    owners = request.get("owners", {})
    for key, (identifier, _, _) in identities.items():
        holders = [owner for owner, _ in owners[key]] if key in owners else [identifier]
        for owner in holders:
            weights[owner] += size(request["questions"][key]) / len(holders)
    total = sum(weights.values())
    return {cid: weight / total for cid, weight in weights.items()}
