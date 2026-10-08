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
