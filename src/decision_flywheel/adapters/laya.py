"""Optional local Laya adapter."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Sequence

from ..models import DecisionResult, DecisionTask, Item, LabeledItem


class LayaAdapter:
    """Wrap a Laya-compatible local model exposing synchronous ``system_one``."""
    name = "laya"

    def __init__(self, model: Any, *, model_name: str = "laya"):
        self.model, self.model_name = model, model_name

    @classmethod
    def from_default(cls):
        try:
            import laya
        except ImportError as error:  # pragma: no cover - optional dependency
            raise ImportError("Install decision-flywheel[laya] to load the upstream Laya model.") from error
        return cls(laya.load(), model_name=f"laya:{getattr(laya, '__version__', 'unknown')}")

    async def decide(self, task: DecisionTask, target: Item,
                     context: Sequence[LabeledItem]) -> DecisionResult:
        if context:
            raise NotImplementedError("Laya context serialization is pending upstream API confirmation.")
        text = target.values.get(task.input_field)
        if not isinstance(text, str):
            raise ValueError(f"target lacks string {task.input_field!r}")
        question = {"type": "choice", "instructions": task.instructions,
                    "criteria": {label: label for label in task.labels}}
        started = time.perf_counter()
        response = await asyncio.to_thread(self.model.system_one, state=text, questions={task.name: question})
        answers = response.get("answers", response); answer = answers[task.name]
        label = answer.get("choice", answer.get("value")); task.validate_label(label)
        return DecisionResult(label, answer.get("probabilities", {}), self.model_name,
                              response.get("usage"), response.get("latency_ms", round((time.perf_counter()-started)*1000, 2)))
