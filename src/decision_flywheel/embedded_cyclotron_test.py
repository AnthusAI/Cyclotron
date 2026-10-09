"""An application decides and reviews its own items without managing learning data."""
import asyncio

import pytest

from .batched_classification import BatchedAnswers
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .flywheel_test import agent
from .learning_loop import review_role
from .models import DecisionResult, Item

RELEVANT = ClassifierSpec("relevant", ("include", "exclude"), "Is this relevant to the publication?",
                          positive_label="include")
DEFINITION = CyclotronDefinition("papyrus-relevance", (RELEVANT,), seed="spec-seed")


class Model:
    """A scripted batched decision model: 'yes' items look relevant."""
    model_identity = "scripted-batch"

    def __init__(self):
        self.calls = 0

    async def classify_many(self, configs, target, training, **kwargs):
        self.calls += 1
        p = .8 if "yes" in target.values["text"] else .3
        return BatchedAnswers({cid: {"decision": DecisionResult("include" if p > .5 else "exclude",
                                                                {"include": p, "exclude": 1 - p})}
                               for cid in configs}, "scripted", {}, 1)


def run(coroutine):
    return asyncio.run(coroutine)


def item(identifier, text="yes, on our beat"):
    return Item(identifier, {"text": text})


def items_with_role(role, count, prefix="item"):
    found, index = [], 0
    while len(found) < count:
        identifier = f"{prefix}-{index}"
        if review_role(DEFINITION.seed, identifier) == role:
            found.append(identifier)
        index += 1
    return found


def test_an_application_decides_and_reviews_by_its_own_ids(tmp_path):
    model = Model()
    with Cyclotron.open(tmp_path / "c", DEFINITION, model) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        assert decision.item_id == "ref-1" and decision.label == "include"
        assert decision.confidence == pytest.approx(.8) and decision.version == 1
        assert decision.review.selected and decision.review.propensity == 1.0
        review = run(cyclotron.review(decision.decision_id, "exclude", explanation="Vendor marketing.",
                                      reason_code="out_of_scope", reviewer="editor-1"))
        assert review.kind == "label" and review.label == "exclude"
        snapshot = cyclotron.status().to_json()
    assert snapshot["cyclotron"]["id"] == "papyrus-relevance"
    assert snapshot["alignment"]["labels"] == 1 and snapshot["alignment"]["accuracy"] == 0.0
    assert snapshot["pending"]["decisionsAwaitingReview"] == 0
    assert model.calls == 1


def test_an_unchanged_item_keeps_its_decision_without_a_model_call_even_after_a_restart(tmp_path):
    model = Model()
    with Cyclotron.open(tmp_path / "c", DEFINITION, model) as cyclotron:
        first = run(cyclotron.decide(item("ref-1")))
        assert run(cyclotron.decide(item("ref-1"))) == first
    with Cyclotron.open(tmp_path / "c", DEFINITION, model) as cyclotron:
        assert run(cyclotron.decide(item("ref-1"))) == first
        assert cyclotron.status().pending.decisions_awaiting_review == 1
    assert model.calls == 1


def test_a_changed_item_gets_a_new_decision_and_the_old_one_is_superseded(tmp_path):
    model = Model()
    with Cyclotron.open(tmp_path / "c", DEFINITION, model) as cyclotron:
        first = run(cyclotron.decide(item("ref-1", "yes")))
        second = run(cyclotron.decide(item("ref-1", "no longer relevant")))
        assert second.decision_id != first.decision_id and second.label == "exclude"
        assert cyclotron.status().pending.decisions_awaiting_review == 1
        run(cyclotron.review(second.decision_id, "exclude"))
        assert cyclotron.status().pending.decisions_awaiting_review == 0
    assert model.calls == 2


def test_audit_reviews_never_reach_the_optimizer_or_the_ml_model_fit(tmp_path):
    audit = items_with_role("scoreboard", 1)[0]
    learning = items_with_role("training", 1)[0]
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        for identifier, note in ((audit, "secret audit note"), (learning, "training note")):
            decision = run(cyclotron.decide(item(identifier)))
            run(cyclotron.review(decision.decision_id, "include", explanation=note))
        training, development, protected = cyclotron._partitions("relevant")
        assert [row.item.id for row in training] == [learning]
        assert audit in [entry.id for entry in protected]
        assert cyclotron.wheels["relevant"].optimizer_context["human_explanations"] == ["training note"]
        # Both reviews were selected by the cyclotron, so both count toward alignment.
        assert cyclotron.status().alignment.labels == 2
    # Partition assignment depends only on the seed and the item identity.
    assert review_role(DEFINITION.seed, audit) == "scoreboard"


def test_a_review_the_reviewer_chose_trains_but_is_not_alignment_or_audit_evidence(tmp_path):
    audit = items_with_role("scoreboard", 1)[0]
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        decision = run(cyclotron.decide(item(audit)))
        run(cyclotron.review(decision.decision_id, "exclude", selected_by="reviewer", explanation="I looked myself."))
        training, _, protected = cyclotron._partitions("relevant")
        assert [row.item.id for row in training] == [audit]
        assert audit not in [entry.id for entry in protected]
        status = cyclotron.status()
    assert status.alignment.labels == 0
    assert status.pending.decisions_awaiting_review == 0


def test_a_second_label_is_a_correction_and_replaces_the_first(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        run(cyclotron.review(decision.decision_id, "exclude"))
        correction = run(cyclotron.review(decision.decision_id, "include", explanation="Misread it."))
        assert correction.kind == "correction"
        status = cyclotron.status()
    assert status.alignment.labels == 1 and status.alignment.accuracy == 1.0


def test_undo_reopens_the_decision_for_review(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        run(cyclotron.review(decision.decision_id, "exclude"))
        undone = run(cyclotron.undo_review(decision.decision_id))
        assert undone.kind == "undo"
        assert cyclotron.status().alignment.labels == 0
        assert cyclotron.status().pending.decisions_awaiting_review == 1
        assert run(cyclotron.review(decision.decision_id, "include")).kind == "label"
        assert cyclotron.status().alignment.accuracy == 1.0
        with pytest.raises(ValueError, match="no label"):
            run(cyclotron.undo_review(run(cyclotron.decide(item("ref-2"))).decision_id))


def test_a_review_without_a_label_closes_the_decision_without_teaching(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        review = run(cyclotron.review(decision.decision_id, None, reason_code="duplicate"))
        assert review.kind == "no-label"
        status = cyclotron.status()
        assert status.alignment.labels == 0 and status.pending.decisions_awaiting_review == 0
        assert run(cyclotron.decide(item("ref-1"))) == decision


def test_a_review_identity_makes_resubmission_safe(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        first = run(cyclotron.review(decision.decision_id, "include", review_id="message-1"))
        assert run(cyclotron.review(decision.decision_id, "include", review_id="message-1")) == first
        with pytest.raises(ValueError, match="different content"):
            run(cyclotron.review(decision.decision_id, "exclude", review_id="message-1"))
        assert cyclotron.status().alignment.labels == 1


def test_invalid_reviews_are_refused_before_anything_is_recorded(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        with pytest.raises(ValueError):
            run(cyclotron.review(decision.decision_id, "maybe"))
        with pytest.raises(ValueError, match="selected_by"):
            run(cyclotron.review(decision.decision_id, "include", selected_by="robot"))
        with pytest.raises(KeyError):
            run(cyclotron.review("no-such-decision", "include"))
        assert cyclotron.subscribe()["events"][-1]["kind"] == "decision"


def test_subscribe_pages_committed_events_with_a_cursor_that_survives_restart(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        run(cyclotron.review(decision.decision_id, "include", reviewer="editor-1"))
        page = cyclotron.subscribe(after=0, limit=1)
        assert [e["kind"] for e in page["events"]] == ["decision"]
        assert page["events"][0]["decision"]["decisionId"] == decision.decision_id
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        rest = cyclotron.subscribe(after=page["cursor"])
        assert [e["kind"] for e in rest["events"]] == ["review"]
        assert rest["events"][0]["review"]["reviewer"] == "editor-1"
        assert cyclotron.subscribe(after=rest["cursor"])["events"] == []


def test_a_store_refuses_a_different_definition(tmp_path):
    Cyclotron.open(tmp_path / "c", DEFINITION, Model()).close()
    changed = CyclotronDefinition("papyrus-relevance", (RELEVANT,), seed="other-seed")
    with pytest.raises(ValueError, match="different cyclotron definition"):
        Cyclotron.open(tmp_path / "c", changed, Model())


def test_several_classifiers_share_one_request_and_are_reviewed_separately(tmp_path):
    timely = ClassifierSpec("timely", ("include", "exclude"), "Is this timely?")
    definition = CyclotronDefinition("newsroom", (RELEVANT, timely))
    model = Model()
    with Cyclotron.open(tmp_path / "c", definition, model) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        assert set(decision.results) == {"relevant", "timely"}
        assert model.calls == 1
        with pytest.raises(ValueError, match="several"):
            decision.label
        run(cyclotron.review(decision.decision_id, "include", classifier="relevant"))
        assert cyclotron.status("relevant").alignment.labels == 1
        assert cyclotron.status("timely").pending.decisions_awaiting_review == 1


def test_the_request_ceiling_applies_to_each_open(tmp_path):
    model = Model()
    with Cyclotron.open(tmp_path / "c", DEFINITION, model, max_requests=1) as cyclotron:
        run(cyclotron.decide(item("ref-1", "yes one")))
        with pytest.raises(RuntimeError, match="ceiling"):
            run(cyclotron.decide(item("ref-2", "yes two")))
    with Cyclotron.open(tmp_path / "c", DEFINITION, model, max_requests=1) as cyclotron:
        run(cyclotron.decide(item("ref-2", "yes two")))
    assert model.calls == 2


def test_reviews_drive_the_existing_learning_loop_when_an_optimizer_is_supplied(tmp_path):
    definition = CyclotronDefinition("papyrus-relevance", (RELEVANT,), seed="spec-seed", optimize_every=2,
                                     rubric_changes_every=1)
    events = []
    with Cyclotron.open(tmp_path / "c", definition, Model(), agent([]), max_requests=50,
                        observer=events.append) as cyclotron:
        for index, (text, label) in enumerate([("yes a", "include"), ("no b", "exclude"),
                                               ("yes c", "exclude"), ("no d", "include")]):
            decision = run(cyclotron.decide(item(f"learn-{index}", text)))
            run(cyclotron.review(decision.decision_id, label, explanation=f"reason {index}"))
        history = cyclotron.wheels["relevant"].history(100000)
    assert any(e["kind"] == "trigger-evaluated" and e["stage"] == "rubric" for e in history)
    assert any(e["kind"] == "cycle-metrics" for e in history)
    assert [e["kind"] for e in events if e["kind"] in ("decision", "review")][:2] == ["decision", "review"]


def test_a_promoted_version_reaches_subscribers_and_the_status(tmp_path):
    from .optimizer_agent import OptimizerAgent, OptimizerReply
    optimizer = OptimizerAgent(lambda _: OptimizerReply(
        '{"rationale":"Editors include items that say yes","rubric":"Include items that say yes."}', "fake-optimizer"))
    definition = CyclotronDefinition("papyrus-relevance", (RELEVANT,), seed="spec-seed", optimize_every=1000,
                                     rubric_changes_every=1)
    with Cyclotron.open(tmp_path / "c", definition, Model(), optimizer, max_requests=500) as cyclotron:
        for index in range(16):
            decision = run(cyclotron.decide(item(f"learn-{index}", ("yes " if index % 2 else "no ") + str(index))))
            run(cyclotron.review(decision.decision_id, "include" if index % 2 else "exclude"))
        promoted = [e for e in cyclotron.subscribe(limit=1000)["events"] if e["kind"] == "promoted"]
        status = cyclotron.status()
        latest = run(cyclotron.decide(item("after", "yes after")))
    assert promoted and promoted[0]["summary"] == "Changed the rubric."
    assert status.version == promoted[-1]["version"] and status.last_change.kind == "promoted"
    assert latest.version == status.version
