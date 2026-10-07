"""Provider wire answers must reach the same learned and observable head."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from decision_flywheel.adapters.jev import JevAdapter
from decision_flywheel.adapters.kev import KevAdapter
from decision_flywheel.adapters.laya import LayaAdapter
from decision_flywheel.classifier_config import ClassifierConfig
from decision_flywheel.flywheel import DecisionFlywheel
from decision_flywheel.flywheel_test import TASK, TRAIN, DEV
from decision_flywheel.models import Item
from decision_flywheel.optimizer_agent import OptimizerAgent, OptimizerReply
from decision_flywheel.staged_optimization import optimize_stage


class Wire:
    def __init__(self):
        self.calls = []

    def answer(self, state, questions):
        self.calls.append((state, questions))
        relevant = state['target']['text'].startswith('yes')
        answers = {}
        for name, question in questions.items():
            labels = tuple(question['criteria'])
            if labels == ('include', 'exclude'):
                distribution = {'include': .5, 'exclude': .5}
                label = 'include'
            else:
                distribution = {'yes': .9 if relevant else .1, 'no': .1 if relevant else .9}
                label = 'yes' if relevant else 'no'
            answers[name] = {'choice': label, 'probabilities': distribution, 'confidence': .7}
        return {'answers': answers, 'usage': {'input_tokens': 10}, 'model': 'synthetic-wire'}


def adapter(provider, wire):
    class Local:
        def system_one(self, *, state, questions):
            result = wire.answer(state, questions)
            return SimpleNamespace(**result) if provider == 'jev' else result
    class HTTP:
        async def post(self, url, *, json, timeout):
            result = wire.answer(json['state'], json['questions'])
            return SimpleNamespace(status_code=200, json=lambda: result)
    return KevAdapter(transport=HTTP()) if provider == 'kev' else (
        JevAdapter(Local()) if provider == 'jev' else LayaAdapter(Local()))


@pytest.mark.parametrize('provider', ['jev', 'kev', 'laya'])
def test_a_discovered_question_becomes_calibrated_features_and_survives_restart_for_each_provider(tmp_path, provider):
    wire, optimizer_calls = Wire(), []
    def propose(messages):
        optimizer_calls.append(messages)
        return OptimizerReply(json.dumps({'rationale': 'Human explanations identify practical work', 'tasks': [
            {'name': 'practical', 'instructions': 'Is this practical work?', 'labels': ['yes', 'no']}]}), 'fake-optimizer')
    path = tmp_path / 'wheel.sqlite3'
    config = ClassifierConfig(TASK)
    wheel = DecisionFlywheel(path, config, adapter(provider, wire), OptimizerAgent(propose))
    target = Item('fresh', {'text': 'yes new practical article'})
    with wheel.cycle(target) as cycle:
        cycle.check_trigger('questions', due=True, reason='eligible feedback cadence')
        report = asyncio.run(optimize_stage(wheel, 'questions', TRAIN, DEV, protected=(),
            propensities={row.item.id: 1. for row in TRAIN}, min_development_per_class=1))
        assert report['classifier_training']['promoted']
        prediction = asyncio.run(wheel.predict(target, TRAIN))
    assert wheel.active.head is not None
    # K-1 columns avoid redundant features; full vectors stay in the trace.
    assert set(wheel.active.head.feature_names) == {'decision/include', 'practical/yes'}
    assert len(optimizer_calls) == 1
    assert 'practical work' in optimizer_calls[0][1]['content']
    events = wheel.history(10000)
    recorded = next(e for e in reversed(events) if e['kind'] == 'prediction')
    assert recorded['fitted_head'] and prediction.label == 'include'
    assert recorded['ml_features']['practical/yes'] == .9
    answered = [e for e in events if e['kind'] == 'features-completed' and e.get('target_id') == target.id][-1]
    assert answered['answers']['practical']['probabilities'] == {'yes': .9, 'no': .1}
    assert set(recorded['uncalibrated_probabilities']) == set(recorded['probabilities']) == set(TASK.labels)
    assert recorded['confidence'] == recorded['probabilities'][prediction.label]
    assert recorded['calibration_provenance']['fit_on'] == 'out_of_fold'
    assert set(recorded['calibration_provenance']['training_ids']) == {row.item.id for row in TRAIN}
    assert not set(recorded['calibration_provenance']['training_ids']).intersection(row.item.id for row in DEV)
    kinds = {e['kind'] for e in events}
    assert kinds >= {'optimizer-request', 'optimizer-response', 'decision-request', 'decision-response',
                     'fit-started', 'fit-completed', 'candidate-evaluated', 'promoted', 'cycle-completed'}
    assert len({e['cycle_id'] for e in events if 'cycle_id' in e}) == 1
    calls = len(wire.calls)
    version = wheel.active.fingerprint
    wheel.close()
    def forbidden(_):
        pytest.fail('restart must not invoke the optimizer')
    restored = DecisionFlywheel(path, config, adapter(provider, wire), OptimizerAgent(forbidden))
    try:
        replayed = asyncio.run(restored.predict(target, TRAIN))
        assert restored.active.fingerprint == version
        assert replayed == prediction
        assert len(wire.calls) == calls
    finally:
        restored.close()
