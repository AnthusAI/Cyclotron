"""Adapter for Jev/Kev-style System One clients."""
from __future__ import annotations

import time
from typing import Any, Sequence

from ..models import DecisionResult, DecisionTask, Item, LabeledItem


def _as_dict(value: Any) -> dict:
    return value.model_dump() if hasattr(value, "model_dump") else dict(value)


class SystemOneAdapter:
    """A client-injected adapter for Jev or any compatible System One endpoint.

    The client must provide async ``system_one(state=..., questions=...)``.
    This module never constructs a client or reads a credential.
    """
    name = "system-one"

    def __init__(self, client: Any, *, name: str = "system-one"):
        self.client, self.name = client, name

    async def decide(self, task: DecisionTask, target: Item,
                     context: Sequence[LabeledItem]) -> DecisionResult:
        text = target.values.get(task.input_field)
        if not isinstance(text, str):
            raise ValueError(f"target lacks string {task.input_field!r}")
        state = {"labeled_examples": [{"text": item.item.values[task.input_field], "label": item.label}
                                      for item in context], "target": {"text": text}}
        question = {"type": "choice", "instructions": task.instructions,
                    "criteria": {label: None for label in task.labels}}
        started = time.perf_counter()
        response = await self.client.system_one(state=state, questions={task.name: question})
        answer = _as_dict(response.answers[task.name]); answer = answer.get("root", answer)
        task.validate_label(answer["choice"])
        usage = _as_dict(response.usage) if getattr(response, "usage", None) else None
        return DecisionResult(answer["choice"], answer.get("probabilities", {}),
                              getattr(response, "model", None), usage,
                              round((time.perf_counter() - started) * 1000, 2))
