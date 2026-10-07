import pytest
from .calibration_history import reviewed_calibration_metrics


def prediction(identifier, confidence=.8, **extra):
    return {'kind':'prediction','event_id':identifier,'target_id':str(identifier),'label':'yes',
            'probabilities':{'yes':confidence,'no':1-confidence},'version':f'v{identifier}',**extra}


def vote(identifier,label='yes',action='recorded'):
    return {'kind':'human-feedback','event_id':1000+identifier,'action':action,
            'feedback':{'item_id':str(identifier),'final_answer_value':label}}


def test_curves_identify_the_latest_two_hundred_pre_vote_predictions_and_model_mix():
    events=[event for i in range(201) for event in (prediction(i),vote(i))]
    metrics=reviewed_calibration_metrics(('yes','no'),events)
    assert metrics['count']==200
    assert metrics['calibration']['samples'][0]['item_id']=='1'
    assert metrics['calibration']['samples'][-1]['prediction_event_id']==200
    assert metrics['calibration']['source_counts']=={'decision-passthrough':200}


def test_raw_and_calibrated_comparisons_use_only_matching_fitted_head_predictions():
    events=[prediction(1,fitted_head=True,uncalibrated_probabilities={'yes':.99,'no':.01},calibration_temperature=1.5),vote(1),prediction(2),vote(2)]
    result=reviewed_calibration_metrics(('yes','no'),events)['calibration']
    assert result['count']==2
    assert result['matched_head_comparison']['raw']['count']==1
    assert result['matched_head_comparison']['calibrated']['ece']==pytest.approx(.2)
    assert result['samples'][0]['temperature']==1.5


def test_later_predictions_cannot_rewrite_old_curves_and_retractions_remove_samples():
    original=[prediction(1),vote(1)]
    first=reviewed_calibration_metrics(('yes','no'),original)
    future=reviewed_calibration_metrics(('yes','no'),[*original,prediction(1,.6)])
    assert first==future
    removed=reviewed_calibration_metrics(('yes','no'),[*original,vote(1,action='retracted')])
    assert removed['count']==0
    assert removed['calibration']['ece'] is None

def test_missing_probability_vectors_keep_human_agreement_without_fabricating_calibration():
    result=reviewed_calibration_metrics(('yes','no'),[prediction(1,probabilities=None),vote(1)])
    assert result['accuracy']==1
    assert result['count']==1
    assert result['calibration']['count']==0
    assert result['calibration']['missing_probability_count']==1
    assert result['calibration']['ece'] is None
    assert result['brier'] is None


def test_decision_model_and_final_classifier_compare_the_same_pre_vote_items():
    events=[prediction(1,decision_model_label='no',decision_model_probabilities={'yes':.2,'no':.8}),vote(1),
            prediction(2),vote(2)]
    comparison=reviewed_calibration_metrics(('yes','no'),events)['decision_model_comparison']
    assert comparison['count']==1
    assert comparison['missing_raw_count']==1
    assert comparison['raw']['accuracy']==0
    assert comparison['final']['accuracy']==1
    assert comparison['raw']['calibration']['ece']==pytest.approx(.8)
    assert comparison['final']['calibration']['ece']==pytest.approx(.2)
    assert comparison['item_ids']==['1']


def test_raw_final_calibration_uses_only_items_with_both_probability_vectors():
    events=[prediction(1,decision_model_label='no',decision_model_probabilities=None),vote(1)]
    comparison=reviewed_calibration_metrics(('yes','no'),events)['decision_model_comparison']
    assert comparison['raw']['count']==comparison['final']['count']==1
    assert comparison['raw']['calibration']['count']==comparison['final']['calibration']['count']==0
    assert comparison['missing_paired_probability_count']==1
