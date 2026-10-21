"""Reviewed hosted provider configuration remains inert until worker dispatch."""

import pytest

from .hosted_jev import HostedJevFactory
from .jev import JevAdapter


def test_explicit_factory_resolves_provider_only_for_matching_bounded_baseline(monkeypatch):
    calls = []
    model = object()
    monkeypatch.setattr(JevAdapter, "from_environment", lambda **kw: calls.append(kw) or model)
    factory = HostedJevFactory.from_environment(
        {"CYCLOTRON_PREDICTION_MODEL": "synthetic-reviewed-model", "CYCLOTRON_PREDICTION_MAX_REQUESTS": "2"}
    )
    assert not calls
    config = {"decisions_provider": "jev", "decisions_model": factory.model, "max_requests": 2}
    for changed in (
        {"max_requests": 3},
        {"decisions_provider": "other"},
        {"decisions_model": "other"},
        {"priming_examples": [{"synthetic": True}]},
    ):
        with pytest.raises(ValueError):
            factory(config | changed)
    assert not calls
    actual, optimizer = factory(config)
    assert actual is model and not optimizer.enabled and len(calls) == 1


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"CYCLOTRON_PREDICTION_MODEL": "synthetic"},
        {"CYCLOTRON_PREDICTION_MODEL": "synthetic", "CYCLOTRON_PREDICTION_MAX_REQUESTS": "101"},
    ],
)
def test_incomplete_or_unbounded_activation_is_rejected(environment):
    with pytest.raises(ValueError):
        HostedJevFactory.from_environment(environment)
