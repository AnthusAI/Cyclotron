"""Round-scoped display indexes; the recorded events remain immutable."""


def round_details(events):
    rounds, owners, steps = {}, {}, {}
    legacy = None
    categories = {
        'optimizer-request': 'optimizer_requests', 'optimizer-response': 'optimizer_responses',
        'proposal-validated': 'proposals', 'candidate-evaluated': 'evaluations',
        'fit-started': 'fits', 'fit-completed': 'fits',
        'step-completed': 'outcomes', 'step-paused': 'outcomes', 'step-failed': 'outcomes',
        'candidate-rejected': 'outcomes', 'promoted': 'outcomes', 'candidate-qualified': 'outcomes',
        'decision-request': 'decision_requests', 'decision-response': 'decision_responses',
    }
    for index, event in enumerate(events):
        kind, step_id = event.get('kind'), event.get('step_id')
        if kind == 'step-started' or (kind == 'round-started' and not step_id):
            rounds[str(index)] = {key: [] for key in set(categories.values())}
            rounds[str(index)].update(start=index, end=None)
            if step_id:
                steps[step_id] = index
                legacy = None
            else:
                legacy = index
        owner = steps.get(step_id) if step_id else legacy
        if owner is None:
            continue
        owners[str(index)] = owner
        detail = rounds[str(owner)]
        category = categories.get(kind)
        if category:
            detail[category].append(index)
        if kind in {'step-completed', 'step-paused', 'step-failed'}:
            detail['end'] = index
    return {'rounds': rounds, 'owners': owners}
