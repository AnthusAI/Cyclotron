"""Small end-to-end replay/playback smoke for the homepage feedback policies."""
import asyncio
import pytest
from .models import Item,LabeledItem
from .replay import plan_replay
from .cycle_replay import run_cycle_replay
from .replay_feedback_policy import ReplayFeedbackPolicy
from .flywheel import DecisionFlywheel
from .flywheel_test import TASK,FakeModel
from .classifier_config import ClassifierConfig
from .optimizer_agent import OptimizerAgent
from .trace_artifact import render_trace

@pytest.mark.parametrize('mode',('all','reject_half','casual_ten_percent'))
def test_small_policy_replay_keeps_withheld_labels_out_of_learning_and_renders_playback(tmp_path,mode):
    # The engine's split firewall needs seven examples per class, so 14 is the
    # smallest executable fixture; the first ten cycles are the UI smoke window.
    rows=tuple(LabeledItem(Item(f'i-{i}',{'text':f'item {i}'}),TASK.labels[i%2]) for i in range(14))
    plan=plan_replay(TASK,rows,seed='offline-smoke',batch_size=50)
    wheel=DecisionFlywheel(tmp_path/f'{mode}.sqlite',ClassifierConfig(TASK),FakeModel(),OptimizerAgent(lambda _:None),max_requests=100)
    report=asyncio.run(run_cycle_replay(wheel,plan,optimize_every=50,retrain_every=50,stages=('rubric',),rubric_changes_every=None,feedback_policy=ReplayFeedbackPolicy(mode,'offline-smoke'),negative_label='exclude'))
    assert len(report['cycles'])==14
    assert len(report['cycles'][:10])==10
    skipped={c['item_id'] for c in report['cycles'] if not c['feedback_selected']}
    feedback={e['feedback']['item_id'] for e in wheel.history(10000) if e['kind']=='human-feedback'}
    assert not skipped & feedback
    html=render_trace(wheel.history(10000),class_config=[{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}])
    assert 'timeline' in html and 'cycle-metrics' in html
    wheel.close()
