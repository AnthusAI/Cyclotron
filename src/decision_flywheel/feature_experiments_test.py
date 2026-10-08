"""Feature groups are explicit, bounded experiments, not a powerset search."""
import pytest
from .feature_experiments import plan_feature_groups


OLD = {'name': 'topic', 'instructions': 'Old topic?', 'labels': ['yes', 'no']}
OTHER = {'name': 'other', 'instructions': 'Other?', 'labels': ['yes', 'no']}
NEW = {**OLD, 'instructions': 'Specific topic?'}
PRACTICAL = {'name': 'practical', 'instructions': 'Practical?', 'labels': ['yes', 'no']}
BANK = ({'id': 'revision', 'question': NEW}, {'id': 'addition', 'question': PRACTICAL})


def test_a_group_freezes_other_questions_and_records_exact_revision_addition_and_ablations():
    trials = plan_feature_groups((OLD, OTHER), BANK, (('revision', 'addition'),), max_configurations=3)
    assert len(trials) == 3
    assert trials[0]['tasks'] == [NEW, OTHER, PRACTICAL]
    assert trials[0]['experiment']['kind'] == 'combination'
    assert trials[0]['experiment']['added'] == [PRACTICAL]
    assert trials[0]['experiment']['revised'] == [{'before': OLD, 'after': NEW}]
    assert {tuple(task['name'] for task in trial['tasks']) for trial in trials[1:]} == {
        ('other', 'practical'), ('topic', 'other')}
    assert all(trial['experiment']['kind'] == 'ablation' for trial in trials[1:])
    assert OLD['instructions'] == 'Old topic?' and BANK[0]['question'] == NEW


def test_group_order_does_not_change_the_experiment_identity_or_question_order():
    assert plan_feature_groups((OLD, OTHER), BANK, (('revision', 'addition'),)) == \
        plan_feature_groups((OLD, OTHER), BANK, (('addition', 'revision'),))


@pytest.mark.parametrize('groups,limit', [(('missing', 'addition'), 3),
    (('revision', 'revision'), 3), (('revision',), 3), (('revision', 'addition'), 2)])
def test_invalid_groups_or_insufficient_configuration_budgets_are_rejected_not_truncated(groups, limit):
    with pytest.raises(ValueError):
        plan_feature_groups((OLD,), BANK, (groups,), max_configurations=limit)


def test_two_wordings_of_the_same_concept_cannot_be_added_in_the_same_group():
    bank = (*BANK, {'id': 'another-revision', 'question': {**NEW, 'instructions': 'Another wording?'}})
    with pytest.raises(ValueError, match='same question name'):
        plan_feature_groups((OLD,), bank, (('revision', 'another-revision'),))


def test_an_empty_group_plan_does_not_create_trials_and_cannot_mutate_bank_definitions():
    assert plan_feature_groups((OLD,), BANK, ()) == []
    trials = plan_feature_groups((OLD,), BANK, (('revision', 'addition'),))
    trials[0]['tasks'][0]['instructions'] = 'Edited returned plan'
    assert BANK[0]['question']['instructions'] == 'Specific topic?'
