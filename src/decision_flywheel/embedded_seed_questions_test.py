"""An application can seed the supporting questions it already knows matter."""

import pytest

from .batched_classification import BatchedAnswers
from .embedded_cyclotron import Cyclotron, CyclotronDefinition
from .embedded_cyclotron_test import DEFINITION, RELEVANT, item, run
from .models import DecisionResult, DecisionTask

MENTIONS_YES = {"name": "mentions_yes", "instructions": "Does the text say yes?", "labels": ["yes", "no"]}


def choice(labels, first):
    return DecisionResult(labels[0] if first > 0.5 else labels[1], {labels[0]: first, labels[1]: 1 - first})


class Model:
    """The decision answer is uninformative; the seeded question carries the signal."""

    model_identity = "seeded-questions"

    def __init__(self):
        self.requests = []

    async def classify_many(self, configs, target, training, **kwargs):
        self.requests.append({cid: config.tasks for cid, config in configs.items()})
        says_yes = "yes" in target.values["text"]
        answers = {}
        for cid, config in configs.items():
            answers[cid] = {"decision": choice(("include", "exclude"), 0.55)}
            for task in config.tasks:
                answers[cid][task.name] = choice(task.labels, 0.9 if says_yes else 0.1)
        return BatchedAnswers(answers, "scripted", {}, 1)


def test_seeded_supporting_questions_appear_in_the_first_request(tmp_path):
    model = Model()
    with Cyclotron.open(tmp_path / "c", DEFINITION, model, seed_supporting_questions={"relevant": [MENTIONS_YES]}) as cyclotron:
        assert [task.name for task in cyclotron.wheels["relevant"].active.config.tasks] == ["mentions_yes"]
        run(cyclotron.decide(item("ref-1")))
    assert model.requests[0]["relevant"] == (DecisionTask("mentions_yes", ("yes", "no"), "Does the text say yes?"),)


@pytest.mark.slow
def test_seeded_supporting_questions_feed_the_first_ml_model_fit(tmp_path):
    from .flywheel_test import agent

    definition = CyclotronDefinition(
        "papyrus-relevance", (RELEVANT,), seed="spec-seed", optimize_every=8, rubric_changes_every=1000
    )
    with Cyclotron.open(
        tmp_path / "c", definition, Model(), agent([]), max_requests=500, seed_supporting_questions={"relevant": [MENTIONS_YES]}
    ) as cyclotron:
        for index in range(60):
            decision = run(cyclotron.decide(item(f"learn-{index}", ("yes " if index % 2 else "no ") + str(index))))
            run(cyclotron.review(decision.decision_id, "include" if index % 2 else "exclude"))
        fits = [event for event in cyclotron.wheels["relevant"].history(100000) if event["kind"] == "fit-completed"]
    assert fits, "no ML model was fitted"
    assert "mentions_yes/yes" in fits[0]["features"]


def test_the_optimizer_can_replace_seeded_supporting_questions(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), seed_supporting_questions={"relevant": [MENTIONS_YES]}) as cyclotron:
        config = cyclotron.wheels["relevant"].active.config
    replacement = {"name": "names_a_vendor", "instructions": "Does it name a vendor?", "labels": ["yes", "no"]}
    revised = config.apply({"rationale": "A better signal", "tasks": [replacement]}, ())
    assert [task.name for task in revised.tasks] == ["names_a_vendor"]
    assert revised.parent_fingerprint == config.fingerprint


def test_reopening_a_store_never_rewrites_its_seeded_questions(tmp_path):
    other = {"name": "other", "instructions": "Something else?", "labels": ["yes", "no"]}
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), seed_supporting_questions={"relevant": [MENTIONS_YES]}):
        pass
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), seed_supporting_questions={"relevant": [other]}) as cyclotron:
        assert [task.name for task in cyclotron.wheels["relevant"].initial.tasks] == ["mentions_yes"]
        assert cyclotron.status().version == 1


def test_a_store_created_without_seeds_keeps_its_context_when_seeds_are_added_later(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()):
        pass
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), seed_supporting_questions={"relevant": [MENTIONS_YES]}) as cyclotron:
        assert cyclotron.wheels["relevant"].initial.tasks == ()


def test_a_question_can_be_given_as_a_decision_task(tmp_path):
    task = DecisionTask("mentions_yes", ("yes", "no"), "Does the text say yes?")
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), seed_supporting_questions={"relevant": [task]}) as cyclotron:
        assert cyclotron.wheels["relevant"].active.config.tasks == (task,)


@pytest.mark.parametrize(
    "seeds",
    [
        {"other": [MENTIONS_YES]},
        {"relevant": MENTIONS_YES},
        {"relevant": [{"name": "x", "labels": ["yes", "no"]}]},
        {"relevant": [{**MENTIONS_YES, "name": "decision"}]},
        {"relevant": [MENTIONS_YES, MENTIONS_YES]},
    ],
)
def test_invalid_seed_questions_are_refused_before_the_store_is_written(tmp_path, seeds):
    with pytest.raises(ValueError):
        Cyclotron.open(tmp_path / "c", DEFINITION, Model(), seed_supporting_questions=seeds)
    assert not (tmp_path / "c" / "cyclotron.sqlite3").exists()
