"""Evaluation clock and trace behaviour that SQLite and S3 must share, run against both back ends."""
import json

import pytest


@pytest.fixture
def storage(evaluation_backend):
    return evaluation_backend.open()


def trace_payload(number):
    return json.dumps({'kind': 'step', 'number': number})


def test_the_first_clock_for_a_plan_wins_and_later_proposals_see_it(storage):
    assert storage.bind_clock('plan-a', '2026-01-01') == '2026-01-01'
    assert storage.bind_clock('plan-a', '2030-01-01') == '2026-01-01'
    assert storage.bind_clock('plan-b', '2027-01-01') == '2027-01-01'


def test_a_trace_identity_keeps_its_first_payload_and_position(storage):
    first = storage.append_trace('step-1', trace_payload(1))
    second = storage.append_trace('step-2', trace_payload(2))
    repeated = storage.append_trace('step-1', trace_payload(99))
    assert (first[1], second[1]) == (trace_payload(1), trace_payload(2))
    assert first[0] < second[0]
    assert tuple(repeated) == tuple(first)


def test_trace_events_page_in_order_after_a_cursor(storage):
    identifiers = [storage.append_trace(f'step-{number}', trace_payload(number))[0] for number in range(1, 5)]
    assert [identifier for identifier, _ in storage.events(limit=3)] == identifiers[:3]
    assert [identifier for identifier, _ in storage.events(after=identifiers[1], limit=10)] == identifiers[2:]
    assert storage.events(after=identifiers[-1]) == []
    assert storage.events(limit=1)[0][1] == trace_payload(1)


def test_trace_cursors_must_be_bounded(storage):
    for bad_after, bad_limit in ((-1, 10), (0, 0), (0, 1001), ('0', 10), (0, '10')):
        with pytest.raises(ValueError):
            storage.events(after=bad_after, limit=bad_limit)


def test_clock_and_trace_survive_reopening_the_same_store(evaluation_backend):
    storage = evaluation_backend.open()
    storage.bind_clock('plan-a', '2026-01-01')
    identifier, _ = storage.append_trace('step-1', trace_payload(1))
    reopened = evaluation_backend.reopen(storage)
    assert reopened.bind_clock('plan-a', '2030-01-01') == '2026-01-01'
    assert reopened.events() == [(identifier, trace_payload(1))]
    assert reopened.append_trace('step-1', trace_payload(2))[0] == identifier
    assert reopened.append_trace('step-2', trace_payload(2))[0] > identifier
