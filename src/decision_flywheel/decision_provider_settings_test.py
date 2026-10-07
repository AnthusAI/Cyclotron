import pytest
from .decision_provider_settings import normalize_decision_settings


def test_custom_injected_providers_require_an_explicit_model_and_are_not_rewritten():
    values={'decisions_provider':'custom','decisions_model':'local-model'}
    assert normalize_decision_settings(values)==values
    with pytest.raises(ValueError,match='explicit model'):
        normalize_decision_settings({'decisions_provider':'custom'})


@pytest.mark.parametrize('values',[{'decisions_provider':''},{'decisions_provider':None},{'decisions_model':False}])
def test_invalid_provider_or_model_identifiers_reject_without_constructing_a_client(values):
    with pytest.raises(ValueError):normalize_decision_settings(values)


def test_default_models_follow_the_provider_without_mutating_original_settings():
    values={'decisions_provider':'kev'}
    assert normalize_decision_settings(values)['decisions_model']=='kev-latest'
    assert values=={'decisions_provider':'kev'}
    assert normalize_decision_settings({})['decisions_model']=='jev-1.13.0'
