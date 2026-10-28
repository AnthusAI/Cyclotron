"""One cyclotron's decide-review-learn loop, shared by every application.

The web workspace (``WorkspaceSession``) and embedded applications
(``embedded_cyclotron.Cyclotron``) use these functions, so partitions,
propensities, optimization cadence and metrics follow one implementation.
All learning still happens inside ``DecisionFlywheel``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .calibration_history import REVIEW_BINDING_EVENT_KINDS, reviewed_calibration_metrics
from .feedback_trigger import PROTECTED_ASSIGNMENTS, LabelTransitionTrigger, learning_feedback
from .flywheel import development_assignment
from .models import Item, LabeledItem

# Reviews a reviewer chose to make are never audit or development evidence.
REVIEWER_SELECTED = "reviewer-selected"


def feedback_propensities(wheel):
    """Each item's recorded selection propensity, from its active submitted review (default 1.0)."""
    return {event["feedback"]["item_id"]: event["feedback"].get("selection_propensity") or 1.0 for event in wheel.active_labels()}


def review_role(seed, item_id):
    """Assign a review's partition from the item identity, before its label is seen."""
    if development_assignment(seed + ":audit", item_id, rate=0.2):
        return "scoreboard"
    return "development" if development_assignment(seed, item_id) else "training"


def _classified_label(task, event, item, seed):
    """Place one active label as 'protected', 'development' or 'training'."""
    assignment = event.get("assignment")
    if assignment != REVIEWER_SELECTED and (
        assignment in PROTECTED_ASSIGNMENTS or development_assignment(seed + ":audit", item.id, rate=0.2)
    ):
        return "protected", None
    feedback = event["feedback"]
    comment = feedback.get("edit_comment_value")
    labeled = LabeledItem(
        item,
        task.validate_label(feedback["final_answer_value"]),
        "trusted",
        {"human_feedback": comment} if comment else {},
        initial_answer_value=feedback.get("initial_answer_value"),
    )
    is_development = assignment != REVIEWER_SELECTED and (assignment == "development" or development_assignment(seed, item.id))
    return ("development" if is_development else "training"), labeled


def feedback_partitions(wheel, items: Mapping[str, Mapping], seed):
    """Split one classifier's active labels into training, development and protected items.

    ``items`` maps every known item identity to its values. Unlabeled and audit
    items are protected firewalls: they never reach the optimizer or the fit.
    """
    active = {event["feedback"]["item_id"]: event for event in wheel.active_labels()}
    training, development, protected = [], [], []
    task = wheel.initial.task
    for item_id, values in items.items():
        item = Item(item_id, values)
        if item_id not in active:
            protected.append(item)
            continue
        kind, labeled = _classified_label(task, active[item_id], item, seed)
        if kind == "protected":
            protected.append(item)
        else:
            (development if kind == "development" else training).append(labeled)
    # Preserve feedback arrival order for recent-balanced evaluation selection.
    position = {item_id: index for index, item_id in enumerate(active)}
    training.sort(key=lambda row: position[row.item.id])
    development.sort(key=lambda row: position[row.item.id])
    return tuple(training), tuple(development), tuple(protected)


def labeled_partitions(wheel, item_values_for: Callable, seed):
    """Training and development labels only, reading just the labeled items.

    ``item_values_for`` returns one item's values, or ``None`` when the item is
    unknown. Nothing reads the unlabeled items, so a decision costs the number
    of labels rather than the number of items.
    """
    training, development = [], []
    task = wheel.initial.task
    for event in wheel.active_labels():
        item_id = event["feedback"]["item_id"]
        values = item_values_for(item_id)
        if values is None:
            continue
        kind, labeled = _classified_label(task, event, Item(item_id, values), seed)
        if kind != "protected":
            (development if kind == "development" else training).append(labeled)
    return tuple(training), tuple(development)


async def decide_with_shared_context(
    wheels, shared, target, partitions: Callable, *, now, checkpoint=None, labeled_only_partitions: Callable | None = None
):
    """Make one shared decision-model request and one decision per classifier.

    Stale ML models are refit inside the item's cycle first. Each wheel's
    cycle is left suspended, waiting for the review. Returns per-classifier
    ``DecisionResult`` values and the checkpoint's return value. A caller that
    can read labeled items alone passes ``labeled_only_partitions``, so the
    common path skips the protected items.
    """
    training, configs = {}, {}
    for identifier, wheel in wheels.items():
        train, dev = labeled_only_partitions(identifier) if labeled_only_partitions else partitions(identifier)[:2]
        wheel.reconcile_feedback(train, development=dev)
        training[identifier] = train
        configs[identifier] = wheel.active.config
    shared.bind_context(configs, training)
    # Refit stale heads inside the item's operational cycle. Sibling
    # context changes can alter probability features even when this
    # classifier's own question list stays unchanged.
    for _ in range(len(wheels) + 1):
        stale = [
            identifier
            for identifier, wheel in wheels.items()
            if wheel.active.head
            and (
                wheel.active.head.provenance.source_model_provenance
                != wheel.model_context(wheel.active.config, training[identifier])
                or not wheel.active.answer_dependencies
                or not wheel.answer_dependencies_current(wheel.active.answer_dependencies)
            )
        ]
        if not stale:
            break
        for identifier in stale:
            wheel = wheels[identifier]
            cycle = wheel.resume_cycle(target) or wheel.cycle(target).__enter__()
            try:
                wheel.reconcile_model_context(training[identifier])
                cycle.check_trigger("classifier", due=True, reason="shared decision feature source changed", details={})
                train, dev, protected = partitions(identifier)
                recorded = feedback_propensities(wheel)
                await wheel.step(
                    "classifier",
                    train,
                    dev,
                    protected=protected,
                    propensities={r.item.id: recorded.get(r.item.id, 1.0) for r in train},
                    min_development_per_class=2,
                    trigger="shared-context-change",
                )
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


async def learn_from_review(
    wheels,
    shared,
    identifier,
    cycle,
    partitions: Callable,
    *,
    optimize_every,
    rubric_changes_every,
    resume_stages=(),
    since_count=None,
):
    """Run the optimization stages a new review makes due; return warnings.

    ``since_count`` serves a deferred pass covering several reviews: the
    feedback cadence is due when the label count crossed a multiple of
    ``optimize_every`` since that count, not only when it lands on one.
    """
    warnings = []
    wheel = wheels[identifier]
    training, development, protected = partitions(identifier)
    recorded = feedback_propensities(wheel)
    wheel.reconcile_feedback(training, development=development)
    wheel.set_optimizer_context([row.context["human_feedback"] for row in training if row.context.get("human_feedback")])
    history = wheel.events_of(("human-feedback", "trigger-evaluated"))
    latest = next((e for e in reversed(history) if e["kind"] == "human-feedback"), None)
    eligible = bool(latest and learning_feedback(latest) and latest.get("action") == "submitted")
    if since_count is not None:
        # A deferred pass covers every review since since_count, not only the latest.
        submitted = [e for e in history if e["kind"] == "human-feedback" and e.get("action") == "submitted"]
        recent = submitted[-max(1, len(training) + len(development) - since_count) :]
        eligible = any(learning_feedback(e) for e in recent)
    trigger = LabelTransitionTrigger(rubric_changes_every)
    check = trigger.check(history)
    if since_count is not None and not check["due"]:
        # A deferred pass covers several reviews: run the rubric if any of them made it due.
        positions = [index for index, e in enumerate(history) if e["kind"] == "human-feedback" and e.get("action") == "submitted"]
        for position in reversed(positions[-max(1, len(training) + len(development) - since_count) :]):
            earlier = trigger.check(history[: position + 1])
            if earlier["due"]:
                check = {**earlier, "details": {**earlier["details"], "since_count": since_count}}
                break
    cycle.check_trigger("rubric", **check)
    count = len(training) + len(development)
    stages = ["rubric"] if check["due"] else []
    for stage in ("questions", "examples", "classifier"):
        crossed = count % optimize_every == 0 if since_count is None else count // optimize_every > since_count // optimize_every
        due = eligible and count > 0 and crossed
        cycle.check_trigger(
            stage,
            due=due,
            reason="feedback cadence reached" if due else "feedback cadence not reached",
            details={
                "feedback_count": count,
                "threshold": optimize_every,
                **({"since_count": since_count} if since_count is not None else {}),
            },
        )
        if due:
            stages.append(stage)
    stages = list(dict.fromkeys([*stages, *resume_stages]))
    # Every stage binds the same label pools the steps use, even if labels arrive mid-pass.
    pools = {cid: (training if cid == identifier else partitions(cid)[0]) for cid in wheels}
    for stage in stages:
        shared.bind_context({cid: w.active.config for cid, w in wheels.items()}, pools)
        transport = getattr(wheel.optimizer, "complete", None)
        if stage != "classifier" and hasattr(transport, "max_calls") and transport.calls >= transport.max_calls:
            wheel._emit(
                {
                    "kind": "optimization-paused",
                    "stage": stage,
                    "reason": "optimizer call limit reached",
                    "calls": transport.calls,
                    "limit": transport.max_calls,
                }
            )
            warnings.append({"classifier_id": identifier, "reason": "optimizer call limit reached; labeling can continue"})
            continue
        result = await wheel.step(
            stage,
            training,
            development,
            protected=protected,
            propensities={row.item.id: recorded.get(row.item.id, 1.0) for row in training},
            min_development_per_class=2,
            trigger="label-transitions" if stage == "rubric" else "feedback-cadence",
        )
        if result["status"] not in ("completed", "waiting"):
            raise RuntimeError("optimization step did not complete")
        if stage in ("rubric", "questions", "examples") and result["status"] == "completed":
            await wheel.step(
                "classifier",
                training,
                development,
                protected=protected,
                propensities={row.item.id: recorded.get(row.item.id, 1.0) for row in training},
                min_development_per_class=2,
                trigger="context-handoff",
            )
    return warnings


def record_cycle_metrics(wheel, class_config):
    """Emit the latest-200 reviewed metrics event the trace UI reads."""
    wheel._emit(
        {
            "kind": "cycle-metrics",
            "class_config": class_config,
            "metric_scope": "latest 200 human-labeled items; prequential predictions, not protected evaluation",
            "metrics": reviewed_calibration_metrics(wheel.initial.task.labels, wheel.events_of(REVIEW_BINDING_EVENT_KINDS)),
        }
    )
