"""Specs for the reusable, restart-safe flywheel run ledger."""
from __future__ import annotations

import pytest

from .models import DecisionTask, Item, LabeledItem
from .run_ledger import FlywheelRound, JsonlRunLedger, TrialActivity, feedback_fingerprint


def _round(*, input_fingerprint: str = "a" * 64, policy_fingerprint: str = "b" * 64) -> FlywheelRound:
    return FlywheelRound(
        task_fingerprint="c" * 64,
        input_fingerprint=input_fingerprint,
        active_policy_fingerprint=policy_fingerprint,
        model_fingerprint="jev:example",
        feedback_count=12,
        candidate_count=8,
        development_count=4,
        objective_name="brier",
        winner="incumbent",
        promoted=False,
        outcome="incumbent-retained",
        calls_attempted=8,
        calls_succeeded=8,
        trials=(TrialActivity("incumbent", "completed", 4, 0, .2),),
    )


def test_a_run_ledger_survives_restart_and_deduplicates_the_same_measured_round(tmp_path):
    path = tmp_path / "runs.jsonl"
    first = JsonlRunLedger(path)
    round_ = _round()

    assert first.append(round_) is True
    assert first.append(round_) is False

    restarted = JsonlRunLedger(path)
    assert restarted.history() == (round_,)
    assert restarted.status("a" * 64).phase == "current"


def test_a_run_ledger_tells_a_ui_when_new_feedback_makes_its_policy_stale(tmp_path):
    ledger = JsonlRunLedger(tmp_path / "runs.jsonl")
    recorded = _round(input_fingerprint="a" * 64)
    ledger.append(recorded)

    status = ledger.status("d" * 64)

    assert status.phase == "stale"
    assert status.latest == recorded


def test_a_run_ledger_rejects_malformed_or_text_bearing_records(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text('{"not":"a round"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="run ledger"):
        JsonlRunLedger(path).history()

    with pytest.raises(ValueError, match="outcome"):
        FlywheelRound(
            task_fingerprint="c" * 64, input_fingerprint="a" * 64,
            active_policy_fingerprint="b" * 64, model_fingerprint="jev:example",
            feedback_count=1, candidate_count=1, development_count=0, objective_name="brier",
            winner="incumbent", promoted=False, outcome="the user wrote this explanation",
            calls_attempted=0, calls_succeeded=0, trials=(),
        )


def test_feedback_fingerprint_changes_for_a_new_label_without_retaining_its_text():
    task = DecisionTask("decision", ("include", "exclude"), "Classify the target.")
    first = (LabeledItem(Item("one", {"text": "private article text"}), "include"),)
    second = first + (LabeledItem(Item("two", {"text": "another private article"}), "exclude"),)

    assert feedback_fingerprint(task, first) != feedback_fingerprint(task, second)
