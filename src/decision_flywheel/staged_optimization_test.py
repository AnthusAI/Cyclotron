"""Example, rubric and question stages never silently run each other."""
import asyncio
import json
import pytest

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV
from .optimizer_agent import OptimizerAgent, OptimizerReply
from .staged_optimization import optimize_stage


def test_an_explicit_feature_group_runs_through_the_inspectable_numerical_step_only(tmp_path):
    from .feature_bank import FeatureBank
    def forbidden(_):
        raise AssertionError('group experiments must not call the optimizer')
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', ClassifierConfig(TASK), FakeModel(), OptimizerAgent(forbidden))
    keys = [FeatureBank(wheel.db).register({'name':name,'instructions':name+'?','labels':['yes','no']},
        rationale='Feedback concept', evidence={}) for name in ('practical','other')]
    try:
        result = asyncio.run(wheel.step('classifier', TRAIN, DEV, protected=(),
            propensities={row.item.id:1. for row in TRAIN}, min_development_per_class=1,
            feature_groups=(tuple(keys),), max_group_configurations=3, trigger='explicit-feature-group'))
        assert result['status'] == 'completed'
        assert len(result['result']['feature_group_plan']) == 3
        events = [event for event in wheel.history(10000) if event['kind']=='feature-group-trial-completed']
        assert len(events) == 6 and all(event['step_id'] == result['step_id'] for event in events)
        assert all(event['step_stage'] == 'classifier' for event in events)
    finally:
        wheel.close()


@pytest.mark.parametrize('stage',['rubric','examples','questions'])
def test_other_stages_cannot_silently_run_feature_group_experiments(tmp_path, stage):
    def forbidden(_):
        raise AssertionError('invalid mixed stages must not reach the optimizer')
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', ClassifierConfig(TASK), model, OptimizerAgent(forbidden))
    try:
        with pytest.raises(ValueError, match='classifier stage'):
            asyncio.run(optimize_stage(wheel, stage, TRAIN, DEV, protected=(),
                propensities={row.item.id:1. for row in TRAIN}, feature_groups=(('one','two'),)))
        assert model.calls == 0
    finally:
        wheel.close()


def test_question_discovery_can_test_datetime_alone_and_send_it_in_actual_requests(tmp_path):
    from dataclasses import replace
    from datetime import datetime
    from .models import DecisionResult
    from .staged_optimization import stage_briefing
    requests = []
    class RecordingModel(FakeModel):
        async def classify(self, config, target, training, *, now=None, event_sink=None):
            requests.append(config.request(target, training, now=now))
            batch = await super().classify(config, target, training, now=now, event_sink=event_sink)
            if config.dynamic_elements:
                p = .98 if target.values['text'].startswith('yes') else .02
                batch = replace(batch, answers={'decision': DecisionResult(
                    'include' if p > .5 else 'exclude', {'include':p, 'exclude':1-p})})
            return batch
    optimizer = OptimizerAgent(lambda _: OptimizerReply(
        '{"dynamic_elements":["current_datetime"],"rationale":"Compare article date to current time"}', 'fake'))
    config = ClassifierConfig(TASK, rubric='Recent practical work', example_ids=('t0',))
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, RecordingModel(), optimizer)
    briefing = stage_briefing(wheel, 'questions', TRAIN, DEV, ())
    assert briefing.payload['current']['allowed_proposal_controls'] == ['tasks', 'dynamic_elements']
    result = asyncio.run(optimize_stage(wheel, 'questions', TRAIN, DEV, protected=(),
        propensities={row.item.id:1. for row in TRAIN}, min_development_per_class=1))
    assert result['control_under_test'] == 'dynamic_elements'
    dynamic_requests = [request for request in requests if 'current_datetime' in request['state']]
    assert dynamic_requests
    for request in dynamic_requests:
        assert datetime.fromisoformat(request['state']['current_datetime'].replace('Z', '+00:00')).utcoffset().total_seconds() == 0
        assert request['state']['rubric'] == config.rubric
        assert set(request['questions']) == {'decision'}
    assert result['proposal']['dynamic_elements'] == ['current_datetime']
    assert result['promoted']
    assert wheel.active.config.dynamic_elements == ('current_datetime',)
    assert wheel.active.config.rubric == config.rubric
    assert wheel.active.config.example_ids == config.example_ids
    assert wheel.active.config.tasks == config.tasks
    wheel.close()
    reopened = DecisionFlywheel(tmp_path/'wheel.sqlite', config, RecordingModel(), optimizer)
    assert reopened.active.config.dynamic_elements == ('current_datetime',)
    prediction = asyncio.run(reopened.predict(DEV[1].item, TRAIN))
    assert prediction.label == 'include'
    assert 'current_datetime' in requests[-1]['state']
    reopened.close()


@pytest.mark.parametrize('proposal', [
    {'tasks': [], 'dynamic_elements': ['current_datetime']},
    {'dynamic_elements': ['current_datetime'], 'rubric': 'Changed'},
    {'dynamic_elements': ['execute_python']},
])
def test_question_dynamic_candidates_cannot_mix_controls_or_execute_generated_code(tmp_path, proposal):
    model = FakeModel()
    config = ClassifierConfig(TASK, rubric='Existing')
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, model,
        OptimizerAgent(lambda _: OptimizerReply(json.dumps(proposal), 'fake')))
    run = lambda: asyncio.run(optimize_stage(wheel, 'questions', TRAIN, DEV, protected=(),
        propensities={row.item.id:1. for row in TRAIN}, min_development_per_class=1))
    if len(proposal) > 1:
        assert run()['validation_status'] == 'invalid-proposal'
    else:
        with pytest.raises(ValueError):
            run()
    assert model.calls == 0
    assert wheel.active.config == config
    wheel.close()


@pytest.mark.parametrize('stage', ['rubric', 'examples'])
def test_a_proposal_that_reaches_outside_its_stage_is_dropped_without_a_model_call(tmp_path, stage):
    model = FakeModel()
    control = 'rubric' if stage == 'rubric' else 'example_ids'
    proposal = {control: 'Changed' if stage == 'rubric' else [],
                'dynamic_elements': ['current_datetime']}
    config = ClassifierConfig(TASK, rubric='Existing')
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, model,
        OptimizerAgent(lambda _: OptimizerReply(json.dumps(proposal), 'fake')))
    result = asyncio.run(optimize_stage(wheel, stage, TRAIN, DEV, protected=(),
        propensities={row.item.id:1. for row in TRAIN}))
    assert (result['promoted'], result['validation_status']) == (False, 'invalid-proposal')
    assert result['reason'] == f'proposal changed dynamic_elements outside its {control} stage; active configuration retained'
    assert wheel.history()[-1]['kind'] == 'optimization-stage-completed'
    assert model.calls == 0
    assert wheel.active.config == config
    wheel.close()


def test_datetime_candidate_cannot_exceed_the_complete_request_budget(tmp_path):
    from .flywheel import _json
    config = ClassifierConfig(TASK, rubric='Existing')
    ceiling = max(len(_json(config.request(row.item, TRAIN)).encode()) for row in (*TRAIN, *DEV))
    requests = []
    class RecordingModel(FakeModel):
        async def classify(self, config, target, training, **kwargs):
            requests.append(config.request(target, training, now=kwargs.get('now')))
            return await super().classify(config, target, training, **kwargs)
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, RecordingModel(),
        OptimizerAgent(lambda _: OptimizerReply('{"dynamic_elements":["current_datetime"]}', 'fake')),
        max_request_bytes=ceiling)
    result = asyncio.run(optimize_stage(wheel, 'questions', TRAIN, DEV, protected=(),
        propensities={row.item.id:1. for row in TRAIN}, min_development_per_class=1))
    assert not result['promoted']
    assert result['error_type'] == 'ValueError'
    assert requests == []
    assert wheel.active.config == config
    wheel.close()


def test_changed_original_prediction_evidence_does_not_reuse_a_completed_optimizer_stage(tmp_path):
    from dataclasses import replace
    calls = []
    def complete(messages):
        calls.append(messages)
        return OptimizerReply('{"rubric":"Existing","rationale":"Keep criteria"}', 'fake')
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', ClassifierConfig(TASK, rubric='Existing'),
                             FakeModel(), OptimizerAgent(complete))
    kwargs = dict(protected=(), propensities={r.item.id:1. for r in TRAIN}, min_development_per_class=1)
    asyncio.run(optimize_stage(wheel, 'rubric', TRAIN, DEV, **kwargs))
    asyncio.run(optimize_stage(wheel, 'rubric', TRAIN, DEV, **kwargs))
    assert len(calls) == 1
    informed = tuple(replace(row, initial_answer_value=row.label) for row in TRAIN)
    asyncio.run(optimize_stage(wheel, 'rubric', informed, DEV, **kwargs))
    assert len(calls) == 2
    wheel.close()


def test_provisional_rubric_recency_cannot_override_a_secondary_accuracy_guard(tmp_path):
    from .selection_policy import SelectionPolicy
    from .flywheel import FittedClassifier
    optimizer = OptimizerAgent(lambda _: OptimizerReply('{"rubric":"Newer criteria"}', 'fake'))
    wheel = DecisionFlywheel(tmp_path / 'wheel.sqlite', ClassifierConfig(TASK, rubric='Working criteria'),
        FakeModel(), optimizer, selection_policy=SelectionPolicy('recall', 'accuracy', positive_class='include'))
    wheel._activate(FittedClassifier(wheel.active.config, validation_status='provisional'))
    async def score(classifier, *args):
        newer = classifier.config.rubric == 'Newer criteria'
        return {'balanced_brier': .1, 'accuracy': .2 if newer else .9,
                'per_class': {label: {'precision':.5, 'recall':1. if newer else .5} for label in TASK.labels}}
    wheel._score = score
    result = asyncio.run(optimize_stage(wheel, 'rubric', TRAIN, DEV, protected=(),
        propensities={r.item.id:1. for r in TRAIN}, min_development_per_class=1))
    assert not result['activated']
    assert result['selection']['reason'] == 'secondary objective regression exceeds allowance'
    assert wheel.active.config.rubric == 'Working criteria'
    wheel.close()


def test_a_failed_context_refit_never_replaces_the_active_configuration_or_head(tmp_path):
    from .candidate_fitting import fit_candidate
    model=FakeModel()
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',ClassifierConfig(TASK,rubric='Existing'),model,
        OptimizerAgent(lambda _:OptimizerReply('{"rubric":"Replacement"}','fake')))
    kwargs=dict(protected=(),propensities={r.item.id:1. for r in TRAIN})
    candidate,_=asyncio.run(fit_candidate(wheel,wheel.active.config,TRAIN,DEV,
        validation_status='provisional',**kwargs))
    wheel._activate(candidate)
    incumbent=wheel.active
    async def fail(*args,**kwargs):
        raise RuntimeError('fake refit failure')
    model.classify=fail
    with pytest.raises(RuntimeError,match='decision feature request failed'):
        asyncio.run(optimize_stage(wheel,'rubric',TRAIN,DEV,**kwargs))
    assert wheel.active==incumbent
    wheel.close()
    reopened=DecisionFlywheel(tmp_path/'wheel.sqlite',ClassifierConfig(TASK,rubric='Existing'),
        FakeModel(),OptimizerAgent(lambda _:None))
    assert reopened.active.fingerprint==incumbent.fingerprint
    assert reopened.active.head.feature_names==incumbent.head.feature_names
    reopened.close()


def test_example_stage_measures_individual_swaps_even_before_development_is_large_enough_to_promote(tmp_path):
    from .example_attribution_test import SignalModel
    config=ClassifierConfig(TASK,rubric='Useful work',example_ids=('t0','t1'))
    optimizer=OptimizerAgent(lambda _:OptimizerReply('{"example_ids":["t0","t3"]}','fake'))
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',config,SignalModel(),optimizer,max_requests=40)
    report=asyncio.run(optimize_stage(wheel,'examples',TRAIN,DEV,
        protected=(),propensities={r.item.id:1. for r in TRAIN}))
    assert report['example_measurement']['rankings'][0]['added_id']=='t3'
    assert report['pending_evaluation'] and not report['promoted']
    assert wheel.active.config==config
    assert any(e['kind']=='example-ranking-completed' for e in wheel.history(1000))
    wheel.close()


def test_reused_development_experiments_are_visible_to_the_optimizer_and_are_not_claimed_as_independent(tmp_path):
    from .example_attribution_test import SignalModel
    from .example_attribution import measure_example_swaps
    from .staged_optimization import stage_briefing
    config=ClassifierConfig(TASK,example_ids=('t0','t1'))
    requests=[]
    def reply(messages):
        requests.append(json.loads(messages[-1]['content']))
        return OptimizerReply('{"example_ids":["t0","t3"]}','fake')
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',config,SignalModel(),OptimizerAgent(reply),max_requests=40)
    kwargs=dict(protected=(),propensities={r.item.id:1. for r in TRAIN})
    asyncio.run(measure_example_swaps(wheel,TRAIN,DEV,**kwargs))
    result=asyncio.run(optimize_stage(wheel,'examples',TRAIN,DEV,**kwargs))
    experiments=requests[0]['current']['example_experiments']
    assert experiments[0]['rankings'][0]['examples']['added']['values']['text']
    assert not result['evaluation_independent_of_optimizer_context']
    assert stage_briefing(wheel,'examples',TRAIN,(),()).payload['current']['example_experiments']==[]
    wheel.close()


def test_an_optimizer_that_cannot_infer_a_cold_start_rubric_defers_without_stopping_feedback(tmp_path):
    replies=iter(['','Later working rubric'])
    optimizer=OptimizerAgent(lambda _:OptimizerReply(json.dumps({'rubric':next(replies),'rationale':'Insufficient early evidence'}),'fake'))
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',ClassifierConfig(TASK),FakeModel(),optimizer)
    kwargs=dict(protected=(),propensities={r.item.id:1. for r in TRAIN})
    first=asyncio.run(optimize_stage(wheel,'rubric',TRAIN[:1],(),**kwargs))
    assert not first['activated'] and not first['promoted']
    assert first['validation_status']=='insufficient-evidence'
    assert wheel.active.config.rubric==''
    second=asyncio.run(optimize_stage(wheel,'rubric',TRAIN,DEV,**kwargs))
    assert second['activated']
    assert wheel.active.config.rubric=='Later working rubric'
    wheel.close()


def test_an_empty_optimizer_rubric_never_erases_a_working_rubric(tmp_path):
    optimizer=OptimizerAgent(lambda _:OptimizerReply('{"rubric":" "}','fake'))
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',ClassifierConfig(TASK,rubric='Working criteria'),FakeModel(),optimizer)
    result=asyncio.run(optimize_stage(wheel,'rubric',TRAIN,DEV,
        protected=(),propensities={r.item.id:1. for r in TRAIN}))
    assert not result['activated'] and result['validation_status']=='invalid-proposal'
    assert wheel.active.config.rubric=='Working criteria'
    wheel.close()


def test_question_stage_backfills_all_available_training_labels_without_a_development_gate(tmp_path):
    calls = []
    def complete(messages):
        calls.append(json.loads(messages[-1]["content"])["current"]["control_under_test"])
        return OptimizerReply('{"rationale":"Practical work","tasks":[{"name":"practical","instructions":"Practical?","labels":["yes","no"]}]}', "fake")
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK, rubric="Existing"), FakeModel(), OptimizerAgent(complete))
    kwargs = dict(protected=tuple(row.item for row in DEV), propensities={r.item.id: 1. for r in TRAIN})
    report = asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), **kwargs))
    assert calls == ["tasks"]
    assert report["count"] == 6
    assert not report["promoted"]
    assert asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), **kwargs)) == report
    assert calls == ["tasks"]
    wheel.close()


def test_the_first_nonempty_rubric_is_used_provisionally_and_survives_restart_without_claiming_validation(tmp_path):
    calls = []
    def complete(messages):
        calls.append(json.loads(messages[-1]["content"])["current"]["control_under_test"])
        return OptimizerReply('{"rationale":"Practical work","rubric":"Practical"}', "fake")
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), model, OptimizerAgent(complete))
    report = asyncio.run(optimize_stage(wheel, "rubric", TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN}))
    assert calls == ["rubric"]
    assert report["proposal"]["rubric"] == "Practical"
    assert report["minimum_development_per_class"] == 20
    assert model.calls == len(TRAIN)
    assert wheel.active.config.rubric == "Practical"
    assert report['activated'] and not report['promoted']
    assert report['validation_status'] == 'provisional'
    assert wheel.active.validation_status == 'provisional'
    assert wheel.active.head is not None
    assert wheel.active.head.provenance.context_artifact_fingerprint == wheel.active.config.fingerprint
    assert any(e['kind']=='context-initialized' and e['configuration']['rubric']=='Practical' for e in wheel.history())
    wheel.close()
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), model, OptimizerAgent(complete))
    assert wheel.active.config.rubric == 'Practical'
    assert wheel.active.validation_status == 'provisional'
    asyncio.run(wheel.predict(TRAIN[0].item, TRAIN))
    assert wheel.active.config.rubric == 'Practical'
    wheel.close()


def test_insufficient_evidence_for_a_replacement_does_not_clear_the_working_rubric(tmp_path):
    optimizer=OptimizerAgent(lambda _:OptimizerReply('{"rubric":"Replacement"}','fake'))
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',ClassifierConfig(TASK,rubric='Working'),FakeModel(),optimizer)
    report=asyncio.run(optimize_stage(wheel,'rubric',TRAIN,DEV,protected=(),propensities={r.item.id:1. for r in TRAIN}))
    assert not report['promoted']
    assert report['validation_status']=='insufficient-evidence'
    assert wheel.active.config.rubric=='Working'
    wheel.close()


def test_a_pending_rubric_is_retested_when_more_development_labels_arrive_without_rediscovery(tmp_path):
    calls=[]
    def complete(messages):
        calls.append(messages)
        return OptimizerReply('{"rubric":"Replacement"}','fake')
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',ClassifierConfig(TASK,rubric='Working'),FakeModel(),OptimizerAgent(complete))
    kwargs=dict(protected=(),propensities={r.item.id:1. for r in TRAIN},min_development_per_class=1)
    first=asyncio.run(optimize_stage(wheel,'rubric',TRAIN,DEV[:1],**kwargs))
    assert first['validation_status']=='insufficient-evidence'
    second=asyncio.run(optimize_stage(wheel,'rubric',TRAIN,DEV,**kwargs))
    assert len(calls)==1
    assert second.get('candidate',{}).get('count')==len(DEV)
    assert any(e['kind']=='pending-proposal-reused' for e in wheel.history())
    wheel.close()


def test_provisional_rubric_refinement_does_not_need_a_tiny_holdout_win_or_retain_a_stale_head(tmp_path):
    replies=iter(['Early rubric','Refined rubric'])
    optimizer=OptimizerAgent(lambda _:OptimizerReply(json.dumps({'rubric':next(replies)}),'fake'))
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',ClassifierConfig(TASK),FakeModel(),optimizer)
    kwargs=dict(protected=(),propensities={r.item.id:1. for r in TRAIN},min_development_per_class=1)
    asyncio.run(optimize_stage(wheel,'rubric',TRAIN,DEV[:1],**kwargs))
    result=asyncio.run(optimize_stage(wheel,'rubric',TRAIN,DEV,**kwargs))
    assert wheel.active.config.rubric=='Refined rubric'
    assert wheel.active.validation_status=='provisional'
    assert wheel.active.head is not None
    assert wheel.active.head.provenance.context_artifact_fingerprint == wheel.active.config.fingerprint
    assert result['activated'] and not result['promoted']
    assert result['candidate']['count']==len(DEV)
    assert any(e['kind']=='context-refined' for e in wheel.history())
    wheel.close()


def test_a_stages_own_promotion_does_not_repeat_discovery_on_unchanged_feedback(tmp_path):
    calls = []
    def complete(messages):
        calls.append(messages)
        return OptimizerReply('{"rationale":"Practical","rubric":"Practical"}', "fake")
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK,rubric='Existing'), FakeModel(), OptimizerAgent(complete))
    score = wheel._score
    async def configured(classifier, *args):
        result = await score(classifier, *args)
        result["balanced_brier"] = .1 if classifier.head else .9
        # This fixture isolates ledger replay, not the recall safeguard.
        result["balanced_accuracy"] = 1.
        for group in result["per_class"].values():
            group["recall"] = 1.
        return result
    wheel._score = configured
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1)
    first = asyncio.run(optimize_stage(wheel, "rubric", TRAIN, DEV, **kwargs))
    assert first["promoted"]
    assert asyncio.run(optimize_stage(wheel, "rubric", TRAIN, DEV, **kwargs)) == first
    assert len(calls) == 1
    wheel.close()


def test_redundant_task_metadata_is_canonicalized_but_cannot_change_the_input_field(tmp_path):
    import pytest
    for field in ("text", "secret_other_field"):
        def complete(messages):
            return OptimizerReply(json.dumps({"rationale": "Measurement", "tasks": [{"name": "practical",
                "instructions": "Practical?", "labels": ["yes", "no"], "input_field": field}]}), "fake")
        wheel = DecisionFlywheel(tmp_path / f"{field}.sqlite", ClassifierConfig(TASK), FakeModel(), OptimizerAgent(complete))
        kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
        if field == "text":
            assert asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), **kwargs))["count"] == 6
        else:
            with pytest.raises(ValueError, match="input field"):
                asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), **kwargs))
        wheel.close()


def test_a_recorded_optimizer_reply_survives_interruption_before_proposal_validation(tmp_path):
    import pytest
    calls = []
    def complete(messages):
        calls.append(messages)
        return OptimizerReply('{"rationale":"Measured topic","tasks":[{"name":"practical","instructions":"Practical?","labels":["yes","no"]}]}', "fake")
    def interrupt(event):
        if event["kind"] == "optimizer-response":
            raise KeyboardInterrupt
    path = tmp_path / "wheel.sqlite"
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), OptimizerAgent(complete), observer=interrupt)
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), **kwargs))
    wheel.close()
    def forbidden(messages):
        raise AssertionError("recovery must not rediscover a recorded proposal")
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), OptimizerAgent(forbidden))
    report = asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), retry_interrupted=True, **kwargs))
    assert report["count"] == 6
    assert len(calls) == 1
    assert any(e["kind"] == "optimizer-response-reused" for e in wheel.history())
    wheel.close()


def test_question_discovery_flows_into_classifier_training_without_rediscovering_after_promotion(tmp_path):
    calls = []
    def complete(messages):
        calls.append(messages)
        return OptimizerReply('{"rationale":"Feedback feature","tasks":[{"name":"practical","instructions":"Practical?","labels":["yes","no"]}]}', "fake")
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), OptimizerAgent(complete))
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1)
    report = asyncio.run(optimize_stage(wheel, "questions", TRAIN, DEV, **kwargs))
    assert report["classifier_training"]["promoted"]
    assert "practical/yes" in wheel.active.head.feature_names
    asyncio.run(optimize_stage(wheel, "questions", TRAIN, DEV, **kwargs))
    assert len(calls) == 1
    wheel.close()
