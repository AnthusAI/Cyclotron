"""Measured questions must reach a fitted classifier without another discovery call."""
import asyncio

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV
from .optimizer_agent import OptimizerAgent
from .classifier_training import train_classifier


def forbidden(_):
    raise AssertionError("fitting must not invoke the optimizer")


def test_a_winning_group_is_served_with_both_confidence_features_and_restored_after_restart(tmp_path):
    from .feature_bank import FeatureBank
    from .models import DecisionResult, Item, LabeledItem
    from .classifier_config import ClassifiedAnswers
    from .selection_policy import SelectionPolicy

    class TwoFactorModel(FakeModel):
        async def classify(self, config, target, training, **kwargs):
            self.calls += 1
            answers = {'decision': DecisionResult('exclude', {'include': .5, 'exclude': .5})}
            for task in config.tasks:
                p = .95 if target.values[task.name] else .05
                answers[task.name] = DecisionResult('yes' if p > .5 else 'no', {'yes': p, 'no': 1-p})
            return ClassifiedAnswers(answers, 'fake-two-factor', {'tokens': 0}, 0)

    def rows(prefix, counts):
        return tuple(LabeledItem(Item(f'{prefix}-{a}-{b}-{i}', {'text': f'{prefix} {a} {b} {i}',
            'a': a, 'b': b}), 'include' if a and b else 'exclude')
            for (a, b), count in counts for i in range(count))

    training = rows('train', [((False, False), 4), ((False, True), 4),
                              ((True, False), 4), ((True, True), 12)])
    development = rows('dev', [((False, False), 6), ((False, True), 7),
                                ((True, False), 7), ((True, True), 20)])
    config = ClassifierConfig(TASK, rubric='Frozen', example_ids=(training[0].item.id, training[-1].item.id))
    path = tmp_path/'wheel.sqlite'
    model = TwoFactorModel()
    wheel = DecisionFlywheel(path, config, model, OptimizerAgent(forbidden), max_requests=500,
                            selection_policy=SelectionPolicy('f1', positive_class='include'))
    keys = [FeatureBank(wheel.db).register({'name': name, 'instructions': name+'?', 'labels': ['yes', 'no']},
        rationale='Independent training idea', evidence={}) for name in ('a', 'b')]
    kwargs = dict(protected=(Item('sealed', {'text':'not for selection'}),),
        propensities={row.item.id:1. for row in training}, feature_groups=(tuple(keys),),
        max_group_configurations=3)
    result = asyncio.run(train_classifier(wheel, training, development, **kwargs))
    assert result['development_counts'] == {'include':20, 'exclude':20}
    assert result['promoted'] and result['selected']['feature_experiment']['kind'] == 'combination'
    assert result['selected']['candidate']['per_class']['include']['recall'] == 1.
    assert {'a/yes', 'b/yes'} <= set(wheel.active.head.feature_names)
    assert wheel.active.head.calibration.fit_on == 'out_of_fold'
    assert wheel.active.config.rubric == 'Frozen' and wheel.active.config.example_ids == config.example_ids
    version = wheel.active.fingerprint
    predictions = [asyncio.run(wheel.predict(row.item, training)) for row in development]
    assert all(prediction.label == row.label for prediction, row in zip(predictions, development))
    calls = model.calls
    wheel.close()
    restored = DecisionFlywheel(path, config, model, OptimizerAgent(forbidden), max_requests=500)
    try:
        assert restored.active.fingerprint == version
        assert asyncio.run(restored.predict(development[0].item, training)) == predictions[0]
        assert model.calls == calls
    finally:
        restored.close()


def test_group_trials_pause_at_the_request_ceiling_and_require_explicit_restart_authorization(tmp_path):
    import pytest
    from .feature_bank import FeatureBank
    from .observability import RequestBudgetExhausted
    model = FakeModel()
    config = ClassifierConfig(TASK)
    path = tmp_path/'wheel.sqlite'
    wheel = DecisionFlywheel(path, config, model, OptimizerAgent(forbidden), max_requests=24)
    keys = [FeatureBank(wheel.db).register({'name': name, 'instructions': name+'?', 'labels': ['yes', 'no']},
        rationale='Retained', evidence={}) for name in ('practical', 'other')]
    kwargs = dict(protected=(), propensities={row.item.id:1. for row in TRAIN},
        min_development_per_class=1, apply_promotion=False,
        feature_groups=(tuple(keys),), max_group_configurations=3)
    with pytest.raises(RequestBudgetExhausted):
        asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs))
    assert model.calls == 24 and wheel.active.config == config
    assert wheel.db.execute("SELECT status FROM classifier_training").fetchone()[0] == 'pending'
    wheel.close()
    restored = DecisionFlywheel(path, config, model, OptimizerAgent(forbidden), max_requests=100)
    try:
        waiting = asyncio.run(train_classifier(restored, TRAIN, DEV, **kwargs))
        assert 'interrupted' in waiting['reason'] and model.calls == 24
        result = asyncio.run(train_classifier(restored, TRAIN, DEV, retry_interrupted=True, **kwargs))
        assert len([trial for trial in result['trials'] if trial.get('feature_experiment')]) == 6
        assert model.calls == 32  # completed single-factor and ablation requests are reused
        assert not result['promoted'] and restored.active.config == config
    finally:
        restored.close()


def test_explicit_groups_and_their_ablations_freeze_context_and_resume_without_repeating_requests(tmp_path):
    from .feature_bank import FeatureBank
    from .models import Item
    class RecordingModel(FakeModel):
        def __init__(self):
            super().__init__()
            self.configs = []
        async def classify(self, config, target, training, **kwargs):
            self.configs.append((config, target.id))
            return await super().classify(config, target, training, **kwargs)
    model = RecordingModel()
    config = ClassifierConfig(TASK, rubric='Fixed criteria', example_ids=('t0', 't1'))
    path = tmp_path/'wheel.sqlite'
    wheel = DecisionFlywheel(path, config, model, OptimizerAgent(forbidden))
    keys = [FeatureBank(wheel.db).register({'name': name, 'instructions': name+'?', 'labels': ['yes', 'no']},
        rationale='Retained training-only idea', evidence='training-only') for name in ('practical', 'other')]
    kwargs = dict(protected=(Item('audit', {'text':'never show protected evidence'}),),
        propensities={row.item.id: 1. for row in TRAIN}, min_development_per_class=1,
        apply_promotion=False, feature_groups=(tuple(keys),), max_group_configurations=3)
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs))
    grouped = [trial for trial in result['trials'] if trial.get('feature_experiment')]
    assert len(grouped) == 6  # three configurations, two numerical weightings
    assert {trial['feature_experiment']['kind'] for trial in grouped} == {'combination', 'ablation'}
    assert all(trial['incumbent'] == grouped[0]['incumbent'] for trial in grouped)
    assert all(candidate.rubric == config.rubric and candidate.example_ids == config.example_ids
               for candidate, _ in model.configs)
    assert not any(target == 'audit' for _, target in model.configs)
    assert wheel.active.config == config and not result['promoted']
    assert {entry['id'] for entry in wheel.feature_bank()} == set(keys)
    assert len([event for event in wheel.history(10000) if event['kind']=='feature-group-trial-completed']) == 6
    calls = model.calls
    assert asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs)) == result
    assert model.calls == calls
    wheel.close()
    restored = DecisionFlywheel(path, config, model, OptimizerAgent(forbidden))
    try:
        assert asyncio.run(train_classifier(restored, TRAIN, DEV, **kwargs)) == result
        assert model.calls == calls
    finally:
        restored.close()


def test_an_over_budget_group_plan_is_rejected_before_any_provider_or_optimizer_call(tmp_path):
    import pytest
    from .feature_bank import FeatureBank
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', ClassifierConfig(TASK), model, OptimizerAgent(forbidden))
    keys = [FeatureBank(wheel.db).register({'name':name, 'instructions':name+'?', 'labels':['yes','no']},
        rationale='Retained', evidence={}) for name in ('practical','other')]
    try:
        with pytest.raises(ValueError, match='configuration ceiling'):
            asyncio.run(train_classifier(wheel, TRAIN, DEV, protected=(),
                propensities={row.item.id:1. for row in TRAIN}, min_development_per_class=1,
                feature_groups=(tuple(keys),), max_group_configurations=2))
        assert model.calls == 0
        assert not any(event['kind']=='classifier-training-started' for event in wheel.history())
    finally:
        wheel.close()


def test_a_retained_wording_revision_is_evaluated_deployed_and_restored_with_new_features(tmp_path):
    from dataclasses import replace
    from .feature_bank import FeatureBank
    from .models import DecisionTask, DecisionResult
    class RevisionModel(FakeModel):
        async def classify(self, config, target, training, **kwargs):
            batch = await super().classify(config, target, training, **kwargs)
            answers = dict(batch.answers)
            for task in config.tasks:
                p = (.98 if target.values['text'].startswith('yes') else .02) if task.instructions == 'Revised?' else .5
                positive, negative = task.labels
                answers[task.name] = DecisionResult(positive if p > .5 else negative,
                    {positive:p, negative:1-p})
            return replace(batch, answers=answers)
    old = DecisionTask('practical', ('yes','no'), 'Original?')
    other = DecisionTask('other', ('yes','no'), 'Unrelated?')
    config = ClassifierConfig(TASK, rubric='Existing', tasks=(old, other))
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, RevisionModel(), OptimizerAgent(forbidden))
    revision = {'name':'practical', 'instructions':'Revised?', 'labels':['present','absent']}
    key = FeatureBank(wheel.db).register(revision, rationale='Human explanations clarify the factor', evidence={})
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, protected=(),
        propensities={row.item.id:1. for row in TRAIN}, min_development_per_class=1))
    assert any(trial['feature_set'] == f'revision:{key}' for trial in result['trials'])
    assert result['promoted']
    assert wheel.active.config.tasks[0].instructions == 'Revised?'
    assert wheel.active.config.tasks[1] == other
    assert wheel.active.config.rubric == config.rubric
    assert 'practical/present' in wheel.active.head.feature_names
    assert 'practical/yes' not in wheel.active.head.feature_names
    version = wheel.active.fingerprint
    wheel.close()
    reopened = DecisionFlywheel(tmp_path/'wheel.sqlite', config, RevisionModel(), OptimizerAgent(forbidden))
    assert reopened.active.fingerprint == version
    assert reopened.active.config.tasks[0].instructions == 'Revised?'
    reopened.close()


def test_training_can_evaluate_removing_one_question_without_discarding_the_retained_idea(tmp_path):
    from .models import DecisionTask
    from .feature_bank import FeatureBank
    question = DecisionTask('practical', ('yes','no'), 'Practical?')
    config = ClassifierConfig(TASK, tasks=(question,))
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, FakeModel(), OptimizerAgent(forbidden))
    definition = {'name':question.name, 'instructions':question.instructions, 'labels':list(question.labels)}
    FeatureBank(wheel.db).register(definition, rationale='Retain hypothesis', evidence={})
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, protected=(),
        propensities={row.item.id:1. for row in TRAIN}, min_development_per_class=1, apply_promotion=False))
    assert any(trial['feature_set'] == 'without:practical' for trial in result['trials'])
    assert wheel.active.config == config
    assert wheel.feature_bank()[0]['question'] == definition
    wheel.close()


def test_a_winning_question_removal_rebuilds_the_head_and_changes_the_next_request(tmp_path):
    from dataclasses import replace
    from .models import DecisionTask, DecisionResult
    requests = []
    class HarmfulFeatureModel(FakeModel):
        async def classify(self, config, target, training, **kwargs):
            requests.append(config.request(target, training, now=kwargs.get('now')))
            batch = await super().classify(config, target, training, **kwargs)
            positive = target.values['text'].startswith('yes')
            p = .7 if positive else .3
            answers = {'decision': DecisionResult('include' if positive else 'exclude', {'include':p, 'exclude':1-p})}
            for task in config.tasks:
                signal = positive if target.id.startswith('t') else not positive
                p = .98 if signal else .02
                answers[task.name] = DecisionResult('yes' if signal else 'no', {'yes':p, 'no':1-p})
            return replace(batch, answers=answers)
    config = ClassifierConfig(TASK, tasks=(DecisionTask('shortcut', ('yes','no'), 'Shortcut?'),))
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, HarmfulFeatureModel(), OptimizerAgent(forbidden))
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, protected=(),
        propensities={row.item.id:1. for row in TRAIN}, min_development_per_class=1))
    assert result['promoted'] and result['selected']['feature_set'] == 'without:shortcut'
    assert wheel.active.config.tasks == ()
    assert not any(name.startswith('shortcut/') for name in wheel.active.head.feature_names)
    asyncio.run(wheel.predict(DEV[0].item, TRAIN))
    assert set(requests[-1]['questions']) == {'decision'}
    wheel.close()


def test_alternative_revisions_of_a_new_concept_are_not_hidden_by_question_name(tmp_path):
    from .feature_bank import FeatureBank
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', ClassifierConfig(TASK), FakeModel(), OptimizerAgent(forbidden))
    keys = [FeatureBank(wheel.db).register({'name':'practical', 'instructions':wording, 'labels':['yes','no']},
        rationale='Compare measurements', evidence={}) for wording in ['Broad?', 'Specific?']]
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, protected=(),
        propensities={row.item.id:1. for row in TRAIN}, min_development_per_class=1, apply_promotion=False))
    assert {f'addition:{key}' for key in keys} <= {trial['feature_set'] for trial in result['trials']}
    wheel.close()


def test_classifier_selection_uses_configured_f1_even_when_brier_and_other_class_recall_regress(tmp_path):
    from .selection_policy import SelectionPolicy
    wheel = DecisionFlywheel(tmp_path / 'runtime.sqlite', ClassifierConfig(TASK), FakeModel(), OptimizerAgent(forbidden),
                             selection_policy=SelectionPolicy('f1', positive_class='include'))
    async def score(classifier, *args):
        candidate = classifier.head is not None
        return {'balanced_brier': .9 if candidate else .1, 'balanced_accuracy': .5,
                'per_class': {'include': {'precision': .8, 'recall': .8 if candidate else .2},
                              'exclude': {'precision': .8, 'recall': .2 if candidate else .8}}}
    wheel._score = score
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, protected=(),
        propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1))
    assert result['promoted']
    assert result['selected']['selection']['policy']['primary'] == 'f1'
    wheel.close()


def test_raw_decision_model_can_replace_a_head_using_the_same_configured_objective(tmp_path):
    from .selection_policy import SelectionPolicy
    wheel = DecisionFlywheel(tmp_path / 'runtime.sqlite', ClassifierConfig(TASK), FakeModel(), OptimizerAgent(forbidden),
                             selection_policy=SelectionPolicy('f1', positive_class='include'))
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1)
    assert asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs))['promoted']
    async def score(classifier, *args):
        value = .4 if classifier.head else .9
        return {'balanced_brier': .2, 'balanced_accuracy': value,
                'per_class': {label: {'precision':value, 'recall':value} for label in TASK.labels}}
    wheel._score = score
    wheel.set_optimizer_context(['More labels justify a new comparison'])
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs))
    assert result['promoted'] and result['selected']['feature_set'] == 'raw_decision'
    assert wheel.active.head is None
    assert result['selected']['selection']['candidate_scores']['f1'] == .9
    event = next(e for e in wheel.history() if e['kind'] == 'promoted')
    assert event['selection']['policy']['primary'] == 'f1'
    wheel.close()


def test_raw_candidate_can_also_win_under_the_legacy_brier_policy(tmp_path):
    from .feature_bank import FeatureBank
    wheel = DecisionFlywheel(tmp_path / 'runtime.sqlite', ClassifierConfig(TASK), FakeModel(), OptimizerAgent(forbidden))
    FeatureBank(wheel.db).register({'name':'practical','instructions':'Practical?', 'labels':['yes','no']},
                                 rationale='Feedback', evidence='training-only')
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1)
    assert asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs))['promoted']
    async def score(classifier, *args):
        return {'balanced_brier':.8 if classifier.head else .2, 'balanced_accuracy':1.,
                'per_class':{label:{'recall':1.} for label in TASK.labels}}
    wheel._score = score
    wheel.set_optimizer_context(['Recompare the current context'])
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs))
    assert result['promoted'] and wheel.active.head is None
    wheel.close()


def test_training_waits_for_development_coverage_without_paid_calls(tmp_path):
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), model, OptimizerAgent(forbidden))
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, protected=(),
        propensities={r.item.id: 1. for r in TRAIN}))
    assert not result["promoted"]
    assert result["development_counts"] == {"include": 1, "exclude": 1}
    assert model.calls == 0
    wheel.close()


def test_measured_questions_are_fitted_and_restarts_reuse_the_selected_classifier(tmp_path):
    from .feature_bank import FeatureBank
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), model, OptimizerAgent(forbidden))
    FeatureBank(wheel.db).register({"name": "practical", "instructions": "Practical?", "labels": ["yes", "no"]},
        rationale="Feedback", evidence="training-only")
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1)
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs))
    assert len(result["trials"]) == 4
    assert any("practical/yes" in event.get("features", []) for event in wheel.history() if event["kind"] == "fit-completed")
    assert result["promoted"]
    assert wheel.active.head is not None
    assert "practical/yes" in wheel.active.head.feature_names
    calls = model.calls
    assert asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs)) == result
    assert model.calls == calls
    wheel.close()


def test_exploratory_training_does_not_deploy_or_use_protected_labels(tmp_path):
    from .models import Item
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), model, OptimizerAgent(forbidden))
    before = wheel.active.fingerprint
    protected = (Item("sealed", {"text": "sealed target"}),)
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, protected=protected,
        propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1, apply_promotion=False))
    assert wheel.active.fingerprint == before
    assert not result["promoted"]
    assert not any(event.get("target_id") == "sealed" for event in wheel.history())
    assert all(t["candidate"]["count"] == len(DEV) for t in result["trials"])
    wheel.close()


def test_a_context_candidate_cannot_promote_by_sacrificing_a_class_for_lower_brier(tmp_path):
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), FakeModel(), OptimizerAgent(forbidden))
    async def score(classifier, *args):
        candidate = classifier.head is not None
        return {"balanced_brier": .1 if candidate else .9, "balanced_accuracy": .5 if candidate else 1.,
                "per_class": {"include": {"recall": 0. if candidate else 1.}, "exclude": {"recall": 1.}}}
    wheel._score = score
    result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN},
        candidate_proposal={"rubric": "new rubric"}, require_recall_safeguards=True))
    assert result["brier_improved"]
    assert not result["recall_safeguards_passed"]
    assert not result["promoted"]
    assert wheel.active.config.rubric == ""
    wheel.close()
