"""Application-owned operational cycles, independent of optimization-round IDs."""
from dataclasses import asdict
from uuid import uuid4


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
        """Reattach to the last unfinished cycle; never repeat prediction or work."""
        if wheel._cycle_running:
            raise RuntimeError('one operational cycle may run at a time')
        closed = set()
        for event in reversed(wheel.history(100000)):
            if event['kind'] in ('cycle-completed','cycle-failed'):
                closed.add(event.get('cycle_id'))
            if event['kind']=='cycle-started':
                if event['cycle_id'] in closed or event.get('cycle_item_id') != item.id:
                    return None
                cycle=cls(wheel,item,reason=event.get('reason','item-processing'))
                cycle.context={key:event[key] for key in ('cycle_id','cycle_number','cycle_item_id')}
                cycle.token=wheel._cycle_context.set(cycle.context)
                wheel._cycle_running=True
                try:
                    wheel._emit({'kind':'cycle-resumed','reason':cycle.reason,
                        'classifier_snapshot':asdict(wheel.active)})
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
                     'classifier_snapshot':asdict(wheel.active)})
        return self

    def check_trigger(self, stage, *, due, reason, details=None):
        if self.token is None or type(due) is not bool or not reason:
            raise ValueError('trigger checks require an active cycle, a boolean decision and a reason')
        event = self.wheel._emit({'kind':'trigger-evaluated', 'stage':stage, 'due':due,
                                 'reason':reason, 'details':details or {}})
        if due:
            self.wheel._cycle_context.set({**self.context, 'trigger_event_id':event['event_id']})
        return event

    def __exit__(self, error_type, error, traceback):
        try:
            self.wheel._emit({'kind':'cycle-failed' if error_type else 'cycle-completed',
                             'error_type':error_type.__name__ if error_type else None,
                             'classifier_snapshot':asdict(self.wheel.active)})
        finally:
            self.wheel._cycle_context.reset(self.token)
            self.wheel._cycle_running = False
            self.token = None
        return False
