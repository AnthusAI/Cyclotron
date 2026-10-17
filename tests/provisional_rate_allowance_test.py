import pytest

from decision_flywheel.selection_policy import SelectionPolicy
from decision_flywheel.staged_optimization import provisional_primary_allowance


def _metrics(recall):
    return {"per_class": {"publish": {"precision": 0.5, "recall": recall}}}


@pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf"), True, "0.1"])
def test_rate_allowance_rejects_invalid(bad):
    with pytest.raises(ValueError):
        SelectionPolicy(primary="recall", positive_class="publish", provisional_rate_allowance=bad)


def test_rate_allowance_accepts_none_and_numbers():
    assert SelectionPolicy().provisional_rate_allowance is None
    policy = SelectionPolicy(primary="recall", positive_class="publish", provisional_rate_allowance=0)
    assert policy.provisional_rate_allowance == 0
    assert (
        SelectionPolicy(primary="recall", positive_class="publish", provisional_rate_allowance=0.02).compare(
            _metrics(0.8), _metrics(0.8)
        )["policy"]["provisional_rate_allowance"]
        == 0.02
    )


def test_zero_cap_blocks_lower_recall_but_default_accepts():
    incumbent, candidate = _metrics(0.8), _metrics(0.6)
    capped = SelectionPolicy(primary="recall", positive_class="publish", provisional_rate_allowance=0.0)
    assert not capped.compare(incumbent, candidate, primary_allowance=provisional_primary_allowance(capped, 2.0))[
        "provisional_eligible"
    ]
    default = SelectionPolicy(primary="recall", positive_class="publish")
    assert default.compare(incumbent, candidate, primary_allowance=provisional_primary_allowance(default, 2.0))[
        "provisional_eligible"
    ]


def test_provisional_primary_allowance():
    assert provisional_primary_allowance(SelectionPolicy(primary="brier"), 2.0) == 2.0
    assert provisional_primary_allowance(SelectionPolicy(primary="balanced_brier", provisional_rate_allowance=0.0), 2.0) == 2.0
    assert provisional_primary_allowance(SelectionPolicy(primary="accuracy"), 2.0) == 1.0
    assert (
        provisional_primary_allowance(
            SelectionPolicy(primary="recall", positive_class="publish", provisional_rate_allowance=0.02), 2.0
        )
        == 0.02
    )
    assert (
        provisional_primary_allowance(
            SelectionPolicy(primary="recall", positive_class="publish", provisional_rate_allowance=0.02), 0.01
        )
        == 0.005
    )
