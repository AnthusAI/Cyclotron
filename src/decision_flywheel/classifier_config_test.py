"""Specs for the three independently adjustable decision request parts."""
import pytest

from .classifier_config import ClassifierConfig
from .models import DecisionTask, Item, LabeledItem


TASK = DecisionTask("inclusion", ("include", "exclude"), "Should this item be included?")
POOL = (LabeledItem(Item("yes", {"text": "Practical paper"}), "include"),
        LabeledItem(Item("no", {"text": "Other paper"}), "exclude"))


def test_the_initial_classifier_has_no_invented_rubric_or_examples():
    config = ClassifierConfig(TASK)
    request = config.request(Item("target", {"text": "New paper"}), POOL)
    assert request["state"] == {"target": {"text": "New paper"}, "rubric": "", "examples": []}
    assert list(request["questions"]) == ["decision"]


def test_a_proposal_changes_all_three_parts_and_preserves_the_parent_version():
    parent = ClassifierConfig(TASK)
    child = parent.apply({"rationale": "Practical recent work", "rubric": "Practical work",
                          "example_ids": ["yes", "no"], "tasks": [
                              {"name": "practical", "instructions": "Is there an implementation?",
                               "labels": ["yes", "no"]}]}, POOL)
    request = child.request(Item("new", {"text": "New paper"}), POOL)
    assert request["state"]["rubric"] == "Practical work"
    assert [row["label"] for row in request["state"]["examples"]] == ["include", "exclude"]
    assert request["questions"]["practical"]["options"] == ["yes", "no"]
    assert "state.rubric" in request["questions"]["decision"]["instructions"]
    assert child.parent_fingerprint == parent.fingerprint
    assert parent.rubric == ""


@pytest.mark.parametrize("proposal", [{"weights": [1]}, {"example_ids": ["unknown"]},
                                     {"example_ids": ["yes", "yes"]}, {"rubric": 123},
                                     {"dynamic_elements": ["execute_python"]},
                                     {"tasks": [{"name": "decision", "instructions": "x", "labels": ["y", "n"]}]}])
def test_invalid_proposals_cannot_modify_the_active_classifier(proposal):
    parent = ClassifierConfig(TASK)
    with pytest.raises(ValueError):
        parent.apply(proposal, POOL)
    assert parent == ClassifierConfig(TASK)


def test_examples_matching_the_target_by_id_or_normalized_text_are_never_sent():
    config = ClassifierConfig(TASK, example_ids=("yes", "no"))
    request = config.request(Item("alias", {"text": " PRACTICAL  paper "}), POOL)
    assert [row["label"] for row in request["state"]["examples"]] == ["exclude"]


def test_dynamic_time_requires_an_explicit_aware_request_instant():
    from datetime import datetime, timezone
    config = ClassifierConfig(TASK).apply({"dynamic_elements": ["current_datetime"]}, POOL)
    with pytest.raises(ValueError, match="aware"):
        config.request(Item("new", {"text": "New"}), POOL)
    request = config.request(Item("new", {"text": "New"}), POOL,
                             now=datetime(2026, 10, 5, tzinfo=timezone.utc))
    assert request["state"]["current_datetime"] == "2026-10-05T00:00:00Z"
