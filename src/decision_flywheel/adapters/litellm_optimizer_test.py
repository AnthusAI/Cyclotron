"""Provider-neutral optimizer transport specs never contact a provider."""
from types import SimpleNamespace

import pytest

from .litellm_optimizer import LiteLLMOptimizer


def test_a_completion_preserves_messages_usage_and_tool_calls_without_hidden_retries():
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model="returned-model", usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 17}),
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"rubric":"Relevant"}',
                tool_calls=[SimpleNamespace(model_dump=lambda: {"id": "call-1", "function": {"name": "propose"}})]))])
    transport = LiteLLMOptimizer(complete, model="anthropic/fake", max_calls=1)
    messages = [{"role": "user", "content": "The full feedback context"}]
    reply = transport(messages)
    assert calls == [{"model": "anthropic/fake", "messages": messages, "response_format": {"type": "json_object"},
                      "num_retries": 0, "max_retries": 0, "timeout": 60, "drop_params": False}]
    assert reply.model == "returned-model"
    assert reply.usage == {"total_tokens": 17}
    assert reply.tool_calls[0]["function"]["name"] == "propose"
    with pytest.raises(RuntimeError, match="ceiling"):
        transport(messages)
    assert len(calls) == 1


def test_a_failed_provider_attempt_consumes_the_ceiling_instead_of_retrying_silently():
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("fake failure")
    transport = LiteLLMOptimizer(fail, model="ollama/fake", max_calls=1)
    with pytest.raises(RuntimeError, match="fake failure"):
        transport([])
    with pytest.raises(RuntimeError, match="ceiling"):
        transport([])
    assert len(calls) == 1


@pytest.mark.parametrize("model,limit", [("", 1), (" ", 1), (None, 1), ("fake", 0), ("fake", True)])
def test_invalid_configuration_is_rejected_before_any_provider_use(model, limit):
    with pytest.raises(ValueError):
        LiteLLMOptimizer(None, model=model, max_calls=limit)
