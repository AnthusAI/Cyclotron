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
    report=asyncio.run(run_cycle_replay(wheel,plan,optimize_every=18,retrain_every=18,stages=('rubric',)))
    events=wheel.history(10000)
    assert len(report['cycles'])==len(plan.ordered)
    assert sum(e['kind']=='cycle-started' for e in events)==len(plan.ordered)
    for number,row in enumerate(plan.ordered,1):
        cycle=[e for e in events if e.get('cycle_number')==number]
        prediction=next(e for e in cycle if e['kind']=='prediction')
        feedback=next(e for e in cycle if e['kind']=='human-feedback')
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
