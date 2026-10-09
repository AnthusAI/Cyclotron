"""Specs for allowlisted programmatic decision context."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from .context import RandomBalanced
from .dynamic_elements import current_datetime_state, optimizer_dynamic_element_instruction, with_dynamic_state
from .feedback import Element, Cyclotron


HASH = "a" * 64


def test_current_datetime_is_injected_as_an_explicit_utc_value_only_when_selected():
    policy = RandomBalanced(1)
    base = Cyclotron("review", 1, (Element("topic", "choice", ("topic",), HASH),), policy.fingerprint)
    with_datetime = Cyclotron(
        "review", 2,
        (Element("topic", "choice", ("topic",), HASH),
         Element("current_datetime", "programmatic_datetime", ("current_datetime",), HASH)),
        policy.fingerprint, base.fingerprint,
    )
    now = datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)

    assert current_datetime_state(base, now) == {}
    assert current_datetime_state(with_datetime, now) == {"current_datetime": "2026-10-05T14:30:00Z"}
    assert with_dynamic_state({"target": {"text": "An article"}}, with_datetime, now) == {
        "target": {"text": "An article"}, "current_datetime": "2026-10-05T14:30:00Z"
    }


def test_current_datetime_refuses_an_ambiguous_naive_clock():
    policy = RandomBalanced(1)
    cyclotron = Cyclotron("review", 1, (Element("current_datetime", "programmatic_datetime",
                                                   ("current_datetime",), HASH),), policy.fingerprint)

    with pytest.raises(ValueError, match="timezone-aware"):
        current_datetime_state(cyclotron, datetime(2026, 10, 5, 14, 30))


def test_optimizer_instruction_describes_the_only_dynamic_element_and_its_exact_proposal_shape():
    instruction = optimizer_dynamic_element_instruction()

    assert "current_datetime" in instruction
    assert "add_programmatic_element" in instruction
