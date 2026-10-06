"""Presentation-only projection of recorded events onto timeline lanes."""
from datetime import datetime

GROUPS = [('feedback', 'Human labels'), ('decisions', 'Decisions'), ('rubric', 'Rubric'), ('examples', 'Few-shot examples'),
          ('questions', 'Classifier questions'), ('classifier', 'ML optimization'),
          ('fit', 'ML fitting'), ('evaluation', 'Evaluation / outcome'), ('optimizer', 'Optimizer (unscoped)'),
          ('configuration', 'Configuration changes'), ('triggers','Trigger checks'), ('cycles','Cycle boundaries')]


def timeline_data(events):
    items, starts, undated = [], {}, 0
    for index, event in enumerate(events):
        date = event.get('created_at')
        try:
            parsed = datetime.fromisoformat(date.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('undated')
        except (AttributeError, ValueError):
            undated += 1
            continue
        kind = event.get('kind')
        group, label = None, None
        if kind in {'cycle-started','cycle-completed','cycle-failed'}:
            group,label='cycles',kind.replace('-',' ')
        elif kind == 'trigger-evaluated':
            group,label='triggers',f"{event.get('stage')} · {'run' if event.get('due') else 'skip'} · {event.get('reason')}"
        elif kind == 'cycle-metrics':
            group,label='evaluation','Running prediction agreement'
        elif kind == 'human-feedback':
            feedback = event.get('feedback', {})
            group = 'feedback'
            label = ' · '.join(str(value) for value in (feedback.get('final_answer_value', 'unlabeled'),
                event.get('assignment') or 'unassigned', event.get('action', 'submitted')))
            if feedback.get('edit_comment_value'):
                label += ' · comment'
        elif kind == 'step-started':
            group = event.get('step_stage')
            if group not in dict(GROUPS):
                continue
            label = group + ' · started'
        elif kind in {'step-completed', 'step-paused', 'step-failed'}:
            group, label = 'evaluation', event.get('status', kind.removeprefix('step-'))
        elif kind in {'prediction','decision-request','decision-response'}:
            group='decisions'
            label=(str(event.get('label')) if kind=='prediction' else kind.replace('decision-','Decision '))
        elif kind in {'fit-started', 'fit-completed'}:
            group, label = 'fit', kind
        elif kind in {'optimizer-request', 'optimizer-response', 'proposal-validated'}:
            group = event.get('step_stage') or 'optimizer'
            label = {'optimizer-request': 'Optimizer request', 'optimizer-response': 'Optimizer response',
                     'proposal-validated': 'Proposal validated'}[kind]
        elif kind in {'classifier-activated', 'classifier-invalidated'}:
            group, label = 'configuration', kind
        elif kind in {'candidate-evaluated', 'candidate-rejected', 'promoted', 'candidate-qualified'}:
            group, label = 'evaluation', kind
        if group:
            item = {'id': index, 'event_index': index, 'group': group, 'content': label,
                    'start': date, 'type': 'point'}
            items.append(item)
            if kind == 'step-started' and event.get('step_id'):
                starts[event['step_id']] = item
    used={item['group'] for item in items}
    return {'groups': [{'id': key, 'content': title} for key, title in GROUPS if key in used],
            'items': items, 'undated_count': undated}
