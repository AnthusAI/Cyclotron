import pytest
from .calibration_metrics import reliability_curve
from .rolling_metrics import recent_reviewed_metrics


def test_a_perfectly_calibrated_bin_has_zero_calibration_error():
    curve = reliability_curve(('yes', 'no'), ['yes'] * 8 + ['no'] * 2,
                              ['yes'] * 10, [{'yes': .8, 'no': .2}] * 10)
    assert curve['ece'] == pytest.approx(0)
    assert curve['bins'][8]['accuracy'] == .8
    assert curve['bins'][8]['count'] == 10


def test_certainty_belongs_to_the_last_bin_and_empty_bins_are_not_zero_accuracy():
    curve = reliability_curve(('yes', 'no'), ['no'], ['yes'], [{'yes': 1., 'no': 0.}])
    assert curve['bins'][9]['count'] == 1
    assert curve['ece'] == 1
    assert curve['bins'][0]['accuracy'] is None


def test_calibration_uses_only_the_same_recent_unique_labels_as_other_metrics():
    rows = [(str(i), 'yes', 'yes', {'yes': .8, 'no': .2}) for i in range(201)]
    metrics = recent_reviewed_metrics(('yes', 'no'), rows)
    assert metrics['calibration']['count'] == metrics['count'] == 200
    assert metrics['calibration']['ece'] == pytest.approx(.2)


def test_malformed_probabilities_are_rejected_and_empty_curves_are_unavailable():
    with pytest.raises(ValueError):
        reliability_curve(('yes', 'no'), ['yes'], ['yes'], [{'yes': 2., 'no': -1.}])
    assert reliability_curve(('yes', 'no'), [], [], [])['ece'] is None
