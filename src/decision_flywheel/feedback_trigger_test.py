from .feedback_trigger import LabelTransitionTrigger


def vote(number, label):
    return {'kind': 'human-feedback', 'action': 'submitted', 'event_id': number,
            'feedback': {'id': str(number), 'final_answer_value': label}}


def test_every_second_change_fires_in_either_direction_and_repeats_do_not_count():
    events = []
    fired = []
    for number, label in enumerate(['false', 'false', 'false', 'true', 'false', 'true', 'true', 'false'], 1):
        events.append(vote(number, label))
        check = LabelTransitionTrigger().check(events)
        if check['due']:
            fired.append(number)
    assert fired == [5, 8]


def test_restoring_from_recorded_feedback_keeps_the_transition_count():
    events = [vote(1, 'a'), vote(2, 'b')]
    assert not LabelTransitionTrigger().check(events)['due']
    events.append(vote(3, 'c'))
    assert LabelTransitionTrigger().check(events)['due']
    assert LabelTransitionTrigger().check(events)['details']['transition_count'] == 2


def test_a_recorded_check_prevents_firing_again_on_the_same_vote():
    events = [vote(1, 'a'), vote(2, 'b'), vote(3, 'a')]
    check = LabelTransitionTrigger().check(events)
    events.append({'kind': 'trigger-evaluated', 'stage': 'rubric', **check})
    assert not LabelTransitionTrigger().check(events)['due']


def test_retractions_remove_votes_from_the_sequence_but_do_not_trigger_optimization():
    events = [vote(1, 'a'), vote(2, 'b'), vote(3, 'a')]
    events.append({'kind': 'human-feedback', 'action': 'retracted', 'feedback': {'id': '2'}})
    check = LabelTransitionTrigger().check(events)
    assert not check['due']
    assert check['details']['transition_count'] == 0


def test_protected_votes_neither_increment_transition_counts_nor_fire_learning_checks():
    events=[vote(1,'a'),{**vote(2,'b'),'assignment':'scoreboard'},vote(3,'a')]
    check=LabelTransitionTrigger(1).check(events)
    assert not check['due']
    assert check['details']['transition_count']==0
    events.append({**vote(4,'b'),'assignment':'final_audit'})
    assert not LabelTransitionTrigger(1).check(events)['due']
