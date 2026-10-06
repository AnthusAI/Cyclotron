"""Replay fixed historical feedback through the reusable flywheel, not another learner."""
from dataclasses import dataclass
import hashlib
import json
import math

from .classification_metrics import classification_metrics
from .context import _normalized_text


def recent_balanced(ordered, classes, *, per_class=5):
    """Newest rows per declared class, walking back without a recency cutoff."""
    if type(per_class) is not int or per_class < 1:
        raise ValueError("per-class selection limit must be positive")
    ordered, classes = tuple(ordered), tuple(classes)
    if not classes or len(set(classes)) != len(classes) or any(row.label not in classes for row in ordered):
        raise ValueError("selection requires the declared classes")
    counts = {label: sum(row.label == label for row in ordered) for label in classes}
    keep = min(per_class, min(counts.values()))
    if not keep:
        return ()
    used = {label: 0 for label in classes}
    selected = []
    for row in reversed(ordered):
        if used[row.label] < keep:
            selected.append(row)
            used[row.label] += 1
    return tuple(reversed(selected))


@dataclass(frozen=True)
class ReplayPlan:
    ordered: tuple
    training: tuple
    development: tuple
    scoreboard: tuple
    checkpoints: tuple[int, ...]
    seed: str

    def revealed(self, count):
        ids = {row.item.id for row in self.ordered[:count]}
        return (tuple(row for row in self.training if row.item.id in ids),
                tuple(row for row in self.development if row.item.id in ids))

    def evaluation(self, count, classes):
        available = {row.item.id for row in self.ordered[:count]}
        return tuple(row for row in self.scoreboard if row.item.id in available)

    def manifest(self, task):
        roles = {row.item.id: role for role, rows in (("training", self.training),
                 ("development", self.development), ("scoreboard", self.scoreboard)) for row in rows}
        return {"seed": self.seed, "checkpoints": self.checkpoints, "task": task.fingerprint,
                "evaluation_policy": "all revealed protected audit items; report natural and equal-class metrics",
                "development_policy": {"fraction_per_class":.2,"minimum_per_class":2,"weighting":"equal_class",
                                       "selection":"fixed stratified hash; no majority-class downsampling"},
                "records": [{"id": row.item.id, "label": row.label, "role": roles[row.item.id],
                             "text_hash": hashlib.sha256(_normalized_text(row.item, task).encode()).hexdigest(),
                             "comment_hash": hashlib.sha256(str(row.context.get("human_feedback", "")).encode()).hexdigest()}
                            for row in self.ordered]}

    @property
    def request_upper_bound(self):
        return len(self.scoreboard) * (1 + len(self.checkpoints)) + sum(
            len(train) + 2*len(dev) for train, dev in (self.revealed(n) for n in self.checkpoints))


def plan_replay(task, ordered, *, seed="arxiv-feedback-replay-v1", batch_size=20):
    if type(batch_size) is not int or batch_size < 1 or not isinstance(seed, str) or not seed:
        raise ValueError("replay seed and batch size must be explicit and valid")
    ordered = tuple(ordered)
    ids, texts = set(), set()
    for row in ordered:
        task.validate_target(row.item)
        task.validate_label(row.label)
        text = _normalized_text(row.item, task)
        if row.source != "trusted" or row.item.id in ids or text in texts:
            raise ValueError("replay requires unique trusted items and normalized text")
        ids.add(row.item.id)
        texts.add(text)
    roles = {}
    for label in task.labels:
        group = [row for row in ordered if row.label == label]
        if len(group) < 7:
            raise ValueError("replay needs seven eligible votes per class: at least three train, two dev, two scoreboard")
        group.sort(key=lambda row: hashlib.sha256(f"{seed}:{row.item.id}".encode()).hexdigest())
        audit_count = max(2, math.ceil(len(group)*.2))
        held = max(2, math.ceil(len(group)*.2))
        for i, row in enumerate(group):
            roles[row.item.id] = "scoreboard" if i < audit_count else "development" if i < audit_count+held else "training"
    partition = lambda role: tuple(row for row in ordered if roles[row.item.id] == role)
    checkpoints = tuple(range(batch_size, len(ordered), batch_size)) + (len(ordered),)
    return ReplayPlan(ordered, partition("training"), partition("development"), partition("scoreboard"), checkpoints, seed)


async def run_replay(wheel, plan, *, on_checkpoint=None):
    """Start fresh; delayed training/dev feedback, fixed scoreboard outside learning."""
    if wheel.active.head or wheel.active.config.rubric or wheel.active.config.tasks or wheel.active.config.example_ids:
        raise ValueError("a new replay must start without learned configuration or head")
    report = {"protocol": plan.manifest(wheel.initial.task), "checkpoints": []}
    fixed = plan.evaluation(len(plan.ordered), wheel.initial.task.labels)
    for count in (0, *plan.checkpoints):
        train, dev = plan.revealed(count)
        round_result = None
        if count:
            revealed = {row.item.id for row in (*train, *dev)}
            protected = tuple(row.item for row in plan.ordered if row.item.id not in revealed)
            round_result = await wheel.improve(train, dev, protected=protected,
                                               propensities={row.item.id: 1. for row in train})
        recent = recent_balanced(plan.evaluation(count, wheel.initial.task.labels),wheel.initial.task.labels)
        pool = {row.item.id: row for row in (*fixed, *recent)}
        predictions = {key: await wheel.predict(row.item, train) for key, row in pool.items()}
        def score(rows):
            return classification_metrics(wheel.initial.task.labels, [row.label for row in rows],
                                          [predictions[row.item.id].label for row in rows],
                                          [predictions[row.item.id].probabilities for row in rows])
        active = wheel.active
        checkpoint = {"revealed_labels": count, "training_labels": len(train), "development_labels": len(dev),
                      "version": active.fingerprint, "fitted_head": active.head is not None,
                      "rubric": active.config.rubric,
                      "tasks": [{"name": task.name, "instructions": task.instructions, "labels": task.labels}
                                for task in active.config.tasks], "example_ids": active.config.example_ids,
                      "round": round_result, "scoreboard": score(fixed),
                      "recent_balanced_scoreboard": score(recent),
                      "recent_evaluation_ids": [row.item.id for row in recent],
                      "requests": wheel.requests}
        report["checkpoints"].append(checkpoint)
        if on_checkpoint:
            on_checkpoint(report)
        if round_result and round_result.get("reason") == "round failed":
            break
    return report
