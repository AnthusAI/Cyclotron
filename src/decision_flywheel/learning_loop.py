"""One cyclotron's decide-review-learn loop, shared by every application.

The web workspace (``WorkspaceSession``) and embedded applications
(``embedded_cyclotron.Cyclotron``) use these functions, so partitions,
propensities, optimization cadence and metrics follow one implementation.
All learning still happens inside ``DecisionFlywheel``.
"""
from __future__ import annotations

from typing import Callable, Mapping

from .calibration_history import reviewed_calibration_metrics
from .feedback_trigger import LabelTransitionTrigger, PROTECTED_ASSIGNMENTS, learning_feedback
from .flywheel import development_assignment
from .models import Item, LabeledItem

# Reviews a reviewer chose to make are never audit or development evidence.
REVIEWER_SELECTED = 'reviewer-selected'


def review_role(seed, item_id):
    """Assign a review's partition from the item identity, before its label is seen."""
    if development_assignment(seed + ':audit', item_id, rate=.2):
        return 'scoreboard'
    return 'development' if development_assignment(seed, item_id) else 'training'


def feedback_partitions(wheel, items: Mapping[str, Mapping], seed):
    """Split one classifier's active labels into training, development and protected items.

    ``items`` maps every known item identity to its values. Unlabeled and audit
    items are protected firewalls: they never reach the optimizer or the fit.
    """
    active = {}
    for event in wheel.history(100000):
        if event['kind'] == 'human-feedback':
            feedback = event['feedback']
            active.pop(feedback['item_id'], None)
            if event.get('action') == 'submitted':
                active[feedback['item_id']] = event
    training, development, protected = [], [], []
    task = wheel.initial.task
    for item_id, values in items.items():
        item = Item(item_id, values)
        assignment = active[item_id].get('assignment') if item_id in active else None
        if item_id not in active or (assignment != REVIEWER_SELECTED and (
                assignment in PROTECTED_ASSIGNMENTS or development_assignment(seed + ':audit', item_id, rate=.2))):
            protected.append(item)
            continue
        feedback = active[item_id]['feedback']
        comment = feedback.get('edit_comment_value')
        labeled = LabeledItem(item, task.validate_label(feedback['final_answer_value']), 'trusted',
                              {'human_feedback': comment} if comment else {},
                              initial_answer_value=feedback.get('initial_answer_value'))
        is_development = assignment != REVIEWER_SELECTED and (
            assignment == 'development' or development_assignment(seed, item_id))
        (development if is_development else training).append(labeled)
    # Preserve feedback arrival order for recent-balanced evaluation selection.
    position = {item_id: index for index, item_id in enumerate(active)}
    training.sort(key=lambda row: position[row.item.id])
    development.sort(key=lambda row: position[row.item.id])
    return tuple(training), tuple(development), tuple(protected)


async def decide_with_shared_context(wheels, shared, target, partitions: Callable, *, now, checkpoint=None):
    """Make one shared decision-model request and one decision per classifier.

    Stale ML models are refit inside the item's cycle first. Each wheel's
    cycle is left suspended, waiting for the review. Returns per-classifier
    ``DecisionResult`` values and the checkpoint's return value.
    """
    training, configs = {}, {}
    for identifier, wheel in wheels.items():
        train, dev, _ = partitions(identifier)
        wheel.reconcile_feedback(train, development=dev)
        training[identifier] = train
        configs[identifier] = wheel.active.config
    shared.bind_context(configs, training)
    # Refit stale heads inside the item's operational cycle. Sibling
    # context changes can alter probability features even when this
    # classifier's own question list stays unchanged.
    for _ in range(len(wheels) + 1):
        stale = [identifier for identifier, wheel in wheels.items() if wheel.active.head and
                 (wheel.active.head.provenance.source_model_provenance != wheel.model_context(wheel.active.config, training[identifier])
                  or not wheel.active.answer_dependencies
                  or not wheel.answer_dependencies_current(wheel.active.answer_dependencies))]
        if not stale:
            break
        for identifier in stale:
            wheel = wheels[identifier]
            cycle = wheel.resume_cycle(target) or wheel.cycle(target).__enter__()
            try:
                wheel.reconcile_model_context(training[identifier])
                cycle.check_trigger('classifier', due=True, reason='shared decision feature source changed', details={})
                train, dev, protected = partitions(identifier)
                await wheel.step('classifier', train, dev, protected=protected,
                                 propensities={r.item.id: 1. for r in train}, min_development_per_class=2,
                                 limit=200, trigger='shared-context-change')
                cycle.suspend()
            except Exception as error:
                cycle.__exit__(type(error), error, None)
                raise
        configs = {identifier: wheel.active.config for identifier, wheel in wheels.items()}
        shared.bind_context(configs, training)
    # A bounded reconciliation must never serve an incompatible head.
    for identifier, wheel in wheels.items():
        wheel.reconcile_model_context(training[identifier])
    checkpointed = checkpoint() if checkpoint else None
    await shared.prepare(configs, target, training, now=now)
    results = {}
    for identifier, wheel in wheels.items():
        cycle = wheel.resume_cycle(target) or wheel.cycle(target).__enter__()
        try:
            results[identifier] = await wheel.predict(target, training[identifier], now=now)
            cycle.suspend()
        except Exception as error:
            cycle.__exit__(type(error), error, None)
            raise
    return results, checkpointed


async def learn_from_review(wheels, shared, identifier, cycle, partitions: Callable, *, optimize_every,
                            rubric_changes_every, resume_stages=()):
    """Run the optimization stages a new review makes due; return warnings."""
    warnings = []
    wheel = wheels[identifier]
    training, development, protected = partitions(identifier)
    wheel.reconcile_feedback(training, development=development)
    wheel.set_optimizer_context([row.context['human_feedback'] for row in training if row.context.get('human_feedback')])
    history = wheel.history(100000)
    latest = next((e for e in reversed(history) if e['kind'] == 'human-feedback'), None)
    eligible = bool(latest and learning_feedback(latest) and latest.get('action') == 'submitted')
    check = LabelTransitionTrigger(rubric_changes_every).check(history)
    cycle.check_trigger('rubric', **check)
    count = len(training) + len(development)
    stages = ['rubric'] if check['due'] else []
    for stage in ('questions', 'examples', 'classifier'):
        due = eligible and count > 0 and count % optimize_every == 0
        cycle.check_trigger(stage, due=due, reason='feedback cadence reached' if due else 'feedback cadence not reached',
                            details={'feedback_count': count, 'threshold': optimize_every})
        if due:
            stages.append(stage)
    stages = list(dict.fromkeys([*stages, *resume_stages]))
    for stage in stages:
        shared.bind_context({cid: w.active.config for cid, w in wheels.items()},
                            {cid: partitions(cid)[0] for cid in wheels})
        transport = getattr(wheel.optimizer, 'complete', None)
        if stage != 'classifier' and hasattr(transport, 'max_calls') and transport.calls >= transport.max_calls:
            wheel._emit({'kind': 'optimization-paused', 'stage': stage, 'reason': 'optimizer call limit reached',
                         'calls': transport.calls, 'limit': transport.max_calls})
            warnings.append({'classifier_id': identifier, 'reason': 'optimizer call limit reached; labeling can continue'})
            continue
        result = await wheel.step(stage, training, development, protected=protected,
                                  propensities={row.item.id: 1. for row in training}, min_development_per_class=2,
                                  limit=200, trigger='label-transitions' if stage == 'rubric' else 'feedback-cadence')
        if result['status'] not in ('completed', 'waiting'):
            raise RuntimeError('optimization step did not complete')
        if stage in ('rubric', 'questions', 'examples') and result['status'] == 'completed':
            await wheel.step('classifier', training, development, protected=protected,
                             propensities={row.item.id: 1. for row in training}, min_development_per_class=2,
                             trigger='context-handoff')
    return warnings


def record_cycle_metrics(wheel, class_config):
    """Emit the latest-200 reviewed metrics event the trace UI reads."""
    wheel._emit({'kind': 'cycle-metrics', 'class_config': class_config,
                 'metric_scope': 'latest 200 human-labeled items; prequential predictions, not protected evaluation',
                 'metrics': reviewed_calibration_metrics(wheel.initial.task.labels, wheel.history(100000))})
