"""Provider wire contracts must preserve the complete shared learning lifecycle."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from decision_flywheel.adapters.jev import JevAdapter
from decision_flywheel.adapters.kev import KevAdapter
from decision_flywheel.adapters.laya import LayaAdapter
from decision_flywheel.candidate_fitting import fit_candidate
from decision_flywheel.decision_cache import CacheOptions
from decision_flywheel.flywheel_test import TRAIN, DEV, agent
from decision_flywheel.models import DecisionTask, Item, LabeledItem
from decision_flywheel.web_store import WebStore
from decision_flywheel.workspace_session import WorkspaceSession, freeze_configuration


class Wire:
    def __init__(self):
        self.calls = []

    def answer(self, state, questions):
        self.calls.append((state, questions))
        positive = state['target']['text'].startswith('yes')
        answers = {}
        for name, question in questions.items():
            first, second = question['criteria']
            # The auxiliary question supplies the useful signal; raw main
            # answers cannot already win the candidate comparison perfectly.
            probability = .5 if first == 'include' else (.8 if positive else .2)
            answers[name] = {'choice': first if probability >= .5 else second,
                             'probabilities': {first: probability, second: 1-probability},
                             'confidence': max(probability, 1-probability)}
        return {'answers': answers, 'model': 'synthetic-shared-wire',
                'usage': {'input_tokens': 10, 'output_tokens': 3}}


def make_adapter(provider, wire):
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
@pytest.mark.parametrize('change', ['sibling_context', 'training_response', 'legacy_fit'])
def test_shared_provider_features_refit_before_prediction_and_restart_without_recollection(tmp_path, provider, change):
    store = WebStore(tmp_path/'workspace.sqlite3')
    for cid in ('a', 'b'):
        store.save_classifier(cid, cid, {'question': 'Should this be included?', 'classes': [
            {'label': 'include', 'role': 'positive'}, {'label': 'exclude', 'role': 'negative'}]})
    store.save_item_list('items', 'Source-neutral items')
    store.upsert_list_items('items', [{'id': 'fresh', 'occurred_at': '2026-01-01',
                                    'values': {'text': 'yes new paper'}}])
    config = freeze_configuration(store, {'classifier_ids': ['a', 'b'], 'item_list_id': 'items',
        'max_requests': 100, 'max_optimizer_calls': 0, 'optimize_every': 20,
        'rubric_changes_every': 2, 'seed': 'test'})
    run = store.create_run('Provider contract fixture', 'live', config, items=store.list_items('items'))
    wire, events = Wire(), []
    session = WorkspaceSession(store, run, tmp_path/'run', make_adapter(provider, wire), agent([]), events.append)
    dev = (*DEV, *[LabeledItem(Item(row.item.id+'-extra', {'text': row.item.values['text']+' extra'}),
                              row.label) for row in DEV])
    session.partitions = lambda _: (TRAIN, dev, ())
    try:
        a, b = session.wheels['a'], session.wheels['b']
        a_config = replace(a.active.config, rubric='Practical papers', example_ids=('t0', 't1'),
                           tasks=(DecisionTask('practical', ('yes', 'no'), 'Is this practical?'),))
        b_config = replace(b.active.config, rubric='Independent sibling criteria', example_ids=('t2', 't3'))
        a._activate(replace(a.active, config=a_config))
        b._activate(replace(b.active, config=b_config))
        session.shared.bind_context({'a': a_config, 'b': b_config}, {'a': TRAIN, 'b': TRAIN})
        fitted, _ = asyncio.run(fit_candidate(a, a_config, TRAIN, dev, protected=(),
            propensities={row.item.id: 1. for row in TRAIN}, validation_status='evaluated'))
        a._activate(fitted)
        assert set(a.active.head.feature_names) == {'decision/include', 'practical/yes'}
        assert a.active.head.calibration.fit_on == 'out_of_fold'
        if change == 'sibling_context':
            b._activate(replace(b.active, config=replace(b_config, rubric='Changed sibling criteria')))
        elif change == 'training_response':
            asyncio.run(session.shared.adapter('b').classify_with_cache_options(b_config, TRAIN[0].item,
                TRAIN, cache_options=CacheOptions('refresh')))
        else:
            import json
            from dataclasses import asdict
            from decision_flywheel.flywheel import _restore
            saved = asdict(a.active)
            saved.pop('answer_dependencies')
            a.active = _restore(saved)
            with a.db:
                a.db.execute("UPDATE runtime_state SET value=? WHERE key='active'", (json.dumps(saved),))
        before = len(wire.calls)
        shown = asyncio.run(session.prepare())
        assert shown['item']['id'] == 'fresh'
        assert a.active.config.rubric == 'Practical papers'
        assert a.active.config.example_ids == ('t0', 't1')
        assert a.active.head is not None
        assert a.active.head.provenance.source_model_provenance == a.model_context(a.active.config, TRAIN)
        assert set(shown['prediction']['classifiers']) == {'a', 'b'}
        assert len(wire.calls) > before
        assert session.shared.requests == len(wire.calls)
        assert sum(e.get('usage', {}).get('input_tokens', 0) for e in events
                   if e['kind'] == 'shared-decision-batch' and e.get('usage')) == len(wire.calls)*10
        assert all(set(state['classifiers']) == {'a', 'b'} for state, _ in wire.calls)
        assert all(len(questions) >= 2 for _, questions in wire.calls)
        prediction_requests = [questions for state, questions in wire.calls
                               if state['target']['text'] == 'yes new paper']
        assert len(prediction_requests) == 1 and len(prediction_requests[0]) == 3
        for state, questions in wire.calls:
            if provider == 'jev':  # Jev carries the rubric in the decision question's criteria
                assert 'rubric' not in state['classifiers']['a']
                assert any('Practical papers' in q['criteria'].values() for q in questions.values())
            else:
                assert state['classifiers']['a']['rubric'] == 'Practical papers'
            assert state['classifiers']['a']['examples']
            assert 'label' not in state['target'] and 'human_feedback' not in state['target']
        final_predictions = [e for e in events if e['kind'] == 'prediction']
        learned = next(e for e in reversed(final_predictions) if e['classifier_id'] == 'a')
        assert learned['fitted_head']
        assert learned['ml_features']['practical/yes'] == .8
        assert learned['confidence'] == learned['probabilities']['include']
        invalidated = next(e for e in events if e['kind'] == 'head-invalidated' and e['classifier_id'] == 'a')
        assert invalidated['cycle_id'] == learned['cycle_id']
        assert any(e['kind'] == 'step-started' and e.get('trigger') == 'shared-context-change' for e in events)
        calls = len(wire.calls)
        session.close()
        session = WorkspaceSession(store, run, tmp_path/'run', make_adapter(provider, wire), agent([]), events.append)
        session.partitions = lambda _: (TRAIN, dev, ())
        assert asyncio.run(session.prepare()) == shown
        assert session.wheels['a'].active.head is not None
        assert len(wire.calls) == calls
    finally:
        session.close()
