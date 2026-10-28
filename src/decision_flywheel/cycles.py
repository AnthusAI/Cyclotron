"""Application-owned operational cycles, independent of optimization-round IDs."""

from dataclasses import asdict
from uuid import uuid4

from .classifier_snapshot import compact_snapshot

CYCLE_EVENT_KINDS = ("cycle-started", "cycle-completed", "cycle-failed")


def note_cycle_event(index, event):
    """Fold one event into ``{item_id: [[cycle_id, cycle_number, reason, failed], ...]}`` in start order.

    A completed cycle leaves the index; a failed one stays, marked, for a replay to finish under its own number.
    """
    kind, item_id = event.get("kind"), event.get("cycle_item_id")
    if kind not in CYCLE_EVENT_KINDS or item_id is None:
        return index
    rows = index.setdefault(item_id, [])
    if kind == "cycle-started":
        rows.append([event["cycle_id"], event.get("cycle_number"), event.get("reason", "item-processing"), False])
    else:
        for row in rows:
            if row[0] == event.get("cycle_id"):
                row[3] = True
        if kind == "cycle-completed":
            rows[:] = [row for row in rows if row[0] != event.get("cycle_id")]
    if not rows:
        del index[item_id]
    return index


def unfinished_cycles_from(events, item_id):
    """The index entry for one item, folded from cycle events in order (for stores that can query them)."""
    index = {}
    for event in events:
        note_cycle_event(index, event)
    return tuple(tuple(row) for row in index.get(item_id, ()))


class Cycle:
    def __init__(self, wheel, item=None, *, reason="item-processing"):
        if item is not None:
            wheel.initial.task.validate_target(item)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cycle reason must be non-empty")
        self.wheel, self.item, self.reason = wheel, item, reason
        self.token = None

    @classmethod
    def resume(cls, wheel, item, *, include_failed=False):
        """Reattach to this item's latest unfinished cycle, skipping other work.

        A failed cycle counts as unfinished only when ``include_failed`` is set,
        so a replay can complete the cycle it was running under its own number.
        """
        if wheel._cycle_running:
            raise RuntimeError("one operational cycle may run at a time")
        # The store keeps the item's unfinished cycles, so resuming never reads the event history.
        for cycle_id, number, reason, failed in reversed(wheel.runtime_storage.unfinished_cycles(item.id)):
            if failed and not include_failed:
                continue
            cycle = cls(wheel, item, reason=reason)
            cycle.context = {"cycle_id": cycle_id, "cycle_number": number, "cycle_item_id": item.id}
            cycle.token = wheel._cycle_context.set(cycle.context)
            wheel._cycle_running = True
            try:
                wheel._emit(
                    {"kind": "cycle-resumed", "reason": cycle.reason, "classifier_snapshot": compact_snapshot(wheel.active)}
                )
            except Exception:
                wheel._cycle_context.reset(cycle.token)
                wheel._cycle_running = False
                raise
            return cycle
        return None

    def __enter__(self):
        wheel = self.wheel
        if self.token is not None:
            return self
        if wheel._cycle_running:
            raise RuntimeError("one operational cycle may run at a time")
        number = wheel.runtime_storage.next_cycle_number()
        self.context = {"cycle_id": str(uuid4()), "cycle_number": number, "cycle_item_id": self.item.id if self.item else None}
        self.token = wheel._cycle_context.set(self.context)
        wheel._cycle_running = True
        wheel._emit(
            {
                "kind": "cycle-started",
                "reason": self.reason,
                "item": asdict(self.item) if self.item else None,
                "classifier_snapshot": compact_snapshot(wheel.active),
            }
        )
        return self

    def check_trigger(self, stage, *, due, reason, details=None):
        if self.token is None or type(due) is not bool or not reason:
            raise ValueError("trigger checks require an active cycle, a boolean decision and a reason")
        event = self.wheel._emit(
            {"kind": "trigger-evaluated", "stage": stage, "due": due, "reason": reason, "details": details or {}}
        )
        if due:
            self.wheel._cycle_context.set({**self.context, "trigger_event_id": event["event_id"]})
        return event

    def suspend(self):
        """Detach a waiting cycle without marking it complete; resume before feedback."""
        if self.token is None:
            raise RuntimeError("cycle is not attached")
        self.wheel._emit({"kind": "cycle-waiting-for-feedback"})
        self.wheel._cycle_context.reset(self.token)
        self.wheel._cycle_running = False
        self.token = None

    def __exit__(self, error_type, error, traceback):
        try:
            self.wheel._emit(
                {
                    "kind": "cycle-failed" if error_type else "cycle-completed",
                    "error_type": error_type.__name__ if error_type else None,
                    "classifier_snapshot": compact_snapshot(self.wheel.active),
                }
            )
        finally:
            self.wheel._cycle_context.reset(self.token)
            self.wheel._cycle_running = False
            self.token = None
        return False
