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


def test_a_correction_scores_the_original_reviewed_prediction_not_a_later_rescore():
    original=prediction(1,.8,decision_model_label='yes',decision_model_probabilities={'yes':.8,'no':.2})
    later=prediction(2,.95,target_id='1',label='no',probabilities={'yes':.05,'no':.95},
        decision_model_label='no',decision_model_probabilities={'yes':.05,'no':.95})
    corrected=vote(1,'no');corrected['event_id']=1002
    result=reviewed_calibration_metrics(('yes','no'),[original,vote(1),later,corrected])
    assert result['count']==1
    assert result['accuracy']==0
    assert result['calibration']['ece']==pytest.approx(.8)
    assert result['calibration']['samples'][0]['prediction_event_id']==1
    assert result['calibration']['samples'][0]['feedback_event_id']==1002
    assert result['calibration']['version_counts']=={'v1':1}
    assert result['decision_model_comparison']['raw']['accuracy']==0
    assert result['decision_model_comparison']['final']['accuracy']==0


def test_retraction_then_a_fresh_review_can_bind_a_new_prediction_for_the_same_item():
    original=prediction(1,.8)
    later=prediction(2,target_id='1',label='no',probabilities={'yes':.05,'no':.95})
    result=reviewed_calibration_metrics(('yes','no'),
        [original,vote(1),vote(1,action='retracted'),later,vote(1,'no')])
    assert result['count']==1
    assert result['accuracy']==1
    assert result['calibration']['samples'][0]['prediction_event_id']==2


def test_an_explicit_reused_display_binds_the_saved_prediction_not_a_retrospective_score():
    original=prediction(1,.8)
    later=prediction(2,target_id='1',label='no',probabilities={'yes':.05,'no':.95})
    reuse={'kind':'displayed-prediction-reused','target_id':'1','prediction_event_id':1}
    result=reviewed_calibration_metrics(('yes','no'),
        [original,vote(1),later,vote(1,action='retracted'),reuse,vote(1,'no')])
    assert result['accuracy']==0
    assert result['calibration']['samples'][0]['prediction_event_id']==1


def test_reused_prediction_references_cannot_borrow_another_items_output():
    reuse={'kind':'displayed-prediction-reused','target_id':'other','prediction_event_id':1}
    with pytest.raises(ValueError,match='reference does not match target'):
        reviewed_calibration_metrics(('yes','no'),[prediction(1),reuse])

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


def test_each_sample_counts_its_training_items_and_leaves_the_ids_on_the_prediction():
    provenance={'method':'temperature','fit_on':'out_of_fold','training_ids':['a','b','c']}
    events=[prediction(1,fitted_head=True,calibration_provenance=provenance),vote(1)]
    sample=reviewed_calibration_metrics(('yes','no'),events)['calibration']['samples'][0]
    assert sample['calibration_provenance']=={'method':'temperature','fit_on':'out_of_fold','training_count':3}
    assert events[0]['calibration_provenance']['training_ids']==['a','b','c']
