"""Explicit, lazy LiteLLM transport for native decision-context optimization."""
from __future__ import annotations

from ..optimizer_agent import OptimizerReply


class LiteLLMOptimizer:
    def __init__(self, completion, *, model: str, max_calls: int = 10):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("optimizer model must be explicit")
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("optimizer max_calls must be positive")
        self.completion, self.model = completion, model
        self.max_calls, self.calls = max_calls, 0

    @classmethod
    def from_environment(cls, *, model: str, max_calls: int = 10):
        from dotenv import load_dotenv
        from litellm import completion
        load_dotenv(override=False)
        return cls(completion, model=model, max_calls=max_calls)

    def __call__(self, messages):
        if self.calls >= self.max_calls:
            raise RuntimeError("optimizer call ceiling reached")
        self.calls += 1
        # Fail explicitly for unsupported JSON mode; do not drop it or retry
        # a second paid call. Credentials stay in the SDK's environment.
        response = self.completion(model=self.model, messages=messages,
            response_format={"type": "json_object"}, num_retries=0,
            max_retries=0, timeout=60, drop_params=False)
        message = response.choices[0].message
        tools = tuple(call.model_dump() for call in (message.tool_calls or ()))
        usage = response.usage.model_dump() if response.usage else {}
        return OptimizerReply(message.content or "", response.model, usage, tools)
