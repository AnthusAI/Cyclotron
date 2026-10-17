import pytest

from .routing_metrics import Decision, least_confident_capture, routing, threshold_rule


def rows(*values):
    return [Decision("publish", confidence, "publish" if right else "reject") for confidence, right in values]


def test_the_rule_counts_approvals_promises_and_the_whole_system_with_reviewed_items_right():
    window = rows((0.95, True), (0.9, False), (0.8, True), (0.79, False), (0.5, True))
    rule = {row["threshold"]: row for row in threshold_rule(window)}
    at = rule[0.8]
    assert (at["approved"], at["approved_right"], at["to_reviewers"]) == (3, 2, 2)
    assert at["promised"] == pytest.approx((0.95 + 0.9 + 0.8) / 3)
    assert at["got"] == pytest.approx(2 / 3)
    assert (at["whole_system_right"], at["whole_system_accuracy"]) == (4, 0.8)
    assert rule[0.95]["approved"] == 1 and rule[0.75]["approved"] == 4
    assert sorted(rule) == [0.75, 0.8, 0.85, 0.9, 0.95]


def test_the_least_confident_slice_credits_ties_at_the_cut_in_proportion():
    window = rows(*[(0.2, False), (0.5, False), (0.5, True), (0.5, True)] + [(0.9, True)] * 6)
    capture = {row["percent"]: row for row in least_confident_capture(window, (10, 20, 30))}
    assert capture[10]["mistakes_caught"] == 1 and capture[10]["share_of_mistakes"] == 0.5
    assert capture[20]["mistakes_caught"] == pytest.approx(1 + 1 / 3)
    assert capture[20]["tied_at_cut"] == 3
    assert capture[30]["share_of_mistakes"] == pytest.approx((1 + 2 / 3) / 2)


def test_a_window_without_mistakes_or_approvals_reports_none_rather_than_dividing_by_zero():
    result = routing(rows((0.3, True)))
    assert result["least_confident"][0]["share_of_mistakes"] is None
    assert result["threshold_rule"][0]["promised"] is None
    with pytest.raises(ValueError):
        threshold_rule([Decision("publish", 1.5, "publish")])
