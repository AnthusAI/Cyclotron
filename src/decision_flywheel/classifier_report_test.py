"""A newsroom's report scores votes honestly and says what it cannot know."""

from .classifier_report import agreement


def test_a_vote_with_no_recorded_decision_is_counted_but_not_compared():
    votes = [
        {"output": "o", "label": "yes", "classifierRevision": 1, "day": "2026-11-01", "decided": None},
        {"output": "o", "label": "yes", "classifierRevision": 1, "day": "2026-11-02", "decided": "yes"},
        {"output": "o", "label": "no", "classifierRevision": 2, "day": "2026-11-02", "decided": "yes"},
    ]
    scores = agreement(votes)
    assert (scores["votes"], scores["compared"], scores["agreed"]) == (3, 2, 1)
    assert scores["byDay"][0] == {"day": "2026-11-01", "votes": 1, "compared": 0, "agreed": 0}
    assert scores["byClassifierRevision"][1] == {"classifierRevision": 2, "votes": 1, "compared": 1, "agreed": 0}
