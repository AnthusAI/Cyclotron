import pytest

from .routing_metrics import Decision, least_confident_capture, routing, threshold_rule


def rows(*values):
    return [Decision("publish", confidence, "publish" if right else "reject") for confidence, right in values]


def test_the_rule_counts_approvals_promises_and_the_whole_system_with_reviewed_items_right():
    window = rows((.95, True), (.9, False), (.8, True), (.79, False), (.5, True))
    rule = {row["threshold"]: row for row in threshold_rule(window)}
    at = rule[.8]
    assert (at["approved"], at["approved_right"], at["to_reviewers"]) == (3, 2, 2)
    assert at["promised"] == pytest.approx((.95 + .9 + .8) / 3)
    assert at["got"] == pytest.approx(2 / 3)
    assert (at["whole_system_right"], at["whole_system_accuracy"]) == (4, .8)
    assert rule[.95]["approved"] == 1 and rule[.75]["approved"] == 4
    assert sorted(rule) == [.75, .8, .85, .9, .95]


def test_the_least_confident_slice_credits_ties_at_the_cut_in_proportion():
    window = rows(*[(.2, False), (.5, False), (.5, True), (.5, True)] + [(.9, True)] * 6)
    capture = {row["percent"]: row for row in least_confident_capture(window, (10, 20, 30))}
    assert capture[10]["mistakes_caught"] == 1 and capture[10]["share_of_mistakes"] == .5
    assert capture[20]["mistakes_caught"] == pytest.approx(1 + 1 / 3)
    assert capture[20]["tied_at_cut"] == 3
    assert capture[30]["share_of_mistakes"] == pytest.approx((1 + 2 / 3) / 2)


def test_a_window_without_mistakes_or_approvals_reports_none_rather_than_dividing_by_zero():
    result = routing(rows((.3, True)))
    assert result["least_confident"][0]["share_of_mistakes"] is None
    assert result["threshold_rule"][0]["promised"] is None
    with pytest.raises(ValueError):
        threshold_rule([Decision("publish", 1.5, "publish")])
