"""The review-rate program chooses before the label exists and moves only on evidence."""
from datetime import datetime, timedelta, timezone

import pytest

from .review_program import (Override, ProgramState, ReviewProgram, check_window, current_rate, next_step,
                             raise_for_version, select, state_name, window_evidence)

PROGRAM = ReviewProgram(window=100, min_window_labels=20)
NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def met():
    return {"labels": 40, "accuracy": .9, "gap_points": 3, "enough_evidence": True, "met": True}


def failed():
    return {"labels": 40, "accuracy": .7, "gap_points": 3, "enough_evidence": True, "met": False}


@pytest.mark.parametrize("kwargs", [{"rates": (.5, .25)}, {"rates": (1., 1.)}, {"rates": (1., .5, .6)},
                                    {"audit_share": 1.5}, {"window": 0}, {"seed": ""}])
def test_the_program_refuses_impossible_configuration(kwargs):
    with pytest.raises(ValueError):
        ReviewProgram(**kwargs)


def test_every_number_is_configuration():
    custom = ReviewProgram(rates=(1., .3), audit_share=.1, confidence_threshold=.8, target_accuracy=.9,
                           max_gap_points=3, window=50, windows_required=3, calibration_window=100, min_window_labels=10)
    assert custom.manifest()["rates"] == (1., .3) and custom.manifest()["windows_required"] == 3


def test_low_confidence_is_always_reviewed_with_certainty():
    selection = select(PROGRAM, .1, key="d-1", confidence=.55)
    assert (selection.selected, selection.reason, selection.propensity) == (True, "program", 1.0)
    assert "below the 70% threshold" in selection.detail


def test_confident_decisions_are_sampled_at_the_rate_with_an_audit_share():
    choices = [select(PROGRAM, .25, key=f"d-{i}", confidence=.9) for i in range(4000)]
    selected = [c for c in choices if c.selected]
    audit = [c for c in choices if c.reason == "audit"]
    assert 0.22 < len(selected) / 4000 < 0.28
    assert 0.035 < len(audit) / 4000 < 0.065
    assert {c.propensity for c in choices} == {.25}
    assert select(PROGRAM, .25, key="d-7", confidence=.9) == choices[7]  # deterministic


def test_the_audit_share_remains_when_the_rate_is_zero():
    choices = [select(PROGRAM, 0., key=f"d-{i}", confidence=.95) for i in range(2000)]
    assert {c.reason for c in choices if c.selected} == {"audit"}
    assert {c.propensity for c in choices} == {.05}


def test_two_windows_met_in_a_row_step_down_one_notch():
    state, change = check_window(PROGRAM, ProgramState(), met())
    assert change is None and state.consecutive == 1 and state.index == 0
    state, change = check_window(PROGRAM, state, met())
    assert change == "stepped-down" and state.index == 1 and current_rate(PROGRAM, state, NOW) == .5
    assert state_name(PROGRAM, state, NOW) == "tapering" and "Stepped down to 50%" in state.reason


def test_a_failed_window_goes_back_to_full_review():
    state = ProgramState(index=2)
    state, change = check_window(PROGRAM, state, failed())
    assert change == "raised" and state.index == 0 and state.raised
    assert state_name(PROGRAM, state, NOW) == "raised" and "below the 85% target" in state.reason


def test_too_few_labels_hold_the_rate():
    state = ProgramState(index=1, consecutive=1)
    state, change = check_window(PROGRAM, state, {**met(), "labels": 3, "enough_evidence": False, "met": False})
    assert change is None and state.index == 1 and state.consecutive == 1 and "only 3 reviewed" in state.reason


def test_the_floor_is_steady():
    state = ProgramState(index=3, consecutive=1)
    state, change = check_window(PROGRAM, state, met())
    assert change is None and state.index == 3 and state_name(PROGRAM, state, NOW) == "steady"


def test_a_promoted_version_goes_back_to_full_review():
    state, raised = raise_for_version(PROGRAM, ProgramState(index=2, version=1), 2)
    assert raised and state.index == 0 and state.version == 2 and "version 2 was promoted" in state.reason
    state, raised = raise_for_version(PROGRAM, state, 2)
    assert not raised


def test_window_evidence_uses_confident_reviews_and_the_calibration_gap():
    reviews = ([{"confidence": .9, "decision": "include", "label": "include"}] * 18
               + [{"confidence": .9, "decision": "include", "label": "exclude"}] * 2
               + [{"confidence": .5, "decision": "include", "label": "exclude"}] * 10)
    evidence = window_evidence(PROGRAM, reviews, 3)
    assert evidence == {"labels": 20, "accuracy": .9, "gap_points": 3, "enough_evidence": True, "met": True}
    assert not window_evidence(PROGRAM, reviews, 6)["met"]


def test_an_override_applies_until_it_expires():
    state = ProgramState(index=1, override={"rate": 0., "expires_at": (NOW + timedelta(days=1)).isoformat(),
                                            "set_by": "editor", "set_at": NOW.isoformat()})
    assert current_rate(PROGRAM, state, NOW) == 0. and state_name(PROGRAM, state, NOW) == "manual"
    later = NOW + timedelta(days=2)
    assert current_rate(PROGRAM, state, later) == .5 and state_name(PROGRAM, state, later) == "tapering"
    assert not Override(**state.override).active(later)


def test_the_next_step_is_written_in_words():
    text = next_step(PROGRAM, ProgramState(index=1, consecutive=1), decisions_until_check=37, now=NOW)
    assert text == ("Steps down to 25% after 1 more window of 100 decisions with accuracy on confident decisions "
                    "of at least 85% (at least 20 reviewed) and a calibration gap under 5 points. Next check in 37 decisions.")
