"""Example usefulness must be measured by controlled context changes."""
import asyncio
import pytest
from .classifier_config import ClassifierConfig, ClassifiedAnswers
from .models import DecisionResult, Item, LabeledItem
from .flywheel import DecisionFlywheel
from .flywheel_test import TASK, TRAIN, DEV, agent
from .example_attribution import plan_swaps, measure_example_swaps


class SignalModel:
    model_identity='fake-example-signal'
    def __init__(self): self.calls=[]
    async def classify(self,config,target,training,**kwargs):
        self.calls.append((config,target.id))
        p=.9 if 't3' in config.example_ids else .55
        label='include' if 'yes' in target.values['text'] else 'exclude'
        return ClassifiedAnswers({'decision':DecisionResult(label,{key:p if key==label else 1-p for key in TASK.labels})},self.model_identity,{},0.)


def test_probability_only_gain_does_not_win_when_example_selection_optimizes_f1(tmp_path):
    from .selection_policy import SelectionPolicy
    wheel = DecisionFlywheel(tmp_path / 'runtime.sqlite', ClassifierConfig(TASK, example_ids=('t0','t1')),
        SignalModel(), agent([]), max_requests=50, selection_policy=SelectionPolicy('f1', positive_class='include'))
    report = asyncio.run(measure_example_swaps(wheel, TRAIN, DEV, protected=(), propensities={r.item.id:1. for r in TRAIN}))
    assert report['recommended_proposal'] is None
    assert report['rankings'][0]['selection']['policy']['primary'] == 'f1'
    wheel.close()


def test_changed_objective_recomputes_selection_without_repaying_unchanged_requests(tmp_path):
    from .selection_policy import SelectionPolicy
    model = SignalModel()
    path = tmp_path / 'runtime.sqlite'
    config = ClassifierConfig(TASK, example_ids=('t0','t1'))
    kwargs = dict(protected=(), propensities={r.item.id:1. for r in TRAIN})
    wheel = DecisionFlywheel(path, config, model, agent([]), max_requests=50)
    first = asyncio.run(measure_example_swaps(wheel, TRAIN, DEV, **kwargs))
    calls = len(model.calls)
    wheel.close()
    wheel = DecisionFlywheel(path, config, model, agent([]), max_requests=50,
        selection_policy=SelectionPolicy('f1', positive_class='include'))
    second = asyncio.run(measure_example_swaps(wheel, TRAIN, DEV, **kwargs))
    assert first['measurement_fingerprint'] != second['measurement_fingerprint']
    assert first['recommended_proposal'] and second['recommended_proposal'] is None
    assert len(model.calls) == calls
    wheel.close()


def test_each_swap_changes_one_same_class_example_and_keeps_its_display_slot():
    config=ClassifierConfig(TASK,rubric='Keep useful papers',example_ids=('t0','t1'))
    swaps=plan_swaps(config,TRAIN,preferred_ids=('t3',),max_trials=3)
    assert len(swaps)==3
    for swap in swaps:
        candidate=swap['config']
        assert candidate.rubric==config.rubric and candidate.tasks==config.tasks
        assert len(candidate.example_ids)==2
        assert sum(a!=b for a,b in zip(config.example_ids,candidate.example_ids))==1
        labels={row.item.id:row.label for row in TRAIN}
        assert labels[swap['removed_id']]==labels[swap['added_id']]
    assert any(swap['added_id']=='t3' for swap in swaps)


def test_measurement_ranks_probability_gains_without_claiming_accuracy_gains_and_resumes_without_calls(tmp_path):
    config=ClassifierConfig(TASK,rubric='Useful papers',example_ids=('t0','t1'))
    model=SignalModel()
    wheel=DecisionFlywheel(tmp_path/'runtime.sqlite',config,model,agent([]),max_requests=30)
    kwargs=dict(protected=(),propensities={r.item.id:1. for r in TRAIN},preferred_ids=('t3',),max_trials=4)
    first=asyncio.run(measure_example_swaps(wheel,TRAIN,DEV,**kwargs))
    assert first['rankings'][0]['added_id']=='t3'
    assert first['rankings'][0]['brier_gain']>0
    assert first['rankings'][0]['accuracy_change']==0
    assert first['rankings'][0]['question_effects']['decision']['include']['include']>0
    assert wheel.active.config==config
    calls=len(model.calls)
    assert asyncio.run(measure_example_swaps(wheel,TRAIN,DEV,**kwargs))==first
    assert len(model.calls)==calls
    assert first['sample_ids']==[row.item.id for row in wheel.evaluation_policy.select(DEV,TASK.labels)]
    wheel.close()


def test_protected_or_development_items_cannot_be_candidate_examples(tmp_path):
    model=SignalModel();wheel=DecisionFlywheel(tmp_path/'runtime.sqlite',ClassifierConfig(TASK),model,agent([]))
    with pytest.raises(ValueError,match='disjoint'):
        asyncio.run(measure_example_swaps(wheel,TRAIN,DEV,protected=(TRAIN[0].item,),propensities={r.item.id:1. for r in TRAIN}))
    assert not model.calls
    wheel.close()


def test_an_empty_list_requires_seeding_and_does_not_silently_change_example_count():
    assert plan_swaps(ClassifierConfig(TASK),TRAIN)==()


def test_a_budget_preflight_defers_the_whole_measurement_before_any_paid_calls(tmp_path):
    model=SignalModel();wheel=DecisionFlywheel(tmp_path/'runtime.sqlite',ClassifierConfig(TASK,example_ids=('t0','t1')),model,agent([]),max_requests=1)
    report=asyncio.run(measure_example_swaps(wheel,TRAIN,DEV,protected=(),propensities={r.item.id:1. for r in TRAIN}))
    assert report['request_upper_bound']>1 and not model.calls
    wheel.close()


def test_more_development_labels_remeasure_previous_ideas_and_reuse_unchanged_requests(tmp_path):
    model=SignalModel();wheel=DecisionFlywheel(tmp_path/'runtime.sqlite',ClassifierConfig(TASK,example_ids=('t0','t1')),model,agent([]),max_requests=50)
    kwargs=dict(protected=(),propensities={r.item.id:1. for r in TRAIN})
    first=asyncio.run(measure_example_swaps(wheel,TRAIN,DEV,**kwargs));calls=len(model.calls)
    newer=(*DEV,LabeledItem(Item('new-positive',{'text':'yes new development'}),'include'))
    second=asyncio.run(measure_example_swaps(wheel,TRAIN,newer,**kwargs))
    assert first['measurement_fingerprint']!=second['measurement_fingerprint']
    assert second['count']==3
    assert len(model.calls)-calls==1+len(first['rankings'])
    assert {row['added_id'] for row in first['rankings']}=={row['added_id'] for row in second['rankings']}
    wheel.close()


def test_an_interrupted_measurement_reuses_completed_requests_on_explicit_retry(tmp_path):
    from .decision_cache import CacheOptions
    class InterruptedModel(SignalModel):
        interrupt=True
        async def classify(self,*args,**kwargs):
            if args[1].id==DEV[1].item.id and self.interrupt:
                self.interrupt=False
                raise KeyboardInterrupt
            return await super().classify(*args,**kwargs)
    path=tmp_path/'runtime.sqlite';config=ClassifierConfig(TASK,example_ids=('t0','t1'))
    model=InterruptedModel();options=CacheOptions(retry_failed=True)
    wheel=DecisionFlywheel(path,config,model,agent([]),max_requests=30,cache_options=options)
    kwargs=dict(protected=(),propensities={r.item.id:1. for r in TRAIN},max_trials=2)
    with pytest.raises(KeyboardInterrupt):asyncio.run(measure_example_swaps(wheel,TRAIN,DEV,**kwargs))
    assert len(model.calls)==1
    wheel.close()
    wheel=DecisionFlywheel(path,config,model,agent([]),max_requests=30,cache_options=options)
    result=asyncio.run(measure_example_swaps(wheel,TRAIN,DEV,**kwargs))
    assert len(model.calls)==6 # one completed baseline answer was reused, not repeated
    assert len(result['rankings'])==2
    wheel.close()
