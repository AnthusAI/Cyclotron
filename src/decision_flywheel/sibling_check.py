"""Check every sibling output before one output's context change is promoted.

Outputs in one cyclotron share one decision-model request, so a change to one
output's rubric, examples or supporting questions is part of every sibling's
request too. Before such a change is activated, each sibling is scored on its
own development labels twice, with its current ML model held fixed: once under
the incumbent context and once with the candidate substituted. The effect on
each sibling is recorded, and under the ``refuse`` policy a candidate that
regresses any sibling beyond the declared tolerance is not promoted.

A supporting question several outputs ask is asked once and shared, so adding,
revising or dropping it changes every owner's features; the substituted
request carries that change, and the effect on each owner is measured.

A sibling's ML model is refit after any context change, and a refit can
recover part of a regression. The check measures the effect a promotion has
on the sibling's served decisions before that refit, so it is conservative.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Callable, Mapping

ACTIONS = ("refuse", "record")


@dataclass(frozen=True)
class SiblingPolicy:
    """What promotion does when a candidate would regress a sibling output.

    ``refuse`` keeps the incumbent when any sibling's development score drops
    by more than ``max_regression``; ``record`` promotes on the output's own
    evidence and only records the effect on each sibling.
    """
    action: str = "refuse"
    max_regression: float = 0.

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise ValueError("sibling policy action must be refuse or record")
        value = self.max_regression
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("sibling regression tolerance must be finite and nonnegative")


def _metric(wheel):
    """The sibling's own promotion metric, oriented so that higher is better."""
    if wheel.selection_policy is not None:
        policy = wheel.selection_policy
        return policy.primary, lambda metrics: policy.rank(metrics)[0]
    metric = "balanced_brier" if wheel.evaluation_weighting == "equal_class" else "brier"
    return metric, lambda metrics: -metrics[metric]


def sibling_effect(policy: SiblingPolicy, metric: str, incumbent: float, candidate: float) -> dict:
    """Classify one sibling's change in its own promotion metric (higher-is-better scores)."""
    change = candidate - incumbent
    regressed = change < -policy.max_regression - 1e-12
    return {"status": "regressed" if regressed else "no-regression", "metric": metric,
            "incumbent_score": incumbent, "candidate_score": candidate, "change": change}


class SiblingCheck:
    """Measure one output's candidate context against each sibling's development labels."""

    def __init__(self, wheels: Mapping, shared, owner: str, partitions: Callable, policy: SiblingPolicy):
        if not isinstance(policy, SiblingPolicy):
            raise ValueError("policy must be SiblingPolicy")
        self.wheels, self.shared, self.owner, self.partitions, self.policy = wheels, shared, owner, partitions, policy

    def siblings(self):
        return [identifier for identifier in self.wheels if identifier != self.owner]

    def sibling_questions(self) -> dict:
        """Each sibling's current supporting questions, in the optimizer's proposal shape."""
        return {identifier: [{"name": task.name, "instructions": task.instructions, "labels": list(task.labels)}
                             for task in self.wheels[identifier].active.config.tasks]
                for identifier in self.siblings() if self.wheels[identifier].active.config.tasks}

    def shared_owners(self, config) -> dict:
        """Supporting questions of ``config`` that siblings also ask, with every owner (asked once)."""
        from .batched_classification import shared_questions
        configs = {**{identifier: wheel.active.config for identifier, wheel in self.wheels.items()}, self.owner: config}
        shared = shared_questions(configs)
        return {task.name: shared[(task.name, task.instructions, tuple(task.labels))] for task in config.tasks
                if (task.name, task.instructions, tuple(task.labels)) in shared}

    def evidence(self) -> dict:
        """Round-cache identity: a sibling's new development labels must re-run the check."""
        return {"policy": asdict(self.policy),
                "siblings": {identifier: self.wheels[identifier]._evidence(self.partitions(identifier)[1])
                             for identifier in self.siblings()}}

    async def evaluate(self, config, now) -> dict:
        owner = self.wheels[self.owner]
        record = {"policy": asdict(self.policy), "owner": self.owner,
                  "shared_supporting_questions": self.shared_owners(config)}
        if config.fingerprint == owner.active.config.fingerprint:
            return {**record, "context_changed": False, "siblings": {}, "regressed": [], "refused": False,
                    "reason": "candidate leaves the shared decision request unchanged"}
        configs = {identifier: wheel.active.config for identifier, wheel in self.wheels.items()}
        pools = {identifier: self.partitions(identifier)[0] for identifier in self.wheels}
        siblings = {}
        for identifier in self.siblings():
            wheel = self.wheels[identifier]
            training, development, _ = self.partitions(identifier)
            labels = wheel.initial.task.labels
            counts = {label: sum(row.label == label for row in development) for label in labels}
            if min(counts.values()) < wheel.min_evaluation_per_class:
                siblings[identifier] = {"status": "insufficient-evidence", "development_counts": counts,
                                        "minimum_development_per_class": wheel.min_evaluation_per_class}
                continue
            metric, score = _metric(wheel)
            with self.shared.bound(configs, pools):
                incumbent = await wheel._score(wheel.active, development, training, now)
            with self.shared.bound({**configs, self.owner: config}, pools):
                candidate = await wheel._score(wheel.active, development, training, now)
            try:
                effect = sibling_effect(self.policy, metric, score(incumbent), score(candidate))
            except ValueError:
                siblings[identifier] = {"status": "insufficient-evidence", "development_counts": counts,
                                        "reason": "sibling metric needs class coverage in the evaluated sample"}
                continue
            siblings[identifier] = {**effect, "development_counts": counts,
                                    "sample_ids": candidate["sample_ids"],
                                    "incumbent_accuracy": incumbent["accuracy"],
                                    "candidate_accuracy": candidate["accuracy"],
                                    "sibling_version": wheel.active.fingerprint}
        regressed = [identifier for identifier, effect in siblings.items() if effect["status"] == "regressed"]
        refused = bool(regressed) and self.policy.action == "refuse"
        reason = ("candidate regresses sibling " + ", ".join(regressed) + " on its development labels" if regressed
                  else "no sibling regressed on its development labels")
        return {**record, "context_changed": True, "siblings": siblings, "regressed": regressed,
                "refused": refused, "reason": reason}


def install_sibling_checks(wheels: Mapping, shared, partitions: Callable, policy: SiblingPolicy | None = None):
    """Give every output of a multi-output cyclotron a sibling check; one output needs none."""
    policy = policy or SiblingPolicy()
    for identifier, wheel in wheels.items():
        wheel.sibling_check = SiblingCheck(wheels, shared, identifier, partitions, policy) if len(wheels) > 1 else None
