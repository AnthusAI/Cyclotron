"""Specs for explicit ordered class roles and positive-class measurements."""
import pytest
from .trace_classification import class_configuration, positive_metrics, running_metric_series
from .classification_metrics import classification_metrics


def test_class_order_and_polarity_are_explicit_not_guessed_from_names():
    config = [{'label':'include','role':'positive'}, {'label':'exclude','role':'negative'}]
    assert class_configuration(['exclude','include'], config) == config
    assert class_configuration(['yes','no'], None)[0]['role'] == 'neutral'
    with pytest.raises(ValueError):
        class_configuration(['include','exclude'], config[:1])


def test_precision_and_recall_use_the_configured_positive_class():
    metrics = classification_metrics(['include','exclude'], ['include','include','exclude','exclude'],
        ['include','exclude','include','include'], [{'include':.5,'exclude':.5}]*4)
    config = [{'label':'include','role':'positive'}, {'label':'exclude','role':'negative'}]
    result = positive_metrics(metrics, config)
    assert result['precision'] == pytest.approx(1/3)
    assert result['recall'] == .5
    assert result['positive_labels'] == ['include']
    assert positive_metrics(metrics, list(reversed(config))) == result


def test_binary_recorded_counts_determine_precision_but_incomplete_counts_do_not():
    config = [{'label':'include','role':'positive'}, {'label':'exclude','role':'negative'}]
    result = positive_metrics({'per_class':{'include':{'count':2,'correct':0},'exclude':{'count':2,'correct':2}}}, config)
    assert result['precision'] is None  # no positive predictions
    assert result['recall'] == 0
    assert positive_metrics({'per_class':{'include':{'count':2,'correct':1}}}, config)['precision'] is None


def test_multiple_positive_classes_are_measured_as_positive_versus_rest():
    config=[{'label':'a','role':'positive'},{'label':'b','role':'positive'},{'label':'c','role':'negative'}]
    metrics=classification_metrics(['a','b','c'],['a','b','c'],['b','b','a'],[{'a':1/3,'b':1/3,'c':1/3}]*3)
    assert positive_metrics(metrics,config)['precision']==pytest.approx(2/3)
    assert positive_metrics(metrics,config)['recall']==1


def test_running_trends_use_only_recorded_prequential_measurements_and_keep_gaps():
    config=[{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]
    events=[{'kind':'cycle-metrics','cycle_number':1,'metric_scope':'pre-vote','metrics':{
        'accuracy':0,'count':1,'per_class':{'include':{'count':1,'correct':0},'exclude':{'count':0,'correct':0}}}},
        {'kind':'candidate-evaluated','candidate':{'accuracy':1}},
        {'kind':'cycle-metrics','cycle_number':2,'metric_scope':'pre-vote','metrics':{
        'accuracy':.5,'count':2,'per_class':{'include':{'count':1,'correct':0},'exclude':{'count':1,'correct':1}}}}]
    series=running_metric_series(events,config)
    assert [point['accuracy'] for point in series]==[0,.5]
    assert [point['precision'] for point in series]==[None,None]
    assert [point['event_index'] for point in series]==[0,2]
    assert all(point['recall']==0 for point in series)
