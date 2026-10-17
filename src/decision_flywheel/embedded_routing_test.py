"""Only the outputs that need a person are sent to review."""
import pytest

from .batched_classification import BatchedAnswers
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .embedded_cyclotron_test import DEFINITION, RELEVANT, item, run
from .embedded_review_rate_test import FAST
from .models import DecisionResult

TIMELY = ClassifierSpec("timely", ("include", "exclude"), "Is this timely?", positive_label="include")
TWO = CyclotronDefinition("newsroom", (RELEVANT, TIMELY), seed="routing-seed")


class Model:
    """``relevant`` is always 90% sure; ``timely`` is 60% sure on items marked 'unsure'."""
    model_identity = "routing"

    async def classify_many(self, configs, target, training, **kwargs):
        unsure = "unsure" in target.values["text"]
        p = {"relevant": .9, "timely": .6 if unsure else .9}
        return BatchedAnswers({cid: {"decision": DecisionResult("include", {"include": p[cid], "exclude": 1 - p[cid]})}
                               for cid in configs}, "scripted", {}, 1)


def quiet(cyclotron, rate=0.):
    """Confident decisions are sampled only by the audit share (or at ``rate``)."""
    cyclotron.set_review_rate(rate, set_by="spec")


def first_unaudited(cyclotron, text):
    for index in range(100):
        decision = run(cyclotron.decide(item(f"{text}-{index}", f"{text} {index}")))
        if not decision.review.for_output("relevant").selected:
            return decision
    raise AssertionError("every decision was audited")


def test_a_confident_output_beside_an_unsure_one_is_not_sent_to_review(tmp_path):
    with Cyclotron.open(tmp_path / "c", TWO, Model(), review_program=FAST) as cyclotron:
        quiet(cyclotron)
        decision = first_unaudited(cyclotron, "unsure")
        pending = {cid: cyclotron.status(cid).pending.decisions_awaiting_review for cid in ("relevant", "timely")}
    assert decision.review.requested == ("timely",)
    assert decision.review.selected and decision.review.reason == "program"
    assert decision.review.for_output("timely").detail.startswith("Confidence 60% is below")
    assert decision.to_json()["review"]["outputs"]["relevant"]["selected"] is False
    assert pending == {"relevant": 0, "timely": 1}


def test_a_checklist_that_labels_only_the_requested_output_completes_the_decision(tmp_path):
    with Cyclotron.open(tmp_path / "c", TWO, Model(), review_program=FAST) as cyclotron:
        quiet(cyclotron)
        decision = first_unaudited(cyclotron, "unsure")
        reviews = run(cyclotron.review(decision.decision_id, labels={"timely": "exclude"}))
        state = cyclotron.db.execute("SELECT state FROM decisions WHERE id=?", (decision.decision_id,)).fetchone()[0]
    assert reviews["timely"].selected_by == "program" and state == "reviewed"


def test_an_output_that_was_not_requested_can_only_be_reviewed_as_the_reviewers_choice(tmp_path):
    with Cyclotron.open(tmp_path / "c", TWO, Model(), review_program=FAST) as cyclotron:
        quiet(cyclotron)
        decision = first_unaudited(cyclotron, "unsure")
        with pytest.raises(ValueError, match="not sent to review for relevant"):
            run(cyclotron.review(decision.decision_id, labels={"relevant": "include", "timely": "include"},
                                 selected_by="program"))
        reviews = run(cyclotron.review(decision.decision_id, labels={"relevant": "include", "timely": "include"}))
    assert reviews["relevant"].selected_by == "reviewer" and reviews["timely"].selected_by == "program"


@pytest.mark.slow
def test_audit_sampling_and_propensities_are_per_output(tmp_path):
    with Cyclotron.open(tmp_path / "c", TWO, Model(), review_program=FAST, max_requests=500) as cyclotron:
        quiet(cyclotron)
        decisions = [run(cyclotron.decide(item(f"sure-{index}", f"sure {index}"))) for index in range(200)]
        audited = {cid: {d.decision_id for d in decisions if d.review.for_output(cid).reason == "audit"}
                   for cid in ("relevant", "timely")}
        target = next(d for d in decisions if d.decision_id in audited["timely"] - audited["relevant"])
        run(cyclotron.review(target.decision_id, labels={"timely": "include"}))
        feedback = [e["feedback"] for e in cyclotron.wheels["timely"].history(100000) if e["kind"] == "human-feedback"]
    assert audited["relevant"] and audited["timely"] and audited["relevant"] != audited["timely"]
    assert all(d.review.for_output(cid).propensity == FAST.audit_share for d in decisions for cid in audited)
    assert feedback[-1]["selection_propensity"] == FAST.audit_share


@pytest.mark.slow
def test_routing_replays_the_same_selections_in_a_new_store(tmp_path):
    def selections(directory):
        with Cyclotron.open(directory, TWO, Model(), review_program=FAST) as cyclotron:
            quiet(cyclotron, .5)
            return [run(cyclotron.decide(item(f"i-{index}", ("unsure " if index % 3 == 0 else "sure ") + str(index))))
                    .to_json()["review"] for index in range(30)]
    assert selections(tmp_path / "a") == selections(tmp_path / "b")


def test_each_output_window_counts_only_the_reviews_requested_for_that_output(tmp_path):
    with Cyclotron.open(tmp_path / "c", TWO, Model(), review_program=FAST) as cyclotron:
        quiet(cyclotron, .5)
        decisions = []
        for index in range(10):
            decision = run(cyclotron.decide(item(f"i-{index}", f"sure {index}")))
            decisions.append(decision)
            # The reviewer answers the whole checklist; only requested outputs are program or audit evidence.
            run(cyclotron.review(decision.decision_id, labels={"relevant": "include", "timely": "include"},
                                 selected_by=None))
        run(cyclotron.decide(item("next", "sure next")))
        outputs = cyclotron._program_state().last_check["outputs"]
    for cid in ("relevant", "timely"):
        assert outputs[cid]["labels"] == sum(d.review.for_output(cid).selected for d in decisions)
    assert outputs["relevant"]["labels"] != outputs["timely"]["labels"]


def test_a_single_output_decision_keeps_its_review_shape(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
    assert decision.review.outputs is None and decision.review.requested is None
    assert set(decision.to_json()["review"]) == {"selected", "reason", "propensity", "detail"}


def test_the_weekly_estimate_is_the_chance_that_any_routed_output_is_requested(tmp_path):
    with Cyclotron.open(tmp_path / "c", TWO, Model(), review_program=FAST) as cyclotron:
        quiet(cyclotron, .25)
        for index in range(8):
            run(cyclotron.decide(item(f"i-{index}", ("unsure " if index < 2 else "sure ") + str(index))))
        estimate = cyclotron.overview().review_rate.expected_reviews_per_week
    # Two unsure decisions are certain; six with two confident outputs each have 1 - 0.75^2.
    assert estimate == round(2 + 6 * (1 - .75 ** 2), 1)


def test_a_single_output_store_keeps_its_weekly_estimate(tmp_path):
    from .review_program import expected_reviews
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        quiet(cyclotron, .25)
        decisions = [run(cyclotron.decide(item(f"i-{index}", f"sure {index}"))) for index in range(5)]
        estimate = cyclotron.status().review_rate.expected_reviews_per_week
    assert estimate == expected_reviews(FAST, .25, [d.confidence for d in decisions])
