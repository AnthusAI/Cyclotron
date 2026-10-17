"""Improving one output must not quietly make a sibling output worse.

Several outputs share one decision-model request, so one output's rubric is
part of every sibling's context. These specs use a scripted batched model in
which the ``relevant`` rubric can interfere with the ``timely`` answers, the
way prompt interference does with a real model.
"""
import asyncio
import json

import pytest

from decision_flywheel.batched_classification import BatchedAnswers
from decision_flywheel.embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from decision_flywheel.models import DecisionResult, Item
from decision_flywheel.optimizer_agent import OptimizerAgent, OptimizerReply
from decision_flywheel.sibling_check import SiblingPolicy

pytestmark = pytest.mark.slow

RELEVANT = ClassifierSpec("relevant", ("include", "exclude"), "Is this relevant?", positive_label="include")
TIMELY = ClassifierSpec("timely", ("include", "exclude"), "Is this timely?", positive_label="include")
HELPFUL_RUBRIC = "Include items that say yes."
ITEMS = 32


def answer(p):
    return DecisionResult("include" if p > .5 else "exclude", {"include": p, "exclude": 1 - p})


class InterferingModel:
    """``relevant`` improves with the helpful rubric; with interference, that rubric makes ``timely`` uninformative."""
    model_identity = "scripted-interference"

    def __init__(self, interfering=True):
        self.interfering = interfering

    async def classify_many(self, configs, target, training, **kwargs):
        text = target.values["text"]
        helped = HELPFUL_RUBRIC in (configs["relevant"].rubric or "")
        relevant = (.85 if "yes" in text else .15) if helped else .55
        timely = .55 if helped and self.interfering else (.85 if "fresh" in text else .15)
        answers = {"relevant": {"decision": answer(relevant)}, "timely": {"decision": answer(timely)}}
        return BatchedAnswers({cid: answers[cid] for cid in configs}, "scripted", {}, 1)


def optimizer():
    reply = json.dumps({"rationale": "Reviewers include items that say yes", "rubric": HELPFUL_RUBRIC})
    return OptimizerAgent(lambda _: OptimizerReply(reply, "fake-optimizer"))


def drive(tmp_path, model, **definition_options):
    definition = CyclotronDefinition("newsroom", (RELEVANT, TIMELY), seed="sibling-seed", optimize_every=1000,
                                     rubric_changes_every=1, **definition_options)
    with Cyclotron.open(tmp_path / "c", definition, model, optimizer(), max_requests=5000) as cyclotron:
        decisions = []
        for index in range(ITEMS):
            says_yes, fresh = index % 2 == 1, (index // 2) % 2 == 1
            text = ("yes " if says_yes else "no ") + ("fresh " if fresh else "stale ") + str(index)
            decisions.append((asyncio.run(cyclotron.decide(Item(f"item-{index}", {"text": text}))), says_yes, fresh))
        # The sibling has development labels before the other output learns.
        for decision, _, fresh in decisions:
            asyncio.run(cyclotron.review(decision.decision_id, "include" if fresh else "exclude", classifier="timely"))
        for decision, says_yes, _ in decisions:
            asyncio.run(cyclotron.review(decision.decision_id, "include" if says_yes else "exclude",
                                         classifier="relevant"))
        return ({cid: wheel.history(100000) for cid, wheel in cyclotron.wheels.items()},
                {cid: wheel.active.config.rubric for cid, wheel in cyclotron.wheels.items()})


def measured(history):
    """Outcome events of rounds whose candidate changed the shared request."""
    return [e for e in history if "sibling_effects" in e and e["sibling_effects"]["context_changed"]]


ACTIVATED = ("promoted", "context-initialized", "context-refined")


def test_a_candidate_that_helps_one_output_but_hurts_a_sibling_is_refused_by_default(tmp_path):
    histories, rubrics = drive(tmp_path, InterferingModel())
    assert rubrics["relevant"] != HELPFUL_RUBRIC
    outcomes = measured(histories["relevant"])
    assert outcomes, "the effect on the sibling was never measured"
    assert not [e for e in outcomes if e["kind"] in ACTIVATED]
    refused = [e for e in outcomes if e["kind"] == "candidate-rejected" and e["sibling_effects"]["refused"]]
    assert refused
    event = refused[-1]
    assert event["sibling_effects"]["regressed"] == ["timely"]
    effect = event["sibling_effects"]["siblings"]["timely"]
    assert effect["status"] == "regressed" and effect["metric"] == "f1"
    assert effect["candidate_score"] < effect["incumbent_score"]
    assert "sibling timely" in event["reason"]


def test_under_the_record_policy_the_candidate_is_promoted_and_the_sibling_regression_is_recorded(tmp_path):
    histories, rubrics = drive(tmp_path, InterferingModel(), sibling_policy=SiblingPolicy("record"))
    assert rubrics["relevant"] == HELPFUL_RUBRIC
    activated = [e for e in measured(histories["relevant"]) if e["kind"] in ACTIVATED]
    assert activated
    effects = activated[0]["sibling_effects"]
    assert effects["policy"]["action"] == "record" and not effects["refused"]
    assert effects["siblings"]["timely"]["status"] == "regressed"


def test_a_candidate_that_leaves_siblings_no_worse_is_promoted_with_each_sibling_effect_recorded(tmp_path):
    histories, rubrics = drive(tmp_path, InterferingModel(interfering=False))
    assert rubrics["relevant"] == HELPFUL_RUBRIC
    activated = [e for e in measured(histories["relevant"]) if e["kind"] in ACTIVATED]
    assert activated
    effect = activated[0]["sibling_effects"]["siblings"]["timely"]
    assert effect["status"] == "no-regression"
    assert effect["candidate_score"] == effect["incumbent_score"]
