"""Application transport selection is explicit and legacy runs stay compatible."""
import pytest

from .optimizer_transport import optimizer_transport


@pytest.mark.parametrize("setting,expected", [(None,"openai"),("openai","openai"),("litellm","litellm")])
def test_transport_selection_uses_the_frozen_setting_without_constructing_other_providers(monkeypatch, setting, expected):
    from .openai_optimizer import OpenAIOptimizer
    from .litellm_optimizer import LiteLLMOptimizer
    calls=[]
    for name,cls in [("openai",OpenAIOptimizer),("litellm",LiteLLMOptimizer)]:
        monkeypatch.setattr(cls,"from_environment",lambda name=name,**kwargs:calls.append((name,kwargs)) or name)
    config={"optimizer_model":"fake", "max_optimizer_calls":3}
    if setting is not None:config["optimizer_transport"]=setting
    assert optimizer_transport(config)==expected
    assert calls==[(expected,{"model":"fake","max_calls":3})]


def test_unknown_transport_is_rejected_instead_of_silently_using_openai():
    with pytest.raises(ValueError,match="transport"):
        optimizer_transport({"optimizer_transport":"unknown"})
