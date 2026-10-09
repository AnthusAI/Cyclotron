"""Application-owned operational cycles, independent of optimization-round IDs."""
from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass, replace
from uuid import uuid4

# Each out-of-fold row records the IDs, labels and normalizers it was fitted on,
# so this proof grows with the square of the training set. It is recorded in
# full where a head is fitted and activated, not again at every cycle boundary.
PER_ITEM_FIT_PROOF = ('fit_ids', 'fit_labels', 'normalization_fit_ids', 'normalizers')


def _plain(value):
    return asdict(value) if is_dataclass(value) else deepcopy(value)


def cycle_snapshot(classifier):
    """The active classifier for a cycle boundary event, without the per-item fit proof."""
    head = classifier.head
    snapshot = asdict(replace(classifier, head=None))
    if head is None:
        return snapshot
    snapshot['head'] = {field.name: _plain(getattr(head, field.name)) for field in fields(head)
                        if field.name != 'out_of_fold'}
    oof = head.out_of_fold
    snapshot['head']['out_of_fold'] = {
        **{field.name: _plain(getattr(oof, field.name)) for field in fields(oof)
           if field.name not in PER_ITEM_FIT_PROOF},
        'per_item_fit_proof': {'omitted': list(PER_ITEM_FIT_PROOF),
                               'recorded_in': ['fit-completed', 'classifier-activated'],
                               'head_fingerprint': head.refitted_cyclotron_fingerprint}}
    return snapshot


class Cycle:
    def __init__(self, wheel, item=None, *, reason='item-processing'):
        if item is not None:
            wheel.initial.task.validate_target(item)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('cycle reason must be non-empty')
        self.wheel, self.item, self.reason = wheel, item, reason
        self.token = None

    @classmethod
    def resume(cls, wheel, item):
        """Reattach to this item's latest unfinished cycle, skipping other work."""
        if wheel._cycle_running:
            raise RuntimeError('one operational cycle may run at a time')
        closed = set()
        for event in reversed(wheel.history(100000)):
            if event['kind'] in ('cycle-completed','cycle-failed'):
                closed.add(event.get('cycle_id'))
            if event['kind']=='cycle-started':
                if event['cycle_id'] in closed or event.get('cycle_item_id') != item.id:
                    continue
                cycle=cls(wheel,item,reason=event.get('reason','item-processing'))
                cycle.context={key:event[key] for key in ('cycle_id','cycle_number','cycle_item_id')}
                cycle.token=wheel._cycle_context.set(cycle.context)
                wheel._cycle_running=True
                try:
                    wheel._emit({'kind':'cycle-resumed','reason':cycle.reason,
                        'classifier_snapshot':cycle_snapshot(wheel.active)})
                except Exception:
                    wheel._cycle_context.reset(cycle.token)
                    wheel._cycle_running=False
                    raise
                return cycle
        return None

    def __enter__(self):
        wheel = self.wheel
        if wheel._cycle_running:
            raise RuntimeError('one operational cycle may run at a time')
        number = wheel.db.execute("SELECT COALESCE(MAX(json_extract(payload,'$.cycle_number')),0)+1 FROM runtime_events").fetchone()[0]
        self.context = {'cycle_id': str(uuid4()), 'cycle_number': number,
                        'cycle_item_id': self.item.id if self.item else None}
        self.token = wheel._cycle_context.set(self.context)
        wheel._cycle_running = True
        wheel._emit({'kind':'cycle-started', 'reason':self.reason,
                     'item':asdict(self.item) if self.item else None,
                     'classifier_snapshot':cycle_snapshot(wheel.active)})
        return self

    def check_trigger(self, stage, *, due, reason, details=None):
        if self.token is None or type(due) is not bool or not reason:
            raise ValueError('trigger checks require an active cycle, a boolean decision and a reason')
        event = self.wheel._emit({'kind':'trigger-evaluated', 'stage':stage, 'due':due,
                                 'reason':reason, 'details':details or {}})
        if due:
            self.wheel._cycle_context.set({**self.context, 'trigger_event_id':event['event_id']})
        return event

    def suspend(self):
        """Detach a waiting cycle without marking it complete; resume before feedback."""
        if self.token is None: raise RuntimeError('cycle is not attached')
        self.wheel._emit({'kind':'cycle-waiting-for-feedback'})
        self.wheel._cycle_context.reset(self.token)
        self.wheel._cycle_running=False
        self.token=None

    def __exit__(self, error_type, error, traceback):
        try:
            self.wheel._emit({'kind':'cycle-failed' if error_type else 'cycle-completed',
                             'error_type':error_type.__name__ if error_type else None,
                             'classifier_snapshot':cycle_snapshot(self.wheel.active)})
        finally:
            self.wheel._cycle_context.reset(self.token)
            self.wheel._cycle_running = False
            self.token = None
        return False
