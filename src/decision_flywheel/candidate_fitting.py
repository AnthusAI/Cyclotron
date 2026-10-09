"""Build a context-compatible numeric head without activating it."""
from dataclasses import asdict
from datetime import datetime, timezone

from .feedback import Feature, FeedbackItem, LABEL_SOURCE_VETTED
from .head import HeadRow, fit_learned_head


async def fit_candidate(wheel, config, training, development, *, protected,
                        propensities, validation_status, now=None, calibration_method=None):
    from .flywheel import FittedClassifier, _hash
    from .feature_bank import probability_diagnostics
    wheel._validate_partitions(training, development, protected, propensities)
    counts = {label:sum(row.label == label for row in training) for label in config.task.labels}
    if min(counts.values()) < 3:
        wheel._emit({'kind':'head-fit-deferred','reason':'waiting for training class coverage',
                     'counts':counts,'minimum_training_per_class':3,
                     'context_fingerprint':config.fingerprint})
        return FittedClassifier(config, training_evidence=wheel._evidence(training),
                                validation_status=validation_status), {}
    now = now or datetime.now(timezone.utc)
    rows, observations, dependencies = [], {task.name:[] for task in config.tasks}, []
    for row in training:
        batch = await wheel._answers(config, row.item, training, now)
        dependencies.append(wheel.answer_dependency(config, row.item, training, now))
        for task in config.tasks:
            observations[task.name].append((row.label, batch.answers[task.name].probabilities))
        values = wheel._features(config, batch)
        feedback = FeedbackItem('review-'+row.item.id, row.item.id, config.task.name,
            final_answer_value=row.label, edit_comment_value=row.context.get('human_feedback'),
            label_source=LABEL_SOURCE_VETTED, selection_propensity=propensities[row.item.id],
            review_provenance='human-reviewed')
        rows.append(HeadRow(row.item.id, feedback, tuple(
            Feature(key, value, key.split('/',1)[0]) for key,value in values.items())))
    diagnostics = {task.name:probability_diagnostics(config.task.labels, task.labels,
                    observations[task.name]) for task in config.tasks}
    if diagnostics:
        wheel._emit({'kind':'feature-diagnostics','diagnostics':diagnostics,'training_count':len(rows),
                     'scope':'training only'})
    wheel._emit({'kind':'fit-started','training_count':len(rows),'features':list(values),
                 'context_fingerprint':config.fingerprint,
                 'rows':[{'item_id':row.item_id,'label':row.feedback.final_answer_value,
                          'propensity':row.feedback.selection_propensity,
                          'features':{f.name:f.value for f in row.features}} for row in rows]})
    head = fit_learned_head(config.task, rows, declared_features=tuple(values),
        development_ids=tuple(row.item.id for row in development),
        scoreboard_ids=tuple(item.id for item in protected),
        cyclotron_fingerprint=config.fingerprint, policy_fingerprint=_hash(config.example_ids),
        context_artifact_fingerprint=config.fingerprint,
        source_model_provenance=wheel.model_context(config, training),
        training_class_weighting=wheel.training_class_weighting,
        calibration_method=calibration_method or getattr(wheel, 'calibration_method', 'auto'))
    candidate = FittedClassifier(config, head, wheel._evidence(training),
                                wheel._evidence(development), validation_status, tuple(dependencies))
    wheel._emit({'kind':'fit-completed','features':list(head.feature_names),'head':asdict(head),
                 'context_fingerprint':config.fingerprint,'training_count':len(rows),
                 'answer_dependencies': dependencies,
                 'calibration':'out_of_fold'})
    return candidate, diagnostics
