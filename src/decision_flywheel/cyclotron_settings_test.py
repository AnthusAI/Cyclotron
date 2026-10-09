import pytest
from .cyclotron_settings import validate_shared_settings


def test_shared_settings_preserve_extension_values_and_validate_role_independent_objectives():
    value={'extension':{'value':3},'selection_policy':{'primary':'recall','secondary':'accuracy'},'optimize_every':10}
    assert validate_shared_settings(value)==value


@pytest.mark.parametrize('settings',[{'optimize_every':0},{'rubric_changes_every':True},
    {'seed':''},{'optimizer_model':' '},{'selection_policy':{'primary':'recall','secondary':'recall'}}])
def test_invalid_shared_settings_are_rejected_before_a_definition_is_saved(settings):
    with pytest.raises(ValueError):validate_shared_settings(settings)


def test_optimizer_transport_is_an_explicit_validated_shared_setting():
    assert validate_shared_settings({'optimizer_transport':'litellm'})=={'optimizer_transport':'litellm'}
    with pytest.raises(ValueError,match='transport'):
        validate_shared_settings({'optimizer_transport':'unknown'})
