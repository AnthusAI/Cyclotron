"""Plan explicit combinations and ablations against one frozen incumbent."""
from copy import deepcopy

from .feature_bank import question_id


def _difference(before, after):
    old = {task['name']: task for task in before}
    new = {task['name']: task for task in after}
    return {'added': [task for task in after if task['name'] not in old],
            'removed': [task for task in before if task['name'] not in new],
            'revised': [{'before': old[task['name']], 'after': task} for task in after
                        if task['name'] in old and old[task['name']] != task]}


def plan_feature_groups(current_tasks, bank, groups, *, max_configurations=8):
    """Return one full group and its leave-one-out variants; never select groups.

    IDs name retained question revisions, not concepts inferred by this planner.
    The caller explicitly chooses groups; no labels, audits, or model calls are
    used here. A plan exceeding its configuration ceiling is rejected in full.
    """
    if type(max_configurations) is not int or max_configurations < 1:
        raise ValueError('group configuration ceiling must be positive')
    bank_by_id = {entry['id']: entry['question'] for entry in bank}
    planned, seen = [], set()
    current = deepcopy(list(current_tasks))
    for group in groups:
        if isinstance(group, (str, bytes)) or not isinstance(group, (tuple, list)):
            raise ValueError('each group must explicitly list retained question IDs')
        if len(group) < 2 or any(not isinstance(key, str) or key not in bank_by_id for key in group):
            raise ValueError('a group requires at least two known retained question IDs')
        if len(set(group)) != len(group):
            raise ValueError('a group cannot repeat a question revision')
        keys = tuple(sorted(group))
        if keys in seen:
            raise ValueError('duplicate feature group')
        seen.add(keys)
        questions = [deepcopy(bank_by_id[key]) for key in keys]
        names = {task['name'] for task in questions}
        if len(names) != len(questions):
            raise ValueError('a group cannot contain revisions with the same question name')
        replacements = {task['name']: task for task in questions}
        combined = [replacements.get(task['name'], task) for task in current]
        current_names = {task['name'] for task in current}
        combined.extend(task for task in questions if task['name'] not in current_names)
        group_id = '+'.join(keys)
        variants = [('combination:'+group_id, combined, None)]
        variants.extend(('ablation:'+group_id+':without:'+key,
                         [task for task in combined if task['name'] != bank_by_id[key]['name']], key)
                        for key in keys)
        for name, tasks, omitted in variants:
            experiment = {'kind': 'ablation' if omitted else 'combination', 'group_ids': list(keys),
                          'omitted_id': omitted,
                          'baseline_question_ids': [question_id(task) for task in current],
                          'reference_group_question_ids': [question_id(task) for task in combined],
                          **_difference(current, tasks)}
            planned.append({'name': name, 'tasks': tasks, 'experiment': experiment})
        if len(planned) > max_configurations:
            raise ValueError('feature group plan exceeds configuration ceiling; select fewer groups or raise it explicitly')
    return deepcopy(planned)
