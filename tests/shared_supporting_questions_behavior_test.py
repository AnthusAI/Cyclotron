"""A supporting question several outputs ask is asked once, paid once, and feeds each output's ML model."""
import asyncio
from datetime import datetime, timezone
import json

from decision_flywheel.batched_classification import BatchedAnswers, answer_groups, batch_request
from decision_flywheel.classifier_config import ClassifierConfig
from decision_flywheel.embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from decision_flywheel.flywheel import DecisionFlywheel
from decision_flywheel.flywheel_test import agent
from decision_flywheel.models import DecisionResult, DecisionTask, Item, LabeledItem
from decision_flywheel.shared_decisions import SharedDecisions
from decision_flywheel.sibling_check import install_sibling_checks
from pathlib import Path
import sqlite3

RELEVANT = ClassifierSpec("relevant", ("include", "exclude"), "Is this relevant?", positive_label="include")
TIMELY = ClassifierSpec("timely", ("include", "exclude"), "Is this timely?", positive_label="include")
QUESTION = {"name": "mentions_yes", "instructions": "Does the text say yes?", "labels": ["yes", "no"]}
MENTIONS_YES = DecisionTask("mentions_yes", ("yes", "no"), "Does the text say yes?")


def choice(labels, first):
    return DecisionResult(labels[0] if first > .5 else labels[1], {labels[0]: first, labels[1]: 1 - first})


class RequestModel:
    """Answers the composed request the way a provider adapter does, and charges by request size.

    ``scoped_signal`` says whether a supporting question asked inside one
    classifier's scope is informative; a shared question always is.
    """
    model_identity = "request-model"

    def __init__(self, scoped_signal=True):
        self.scoped_signal = scoped_signal
        self.requests = []

    async def classify_many(self, configs, target, training, *, share_supporting_questions=False, **kwargs):
        request, identities = batch_request(configs, target, training, share_questions=share_supporting_questions)
        self.requests.append(request)
        says_yes = "yes" in target.values["text"]

        def answer(key, task):
            if task.name == "mentions_yes" and (key in request.get("owners", {}) or self.scoped_signal):
                return choice(task.labels, .9 if says_yes else .1)
            return choice(task.labels, .55)

        usage = {"input_tokens": len(json.dumps(request)), "output_tokens": 10 * len(request["questions"])}
        return BatchedAnswers(answer_groups(configs, request, identities, answer), "scripted", usage, 1)


def run(coroutine):
    return asyncio.run(coroutine)


def drive(tmp_path, seeds, reviews=60):
    definition = CyclotronDefinition("newsroom", (RELEVANT, TIMELY), seed="shared-seed", optimize_every=8,
                                     rubric_changes_every=1000)
    model = RequestModel()
    with Cyclotron.open(tmp_path / "c", definition, model, agent([]) if reviews else None, max_requests=2000,
                        seed_supporting_questions=seeds) as cyclotron:
        for index in range(max(reviews, 1)):
            text = ("yes " if index % 2 else "no ") + str(index)
            decision = run(cyclotron.decide(Item(f"item-{index}", {"text": text})))
            for classifier in ("relevant", "timely") if reviews else ():
                run(cyclotron.review(decision.decision_id, "include" if index % 2 else "exclude",
                                     classifier=classifier))
        histories = {cid: wheel.history(100000) for cid, wheel in cyclotron.wheels.items()}
    return model, histories


def test_a_question_both_outputs_ask_appears_once_in_the_request_and_costs_less_than_a_duplicate(tmp_path):
    shared, _ = drive(tmp_path / "shared", {"relevant": [QUESTION], "timely": [QUESTION]}, reviews=0)
    duplicate, _ = drive(tmp_path / "duplicate", {"relevant": [QUESTION],
                                                  "timely": [{**QUESTION, "name": "mentions_yes_again"}]}, reviews=0)
    request = shared.requests[0]
    asked = [q for q in request["questions"].values() if q["instructions"].startswith("Does the text say yes?")]
    assert len(asked) == 1 and len(request["questions"]) == 3
    assert list(request["owners"].values()) == [[["relevant", "mentions_yes"], ["timely", "mentions_yes"]]]
    assert len(duplicate.requests[0]["questions"]) == 4 and "owners" not in duplicate.requests[0]
    assert len(json.dumps(request)) < len(json.dumps(duplicate.requests[0]))


def test_the_shared_answer_reaches_both_ml_models(tmp_path):
    _, histories = drive(tmp_path, {"relevant": [QUESTION], "timely": [QUESTION]})
    for classifier, history in histories.items():
        fits = [event for event in history if event["kind"] == "fit-completed"]
        assert fits, f"{classifier} fitted no ML model"
        assert "mentions_yes/yes" in fits[0]["features"]


def wheels_with_shared_question(tmp_path, model):
    shared = SharedDecisions(tmp_path / "shared.sqlite3", model, max_requests=1000, observer=lambda _: None,
                             share_supporting_questions=True)
    configs = {"relevant": ClassifierConfig(RELEVANT.task, tasks=(MENTIONS_YES,)),
               "timely": ClassifierConfig(TIMELY.task, tasks=(MENTIONS_YES,))}
    wheels = {cid: DecisionFlywheel(tmp_path / f"{cid}.sqlite3", config, shared.adapter(cid),
                                    max_requests=1000, min_evaluation_per_class=2,
                                    selection_policy=spec.selection_policy)
              for (cid, config), spec in zip(configs.items(), (RELEVANT, TIMELY))}
    rows = [LabeledItem(Item(f"i-{index}", {"text": ("yes " if index % 2 else "no ") + str(index)}),
                        "include" if index % 2 else "exclude") for index in range(16)]
    training, development = tuple(rows[:10]), tuple(rows[10:])
    partitions = lambda _: (training, development, ())
    install_sibling_checks(wheels, shared, partitions)
    shared.bind_context(configs, {cid: training for cid in wheels})
    return wheels, shared, training, development


def test_dropping_a_shared_question_from_one_output_is_measured_against_the_other_owner(tmp_path):
    from decision_flywheel.candidate_fitting import fit_candidate
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    wheels, shared, training, development = wheels_with_shared_question(tmp_path, RequestModel(scoped_signal=False))
    timely = wheels["timely"]
    fitted, _ = run(fit_candidate(timely, timely.active.config, training, development, protected=(),
                                  propensities={row.item.id: 1. for row in training},
                                  validation_status="evaluated", now=now))
    assert fitted.head is not None
    timely._activate(fitted)
    relevant = wheels["relevant"]
    assert relevant.sibling_check.shared_owners(relevant.active.config) == {"mentions_yes": ["relevant", "timely"]}
    without = ClassifierConfig(RELEVANT.task)
    effects = run(relevant.sibling_effects(without, now))
    # Unsharing the question changes how the other owner's copy is asked, and so its features.
    effect = effects["siblings"]["timely"]
    assert effect["status"] == "regressed" and effects["refused"]
    assert effect["candidate_score"] < effect["incumbent_score"]
    assert effects["shared_supporting_questions"] == {}
    shared.close()


def test_a_candidate_that_adopts_a_sibling_question_records_every_owner(tmp_path):
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    wheels, shared, _, _ = wheels_with_shared_question(tmp_path, RequestModel())
    relevant = wheels["relevant"]
    relevant._activate(type(relevant.active)(ClassifierConfig(RELEVANT.task)))
    assert relevant.sibling_briefing()["sibling_supporting_questions"]["by_classifier"] == {"timely": [QUESTION]}
    effects = run(relevant.sibling_effects(ClassifierConfig(RELEVANT.task, tasks=(MENTIONS_YES,)), now))
    assert effects["context_changed"]
    assert effects["shared_supporting_questions"] == {"mentions_yes": ["relevant", "timely"]}
    assert effects["siblings"]["timely"]["status"] == "no-regression"
    shared.close()


LEGACY_REQUEST = Path(__file__).parent / "fixtures" / "legacy_identical_questions_request.json"


def canonical(request):
    return json.dumps(request, sort_keys=True, separators=(",", ":"))


def test_an_existing_store_with_an_identical_pair_keeps_its_request_byte_for_byte_after_upgrade(tmp_path):
    seeds = {"relevant": [QUESTION], "timely": [QUESTION]}
    definition = CyclotronDefinition("newsroom", (RELEVANT, TIMELY), seed="shared-seed")
    with Cyclotron.open(tmp_path / "c", definition, RequestModel(), seed_supporting_questions=seeds):
        pass
    # A store created before sharing existed has no sharing flag.
    with sqlite3.connect(tmp_path / "c" / "cyclotron.sqlite3") as db:
        db.execute("DELETE FROM meta WHERE key='share_supporting_questions'")
    model = RequestModel()
    with Cyclotron.open(tmp_path / "c", definition, model, share_supporting_questions=True) as cyclotron:
        run(cyclotron.decide(Item("item-1", {"text": "yes 1"})))
        assert not cyclotron.shared.share_supporting_questions
        assert cyclotron.wheels["relevant"].sibling_briefing() == {}
    # Captured from the engine before sharing existed (develop at 8ffd7cd).
    assert canonical(model.requests[0]) == LEGACY_REQUEST.read_text().strip()


def test_a_new_store_shares_by_default_and_can_opt_out_once_at_creation(tmp_path):
    seeds = {"relevant": [QUESTION], "timely": [QUESTION]}
    definition = CyclotronDefinition("newsroom", (RELEVANT, TIMELY), seed="shared-seed")
    model = RequestModel()
    with Cyclotron.open(tmp_path / "out", definition, model, seed_supporting_questions=seeds,
                        share_supporting_questions=False):
        pass
    with Cyclotron.open(tmp_path / "out", definition, model, share_supporting_questions=True) as cyclotron:
        run(cyclotron.decide(Item("item-1", {"text": "yes 1"})))
    assert canonical(model.requests[-1]) == LEGACY_REQUEST.read_text().strip()
    with Cyclotron.open(tmp_path / "in", definition, model, seed_supporting_questions=seeds) as cyclotron:
        run(cyclotron.decide(Item("item-1", {"text": "yes 1"})))
    assert "owners" in model.requests[-1]
