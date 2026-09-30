"""Optional local Laya adapter."""
from __future__ import annotations

import asyncio
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
        supports_labeled_context=False, supports_probability_distributions=True
    )

    # Upstream Agent accepts structured state, but its documentation does not
    # establish an in-context demonstration protocol.  Do not imply that a JSON
    # ``labeled_examples`` field is learned few-shot behavior.
    CONTEXT_EXCLUSION = "Laya upstream has no documented labeled-demonstration semantics; few-shot context is unsupported."

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
        if context:
            raise NotImplementedError(self.CONTEXT_EXCLUSION)
        text = target.values.get(task.input_field)
        if not isinstance(text, str):
            raise ValueError(f"target lacks string {task.input_field!r}")
        self._guard_against_source_truncation(text)
        question = {"type": "choice", "instructions": task.instructions,
                    "criteria": {label: label for label in task.labels}}
        started = time.perf_counter()
        response = await asyncio.to_thread(self.model.system_one, state=text, questions={task.name: question})
        answers = response.get("answers", response); answer = answers[task.name]
        label = answer.get("choice", answer.get("value")); task.validate_label(label)
        result = DecisionResult(label, answer.get("probabilities"), self.model_name,
                                response.get("usage"), response.get("latency_ms", round((time.perf_counter()-started)*1000, 2)),
                                answer.get("confidence"))
        return task.validate_result(result)

    def _guard_against_source_truncation(self, text: str) -> None:
        """Use Laya's own tokenizer/config when available before synchronous inference.

        Agent.system_one silently slices state beyond its state room.  The public
        direct SDK exposes ``tok`` and ``cfg``; injected lightweight fakes need
        not emulate those internals, but real upstream agents do.
        """
        tokenizer, cfg = getattr(self.model, "tok", None), getattr(self.model, "cfg", None)
        if tokenizer is None or not isinstance(cfg, dict):
            return
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
