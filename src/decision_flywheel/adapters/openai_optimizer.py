"""Explicit opt-in OpenAI transport for the otherwise provider-neutral optimizer."""

from __future__ import annotations

from ..optimizer_agent import OptimizerReply


class OpenAIOptimizer:
    def __init__(self, client, *, model: str, max_calls: int = 10, async_client=None):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("optimizer model must be explicit")
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("optimizer max_calls must be positive")
        self.client, self.model, self.max_calls, self.calls = client, model, max_calls, 0
        # With an async client the agent awaits acomplete, so the event loop stays free during the call.
        self.async_client = async_client
        if async_client is not None:
            self.acomplete = self._acomplete

    @classmethod
    def from_environment(cls, *, model: str, max_calls: int = 10, asynchronous: bool = False):
        from dotenv import load_dotenv
        from openai import OpenAI

        load_dotenv(override=False)
        # No SDK retry can silently consume another paid attempt.
        async_client = None
        if asynchronous:
            from openai import AsyncOpenAI

            async_client = AsyncOpenAI(max_retries=0, timeout=60)
        return cls(OpenAI(max_retries=0, timeout=60), model=model, max_calls=max_calls, async_client=async_client)

    def _reserve(self):
        if self.calls >= self.max_calls:
            raise RuntimeError("optimizer call ceiling reached")
        self.calls += 1

    def __call__(self, messages):
        self._reserve()
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, response_format={"type": "json_object"}, store=False
        )
        return self._reply(response)

    async def _acomplete(self, messages):
        """The same request through the async client; one ceiling covers both forms."""
        self._reserve()
        response = await self.async_client.chat.completions.create(
            model=self.model, messages=messages, response_format={"type": "json_object"}, store=False
        )
        return self._reply(response)

    @staticmethod
    def _reply(response):
        message = response.choices[0].message
        tools = tuple(call.model_dump() for call in (message.tool_calls or ()))
        usage = response.usage.model_dump() if response.usage else {}
        return OptimizerReply(message.content or "", response.model, usage, tools)
