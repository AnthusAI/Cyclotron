import pytest

from .iteration_acceptance import STRICT_AFTER_VOTES, accept_iteration, sign_test_p
from .vote_examples import Vote


def votes(count, *, label="include"):
    return [Vote("site", f"item-{n}", {"text": f"story {n}"}, label) for n in range(count)]


def said(rows, *, right, wrong=0):
    """Labels that are right on the first ``right`` rows, wrong on the next ``wrong``, and right after that."""
    other = {"include": "exclude", "exclude": "include"}
    return {
        vote.item_id: other[vote.label] if right <= index < right + wrong else vote.label for index, vote in enumerate(rows)
    }


def test_the_sign_test_is_two_sided_and_certain_when_nothing_differs():
    assert sign_test_p(0, 0) == 1.0
    assert sign_test_p(2, 15) == pytest.approx(0.00235, abs=1e-4)
    assert sign_test_p(15, 2) == sign_test_p(2, 15)


def test_a_young_cyclotron_accepts_an_iteration_with_no_evidence_either_way():
    rows = votes(10)
    decision = accept_iteration(rows, said(rows, right=10), said(rows, right=10), total_votes=10)
    assert decision["accepted"]
    assert decision["phase"] == "accept-unless-significantly-worse"
    assert (decision["candidateRight"], decision["incomingRight"]) == (0, 0)


def test_a_young_cyclotron_accepts_an_iteration_that_is_worse_but_not_significantly():
    rows = votes(10)
    decision = accept_iteration(rows, said(rows, right=10), said(rows, right=8, wrong=2), total_votes=10)
    assert decision["accepted"]
    assert decision["incomingRight"] == 2


def test_a_young_cyclotron_refuses_an_iteration_that_is_significantly_worse():
    rows = votes(10)
    decision = accept_iteration(rows, said(rows, right=10), said(rows, right=2, wrong=8), total_votes=10)
    assert not decision["accepted"]
    assert decision["p"] < 0.05


def test_a_young_cyclotron_accepts_an_iteration_when_nothing_could_be_scored():
    decision = accept_iteration(votes(3), {}, {}, total_votes=3)
    assert decision["accepted"]
    assert decision["compared"] == 0


def test_a_mature_cyclotron_requires_a_measurable_improvement():
    rows = votes(10)
    decision = accept_iteration(rows, said(rows, right=10), said(rows, right=10), total_votes=STRICT_AFTER_VOTES)
    assert decision["phase"] == "require-improvement"
    assert not decision["accepted"]


def test_a_mature_cyclotron_refuses_a_gain_too_small_to_tell_from_chance():
    rows = votes(10)
    decision = accept_iteration(rows, said(rows, right=8, wrong=2), said(rows, right=10), total_votes=STRICT_AFTER_VOTES)
    assert decision["candidateRight"] == 2
    assert not decision["accepted"]


def test_a_mature_cyclotron_accepts_a_significant_improvement():
    rows = votes(10)
    decision = accept_iteration(rows, said(rows, right=2, wrong=8), said(rows, right=10), total_votes=STRICT_AFTER_VOTES)
    assert decision["accepted"]


def test_only_items_decided_both_ways_are_compared():
    rows = votes(4)
    incoming = said(rows, right=4)
    candidate = {key: label for key, label in incoming.items() if key != "item-0"}
    assert accept_iteration(rows, incoming, candidate, total_votes=4)["compared"] == 3


def test_one_sites_votes_never_mix_with_anothers():
    mixed = votes(1) + [Vote("other", "item-9", {"text": "x"}, "include")]
    with pytest.raises(ValueError, match="per site"):
        accept_iteration(mixed, {}, {}, total_votes=2)


def test_the_decision_records_the_rule_it_applied():
    rows = votes(2)
    decision = accept_iteration(rows, said(rows, right=2), said(rows, right=2), total_votes=2)
    assert decision["rule"] == {"test": "paired-sign-two-sided", "significance": 0.05, "strictAfterVotes": STRICT_AFTER_VOTES}
    assert decision["totalVotes"] == 2
