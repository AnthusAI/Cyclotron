"""Specs for label-independent natural and equal-class evaluation."""
import pytest

from .classification_metrics import classification_metrics


def test_three_classes_contribute_equally_without_resampling_the_evaluation_items():
    labels = ("red", "green", "blue")
    truth = ["red"] * 8 + ["green", "blue"]
    probabilities = [{"red": 1., "green": 0., "blue": 0.}] * 10
    result = classification_metrics(labels, truth, ["red"] * 10, probabilities)
    assert result["accuracy"] == .8
    assert result["balanced_accuracy"] == pytest.approx(1/3)
    assert result["brier"] == pytest.approx(.4)
    assert result["balanced_brier"] == pytest.approx(4/3)
    assert result["per_class"]["red"]["count"] == 8
    assert result["per_class"]["blue"]["recall"] == 0
    assert result["per_class"]["blue"]["recall_interval_95"][1] > .7


def test_missing_classes_do_not_get_a_fabricated_balanced_score():
    result = classification_metrics(("one", "two"), ["one"], ["one"], [{"one": 1., "two": 0.}])
    assert result["missing_classes"] == ["two"]
    assert result["balanced_accuracy"] is None
    assert result["balanced_brier"] is None
    assert result["per_class"]["two"]["recall"] is None


def test_malformed_evaluation_records_are_rejected_instead_of_silently_zipped():
    with pytest.raises(ValueError):
        classification_metrics(("one", "two"), ["one"], [], [])
