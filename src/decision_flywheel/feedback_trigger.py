"""Reusable label-transition triggers derived from durable feedback, not UI state."""
from dataclasses import dataclass

PROTECTED_ASSIGNMENTS=frozenset(('scoreboard','rolling_audit','final_audit'))

def learning_feedback(event):
    return event.get('kind')=='human-feedback' and event.get('assignment') not in PROTECTED_ASSIGNMENTS


@dataclass(frozen=True)
class LabelTransitionTrigger:
    every: int = 2

    def __post_init__(self):
        if type(self.every) is not int or self.every < 1:
            raise ValueError('transition cadence must be a positive integer')

    def check(self, events):
        events = tuple(events)
        active = {}
        last_feedback = None
        for event in events:
            if event.get('kind') != 'human-feedback':
                continue
            last_feedback = event
            if not learning_feedback(event):continue
            feedback = event['feedback']
            item_id = feedback.get('item_id', feedback['id'])
            if event['action'] == 'retracted':
                if item_id in active and active[item_id]['feedback']['id'] == feedback['id']:
                    active.pop(item_id)
            else:
                active.pop(item_id, None)
                active[item_id] = event
        votes = list(active.values())
        labels = [event['feedback']['final_answer_value'] for event in votes]
        transitions = sum(left != right for left, right in zip(labels, labels[1:]))
        changed = len(labels) > 1 and labels[-1] != labels[-2]
        feedback_id = votes[-1]['feedback']['id'] if votes else None
        checked = any(event.get('kind') == 'trigger-evaluated' and event.get('stage') == 'rubric'
                      and event.get('details', {}).get('policy') == 'label-transitions'
                      and event['details'].get('feedback_id') == feedback_id for event in events)
        due = bool(last_feedback and learning_feedback(last_feedback) and last_feedback['action'] == 'submitted' and changed
                   and transitions % self.every == 0 and not checked)
        return {'due': due,
                'reason': 'label transition cadence reached' if due else 'label transition cadence not reached',
                'details': {'policy': 'label-transitions', 'threshold': self.every,
                            'transition_count': transitions, 'label_changed': changed,
                            'previous_label': labels[-2] if len(labels) > 1 else None,
                            'current_label': labels[-1] if labels else None, 'feedback_id': feedback_id}}
