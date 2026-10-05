"""Versioned decision request structure; optimizer edits never contain weights."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Mapping, Sequence

from .context import _normalized_text
from .models import DecisionResult, DecisionTask, Item, LabeledItem


@dataclass(frozen=True)
class ClassifiedAnswers:
    """One paid response, with usage counted once rather than per feature."""
    answers: Mapping[str, DecisionResult]
    model: str | None
    usage: Mapping[str, object] | None
    latency_ms: float


@dataclass(frozen=True)
class ClassifierConfig:
    task: DecisionTask
    rubric: str = ""
    example_ids: tuple[str, ...] = ()
    tasks: tuple[DecisionTask, ...] = ()
    dynamic_elements: tuple[str, ...] = ()
    parent_fingerprint: str | None = None

    def __post_init__(self):
        if not isinstance(self.rubric, str):
            raise ValueError("rubric must be a string")
        if not isinstance(self.task, DecisionTask):
            raise ValueError("main task must be a DecisionTask")
        if any(not isinstance(task, DecisionTask) for task in self.tasks):
            raise ValueError("classification tasks must be DecisionTask instances")
        names = [task.name for task in self.tasks]
        if "decision" in names or len(set(names)) != len(names):
            raise ValueError("classification task names must be unique and not decision")
        if any(not isinstance(key, str) or not key for key in self.example_ids):
            raise ValueError("example IDs must be strings")
        if len(set(self.example_ids)) != len(self.example_ids):
            raise ValueError("example IDs must be unique")
        if any(key != "current_datetime" for key in self.dynamic_elements):
            raise ValueError("dynamic element is not allowlisted")
        if len(set(self.dynamic_elements)) != len(self.dynamic_elements):
            raise ValueError("dynamic elements must be unique")

    @property
    def fingerprint(self):
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def briefing_state(self) -> dict:
        return {"rubric": self.rubric, "example_ids": list(self.example_ids),
                "tasks": [asdict(task) for task in self.tasks],
                "dynamic_elements": list(self.dynamic_elements), "version": self.fingerprint}

    def apply(self, proposal: Mapping[str, object], training: Sequence[LabeledItem]) -> ClassifierConfig:
        allowed = {"rationale", "rubric", "example_ids", "tasks", "dynamic_elements"}
        if not isinstance(proposal, Mapping) or set(proposal) - allowed:
            raise ValueError("optimizer proposal contains forbidden fields")
        if "rationale" in proposal and not isinstance(proposal["rationale"], str):
            raise ValueError("rationale must be a string")
        examples = proposal.get("example_ids", list(self.example_ids))
        dynamic = proposal.get("dynamic_elements", list(self.dynamic_elements))
        if not isinstance(examples, list) or not isinstance(dynamic, list):
            raise ValueError("example_ids and dynamic_elements must be lists")
        pool = self._pool(training)
        if any(not isinstance(key, str) or key not in pool for key in examples):
            raise ValueError("examples must refer to eligible training items")
        tasks = self.tasks
        if "tasks" in proposal:
            raw_tasks = proposal["tasks"]
            if not isinstance(raw_tasks, list):
                raise ValueError("tasks must be a list")
            built = []
            for raw in raw_tasks:
                if not isinstance(raw, Mapping) or set(raw) != {"name", "instructions", "labels"}:
                    raise ValueError("task must contain only name, instructions and labels")
                built.append(DecisionTask(raw["name"], raw["labels"], raw["instructions"], self.task.input_field))
            tasks = tuple(built)
        return ClassifierConfig(self.task, proposal.get("rubric", self.rubric), tuple(examples), tasks,
                                tuple(dynamic), self.fingerprint)

    def _pool(self, training: Sequence[LabeledItem]) -> dict[str, LabeledItem]:
        pool = {}
        for row in training:
            if row.source != "trusted":
                raise ValueError("examples require trusted training labels")
            self.task.validate_target(row.item)
            self.task.validate_label(row.label)
            if row.item.id in pool:
                raise ValueError("training IDs must be unique")
            pool[row.item.id] = row
        return pool

    def request(self, target: Item, training: Sequence[LabeledItem], *, now: datetime | None = None) -> dict:
        self.task.validate_target(target)
        pool = self._pool(training)
        examples = []
        for key in self.example_ids:
            if key not in pool:
                raise ValueError("active example is missing from eligible training")
            row = pool[key]
            if key == target.id or _normalized_text(row.item, self.task) == _normalized_text(target, self.task):
                continue
            examples.append({**dict(row.item.values), "label": self.task.validate_label(row.label),
                             **dict(row.context)})
        state = {"target": dict(target.values), "rubric": self.rubric, "examples": examples}
        if "current_datetime" in self.dynamic_elements:
            if now is None or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("current_datetime requires an explicit timezone-aware instant")
            state["current_datetime"] = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        def question(task, main=False):
            suffix = " Use state.rubric as the human's decision criteria." if main else ""
            return {"type": "choice", "instructions": task.instructions + suffix +
                    " Classify only state.target; state.examples are labeled demonstrations.",
                    "options": list(task.labels)}
        return {"state": state, "questions": {"decision": question(self.task, True),
                                               **{task.name: question(task) for task in self.tasks}}}
