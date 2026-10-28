"""Active labels come from a materialized view, never from a window of recent events."""

import json
from uuid import uuid4

import pytest

from .active_labels import ACTIVE_LABELS_STATE_KEY
from .classifier_config import ClassifierConfig
from .feedback import FeedbackItem
from .flywheel import DecisionFlywheel
from .flywheel_test import TASK, FakeModel, agent
from .learning_loop import feedback_partitions, feedback_propensities, labeled_partitions
from .s3_journal_test import journal
from .s3_runtime_storage import S3RuntimeStores

SEED = "active-label-seed"
RECENT_EVENT_WINDOW = 3
OLD_LABEL_COUNT = 4
RECENT_PADDING_COUNT = 10
EXTRA_PADDING_FOR_SQLITE = 100001


def sqlite_wheel(tmp_path):
    return DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), FakeModel(), agent([]))


def s3_wheel(tmp_path):
    stores = S3RuntimeStores(journal(name="classifier-active-labels"))
    stores.initialize()
    stores.acquire(str(uuid4()))
    return DecisionFlywheel(None, ClassifierConfig(TASK), FakeModel(), None, storage=stores)


ADAPTERS = [pytest.param(sqlite_wheel, id="sqlite"), pytest.param(s3_wheel, id="s3")]


def submit(wheel, feedback_id, item_id, label, **extra):
    return wheel.record_feedback_event(
        FeedbackItem(
            feedback_id,
            item_id,
            "inclusion",
            initial_answer_value="exclude",
            final_answer_value=label,
            selection_propensity=extra.pop("selection_propensity", None),
        ),
        **extra,
    )


def known_items(count):
    return {f"item-{index}": {"text": f"item {index}"} for index in range(count)}


def restrict_history_window(wheel, monkeypatch):
    unrestricted = wheel.runtime_storage.history
    monkeypatch.setattr(
        wheel.runtime_storage,
        "history",
        lambda limit=100: unrestricted(min(limit, RECENT_EVENT_WINDOW)) if limit >= 0 else unrestricted(limit),
    )


@pytest.mark.parametrize("make_wheel", ADAPTERS)
def test_partitions_keep_every_label_when_the_event_window_no_longer_reaches_them(make_wheel, tmp_path, monkeypatch):
    wheel = make_wheel(tmp_path)
    items = known_items(OLD_LABEL_COUNT + 2)
    for index in range(OLD_LABEL_COUNT):
        submit(wheel, f"feedback-{index}", f"item-{index}", "include" if index % 2 else "exclude", selection_propensity=0.5)
    for index in range(RECENT_PADDING_COUNT):
        wheel._emit({"kind": "padding", "index": index})
    expected = feedback_partitions(wheel, items, SEED)
    restrict_history_window(wheel, monkeypatch)
    assert "human-feedback" not in {event["kind"] for event in wheel.history(100000)}
    after = feedback_partitions(wheel, items, SEED)
    assert after == expected
    labeled_ids = {row.item.id for row in (*after[0], *after[1])} | {
        row.id for row in after[2] if row.id in {f"item-{index}" for index in range(OLD_LABEL_COUNT)}
    }
    assert labeled_ids == {f"item-{index}" for index in range(OLD_LABEL_COUNT)}
    assert feedback_propensities(wheel) == {f"item-{index}": 0.5 for index in range(OLD_LABEL_COUNT)}
    wheel.close()


@pytest.mark.parametrize("make_wheel", ADAPTERS)
def test_a_retracted_or_replaced_label_leaves_the_view_in_arrival_order(make_wheel, tmp_path):
    wheel = make_wheel(tmp_path)
    submit(wheel, "first", "item-0", "include")
    submit(wheel, "second", "item-1", "include")
    submit(wheel, "third", "item-2", "exclude")
    wheel.record_feedback_event(FeedbackItem("second", "item-1", "inclusion", final_answer_value="include"), action="retracted")
    submit(wheel, "first-again", "item-0", "exclude")
    labels = wheel.active_labels()
    assert [(event["feedback"]["item_id"], event["feedback"]["final_answer_value"]) for event in labels] == [
        ("item-2", "exclude"),
        ("item-0", "exclude"),
    ]
    assert [event["event_id"] for event in labels] == sorted(event["event_id"] for event in labels)
    wheel.close()


@pytest.mark.parametrize("make_wheel", ADAPTERS)
def test_a_store_saved_before_the_view_existed_rebuilds_it_from_its_events(make_wheel, tmp_path):
    wheel = make_wheel(tmp_path)
    submit(wheel, "first", "item-0", "include")
    submit(wheel, "second", "item-1", "exclude")
    kept = wheel.active_labels()
    storage = wheel.runtime_storage
    if hasattr(storage, "db"):
        with storage.db:
            storage.db.execute("DELETE FROM runtime_state WHERE key=?", (ACTIVE_LABELS_STATE_KEY,))
    else:
        storage.store.change(lambda data, value: data["state"].pop(ACTIVE_LABELS_STATE_KEY))
    assert wheel.active_labels() == kept
    submit(wheel, "third", "item-2", "include")
    assert [event["feedback"]["id"] for event in wheel.active_labels()] == ["first", "second", "third"]
    wheel.close()


def test_sqlite_keeps_every_label_after_more_than_one_hundred_thousand_later_events(tmp_path):
    wheel = sqlite_wheel(tmp_path)
    items = known_items(OLD_LABEL_COUNT + 1)
    for index in range(OLD_LABEL_COUNT):
        submit(wheel, f"feedback-{index}", f"item-{index}", "include" if index % 2 else "exclude")
    expected = feedback_partitions(wheel, items, SEED)
    with wheel.runtime_storage.db:
        wheel.runtime_storage.db.executemany(
            "INSERT INTO runtime_events(payload) VALUES (?)", [('{"kind":"padding"}',)] * EXTRA_PADDING_FOR_SQLITE
        )
    assert not [event for event in wheel.history(100000) if event["kind"] == "human-feedback"]
    assert feedback_partitions(wheel, items, SEED) == expected
    wheel.close()


@pytest.mark.parametrize("make_wheel", ADAPTERS)
def test_labeled_partitions_read_only_the_labeled_items_and_match_the_full_partitions(make_wheel, tmp_path):
    wheel = make_wheel(tmp_path)
    items = known_items(30)
    for index in range(0, 30, 3):
        submit(wheel, f"feedback-{index}", f"item-{index}", "include" if index % 2 else "exclude")
    requested = []

    def item_values_for(item_id):
        requested.append(item_id)
        return items.get(item_id)

    training, development = labeled_partitions(wheel, item_values_for, SEED)
    full_training, full_development, _ = feedback_partitions(wheel, items, SEED)
    assert (training, development) == (full_training, full_development)
    assert sorted(requested) == sorted(f"item-{index}" for index in range(0, 30, 3))
    wheel.close()


def test_the_view_is_stored_as_ordinary_runtime_state_that_snapshots_already_carry(tmp_path):
    wheel = sqlite_wheel(tmp_path)
    submit(wheel, "first", "item-0", "include")
    saved = json.loads(wheel.runtime_storage.state(ACTIVE_LABELS_STATE_KEY))
    assert [event["feedback"]["id"] for event in saved] == ["first"]
    wheel.close()
