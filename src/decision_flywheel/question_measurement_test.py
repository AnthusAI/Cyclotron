"""New questions are measured retrospectively under the deployed context."""
import asyncio

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV, agent
from .question_measurement import measure_questions, rank_question
from .models import Item, LabeledItem


QUESTION = {"name": "practical", "instructions": "Is this practical?", "labels": ["yes", "no"]}


def test_backfill_preserves_current_rubric_and_examples_and_never_promotes_a_classifier(tmp_path):
    class Recording(FakeModel):
        requests = []
        async def classify(self, config, target, training, **kwargs):
            self.requests.append(config.request(target, training, now=kwargs["now"]))
            return await super().classify(config, target, training, **kwargs)
    model = Recording()
    config = ClassifierConfig(TASK, rubric="Current criteria", example_ids=("t0", "t1"))
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", config, model, agent([]))
    before = wheel.active.fingerprint
    kwargs = dict(protected=tuple(r.item for r in DEV), propensities={r.item.id: 1. for r in TRAIN})
    report = asyncio.run(measure_questions(wheel, TRAIN, [QUESTION], **kwargs))
    assert report["count"] == 6
    assert report["limit"] == 200
    assert report["by_class"] == {"include": 3, "exclude": 3}
    assert report["main_answer_agreement"]["count"] == 6
    assert report["main_answer_agreement"]["accuracy"] == .5
    assert report["rankings"][0]["cross_validated"]["accuracy"] == 1.
    assert all(r["state"]["rubric"] == "Current criteria" for r in model.requests)
    assert all("human_feedback" not in r["state"]["target"] and "label" not in r["state"]["target"] for r in model.requests)
    assert wheel.active.fingerprint == before
    calls = model.calls
    assert asyncio.run(measure_questions(wheel, TRAIN, [QUESTION], **kwargs)) == report
    assert model.calls == calls
    wheel.close()


def test_an_inverse_question_can_rank_high_without_matching_the_final_label_names():
    rows = [(str(i), "include" if i % 2 else "exclude",
             {"yes": .02 if i % 2 else .98, "no": .98 if i % 2 else .02}) for i in range(12)]
    result = rank_question(TASK.labels, ["yes", "no"], rows)
    assert result["cross_validated"]["accuracy"] == 1.
    assert result["cross_validated"]["count"] == 12
    assert result["direct_agreement"] is None
    assert all(row["target_id"] not in row["fit_ids"] for row in result["fold_evidence"])


def test_multiclass_question_options_can_predict_different_final_label_names():
    classes, options = ("accept", "reject", "defer"), ("a", "b", "c")
    rows = [(str(i), classes[i % 3], {option: .98 if j == i % 3 else .01 for j, option in enumerate(options)})
            for i in range(15)]
    result = rank_question(classes, options, rows)
    assert result["cross_validated"]["accuracy"] == 1.
    assert result["cross_validated"]["balanced_accuracy"] == 1.


def test_small_minority_coverage_is_reported_not_turned_into_an_accuracy_claim():
    result = rank_question(TASK.labels, ["yes", "no"], [("one", "include", {"yes": .9, "no": .1})])
    assert result["cross_validated"] is None
    assert result["by_class"] == {"include": 1, "exclude": 0}


def test_recent_window_keeps_older_scarce_class_labels_without_exceeding_two_hundred():
    from .question_measurement import select_window
    pool = tuple(LabeledItem(Item(str(i), {"text": f"Text {i}"}), "include" if i < 3 else "exclude")
                 for i in range(210))
    selected = select_window(pool, TASK.labels, 200)
    assert len(selected) == 200
    assert sum(row.label == "include" for row in selected) == 3
    assert selected[-1].item.id == "209"


def test_partial_backfill_resumes_cached_answers_only_after_explicit_authorization(tmp_path):
    import pytest
    path = tmp_path / "wheel.sqlite"
    model = FakeModel()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), model, agent([]), max_requests=3)
    kwargs = dict(protected=tuple(r.item for r in DEV), propensities={r.item.id: 1. for r in TRAIN})
    with pytest.raises(RuntimeError):
        asyncio.run(measure_questions(wheel, TRAIN, [QUESTION], **kwargs))
    assert model.calls == 3
    wheel.close()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), model, agent([]), max_requests=10)
    blocked = asyncio.run(measure_questions(wheel, TRAIN, [QUESTION], **kwargs))
    assert "explicit retry" in blocked["reason"]
    assert model.calls == 3
    report = asyncio.run(measure_questions(wheel, TRAIN, [QUESTION], retry_interrupted=True, **kwargs))
    assert report["count"] == 6
    assert model.calls == 6
    assert wheel.requests == 3
    wheel.close()


def test_protected_items_cannot_enter_backfill_even_when_they_have_human_labels(tmp_path):
    import pytest
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), model, agent([]))
    with pytest.raises(ValueError, match="disjoint"):
        asyncio.run(measure_questions(wheel, TRAIN, [QUESTION], protected=(TRAIN[0].item,),
                                    propensities={r.item.id: 1. for r in TRAIN}))
    assert model.calls == 0
    wheel.close()
