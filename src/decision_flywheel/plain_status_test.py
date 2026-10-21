"""Editors read plain sentences, never the engine's own vocabulary."""

import pytest

from .plain_status import plain_change_summary, plain_next_step, plain_reason

JARGON = ("calibration", "version", "refit", "onboarding", "prim", "cyclotron", "window", "gap", "fingerprint", "rubric")
STATES = ("full", "onboarding", "tapering", "steady", "raised", "manual")


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("rate", (1.0, 0.25))
def test_every_plain_review_sentence_avoids_engine_vocabulary(state, rate):
    for text in (plain_reason(state, rate, "editor-7"), plain_next_step(state, rate)):
        assert text and not any(word in text.lower() for word in JARGON)


def test_a_manual_rate_names_who_set_it():
    assert plain_reason("manual", 0.5, "editor-7") == "The review rate was set to 50% by editor-7."


def test_a_steady_rate_says_how_much_still_needs_review():
    assert "25%" in plain_reason("steady", 0.25)


@pytest.mark.parametrize("kind", ("promoted", "refit", "dropped", "definition"))
def test_every_plain_change_summary_avoids_engine_vocabulary(kind):
    text = plain_change_summary(kind, 12)
    assert not any(word in text.lower() for word in JARGON)


def test_a_refit_reports_how_many_reviews_it_learned_from():
    assert plain_change_summary("refit", 12) == "Learned from 12 reviews."
