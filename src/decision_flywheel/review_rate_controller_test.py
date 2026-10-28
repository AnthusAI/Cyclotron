"""The review-rate controller chooses what goes to review and keeps the operator's override."""

from datetime import datetime, timedelta, timezone

import pytest

from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import DEFINITION, Model, item, run
from .embedded_review_rate_test import FAST


def test_a_cyclotron_without_a_program_sends_every_decision_to_review_with_full_propensity(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=None) as cyclotron:
        selection = cyclotron.review_rate.select("any-item", {"relevant": 0.99})
        assert selection.selected and selection.reason == "program" and selection.propensity == 1.0
        with pytest.raises(ValueError, match="no review-rate program"):
            cyclotron.review_rate.set_rate(0.5, set_by="editor")


def test_the_controller_stores_its_state_in_the_root_store_and_clearing_an_absent_override_changes_nothing(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        cyclotron.review_rate.clear_rate(set_by="editor")
        assert cyclotron.subscribe()["events"] == []
        expires = datetime.now(timezone.utc) + timedelta(days=1)
        cyclotron.review_rate.set_rate(0.3, set_by="editor", expires_at=expires)
        assert cyclotron.review_rate.state().override["set_by"] == "editor"
        assert cyclotron.root_storage.meta("review_program_state") is not None
        cyclotron.review_rate.clear_rate(set_by="editor")
        assert cyclotron.review_rate.state().override is None
        assert [event["kind"] for event in cyclotron.subscribe()["events"]] == ["review-rate-changed"] * 2


def test_a_decision_advances_the_program_through_the_controller(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        decision = run(cyclotron.decide(item("first", "first")))
        assert decision.review.selected
        assert cyclotron.review_rate.status() == cyclotron.status().review_rate


def test_expected_weekly_reviews_count_the_last_weeks_decisions_without_reading_every_decision(tmp_path):
    import json

    from .embedded_review_rate_test import Model as ScriptedConfidence
    from .review_program import expected_item_reviews

    with Cyclotron.open(tmp_path / "c", DEFINITION, ScriptedConfidence(), review_program=FAST) as cyclotron:
        for index, text in enumerate(("sure", "low", "sure", "sure", "low")):
            run(cyclotron.decide(item(f"i-{index}", f"{text} {index}")))
        cyclotron.review_rate.set_rate(0.0, set_by="editor")
        threshold, sampled = FAST.confidence_threshold, max(0.0, FAST.audit_share)
        every = [json.loads(payload) for (payload,) in cyclotron.root_storage.decision_payloads()]
        scanned = [tuple(r["confidence"] for r in d["classifiers"].values()) for d in every]
        assert sum(c < threshold for (c,) in scanned) == 2 and sampled > 0

        def refuse():
            raise AssertionError("status read every decision")

        cyclotron.root_storage.decision_payloads = refuse
        assert cyclotron.review_rate.status().expected_reviews_per_week == expected_item_reviews(FAST, 0.0, scanned)
        cyclotron.review_rate.clock = lambda: datetime.now(timezone.utc) + timedelta(days=8)
        assert cyclotron.review_rate.status().expected_reviews_per_week == 0.0
