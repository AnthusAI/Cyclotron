"""Live optimizer transport is injected and capped; specs use a fake SDK."""
from types import SimpleNamespace

import pytest

from .openai_optimizer import OpenAIOptimizer


def test_the_optimizer_transport_records_returned_usage_and_disables_hidden_retries():
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model="fake-model", usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 9}),
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"rubric":"Practical"}', tool_calls=None))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    transport = OpenAIOptimizer(client, model="fake", max_calls=1)
    reply = transport([{"role": "user", "content": "feedback"}])
    assert reply.usage == {"total_tokens": 9}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["store"] is False
    with pytest.raises(RuntimeError, match="ceiling"):
        transport([])
    assert len(calls) == 1


def test_invalid_optimizer_call_limits_are_rejected_before_client_use():
    with pytest.raises(ValueError):
        OpenAIOptimizer(None, model="fake", max_calls=0)


def _response(content='{"rubric":"Practical"}'):
    return SimpleNamespace(model="fake-model", usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 9}),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])


def test_an_async_client_is_awaited_under_the_same_ceiling_as_the_sync_client():
    import asyncio
    calls = []

    async def acreate(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0)
        return _response()
    sync = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or _response())))
    transport = OpenAIOptimizer(sync, model="fake", max_calls=2,
                                async_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=acreate))))
    assert asyncio.run(transport.acomplete([])).usage == {"total_tokens": 9}
    transport([])
    with pytest.raises(RuntimeError, match="ceiling"):
        asyncio.run(transport.acomplete([]))
    assert len(calls) == 2 and calls[0]["store"] is False


def test_without_an_async_client_the_transport_stays_synchronous():
    transport = OpenAIOptimizer(None, model="fake", max_calls=1)
    assert not hasattr(transport, "acomplete")
