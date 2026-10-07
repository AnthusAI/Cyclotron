import pytest

from .models import DecisionResult, DecisionTask, Item


def test_a_task_normalizes_labels_the_way_primus_evaluation_does():
    task = DecisionTask("sentiment", ("positive", "negative"), "Choose one.")

    assert task.labels == ("positive", "negative")
    assert task.validate_label(" positive ") == "positive"


def test_a_task_rejects_duplicate_or_empty_normalized_labels():
    with pytest.raises(ValueError, match="unique"):
        DecisionTask("sentiment", ("yes", "YES!"), "Choose one.")
    with pytest.raises(ValueError, match="non-empty"):
        DecisionTask("sentiment", ("", "no"), "Choose one.")


def test_a_task_freezes_a_label_sequence_and_rejects_a_string_as_its_labels():
    labels = ["yes", "no"]
    task = DecisionTask("sentiment", labels, "Choose one.")
    fingerprint = task.fingerprint
    labels[0] = "maybe"

    assert task.labels == ("yes", "no")
    assert task.fingerprint == fingerprint
    with pytest.raises(ValueError, match="collection"):
        DecisionTask("sentiment", "yes", "Choose one.")


@pytest.mark.parametrize("labels", [{"yes", "no"}, {"yes": 1, "no": 2}])
def test_a_task_rejects_unordered_label_collections(labels):
    with pytest.raises(ValueError, match="ordered sequence"):
        DecisionTask("sentiment", labels, "Choose one.")


def test_a_task_rejects_unknown_labels_and_non_string_input():
    task = DecisionTask("sentiment", ("yes", "no"), "Choose one.")

    with pytest.raises(ValueError, match="not one of"):
        task.validate_label("maybe")
    with pytest.raises(ValueError, match="lacks string"):
        task.validate_target(Item("target", {"text": 7}))


def test_a_task_fingerprint_is_stable_and_captures_its_contract():
    first = DecisionTask("sentiment", ("yes", "no"), "Choose one.")
    equivalent = DecisionTask("sentiment", ("yes", "no"), "Choose one.")
    changed = DecisionTask("sentiment", ("yes", "no"), "Choose the best one.")

    assert first.fingerprint == equivalent.fingerprint
    assert first.fingerprint != changed.fingerprint


def test_a_result_can_omit_provider_metadata_and_probabilities():
    result = DecisionResult("yes")

    assert result.probabilities is None
    assert result.usage is None
    assert result.latency_ms is None
    assert result.confidence is None


def test_a_result_preserves_only_an_explicit_provider_confidence():
    result = DecisionResult("yes", {"yes": 0.9, "no": 0.1}, confidence=0.37)

    assert result.confidence == 0.37
    assert result.confidence != max(result.probabilities.values())


@pytest.mark.parametrize("confidence", [True, -0.01, 1.01, float("nan"), "certain"])
def test_a_result_rejects_malformed_explicit_confidence(confidence):
    with pytest.raises(ValueError, match="confidence"):
        DecisionResult("yes", confidence=confidence)


@pytest.mark.parametrize(
    "probabilities",
    [
        {"yes": 0.8},
        {"yes": 0.8, "no": 0.1, "maybe": 0.1},
        {"yes": 1.2, "no": -0.2},
        {"yes": 0.6, "no": 0.3},
    ],
)
def test_a_present_probability_distribution_must_cover_every_task_label(probabilities):
    task = DecisionTask("sentiment", ("yes", "no"), "Choose one.")

    with pytest.raises(ValueError):
        task.validate_result(DecisionResult("yes", probabilities))


def test_a_result_label_is_validated_against_the_task():
    task = DecisionTask("sentiment", ("yes", "no"), "Choose one.")

    with pytest.raises(ValueError, match="not one of"):
        task.validate_result(DecisionResult("maybe"))


def test_a_distribution_with_common_provider_rounding_is_preserved():
    task = DecisionTask("sentiment", ("yes", "no", "maybe"), "Choose one.")

    result = task.validate_result(DecisionResult("yes", {"yes": 0.33, "no": 0.33, "maybe": 0.33}))

    assert result.probabilities == {"yes": 0.33, "no": 0.33, "maybe": 0.33}
