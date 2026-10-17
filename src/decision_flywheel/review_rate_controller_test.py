"""The review-rate controller chooses what goes to review and keeps the operator's override."""
from datetime import datetime, timedelta, timezone

import pytest

from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import DEFINITION, Model, item, run
from .embedded_review_rate_test import FAST


def test_a_cyclotron_without_a_program_sends_every_decision_to_review_with_full_propensity(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=None) as cyclotron:
        selection = cyclotron.review_rate.select("any-item", {"relevant": .99})
        assert selection.selected and selection.reason == "program" and selection.propensity == 1.0
        with pytest.raises(ValueError, match="no review-rate program"):
            cyclotron.review_rate.set_rate(.5, set_by="editor")


def test_the_controller_stores_its_state_in_the_root_store_and_clearing_an_absent_override_changes_nothing(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        cyclotron.review_rate.clear_rate(set_by="editor")
        assert cyclotron.subscribe()["events"] == []
        expires = datetime.now(timezone.utc) + timedelta(days=1)
        cyclotron.review_rate.set_rate(.3, set_by="editor", expires_at=expires)
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
