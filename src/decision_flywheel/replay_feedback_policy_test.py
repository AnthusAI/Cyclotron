from .replay_feedback_policy import (ReplayFeedbackPolicy, FeedbackPhase, PhasedFeedbackPolicy,
                                     onboarding_all_then_half, onboarding_publish_priority_taper)

def test_reject_half_never_uses_the_hidden_label_and_is_deterministic():
    policy=ReplayFeedbackPolicy('reject_half','demo')
    assert not policy.selects(item_id='a',predicted_label='publish',negative_label='reject')
    assert policy.selects(item_id='same',predicted_label='reject',negative_label='reject') == policy.selects(item_id='same',predicted_label='reject',negative_label='reject')

def test_casual_ten_percent_is_deterministic_and_all_selects_everything():
    casual=ReplayFeedbackPolicy('casual_ten_percent','demo')
    first=[casual.selects(item_id=str(i),predicted_label='publish',negative_label='reject') for i in range(100)]
    assert first == [casual.selects(item_id=str(i),predicted_label='reject',negative_label='reject') for i in range(100)]
    assert 0 < sum(first) < 25
    assert all(ReplayFeedbackPolicy().selects(item_id=str(i),predicted_label='reject',negative_label='reject') for i in range(5))


def test_phased_policy_honors_cycle_boundary_and_records_true_inclusion_propensity():
    policy=PhasedFeedbackPolicy((FeedbackPhase(50,{'include':1.,'exclude':.5}),
                                 FeedbackPhase(None,{'include':.5,'exclude':.25})),seed='schedule',name='demo')
    assert policy.select(item_id='a',predicted_label='include',negative_label='exclude',cycle_number=50).inclusion_propensity==1.
    before=policy.select(item_id='a',predicted_label='exclude',negative_label='exclude',cycle_number=50)
    after=policy.select(item_id='a',predicted_label='exclude',negative_label='exclude',cycle_number=51)
    assert before.inclusion_propensity==.5 and after.inclusion_propensity==.25
    assert before == policy.select(item_id='a',predicted_label='exclude',negative_label='include',cycle_number=50)
    manifest=policy.manifest('exclude')
    assert manifest['selection_basis'].startswith('issued predicted label')
    assert manifest['phases']==[
        {'start_cycle':1,'end_cycle':50,'predicted_label_rates':{'exclude':.5,'include':1.}},
        {'start_cycle':51,'end_cycle':None,'predicted_label_rates':{'exclude':.25,'include':.5}}]


def test_editorial_onboarding_presets_are_explicit_and_not_hidden_label_rules():
    first=onboarding_all_then_half(seed='a').manifest('reject')
    second=onboarding_publish_priority_taper(seed='b').manifest('reject')
    assert first['phases'][0]['predicted_label_rates']=={'publish':1.,'reject':1.}
    assert first['phases'][1]['predicted_label_rates']=={'publish':.5,'reject':.5}
    assert second['phases'][0]['predicted_label_rates']=={'publish':1.,'reject':.5}
    assert second['phases'][1]['predicted_label_rates']=={'publish':.5,'reject':.25}


def test_least_confident_reviews_items_at_or_below_the_recent_quantile():
    from .replay_feedback_policy import ConfidenceFeedbackPolicy
    policy = ConfidenceFeedbackPolicy('least_confident', rate=.25, seed='s')
    for n in range(100):
        policy.select(item_id=f'warm-{n}', predicted_label='publish', negative_label='reject', cycle_number=n + 1,
                      confidence=.5 + n / 200)
    low = policy.select(item_id='low', predicted_label='publish', negative_label='reject', confidence=.55)
    high = policy.select(item_id='high', predicted_label='publish', negative_label='reject', confidence=.95)
    assert (low.selected, low.propensity) == (True, 1.)
    assert (high.selected, high.propensity, high.inclusion_propensity) == (False, 0., 0.)


def test_mixed_audits_confident_items_at_a_known_rate_and_random_reviews_a_quarter():
    from .replay_feedback_policy import ConfidenceFeedbackPolicy
    mixed = ConfidenceFeedbackPolicy('mixed', rate=.25, audit_rate=.05, seed='s')
    rows = [mixed.select(item_id=f'i{n}', predicted_label='reject', negative_label='reject', confidence=(n * 37 % 100) / 100)
            for n in range(4000)]
    audited = [row for row in rows[100:] if row.selected and row.propensity < 1]
    assert {round(row.propensity, 4) for row in audited} == {round(.05 / .8, 4)}
    assert .2 < sum(row.selected for row in rows[100:]) / 3900 < .3
    random = ConfidenceFeedbackPolicy('random', rate=.25, seed='s')
    picks = [random.select(item_id=f'i{n}', predicted_label='reject', negative_label='reject') for n in range(4000)]
    assert .22 < sum(p.selected for p in picks) / 4000 < .28 and {p.propensity for p in picks if p.selected} == {.25}


def test_confidence_rules_need_the_issued_confidence():
    import pytest
    from .replay_feedback_policy import ConfidenceFeedbackPolicy
    with pytest.raises(ValueError, match='issued confidence'):
        ConfidenceFeedbackPolicy('least_confident').select(item_id='a', predicted_label='publish', negative_label='reject')


def _taper_run(policy, accuracy_for, cycles=800):
    import random
    draws = random.Random(7)
    for n in range(1, cycles + 1):
        confidence = draws.choice((.85, .9, .95, .99))
        selection = policy.select(item_id=f'i{n}', predicted_label='reject', negative_label='reject', cycle_number=n,
                                  confidence=confidence)
        if selection.selected:
            right = draws.random() < accuracy_for(n, confidence)
            policy.observe_review(cycle_number=n, label='reject' if right else 'publish', predicted_label='reject',
                                  confidence=confidence, propensity=selection.propensity)
    return policy.log


def test_the_taper_steps_down_after_two_passing_windows_and_back_up_after_a_failing_one():
    from .replay_feedback_policy import MetricTaperPolicy
    log = _taper_run(MetricTaperPolicy(), lambda n, c: c if n <= 500 else .5)
    streak = 0
    for entry in log:
        streak = streak + 1 if entry['passed'] else 0
        if entry['rate_after'] < entry['rate_before']:
            assert streak == 2
            streak = 0
        if not entry['passed'] and entry['rate_before'] < 1.:
            assert entry['rate_after'] > entry['rate_before']
    assert min(entry['rate_after'] for entry in log if entry['cycle'] <= 500) < 1.
    assert next(entry for entry in log if entry['cycle'] == 600)['rate_after'] == 1.


def test_the_taper_holds_full_review_while_the_cyclotron_is_overconfident():
    from .replay_feedback_policy import MetricTaperPolicy
    log = _taper_run(MetricTaperPolicy(), lambda n, c: .6)
    assert {entry['rate_after'] for entry in log} == {1.}
    assert all(not entry['passed'] for entry in log)
