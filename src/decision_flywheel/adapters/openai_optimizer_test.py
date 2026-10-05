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
