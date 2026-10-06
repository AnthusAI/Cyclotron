"""Group linked recorded markers into ordinal item steps, without changing history."""
from datetime import datetime


def step_projection(events, history, items):
    articles = {str(source['article'].get('id')): source['article'] for source in history}
    operational = {event['cycle_id']:event for event in events if event.get('kind')=='cycle-started' and event.get('cycle_id')}
    for event in operational.values():
        item=event.get('item')
        if item:
            title=item.get('values',{}).get('title') or item.get('values',{}).get('text','').split('\n')[0].removeprefix('Title: ')
            articles[item['id']]={'id':item['id'],'title':title}
    parents = {event['step_id']: event['parent_step_id'] for event in events
               if event.get('step_id') and event.get('parent_step_id')}
    roots = {}
    for event in events:
        if event.get('kind') in {'step-started', 'round-started'} and event.get('step_id') and not event.get('parent_step_id'):
            roots.setdefault(event['step_id'], event)

    def cycle_key(event):
        if event.get('cycle_id'):
            return event['cycle_id']
        root = event.get('step_id')
        seen = set()
        while root and parents.get(root) and root not in seen:
            seen.add(root)
            root = parents[root]
        return root if root in roots else 'unscoped'

    records = []
    for item in items:
        event = events[item['event_index']]
        target = event.get('target_id') or event.get('item_id') or event.get('feedback',{}).get('item_id')
        kind = event.get('kind', '')
        stage = event.get('step_stage') or 'Context'
        if target:
            identity = ('target', event.get('step_id'), target)
            article = articles.get(str(target), {})
            title = article.get('title') or event.get('state', {}).get('target', {}).get('title') or str(target)
        else:
            family = ('optimization' if kind in {'step-started', 'optimizer-request', 'optimizer-response', 'proposal-validated'}
                      else 'fit' if kind.startswith('fit-') else 'outcome' if kind in
                      {'candidate-evaluated', 'candidate-rejected', 'promoted', 'candidate-qualified', 'step-completed', 'step-failed', 'step-paused'}
                      else kind)
            identity = ('runtime', event.get('step_id') or str(item['id']), family)
            title = stage.capitalize() + ' optimization' if family == 'optimization' else kind.replace('-', ' ').capitalize()
        records.append((item['start'], str(item['id']), identity, title, target, cycle_key(event)))
    for index, source in enumerate(history):
        row, article = source['record'], source['article']
        date = row.get('shown_at') or row.get('created_at')
        if not date:
            continue
        presentation = row.get('id') if source.get('source_table') == 'presentations' else row.get('presentation_id')
        identity = ('review', presentation) if presentation is not None else ('review-event', index)
        records.append((date, f'source:{index}', identity, article.get('title') or article.get('id') or 'Unnamed item', article.get('id'), 'source-history'))
    records.sort(key=lambda record: datetime.fromisoformat(record[0].replace('Z', '+00:00')))
    steps, positions, order = [], {}, []
    previous = None
    for date, key, identity, title, target, cycle in records:
        if (cycle, identity) != previous:
            steps.append({'number': len(steps)+1, 'title': title, 'item_id': target, 'keys': [], 'cycle_key': cycle})
        steps[-1]['keys'].append(key)
        order.append(key)
        previous = (cycle, identity)
    for index, step in enumerate(steps):
        for offset, key in enumerate(step['keys']):
            positions[key] = index * 1000 + 1000 * (offset+1) / (len(step['keys'])+1)
    cycles, numbers = [], {}
    for index, step in enumerate(steps):
        key = step['cycle_key']
        if not cycles or cycles[-1]['key'] != key:
            if key in operational:
                event=operational[key]
                item=event.get('item') or {}
                title=f"Cycle {event['cycle_number']} · {articles.get(item.get('id'),{}).get('title') or 'Maintenance'}"
            elif key in roots:
                title=f"Retrospective optimization · {(roots[key].get('step_stage') or 'Optimization').capitalize()}"
            else:
                title = 'Imported review history' if key == 'source-history' else 'Unscoped recorded events'
            cycles.append({'key': key, 'title': title, 'recorded': key in operational,
                           'start': index*1000, 'step_start': index+1, 'keys': []})
        cycles[-1].update(end=(index+1)*1000, step_end=index+1)
        cycles[-1]['keys'].extend(step['keys'])
    for index,step in enumerate(steps):
        step.update(start=index*1000,end=(index+1)*1000)
    axis='cycle' if cycles and all(cycle['recorded'] for cycle in cycles) else 'step'
    if axis=='cycle':
        for index,cycle in enumerate(cycles):
            inside=steps[cycle['step_start']-1:cycle['step_end']]
            cycle.update(start=index*1000,end=(index+1)*1000)
            for offset,step in enumerate(inside):
                step.update(start=index*1000+offset*1000/len(inside),end=index*1000+(offset+1)*1000/len(inside),cycle_step=offset+1)
                for marker,key in enumerate(step['keys']):
                    positions[key]=step['start']+(step['end']-step['start'])*(marker+1)/(len(step['keys'])+1)
    return {'steps': steps, 'positions': positions, 'order': order, 'cycles': cycles,'axis':axis}
