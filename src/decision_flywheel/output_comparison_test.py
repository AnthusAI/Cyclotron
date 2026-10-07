import pytest
from .output_comparison import compare_outputs


def test_raw_and_final_rates_share_targets_and_calibration_requires_both_vectors():
    records=[{'item_id':'a','actual_label':'yes','decision_model_label':'no','decision_model_probabilities':{'yes':.2,'no':.8},'label':'yes','probabilities':{'yes':.8,'no':.2}},
             {'item_id':'b','actual_label':'no','decision_model_label':'no','label':'yes','probabilities':{'yes':.8,'no':.2}},
             {'item_id':'legacy','actual_label':'no','label':'no','probabilities':{'yes':.2,'no':.8}}]
    result=compare_outputs(('yes','no'),records,scope='protected-matched')
    assert result['count']==2 and result['missing_raw_count']==1
    assert result['raw']['accuracy']==result['final']['accuracy']==.5
    assert result['raw']['calibration']['count']==result['final']['calibration']['count']==1
    assert result['missing_paired_probability_count']==1
    assert result['evaluation_scope']=='protected-matched'
    assert result['accuracy_delta']==0
    assert result['ece_delta']==pytest.approx(-.6)


def test_comparison_selects_latest_unique_items_before_excluding_missing_raw_outputs():
    records=[{'item_id':str(i),'actual_label':'yes','label':'yes','probabilities':None} for i in range(201)]
    records[0].update(decision_model_label='yes',decision_model_probabilities={'yes':1.,'no':0.})
    result=compare_outputs(('yes','no'),records)
    assert result['count']==0 and result['missing_raw_count']==200
    assert result['accuracy_delta'] is None
