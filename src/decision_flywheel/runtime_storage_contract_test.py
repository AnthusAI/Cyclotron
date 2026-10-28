"""Runtime state and evidence behaviour that SQLite and S3 must share, run against both back ends."""

import json

import pytest

from .active_labels import FEEDBACK_EVENT_KIND


@pytest.fixture
def storage(runtime_backend):
    return runtime_backend.open()


def feedback_event(item_id, label, **extra):
    return json.dumps(
        {
            "kind": FEEDBACK_EVENT_KIND,
            "action": "submitted",
            "feedback": {"item_id": item_id, "final_answer_value": label},
            **extra,
        }
    )


def test_state_round_trips_and_replaces_by_key(storage):
    assert storage.state("context") is None
    storage.put_state("context", '{"v":1}')
    storage.put_state("context", '{"v":2}')
    assert storage.state("context") == '{"v":2}'


def test_state_and_events_refuse_text_that_is_not_json(storage):
    with pytest.raises(ValueError):
        storage.put_state("context", "not json")
    with pytest.raises(ValueError):
        storage.append_event("not json")
    with pytest.raises(ValueError):
        storage.bind_contract("not json")
    assert storage.state("context") is None
    assert storage.history() == ()


def test_the_task_contract_binds_once_and_refuses_a_different_one(storage):
    storage.bind_contract('{"model":"synthetic"}')
    storage.bind_contract('{"model":"synthetic"}')
    with pytest.raises(ValueError):
        storage.bind_contract('{"model":"different"}')
    assert storage.state("contract") == '{"model":"synthetic"}'


def test_state_and_events_survive_reopening_the_same_store(runtime_backend):
    storage = runtime_backend.open()
    storage.bind_contract('{"model":"synthetic"}')
    storage.put_state("context", '{"v":1}')
    storage.append_event('{"kind":"first"}')
    reopened = runtime_backend.reopen(storage)
    assert reopened.state("contract") == '{"model":"synthetic"}'
    assert reopened.state("context") == '{"v":1}'
    assert reopened.history() == ({"kind": "first", "event_id": 1},)
    assert reopened.append_event('{"kind":"second"}') == 2


def test_events_get_increasing_identifiers_and_history_returns_the_latest_oldest_first(storage):
    assert [storage.append_event(json.dumps({"kind": "tick", "n": number})) for number in range(1, 6)] == [1, 2, 3, 4, 5]
    assert [event["event_id"] for event in storage.history()] == [1, 2, 3, 4, 5]
    assert [event["event_id"] for event in storage.history(2)] == [4, 5]
    assert storage.history(0) == ()
    assert storage.history(2)[0] == {"kind": "tick", "n": 4, "event_id": 4}


def test_trace_pages_follow_a_stable_cursor_and_reject_bad_bounds(storage):
    for number in range(1, 5):
        storage.append_event(json.dumps({"kind": "tick", "n": number}))
    first_page = storage.trace_events(after_event_id=0, limit=3)
    assert [event["event_id"] for event in first_page["events"]] == [1, 2, 3]
    assert first_page["cursor"] == 3
    second_page = storage.trace_events(after_event_id=first_page["cursor"], limit=3)
    assert [event["event_id"] for event in second_page["events"]] == [4]
    exhausted = storage.trace_events(after_event_id=second_page["cursor"], limit=3)
    assert exhausted == {"events": [], "cursor": 4}
    for bad_cursor, bad_limit in ((-1, 10), (0, 0), ("0", 10), (0, "10")):
        with pytest.raises(ValueError):
            storage.trace_events(after_event_id=bad_cursor, limit=bad_limit)
    assert [event["event_id"] for event in storage.trace_events(limit=5000)["events"]] == [1, 2, 3, 4]


def test_events_can_be_read_by_explicit_kind_in_order(storage):
    storage.append_event('{"kind":"prediction","n":1}')
    storage.append_event('{"kind":"noise"}')
    storage.append_event('{"kind":"human-feedback","n":2}')
    storage.append_event('{"kind":"prediction","n":3}')
    assert [json.loads(payload)["n"] for payload in storage.event_payloads(("prediction",))] == [1, 3]
    assert [event["event_id"] for event in storage.events_of(("prediction", "human-feedback"))] == [1, 3, 4]
    assert storage.events_of(("absent",)) == ()


def test_event_kind_filters_must_be_a_nonempty_tuple_of_text(storage):
    for bad_kinds in ((), ["prediction"], (1,)):
        with pytest.raises(ValueError):
            list(storage.event_payloads(bad_kinds))
        with pytest.raises(ValueError):
            storage.events_of(bad_kinds)


def test_an_event_kind_filter_longer_than_the_old_hundred_kind_ceiling_is_served(storage):
    storage.append_event('{"kind":"prediction","n":1}')
    kinds = ("prediction", *(f"kind-{number}" for number in range(150)))
    assert [json.loads(payload)["n"] for payload in storage.event_payloads(kinds)] == [1]
    assert [event["event_id"] for event in storage.events_of(kinds)] == [1]


def test_the_next_cycle_number_follows_the_highest_recorded_cycle(storage):
    assert storage.next_cycle_number() == 1
    storage.append_event('{"kind":"cycle-started","cycle_number":1}')
    storage.append_event('{"kind":"prediction"}')
    storage.append_event('{"kind":"cycle-started","cycle_number":2}')
    assert storage.next_cycle_number() == 3


def test_active_labels_follow_the_latest_feedback_per_item(storage):
    assert storage.active_labels() == ()
    storage.append_event(feedback_event("item-a", "relevant"))
    storage.append_event('{"kind":"noise"}')
    storage.append_event(feedback_event("item-b", "irrelevant"))
    storage.append_event(feedback_event("item-a", "irrelevant"))
    assert [
        (label["feedback"]["item_id"], label["feedback"]["final_answer_value"], label["event_id"])
        for label in storage.active_labels()
    ] == [("item-b", "irrelevant", 3), ("item-a", "irrelevant", 4)]


def test_active_labels_agree_after_a_reopen_without_replaying_every_event(runtime_backend):
    storage = runtime_backend.open()
    storage.append_event(feedback_event("item-a", "relevant"))
    storage.append_event(feedback_event("item-b", "irrelevant"))
    before = storage.active_labels()
    reopened = runtime_backend.reopen(storage)
    assert reopened.active_labels() == before


def test_events_keep_their_order_and_ids_and_page_without_gaps(storage):
    ids = [storage.append_event(json.dumps({"kind": "step", "n": n})) for n in range(1, 91)]
    assert ids == list(range(1, 91))
    seen, cursor = [], 0
    while True:
        page = storage.trace_events(after_event_id=cursor, limit=40)
        if not page["events"]:
            break
        seen += [(e["event_id"], e["n"]) for e in page["events"]]
        cursor = page["cursor"]
    assert seen == [(n, n) for n in range(1, 91)]
    assert [e["event_id"] for e in storage.history(3)] == [88, 89, 90]
    assert [e["event_id"] for e in storage.history(-1)] == list(range(1, 91))
    assert storage.history(0) == ()
    assert storage.trace_events(after_event_id=90)["cursor"] == 90


def test_events_of_and_payloads_filter_by_kind_in_order(storage):
    for n in range(30):
        storage.append_event(json.dumps({"kind": "even" if n % 2 == 0 else "odd", "n": n}))
    assert [e["n"] for e in storage.events_of(("odd",))] == list(range(1, 30, 2))
    assert [json.loads(p)["n"] for p in storage.event_payloads(("even",))] == list(range(0, 30, 2))


def test_cycle_numbers_follow_the_highest_cycle_seen(storage):
    assert storage.next_cycle_number() == 1
    storage.append_event('{"kind":"cycle-started","cycle_number":1}')
    storage.append_event('{"kind":"cycle-started","cycle_number":4}')
    storage.append_event('{"kind":"cycle-completed","cycle_number":2}')
    assert storage.next_cycle_number() == 5
