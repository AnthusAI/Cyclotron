"""Candidate selection reflects explicit user priorities, not hidden scoring."""
import pytest

from .selection_policy import SelectionPolicy


def scores(accuracy, precision, recall):
    return {'accuracy': accuracy, 'balanced_brier': 0.3,
            'per_class': {'include': {'precision': precision, 'recall': recall},
                          'exclude': {'precision': 0.8, 'recall': 0.8}}}


def test_recall_priority_cannot_buy_a_win_by_regressing_secondary_accuracy():
    policy = SelectionPolicy('recall', 'accuracy', positive_class='include')
    result = policy.compare(scores(.9, .8, .5), scores(.2, .2, 1.))
    assert not result['improved']
    assert result['reason'] == 'secondary objective regression exceeds allowance'


def test_secondary_accuracy_breaks_a_tie_in_primary_recall():
    policy = SelectionPolicy('recall', 'accuracy', positive_class='include')
    assert policy.compare(scores(.7, .7, .8), scores(.8, .8, .8))['improved']
    assert not policy.compare(scores(.8, .8, .8), scores(.8, .8, .8))['improved']


def test_f1_balances_precision_and_recall_instead_of_probability_error():
    policy = SelectionPolicy('f1', positive_class='include')
    result = policy.compare(scores(.9, 1., .2), scores(.8, .6, .6))
    assert result['improved']
    assert result['candidate_scores']['f1'] == pytest.approx(.6)


def test_macro_metrics_support_more_than_two_classes_without_a_positive_class():
    policy = SelectionPolicy('recall', aggregation='macro')
    assert policy.score(scores(.8, .7, .4), 'recall') == pytest.approx(.6)


def test_undefined_precision_is_zero_and_missing_class_coverage_is_rejected():
    policy = SelectionPolicy('f1', positive_class='include')
    assert policy.score(scores(.8, None, 0.), 'f1') == 0.
    with pytest.raises(ValueError):
        policy.score(scores(.8, .8, None), 'f1')


def test_guardrails_apply_even_to_provisional_recency_acceptance():
    policy = SelectionPolicy('recall', 'accuracy', positive_class='include', minimum_secondary=.8)
    assert not policy.compare(scores(.9, .8, .5), scores(.7, .6, .6), primary_allowance=.5)['eligible']


def test_cli_exposes_primary_secondary_positive_class_and_safety_bounds():
    import argparse
    from .selection_policy import add_selection_arguments, selection_from_arguments
    parser = argparse.ArgumentParser()
    add_selection_arguments(parser)
    args = parser.parse_args(['--selection-primary', 'recall', '--selection-secondary', 'accuracy',
        '--selection-positive-class', 'include', '--selection-minimum-secondary', '.8'])
    policy = selection_from_arguments(args)
    assert policy.primary == 'recall' and policy.secondary == 'accuracy'
    assert policy.minimum_secondary == .8


@pytest.mark.parametrize('kwargs', [{'primary': 'wat'}, {'primary': 'precision'},
    {'primary': 'accuracy', 'secondary': 'accuracy'}, {'primary': 'accuracy', 'max_secondary_regression': -1}])
def test_invalid_selection_configuration_fails_before_any_model_calls(kwargs):
    with pytest.raises(ValueError):
        SelectionPolicy(**kwargs)
