"""Every decision request is composed to fit one configured size, and the composition is recorded."""

from .batched_classification import batch_request
from .classifier_config import ClassifierConfig
from .models import DecisionTask, Item, LabeledItem
from .request_composition import DECISION_REQUEST_BYTES, request_bytes

TASK = DecisionTask("relevant", ("include", "exclude"), "Include it?")


def labeled(n, label, *, text="Synthetic body " * 40):
    values = {"title": f"Title {n}", "summary": f"Summary {n}", "text": text}
    return LabeledItem(Item(f"e{n}", values), label, "trusted", {"human_feedback": f"Reason {n}"})


def test_a_small_request_is_unchanged_and_carries_only_compact_examples():
    training = [labeled(0, "include"), labeled(1, "exclude")]
    config = ClassifierConfig(TASK, rubric="Rubric", example_ids=("e0", "e1"))
    target = Item("t", {"text": "A short target story"})
    request, composition = config.compose(target, training)
    assert "composition" not in request and request == config.request(target, training)
    assert request["state"]["target"] == {"text": "A short target story"}
    assert request["state"]["examples"] == [
        {"id": "e0", "title": "Title 0", "summary": "Summary 0", "label": "include", "explanation": "Reason 0"},
        {"id": "e1", "title": "Title 1", "summary": "Summary 1", "label": "exclude", "explanation": "Reason 1"},
    ]
    assert composition["budget_bytes"] == DECISION_REQUEST_BYTES == 65536
    assert composition["examples_shown"] == composition["examples_available"] == 2
    assert not composition["target_trimmed"] and composition["fits"]
    assert composition["request_bytes"] == request_bytes(request)


def test_a_200_kilobyte_story_decides_with_a_trimmed_target_and_records_the_trim():
    training = [labeled(0, "include"), labeled(1, "exclude")]
    config = ClassifierConfig(TASK, rubric="Rubric", example_ids=("e0", "e1"))
    story = "word " * 40_000
    request = config.request(Item("t", {"text": story}), training)
    composition = request["composition"]
    assert composition["target_trimmed"] and composition["fits"]
    assert composition["target_original_chars"] == 200_000
    assert composition["target_sent_chars"] == len(request["state"]["target"]["text"]) < 200_000
    assert story.startswith(request["state"]["target"]["text"])
    assert DECISION_REQUEST_BYTES - 64 < request_bytes(request) <= DECISION_REQUEST_BYTES


def test_500_examples_fit_by_the_recorded_rule_keeping_the_newest_and_both_labels():
    training = [labeled(n, "include" if n < 450 else "exclude", text="x") for n in range(500)]
    training = [LabeledItem(row.item, row.label, row.source, {"human_feedback": "Because " * 30}) for row in training]
    config = ClassifierConfig(TASK, rubric="Rubric", example_ids=tuple(row.item.id for row in training))
    request = config.request(Item("t", {"text": "Target"}), training)
    composition = request["composition"]
    shown = request["state"]["examples"]
    assert composition["examples_available"] == 500
    assert 0 < composition["examples_shown"] == len(shown) < 500
    assert composition["fits"] and not composition["target_trimmed"]
    assert {row["label"] for row in shown} == {"include", "exclude"}
    titles = {row["title"] for row in shown}
    assert "Title 449" in titles and "Title 499" in titles and "Title 0" not in titles
    assert request == config.request(Item("t", {"text": "Target"}), training)


def test_a_batched_request_fits_the_combined_request_and_records_each_classifier():
    other = DecisionTask("timely", ("yes", "no"), "Timely?")
    training = [
        LabeledItem(row.item, row.label, row.source, {"human_feedback": "Because " * 30})
        for row in (labeled(n, "include" if n % 2 else "exclude", text="x") for n in range(300))
    ]
    timely = [LabeledItem(row.item, "yes" if row.label == "include" else "no", "trusted", row.context) for row in training]
    ids = tuple(row.item.id for row in training)
    request, _ = batch_request(
        {"relevant": ClassifierConfig(TASK, example_ids=ids), "timely": ClassifierConfig(other, example_ids=ids)},
        Item("t", {"text": "word " * 40_000}),
        {"relevant": training, "timely": timely},
    )
    composition = request["composition"]
    assert composition["fits"] and request_bytes(request) <= DECISION_REQUEST_BYTES
    assert composition["target_trimmed"] and composition["examples_shown"] == 0
    assert composition["classifiers"]["relevant"]["examples_available"] == 300


def test_every_prediction_records_how_its_request_was_composed(tmp_path):
    import asyncio

    from .flywheel import DecisionFlywheel
    from .flywheel_test import TASK as WHEEL_TASK
    from .flywheel_test import TRAIN, FakeModel, agent

    wheel = DecisionFlywheel(tmp_path / "w.sqlite", ClassifierConfig(WHEEL_TASK), FakeModel(), agent([]), max_requests=5)
    asyncio.run(wheel.predict(Item("big", {"text": "yes " * 50_000}), TRAIN))
    event = next(e for e in wheel.events_of(("prediction",)) if e["target_id"] == "big")
    assert event["request_composition"]["target_trimmed"] and event["request_composition"]["target_original_chars"] == 200_000
    wheel.close()


def test_a_story_with_a_long_copy_of_its_text_in_another_field_still_fits_by_trimming_that_field_first():
    story = "word " * 56_000
    values = {"title": "A policy guide", "summary": "Short summary", "body": story[:-1000], "text": story}
    for request in (
        ClassifierConfig(TASK, rubric="Rubric").request(Item("t", values), []),
        batch_request(
            {"relevant": ClassifierConfig(TASK), "timely": ClassifierConfig(DecisionTask("timely", ("yes", "no"), "Timely?"))},
            Item("t", values),
            {"relevant": [], "timely": []},
        )[0],
    ):
        composition, sent = request["composition"], request["state"]["target"]
        assert composition["fits"] and request_bytes(request) <= DECISION_REQUEST_BYTES
        assert composition["target_trimmed"] and composition["target_field"] == "text"
        assert sent["title"] == "A policy guide" and sent["summary"] == "Short summary"
        assert sent["body"] == "" and composition["trimmed_fields"] == {
            "body": {"original_chars": len(story) - 1000, "sent_chars": 0}
        }
        assert story.startswith(sent["text"]) and composition["target_sent_chars"] == len(sent["text"]) > 60_000


def test_an_untrimmed_request_records_no_trimmed_fields():
    request, composition = ClassifierConfig(TASK).compose(Item("t", {"title": "T", "body": "B", "text": "A short story"}), [])
    assert "trimmed_fields" not in composition and not composition["target_trimmed"]
