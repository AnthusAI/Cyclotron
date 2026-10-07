"""Live agreement forgets old errors, independently of evaluation selection."""
from .rolling_metrics import recent_reviewed_metrics


def test_only_the_latest_two_hundred_labeled_items_contribute_to_each_reading():
    records=[(str(i),'yes','no',{'yes':0.,'no':1.}) for i in range(25)]
    records += [(str(i),'yes','yes',{'yes':1.,'no':0.}) for i in range(25,225)]
    metrics=recent_reviewed_metrics(('yes','no'),records)
    assert metrics['count']==200
    assert metrics['available_count']==225
    assert metrics['accuracy']==1.
    assert metrics['per_class']['yes']['recall']==1.
    assert metrics['per_class']['yes']['precision']==1.


def test_a_short_history_uses_all_available_labels_and_corrections_do_not_duplicate_items():
    metrics=recent_reviewed_metrics(('yes','no'),[
        ('a','no','yes',{'yes':1.,'no':0.}),
        ('b','no','no',{'yes':0.,'no':1.}),
        ('a','yes','yes',{'yes':1.,'no':0.})])
    assert metrics['count']==2
    assert metrics['accuracy']==1.
    assert metrics['window_size']==200


def test_each_classifier_has_its_own_label_window():
    a=recent_reviewed_metrics(('yes','no'),[(str(i),'yes','yes',{'yes':1.,'no':0.}) for i in range(201)])
    b=recent_reviewed_metrics(('yes','no'),[('one','yes','no',{'yes':0.,'no':1.})])
    assert a['count']==200 and a['accuracy']==1.
    assert b['count']==1 and b['accuracy']==0.
def test_label_only_predictions_count_toward_agreement_but_not_probability_metrics():
    result=recent_reviewed_metrics(('yes','no'),[('one','yes','yes',None),('two','no','yes',{'yes':.8,'no':.2})])
    assert result['count']==2
    assert result['accuracy']==.5
    assert result['per_class']['yes']['precision']==.5
    assert result['calibration']['count']==1
    assert result['calibration']['missing_probability_count']==1
    assert result['probability_count']==1
    assert result['brier']==__import__('pytest').approx(1.28)
