"""Explicit, lazy LiteLLM transport for native decision-context optimization."""

from __future__ import annotations

from ..optimizer_agent import OptimizerReply


class LiteLLMOptimizer:
    def __init__(self, completion, *, model: str, max_calls: int = 10, acompletion=None):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("optimizer model must be explicit")
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("optimizer max_calls must be positive")
        self.completion, self.model = completion, model
        self.max_calls, self.calls = max_calls, 0
        # With acompletion the agent awaits acomplete, so the event loop stays free during the call.
        self.acompletion = acompletion
        if acompletion is not None:
            self.acomplete = self._acomplete

    @classmethod
    def from_environment(cls, *, model: str, max_calls: int = 10, asynchronous: bool = False):
        from dotenv import load_dotenv
        from litellm import completion

        load_dotenv(override=False)
        acompletion = None
        if asynchronous:
            from litellm import acompletion
        return cls(completion, model=model, max_calls=max_calls, acompletion=acompletion)

    def _reserve(self):
        if self.calls >= self.max_calls:
            raise RuntimeError("optimizer call ceiling reached")
        self.calls += 1

    def _arguments(self, messages):
        # Fail explicitly for unsupported JSON mode; do not drop it or retry
        # a second paid call. Credentials stay in the SDK's environment.
        return dict(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            num_retries=0,
            max_retries=0,
            timeout=60,
            drop_params=False,
        )

    def __call__(self, messages):
        self._reserve()
        return self._reply(self.completion(**self._arguments(messages)))

    async def _acomplete(self, messages):
        """The same request through litellm.acompletion; one ceiling covers both forms."""
        self._reserve()
        return self._reply(await self.acompletion(**self._arguments(messages)))

    @staticmethod
    def _reply(response):
        message = response.choices[0].message
        tools = tuple(call.model_dump() for call in (message.tool_calls or ()))
        usage = response.usage.model_dump() if response.usage else {}
        return OptimizerReply(message.content or "", response.model, usage, tools)
