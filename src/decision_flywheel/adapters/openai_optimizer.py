"""Explicit opt-in OpenAI transport for the otherwise provider-neutral optimizer."""
from __future__ import annotations

from ..optimizer_agent import OptimizerReply


class OpenAIOptimizer:
    def __init__(self, client, *, model: str, max_calls: int = 10):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("optimizer model must be explicit")
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("optimizer max_calls must be positive")
        self.client, self.model, self.max_calls, self.calls = client, model, max_calls, 0

    @classmethod
    def from_environment(cls, *, model: str, max_calls: int = 10):
        from dotenv import load_dotenv
        from openai import OpenAI
        load_dotenv(override=False)
        # No SDK retry can silently consume another paid attempt.
        return cls(OpenAI(max_retries=0, timeout=60), model=model, max_calls=max_calls)

    def __call__(self, messages):
        if self.calls >= self.max_calls:
            raise RuntimeError("optimizer call ceiling reached")
        self.calls += 1
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, response_format={"type": "json_object"}, store=False)
        message = response.choices[0].message
        tools = tuple(call.model_dump() for call in (message.tool_calls or ()))
        usage = response.usage.model_dump() if response.usage else {}
        return OptimizerReply(message.content or "", response.model, usage, tools)
