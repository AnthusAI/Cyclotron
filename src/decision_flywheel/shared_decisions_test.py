import asyncio
import pytest
from .shared_decisions import SharedDecisions
from .classifier_config import ClassifierConfig
from .models import DecisionTask,Item,DecisionResult
from .batched_classification import BatchedAnswers
from .decision_cache import CacheOptions


@pytest.mark.parametrize('example_ids', [(), ('target',)])
def test_shared_no_context_models_allow_zero_effective_examples_with_a_training_pool(tmp_path, example_ids):
    from .models import ModelCapabilities, LabeledItem
    class Model:
        model_identity = 'fake'
        capabilities = ModelCapabilities(supports_labeled_context=False)
        calls = 0
        async def classify_many(self, configs, *args, **kwargs):
            self.calls += 1
            return BatchedAnswers({cid:{'decision':DecisionResult('yes', {'yes':.8,'no':.2})}
                for cid in configs}, 'fake', {}, 1)
    target = Item('target', {'text':'New'})
    config = ClassifierConfig(DecisionTask('topic', ('yes','no'), 'Relevant?'), example_ids=example_ids)
    pool = (LabeledItem(target, 'yes'),)
    model = Model()
    shared = SharedDecisions(tmp_path/'shared.sqlite', model, max_requests=5, observer=lambda _:None)
    asyncio.run(shared.prepare({'a':config,'b':config}, target, {'a':pool,'b':pool}))
    assert model.calls == shared.requests == 1
    assert shared.adapter('a').capabilities == model.capabilities
    shared.close()


def test_shared_requests_reject_unsupported_sibling_examples_before_reserving_an_attempt(tmp_path):
    from .models import ModelCapabilities, LabeledItem
    class Model:
        model_identity = 'fake-no-context'
        capabilities = ModelCapabilities(supports_labeled_context=False)
        calls = 0
        async def classify_many(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError('unsupported request reached provider')
    task = DecisionTask('topic', ('yes','no'), 'Relevant?')
    configs = {'a':ClassifierConfig(task), 'b':ClassifierConfig(task, example_ids=('demo',))}
    pools = {'a':(), 'b':(LabeledItem(Item('demo', {'text':'Example'}), 'yes'),)}
    model = Model()
    shared = SharedDecisions(tmp_path/'shared.sqlite', model, max_requests=5, observer=lambda _:None)
    shared.bind_context(configs, pools)
    with pytest.raises(ValueError, match='labeled context'):
        asyncio.run(shared.prepare(configs, Item('target', {'text':'New'}), pools))
    assert model.calls == shared.requests == 0
    with pytest.raises(ValueError, match='labeled context'):
        asyncio.run(shared.adapter('a').classify(configs['a'], Item('target', {'text':'New'}), pools['a']))
    assert model.calls == shared.requests == 0
    shared.close()


def test_a_prepared_batch_does_not_bypass_a_changed_declared_context_capability(tmp_path):
    from .models import ModelCapabilities, LabeledItem
    class Model:
        model_identity = 'fake'
        capabilities = ModelCapabilities()
        calls = 0
        async def classify_many(self, configs, *args, **kwargs):
            self.calls += 1
            return BatchedAnswers({cid:{'decision':DecisionResult('yes', {'yes':.8,'no':.2})}
                for cid in configs}, 'fake', {}, 1)
    task = DecisionTask('topic', ('yes','no'), 'Relevant?')
    configs = {'a':ClassifierConfig(task), 'b':ClassifierConfig(task, example_ids=('demo',))}
    pools = {'a':(), 'b':(LabeledItem(Item('demo', {'text':'Example'}), 'yes'),)}
    target = Item('target', {'text':'New'})
    model = Model()
    shared = SharedDecisions(tmp_path/'shared.sqlite', model, max_requests=5, observer=lambda _:None)
    shared.bind_context(configs, pools)
    asyncio.run(shared.prepare(configs, target, pools))
    model.capabilities = ModelCapabilities(supports_labeled_context=False)
    with pytest.raises(ValueError, match='labeled context'):
        asyncio.run(shared.adapter('a').classify(configs['a'], target, pools['a']))
    assert model.calls == shared.requests == 1
    shared.close()


@pytest.mark.parametrize('restart', [False, True])
def test_refreshing_a_used_training_answer_invalidates_the_head_and_preserves_the_rubric(tmp_path, restart):
    from datetime import datetime, timezone
    from .candidate_fitting import fit_candidate
    from .flywheel import DecisionFlywheel
    from .flywheel_test import FakeModel, TASK, TRAIN, DEV
    class Model(FakeModel):
        async def classify_many(self, configs, target, training, **kwargs):
            answers = {}
            for cid, config in configs.items():
                answers[cid] = (await self.classify(config, target, training[cid], **kwargs)).answers
            return BatchedAnswers(answers, self.model_identity, None, 1)
    config = ClassifierConfig(TASK, rubric='Keep the human criteria')
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    shared = SharedDecisions(tmp_path/'cache.sqlite', Model(), max_requests=100, observer=lambda _: None)
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, shared.adapter('a'), max_requests=100)
    shared.bind_context({'a': config, 'b': config}, {'a': TRAIN, 'b': TRAIN})
    try:
        fitted, _ = asyncio.run(fit_candidate(wheel, config, TRAIN, DEV, protected=(),
            propensities={row.item.id: 1. for row in TRAIN}, validation_status='evaluated', now=now))
        wheel._activate(fitted)
        assert wheel.active.head is not None
        # Refresh through the sibling adapter: this wheel's local answer rows
        # are unchanged, but the physical joint response has a new revision.
        asyncio.run(shared.adapter('b').classify_with_cache_options(config, TRAIN[0].item, TRAIN,
            now=now, cache_options=CacheOptions('refresh')))
        if restart:
            wheel.close(); shared.close()
            shared = SharedDecisions(tmp_path/'cache.sqlite', Model(), max_requests=100, observer=lambda _: None)
            wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, shared.adapter('a'), max_requests=100)
            shared.bind_context({'a': config, 'b': config}, {'a': TRAIN, 'b': TRAIN})
        requests = shared.requests
        assert wheel.reconcile_model_context(TRAIN)
        assert wheel.active.head is None
        assert wheel.active.config.rubric == 'Keep the human criteria'
        assert shared.requests == requests
        assert any(e['kind'] == 'head-invalidated' for e in wheel.history())
    finally:
        wheel.close(); shared.close()


def test_collecting_an_unrelated_answer_does_not_invalidate_a_fitted_head(tmp_path):
    from datetime import datetime, timezone
    from .candidate_fitting import fit_candidate
    from .flywheel import DecisionFlywheel
    from .flywheel_test import FakeModel, TASK, TRAIN, DEV
    class Model(FakeModel):
        async def classify_many(self, configs, target, training, **kwargs):
            answers = {cid: (await self.classify(config, target, training[cid], **kwargs)).answers
                       for cid, config in configs.items()}
            return BatchedAnswers(answers, self.model_identity, None, 1)
    config = ClassifierConfig(TASK)
    shared = SharedDecisions(tmp_path/'cache.sqlite', Model(), max_requests=100, observer=lambda _: None)
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, shared.adapter('a'), max_requests=100)
    shared.bind_context({'a': config}, {'a': TRAIN})
    try:
        fitted, _ = asyncio.run(fit_candidate(wheel, config, TRAIN, DEV, protected=(),
            propensities={row.item.id: 1. for row in TRAIN}, validation_status='evaluated',
            now=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        wheel._activate(fitted)
        asyncio.run(shared.prepare({'a': config}, Item('unrelated', {'text': 'New item'}), {'a': TRAIN}))
        assert not wheel.reconcile_model_context(TRAIN)
        assert wheel.active.head is fitted.head
    finally:
        wheel.close(); shared.close()


def test_a_measured_head_cannot_be_promoted_after_a_sibling_changes_the_joint_request(tmp_path):
    from dataclasses import replace
    from .flywheel import DecisionFlywheel
    from .flywheel_test import FakeModel, TASK, TRAIN, DEV
    from .optimizer_agent import OptimizerAgent
    class Model(FakeModel):
        async def classify_many(self, configs, target, training, **kwargs):
            answers = {}
            for cid, config in configs.items():
                batch = await self.classify(config, target, training[cid], **kwargs)
                answers[cid] = batch.answers
            return BatchedAnswers(answers, self.model_identity, None, 1)
    config = ClassifierConfig(TASK)
    shared = SharedDecisions(tmp_path/'cache.sqlite', Model(), max_requests=100,
                             observer=lambda _: None)
    shared.bind_context({'a': config, 'b': config}, {'a': TRAIN, 'b': TRAIN})
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, shared.adapter('a'),
                             OptimizerAgent(lambda _: None), max_requests=100)
    try:
        result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(),
            propensities={row.item.id: 1. for row in TRAIN},
            candidate_proposal={'rubric': 'Use the human criteria'}, apply_promotion=False))
        assert result['improved']
        before = wheel.active.fingerprint
        shared.bind_context({'a': config, 'b': replace(config, rubric='Changed sibling criteria')},
                            {'a': TRAIN, 'b': TRAIN})
        with pytest.raises(ValueError, match='decision feature context changed'):
            wheel.promote_trial(result['trial_fingerprint'], TRAIN, DEV)
        assert wheel.active.fingerprint == before
    finally:
        wheel.close()
        shared.close()


def test_a_refreshed_trial_is_refitted_under_a_new_identity_without_erasing_the_old_trial(tmp_path):
    from datetime import datetime, timezone
    from .flywheel import DecisionFlywheel
    from .flywheel_test import FakeModel, TASK, TRAIN, DEV
    class Model(FakeModel):
        async def classify_many(self, configs, target, training, **kwargs):
            answers = {cid: (await self.classify(config, target, training[cid], **kwargs)).answers
                       for cid, config in configs.items()}
            return BatchedAnswers(answers, self.model_identity, None, 1)
    config = ClassifierConfig(TASK)
    proposal = {'tasks': [{'name': 'practical', 'instructions': 'Is this practical?', 'labels': ['yes', 'no']}]}
    candidate = config.apply(proposal, TRAIN)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    shared = SharedDecisions(tmp_path/'cache.sqlite', Model(), max_requests=100, observer=lambda _: None)
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, shared.adapter('a'), max_requests=100)
    shared.bind_context({'a': config}, {'a': TRAIN})
    kwargs = dict(protected=(), propensities={row.item.id: 1. for row in TRAIN},
                  candidate_proposal=proposal, apply_promotion=False, evaluation_time=now)
    try:
        first = asyncio.run(wheel.improve(TRAIN, DEV, **kwargs))
        assert first['improved']
        asyncio.run(wheel._answers(candidate, TRAIN[0].item, TRAIN, now, CacheOptions('refresh')))
        with pytest.raises(ValueError, match='decision answers changed'):
            wheel.promote_trial(first['trial_fingerprint'], TRAIN, DEV)
        second = asyncio.run(wheel.improve(TRAIN, DEV, **kwargs))
        assert second['trial_fingerprint'] != first['trial_fingerprint']
        assert sum(event['kind'] == 'fit-completed' for event in wheel.history()) == 2
        assert wheel.db.execute('SELECT COUNT(*) FROM runtime_rounds WHERE status="complete"').fetchone()[0] == 2
        requests = shared.requests
        assert asyncio.run(wheel.improve(TRAIN, DEV, **kwargs)) == second
        assert shared.requests == requests
    finally:
        wheel.close(); shared.close()


@pytest.mark.parametrize('operation', ['questions', 'examples'])
def test_refreshing_a_measurement_response_recomputes_only_the_derived_measurement(tmp_path, operation):
    from .example_attribution import measure_example_swaps
    from .question_measurement import measure_questions
    from .flywheel import DecisionFlywheel
    from .flywheel_test import FakeModel, TASK, TRAIN, DEV
    class Model(FakeModel):
        async def classify_many(self, configs, target, training, **kwargs):
            answers = {cid: (await self.classify(config, target, training[cid], **kwargs)).answers
                       for cid, config in configs.items()}
            return BatchedAnswers(answers, self.model_identity, None, 1)
    config = ClassifierConfig(TASK, example_ids=('t0', 't1'))
    shared = SharedDecisions(tmp_path/'cache.sqlite', Model(), max_requests=100, observer=lambda _: None)
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', config, shared.adapter('a'), max_requests=100)
    shared.bind_context({'a': config}, {'a': TRAIN})
    question = {'name': 'practical', 'instructions': 'Is this practical?', 'labels': ['yes', 'no']}
    props = {row.item.id: 1. for row in TRAIN}
    async def measure():
        if operation == 'questions':
            return await measure_questions(wheel, TRAIN, [question], protected=(), propensities=props)
        return await measure_example_swaps(wheel, TRAIN, DEV, protected=(), propensities=props, max_trials=2)
    target = TRAIN[0].item if operation == 'questions' else DEV[0].item
    measured_config = config.apply({'tasks': [question]}, TRAIN) if operation == 'questions' else config
    try:
        first = asyncio.run(measure())
        assert 'measurement_fingerprint' in first
        asyncio.run(wheel._answers(measured_config, target, TRAIN, None, CacheOptions('refresh')))
        requests = shared.requests
        second = asyncio.run(measure())
        assert second['measurement_fingerprint'] != first['measurement_fingerprint']
        assert shared.requests == requests
        assert asyncio.run(measure()) == second
        assert shared.requests == requests
    finally:
        wheel.close(); shared.close()


def test_explicit_refresh_recollects_the_joint_request_and_updates_all_child_caches(tmp_path):
    from datetime import datetime,timezone
    from .flywheel import DecisionFlywheel
    from .optimizer_agent import OptimizerAgent
    class Model:
        model_identity='fake';calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            positive=self.calls==1
            return BatchedAnswers({name:{'decision':DecisionResult('yes' if positive else 'no',
                {'yes':.8 if positive else .2,'no':.2 if positive else .8})} for name in configs},'fake',None,1)
    def no_optimizer(_):raise AssertionError('cache verification does not optimize')
    config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'));target=Item('one',{'text':'Text'})
    now=datetime(2026,1,1,tzinfo=timezone.utc);model=Model()
    events=[]
    shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=2,observer=events.append)
    shared.bind_context({'a':config,'b':config},{'a':[],'b':[]})
    wheels={cid:DecisionFlywheel(tmp_path/(cid+'.sqlite'),config,shared.adapter(cid),OptimizerAgent(no_optimizer),max_requests=5) for cid in ('a','b')}
    try:
        for wheel in wheels.values():assert asyncio.run(wheel.predict(target,[],now=now)).label=='yes'
        refreshed=asyncio.run(wheels['a'].predict(target,[],now=now,cache_options=CacheOptions('refresh')))
        assert refreshed.label=='no' and model.calls==2
        assert asyncio.run(wheels['b'].predict(target,[],now=now)).label=='no'
        assert asyncio.run(wheels['a'].predict(target,[],now=now)).label=='no'
        assert model.calls==2
        refresh=next(event for event in events if event.get('cache_policy')=='refresh')
        assert refresh['generation']==2 and refresh['cached'] is False
        history=shared.db.execute('SELECT payload FROM batch_history').fetchall()
        assert any('"yes"' in row[0] for row in history if row[0])
    finally:
        for wheel in wheels.values():wheel.close()
        shared.close()


def test_a_failed_shared_refresh_requires_explicit_retry_and_keeps_the_old_answer(tmp_path):
    from datetime import datetime,timezone
    from .flywheel import DecisionFlywheel
    from .optimizer_agent import OptimizerAgent
    class Model:
        model_identity='fake';calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            if self.calls==2:raise RuntimeError('synthetic transport failure')
            return BatchedAnswers({name:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})}
                for name in configs},'fake',None,1)
    def no_optimizer(_):raise AssertionError('cache verification does not optimize')
    model=Model();config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'))
    target=Item('one',{'text':'Text'});now=datetime(2026,1,1,tzinfo=timezone.utc)
    shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=3,observer=lambda _:None)
    shared.bind_context({'a':config},{'a':[]})
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',config,shared.adapter('a'),OptimizerAgent(no_optimizer),max_requests=5)
    try:
        asyncio.run(wheel.predict(target,[],now=now))
        with pytest.raises(RuntimeError):asyncio.run(wheel.predict(target,[],now=now,cache_options=CacheOptions('refresh')))
        with pytest.raises(RuntimeError):asyncio.run(wheel.predict(target,[],now=now))
        assert model.calls==2
        assert asyncio.run(wheel.predict(target,[],now=now,cache_options=CacheOptions('reuse',retry_failed=True))).label=='yes'
        assert model.calls==3
        assert shared.db.execute('SELECT COUNT(*) FROM batch_history WHERE status="complete"').fetchone()[0]==1
    finally:wheel.close();shared.close()


def test_candidate_feature_collection_keeps_the_sibling_cyclotron_context(tmp_path):
    from dataclasses import replace
    class Model:
        model_identity='fake'
        requests=[]
        async def classify_many(self,configs,target,training,**kwargs):
            self.requests.append(configs)
            return BatchedAnswers({name:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})}
                for name in configs},'fake',None,1)
    config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'))
    sibling=replace(config,rubric='Sibling criteria');candidate=replace(config,rubric='Candidate criteria')
    model=Model();shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=3,observer=lambda _:None)
    try:
        shared.bind_context({'a':config,'b':sibling},{'a':[],'b':[]})
        adapter=shared.adapter('a');original=adapter.feature_context_identity(config,[])
        asyncio.run(adapter.classify(candidate,Item('one',{'text':'Text'}),[]))
        assert model.requests[-1]=={'a':candidate,'b':sibling}
        assert adapter.feature_context_identity(config,[])==original
        asyncio.run(shared.adapter('b').classify(sibling,Item('one',{'text':'Text'}),[]))
        assert model.requests[-1]=={'a':config,'b':sibling}
        shared.bind_context({'a':config,'b':replace(sibling,rubric='Revised sibling')},{'a':[],'b':[]})
        assert adapter.feature_context_identity(config,[])!=original
    finally:shared.close()


def test_a_previous_joint_batch_is_not_reused_after_the_request_scope_changes(tmp_path):
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            probability=.8 if len(configs)==2 else .3
            return BatchedAnswers({name:{'decision':DecisionResult('yes' if probability>.5 else 'no',
                {'yes':probability,'no':1-probability})} for name in configs},'fake',None,1)
    model=Model();config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'))
    target=Item('one',{'text':'Text'})
    shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=3,observer=lambda _:None)
    try:
        asyncio.run(shared.prepare({'a':config,'b':config},target,{'a':[],'b':[]}))
        asyncio.run(shared.prepare({'a':config},target,{'a':[]}))
        answer=asyncio.run(shared.adapter('b').classify(config,target,[]))
        assert answer.answers['decision'].label=='no'
        assert model.calls==3
    finally:shared.close()


def test_an_answer_cache_identity_names_the_complete_request_before_and_after_execution(tmp_path):
    class Model:
        model_identity='fake'
        async def classify_many(self,configs,target,training,**kwargs):
            return BatchedAnswers({name:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})}
                for name in configs},'fake',None,1)
    config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'));target=Item('one',{'text':'Text'})
    shared=SharedDecisions(tmp_path/'cache.sqlite',Model(),max_requests=3,observer=lambda _:None)
    try:
        adapter=shared.adapter('a')
        solo=adapter.cache_identity(config,target,[],now=None)
        asyncio.run(adapter.classify(config,target,[]))
        assert adapter.cache_identity(config,target,[],now=None)==solo
        asyncio.run(shared.prepare({'a':config,'b':config},target,{'a':[],'b':[]}))
        joint=adapter.cache_identity(config,target,[],now=None)
        assert joint!=solo
        asyncio.run(shared.prepare({'a':config,'b':ClassifierConfig(config.task,rubric='Changed sibling')},target,{'a':[],'b':[]}))
        assert adapter.cache_identity(config,target,[],now=None)!=joint
    finally:shared.close()


def test_the_flywheel_caches_joint_and_solo_answers_under_their_actual_context(tmp_path):
    from datetime import datetime,timezone
    from .flywheel import DecisionFlywheel
    from .optimizer_agent import OptimizerAgent
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            positive=len(configs)==2
            return BatchedAnswers({name:{'decision':DecisionResult('yes' if positive else 'no',
                {'yes':.8 if positive else .2,'no':.2 if positive else .8})} for name in configs},'fake',None,1)
    def no_optimizer(_):raise AssertionError('answer collection does not optimize')
    model=Model();config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'));target=Item('one',{'text':'Text'})
    shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=3,observer=lambda _:None)
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',config,shared.adapter('a'),OptimizerAgent(no_optimizer),max_requests=3)
    now=datetime(2026,1,1,tzinfo=timezone.utc)
    try:
        solo=asyncio.run(wheel._answers(config,target,[],now))
        asyncio.run(shared.prepare({'a':config,'b':config},target,{'a':[],'b':[]},now=now))
        joint=asyncio.run(wheel._answers(config,target,[],now))
        asyncio.run(shared.prepare({'a':config},target,{'a':[]},now=now))
        repeated=asyncio.run(wheel._answers(config,target,[],now))
        assert solo.answers['decision'].label==repeated.answers['decision'].label=='no'
        assert joint.answers['decision'].label=='yes'
        assert model.calls==2 and wheel.requests==2
    finally:wheel.close();shared.close()


def test_complete_batches_resume_from_cache_and_changes_require_another_call(tmp_path):
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            return BatchedAnswers({key:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})} for key in configs},'fake',{'tokens':3},1)
    model=Model(); config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'))
    target=Item('one',{'text':'Text'}); events=[]
    shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=2,observer=events.append)
    asyncio.run(shared.prepare({'a':config,'b':config},target,{'a':[],'b':[]}))
    shared.close()
    shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=2,observer=events.append)
    asyncio.run(shared.prepare({'a':config,'b':config},target,{'a':[],'b':[]}))
    assert model.calls==1 and shared.requests==1
    asyncio.run(shared.prepare({'a':ClassifierConfig(config.task,rubric='New'),'b':config},target,{'a':[],'b':[]}))
    assert model.calls==2
    with pytest.raises(Exception):
        asyncio.run(shared.prepare({'a':config},target,{'a':[]},options=CacheOptions('refresh')))
    assert model.calls==2
    shared.close()


def test_same_named_models_on_different_servers_do_not_reuse_joint_answers(tmp_path):
    from .adapters.kev import KevAdapter, KevConfiguration
    from .adapters.kev_test import FakeTransport, Response
    config = ClassifierConfig(DecisionTask('main', ('yes', 'no'), 'Choose'))
    target = Item('one', {'text': 'Text'})
    first = FakeTransport(Response(payload={'answers': {'q0': {
        'choice': 'yes', 'probabilities': {'yes': .8, 'no': .2}}}}))
    second = FakeTransport(Response(payload={'answers': {'q0': {
        'choice': 'no', 'probabilities': {'yes': .2, 'no': .8}}}}))
    cache = tmp_path / 'cache.sqlite'
    for endpoint, transport, label in [
        ('https://first.example', first, 'yes'),
        ('https://second.example', second, 'no'),
        ('https://first.example/', first, 'yes'),
    ]:
        model = KevAdapter(transport=transport, configuration=KevConfiguration(base_url=endpoint))
        shared = SharedDecisions(cache, model, max_requests=2, observer=lambda _: None)
        try:
            payload, _ = asyncio.run(shared.prepare({'a': config}, target, {'a': []}))
            assert payload['result']['answers']['a']['decision']['label'] == label
        finally:
            shared.close()
    assert first.calls == second.calls == 1
