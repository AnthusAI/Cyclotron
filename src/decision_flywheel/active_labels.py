"""The materialized view of one classifier's active human labels.

A label is active from its submitted feedback event until a later feedback
event for the same item retracts or replaces it. Runtime storage keeps this
view beside the event log and updates it when a feedback event is recorded, so
training, development and audit partitions never depend on how many events
the store holds.
"""
import json

ACTIVE_LABELS_STATE_KEY = 'active_labels'
FEEDBACK_EVENT_KIND = 'human-feedback'


def record_feedback_event(active_by_item, event_id, event):
    """Apply one feedback event to the item-keyed view, keeping arrival order."""
    item_id = (event.get('feedback') or {}).get('item_id')
    if item_id is None:
        return
    active_by_item.pop(item_id, None)
    if event.get('action') == 'submitted':
        active_by_item[item_id] = {**event, 'event_id': event_id}


def rebuild_active_labels(identified_payloads):
    """Derive the view from every stored event, in event order."""
    active_by_item = {}
    for event_id, payload in identified_payloads:
        event = json.loads(payload)
        if event.get('kind') == FEEDBACK_EVENT_KIND:
            record_feedback_event(active_by_item, event_id, event)
    return active_by_item


def load_active_labels(saved):
    return {event['feedback']['item_id']: event for event in json.loads(saved)}


def dump_active_labels(active_by_item):
    return json.dumps(list(active_by_item.values()), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
