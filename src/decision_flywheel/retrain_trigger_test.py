import pytest

from .retrain_trigger import STARTING_REQUIREMENT, LiveDecision, new_votes, next_requirement, retrain_due, vote_weight
from .vote_examples import Vote


def vote(n, label="exclude", comment="", output="site"):
    return Vote(output, f"item-{n}", {"text": str(n)}, label, comment)


def wrong(count, *, confidence=1.0, comment=""):
    votes = [vote(n, comment=comment) for n in range(count)]
    return votes, {v.item_id: LiveDecision("include", confidence) for v in votes}


def test_a_vote_agreeing_with_a_confident_decision_teaches_nothing():
    assert vote_weight(vote(1), LiveDecision("exclude", 0.99)) == 0.0


def test_a_confident_mistake_outweighs_an_unsure_one_and_an_explanation_adds_to_either():
    unsure = vote_weight(vote(1), LiveDecision("include", 0.5))
    confident = vote_weight(vote(1), LiveDecision("include", 0.99))
    assert confident > unsure
    assert vote_weight(vote(1, comment="not about machines"), LiveDecision("include", 0.5)) > unsure


def test_a_blank_explanation_adds_nothing():
    assert vote_weight(vote(1, comment="  "), LiveDecision("include", 0.5)) == vote_weight(vote(1), LiveDecision("include", 0.5))


def test_any_number_of_agreeing_votes_never_triggers_a_retrain():
    votes = [vote(n) for n in range(1000)]
    decision = retrain_due(votes, {v.item_id: LiveDecision("exclude", 0.99) for v in votes}, seen={})
    assert not decision["due"]
    assert decision["disagreements"] == 0


def test_enough_evidence_of_mistakes_triggers_a_retrain():
    votes, decisions = wrong(3)
    decision = retrain_due(votes, decisions, seen={})
    assert decision["evidence"] == 6.0 >= STARTING_REQUIREMENT
    assert decision["due"]


def test_too_little_evidence_keeps_waiting():
    votes, decisions = wrong(1)
    decision = retrain_due(votes, decisions, seen={})
    assert not decision["due"]
    assert decision["reason"] == "waiting for more evidence of mistakes"


def test_votes_already_learned_from_count_nothing_but_a_changed_vote_counts_again():
    votes, decisions = wrong(3)
    learned = {v.item_id: v.label for v in votes}
    assert retrain_due(votes, decisions, seen=learned)["newVotes"] == 0
    changed = {**learned, "item-0": "include"}
    assert [v.item_id for v in new_votes(votes, changed)] == ["item-0"]


def test_accuracy_confidently_below_the_claim_triggers_before_the_requirement_is_met():
    agreeing = [vote(n, label="include") for n in range(2, 12)]
    votes, decisions = wrong(12)
    votes = votes[:2] + agreeing
    decision = retrain_due(votes, decisions | {}, seen={}, requirement=1000, claimed_accuracy=1.0)
    assert decision["accuracyUpperBound"] < 1.0
    assert decision["due"]


def test_a_retrain_that_gained_little_raises_the_requirement_and_one_that_gained_a_lot_lowers_it():
    assert next_requirement(6.0, 0) > 6.0
    assert next_requirement(6.0, 1) == 6.0
    assert next_requirement(6.0, 5) < 6.0


def test_one_sites_votes_never_mix_with_anothers():
    with pytest.raises(ValueError, match="per site"):
        retrain_due([vote(1), vote(2, output="other")], {}, seen={})
