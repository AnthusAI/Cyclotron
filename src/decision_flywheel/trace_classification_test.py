"""Specs for explicit ordered class roles and positive-class measurements."""
import pytest
from .trace_classification import class_configuration, positive_metrics
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
