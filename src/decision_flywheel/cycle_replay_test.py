import asyncio
import json
from .cycle_replay import run_cycle_replay
from .replay import plan_replay
from .replay_test import rows
from .flywheel_test import TASK, FakeModel
from .flywheel import DecisionFlywheel
from .classifier_config import ClassifierConfig
from .optimizer_agent import OptimizerAgent, OptimizerReply


def test_each_vote_is_predicted_before_reveal_and_triggered_work_stays_inside_that_item_cycle(tmp_path):
    replies=[]
    optimizer=OptimizerAgent(lambda messages: OptimizerReply('{"rationale":"Test human guidance","rubric":"Practical papers"}', 'fake'),observer=replies.append)
    wheel=DecisionFlywheel(tmp_path/'cycles.sqlite3',ClassifierConfig(TASK),FakeModel(),optimizer,max_requests=2000)
    plan=plan_replay(TASK,rows(),seed='cycles',batch_size=18)
    report=asyncio.run(run_cycle_replay(wheel,plan,optimize_every=18,retrain_every=18,stages=('rubric',),rubric_changes_every=None))
    events=wheel.history(10000)
    assert len(report['cycles'])==len(plan.ordered)
    assert sum(e['kind']=='cycle-started' for e in events)==len(plan.ordered)
    for number,row in enumerate(plan.ordered,1):
        cycle=[e for e in events if e.get('cycle_number')==number]
        prediction=next(e for e in cycle if e['kind']=='prediction')
        feedback=next((e for e in cycle if e['kind']=='human-feedback'),None)
        if feedback is not None:
            assert prediction['event_id']<feedback['event_id']
        assert prediction['target_id']==row.item.id
        assert cycle[-1]['kind']=='cycle-completed'
        if any(e['kind']=='optimizer-request' for e in cycle):
            trigger=next(e for e in cycle if e['kind']=='trigger-evaluated' and e['due'])
            assert all(e['trigger_event_id']==trigger['event_id'] for e in cycle if e['kind']=='optimizer-request')
            briefing=json.loads(next(e for e in cycle if e['kind']=='optimizer-request')['messages'][-1]['content'])
            assert {r['id'] for r in briefing['feedback']} <= {r.item.id for r in plan.ordered[:number]}
    assert any(e['kind']=='trigger-evaluated' and not e['due'] for e in events)
    wheel.close()


def test_transition_replay_runs_on_eligible_label_changes_not_protected_votes(tmp_path):
    from dataclasses import replace
    labels = [TASK.labels[0], TASK.labels[0], TASK.labels[0], TASK.labels[1], TASK.labels[0], TASK.labels[1], TASK.labels[0]]
    ordered = tuple(replace(row, label=labels[index]) if index < len(labels) else row
                    for index,row in enumerate(rows()))
    optimizer=OptimizerAgent(lambda _: OptimizerReply('{"rubric":"Use feedback"}', 'fake'))
    wheel=DecisionFlywheel(tmp_path/'transitions.sqlite',ClassifierConfig(TASK),FakeModel(),optimizer,max_requests=2000)
    plan=plan_replay(TASK,ordered,seed='transitions',batch_size=20)
    report=asyncio.run(run_cycle_replay(wheel,plan,stages=('rubric',)))
    events=wheel.history(10000)
    triggers=[event for event in events if event['kind']=='trigger-evaluated' and event['stage']=='rubric' and event['due']]
    assert {ordered[4].item.id,ordered[5].item.id} <= {row.item.id for row in plan.scoreboard}
    assert [event['cycle_number'] for event in triggers if event['cycle_number'] <= 7] == [7]
    requests=[event for event in events if event['kind']=='optimizer-request']
    assert [event['cycle_number'] for event in requests if event['cycle_number'] <= 7] == [7]
    assert [event['trigger_event_id'] for event in requests] == [event['event_id'] for event in triggers]
    assert len(report['cycles']) == len(ordered)
    wheel.close()


def test_protected_replay_votes_do_not_trigger_fixed_learning_cadences(tmp_path):
    wheel=DecisionFlywheel(tmp_path/'protected.sqlite',ClassifierConfig(TASK),FakeModel(),OptimizerAgent(lambda _:OptimizerReply('{"rubric":"Guidance"}','fake')),max_requests=2000)
    plan=plan_replay(TASK,rows(),seed='cycles',batch_size=18)
    asyncio.run(run_cycle_replay(wheel,plan,optimize_every=1,retrain_every=1,stages=('rubric',),rubric_changes_every=None))
    audit_cycles={index+1 for index,row in enumerate(plan.ordered) if row in plan.scoreboard}
    assert not any(event['due'] for event in wheel.history(100000) if event['kind']=='trigger-evaluated' and event['cycle_number'] in audit_cycles)
    wheel.close()


def test_operational_replay_records_latest_two_hundred_paired_calibration_samples(tmp_path):
    from .models import Item,LabeledItem
    ordered=tuple(LabeledItem(Item(f'paper-{i}',{'text':f'Unique abstract {i}'}),TASK.labels[i%2]) for i in range(205))
    wheel=DecisionFlywheel(tmp_path/'window.sqlite',ClassifierConfig(TASK),FakeModel(),OptimizerAgent(lambda _:OptimizerReply('{}','fake')),max_requests=300)
    plan=plan_replay(TASK,ordered,seed='window',batch_size=1000)
    report=asyncio.run(run_cycle_replay(wheel,plan,optimize_every=1000,retrain_every=1000,stages=('rubric',),rubric_changes_every=None))
    metrics=report['cycles'][-1]['metrics']
    feedback_count=sum(event['kind']=='human-feedback' for event in wheel.history(100000))
    assert metrics['count']==min(200,feedback_count)
    assert metrics['decision_model_comparison']['count']==min(200,feedback_count)
    assert report['all_item_evaluator']['count']==200
    assert report['all_item_evaluator']['evaluation_scope']=='replay-oracle'
    assert metrics['decision_model_comparison']['raw']['accuracy']==metrics['decision_model_comparison']['final']['accuracy']
    wheel.close()


def test_replay_can_resume_a_completed_prefix_without_repaying_predictions(tmp_path):
    ordered=rows()
    plan=plan_replay(TASK,ordered,seed='resume',batch_size=100)
    path=tmp_path/'resume.sqlite'
    wheel=DecisionFlywheel(path,ClassifierConfig(TASK),FakeModel(),OptimizerAgent(lambda _:OptimizerReply('{}','fake')),max_requests=2)
    try:
        asyncio.run(run_cycle_replay(wheel,plan,optimize_every=100,retrain_every=100,stages=('rubric',),rubric_changes_every=None))
    except RuntimeError:
        pass
    else:
        raise AssertionError('the constrained first pass must pause before the third prediction')
    first_paid=sum(event['kind']=='features-requested' for event in wheel.history(10000))
    assert first_paid==2
    wheel.close()
    wheel=DecisionFlywheel(path,ClassifierConfig(TASK),FakeModel(),OptimizerAgent(lambda _:OptimizerReply('{}','fake')),max_requests=200)
    report=asyncio.run(run_cycle_replay(wheel,plan,optimize_every=100,retrain_every=100,stages=('rubric',),rubric_changes_every=None,resume=True))
    assert report['resumed_from_cycles']==2
    assert sum(event['kind']=='features-requested' for event in wheel.history(10000))==len(ordered)
    assert sum(event['kind']=='cycle-completed' for event in wheel.history(10000))==len(ordered)
    wheel.close()


def test_revealed_feedback_trigger_uses_exact_counts_with_partial_feedback(tmp_path):
    from .replay_feedback_policy import FeedbackSelection
    class EveryOther:
        def select(self, *, cycle_number, **kwargs):
            return FeedbackSelection(cycle_number%2==0, .5 if cycle_number%2==0 else 0., .5)
        def manifest(self, negative_label): return {'mode':'every-other','negative_label':negative_label}
    plan=plan_replay(TASK,rows(),seed='feedback-count',batch_size=100)
    wheel=DecisionFlywheel(tmp_path/'feedback-count.sqlite',ClassifierConfig(TASK),FakeModel(),
                           OptimizerAgent(lambda _:OptimizerReply('{}','fake')),max_requests=2000)
    attempted=[]
    async def recorded_step(stage, *args, **kwargs):
        attempted.append(stage)
        return {'status':'completed','stage':stage}
    wheel.step=recorded_step
    asyncio.run(run_cycle_replay(wheel,plan,optimize_every=100,retrain_every=100,stages=('rubric',),
        rubric_changes_every=2,rubric_trigger_basis='revealed_feedback_count',feedback_policy=EveryOther(),
        negative_label='exclude',max_rubric_optimizations=2))
    events=wheel.history(100000)
    due=[event for event in events if event['kind']=='trigger-evaluated' and event['stage']=='rubric' and event['due']]
    assert [event['details']['feedback_count'] for event in due][:2]==[2,4]
    assert attempted==['rubric','rubric']
    assert all(event['details']['policy']=='revealed-feedback-count' for event in due)
    wheel.close()


def test_revealed_feedback_trigger_resumes_without_duplicate_attempt(tmp_path):
    from .replay_feedback_policy import FeedbackSelection
    class EveryOther:
        def select(self, *, cycle_number, **kwargs):
            return FeedbackSelection(cycle_number%2==0, .5 if cycle_number%2==0 else 0., .5)
        def manifest(self, negative_label): return {'mode':'every-other','negative_label':negative_label}
    plan=plan_replay(TASK,rows(),seed='feedback-resume',batch_size=100)
    path=tmp_path/'feedback-resume.sqlite'
    def wheel(limit):
        value=DecisionFlywheel(path,ClassifierConfig(TASK),FakeModel(),OptimizerAgent(lambda _:OptimizerReply('{}','fake')),max_requests=limit)
        async def step(*args, **kwargs): return {'status':'completed'}
        value.step=step
        return value
    first=wheel(2)
    try:
        asyncio.run(run_cycle_replay(first,plan,optimize_every=100,retrain_every=100,stages=('rubric',),
            rubric_changes_every=2,rubric_trigger_basis='revealed_feedback_count',feedback_policy=EveryOther(),negative_label='exclude'))
    except RuntimeError: pass
    first.close()
    second=wheel(200)
    asyncio.run(run_cycle_replay(second,plan,optimize_every=100,retrain_every=100,stages=('rubric',),
        rubric_changes_every=2,rubric_trigger_basis='revealed_feedback_count',feedback_policy=EveryOther(),negative_label='exclude',
        max_rubric_optimizations=1,resume=True))
    due=[event for event in second.history(100000) if event['kind']=='trigger-evaluated' and event['stage']=='rubric' and event['due']]
    assert [event['details']['feedback_count'] for event in due].count(2)==1
    second.close()
