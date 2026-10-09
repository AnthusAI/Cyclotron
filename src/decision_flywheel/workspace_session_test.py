"""The source-neutral session predicts before learning independent human labels."""
import asyncio
import pytest
from .workspace_session import WorkspaceSession
from .web_store import WebStore
from .batched_classification import BatchedAnswers
from .models import DecisionResult
from .observability import StepFailed
from .flywheel_test import agent


@pytest.mark.parametrize('change', ['sibling_context', 'training_response', 'legacy_fit'])
def test_a_changed_feature_source_refits_in_the_joint_context_before_the_next_prediction(tmp_path, change):
    from dataclasses import replace
    from .workspace_session import freeze_configuration
    from .candidate_fitting import fit_candidate
    from .models import Item,LabeledItem
    from .flywheel_test import TRAIN,DEV
    store=WebStore(tmp_path/'workspace.sqlite')
    for cid in ('a','b'):
        store.save_classifier(cid,cid,{'question':'Choose','classes':[{'label':'include'},{'label':'exclude'}]})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':'new','occurred_at':'2026-01-01','values':{'text':'yes new paper'}}])
    config=freeze_configuration(store,{'classifier_ids':['a','b'],'item_list_id':'items','max_requests':100,
        'max_optimizer_calls':0,'optimize_every':20,'rubric_changes_every':2,'seed':'test'})
    run=store.create_run('Joint refit','live',config,items=store.list_items('items'))
    class Model:
        model_identity='fake';requests=[]
        async def classify_many(self,configs,target,training,**kwargs):
            self.requests.append((configs,target))
            probability=.8 if 'yes' in target.values['text'] else .2
            return BatchedAnswers({cid:{'decision':DecisionResult('include' if probability>.5 else 'exclude',
                {'include':probability,'exclude':1-probability})} for cid in configs},'fake',{},1)
    model=Model();events=[];session=WorkspaceSession(store,run,tmp_path/'run',model,agent([]),events.append)
    dev=(*DEV,*[LabeledItem(Item(r.item.id+'-extra',{'text':r.item.values['text']+' extra'}),r.label) for r in DEV])
    session.partitions=lambda _: (TRAIN,dev,())
    try:
        session.shared.bind_context({cid:w.active.config for cid,w in session.wheels.items()},{cid:TRAIN for cid in session.wheels})
        wheel=session.wheels['a']
        fitted,_=asyncio.run(fit_candidate(wheel,wheel.active.config,TRAIN,dev,protected=(),
            propensities={r.item.id:1. for r in TRAIN},validation_status='evaluated'))
        wheel._activate(fitted)
        sibling=session.wheels['b']
        if change == 'sibling_context':
            sibling._activate(replace(sibling.active,config=replace(sibling.active.config,rubric='New sibling criteria')))
        elif change == 'training_response':
            from .decision_cache import CacheOptions
            asyncio.run(session.shared.adapter('b').classify_with_cache_options(sibling.active.config,
                TRAIN[0].item, TRAIN, cache_options=CacheOptions('refresh')))
        else:
            import json
            from dataclasses import asdict
            from .flywheel import _restore
            legacy = asdict(wheel.active)
            legacy.pop('answer_dependencies')
            wheel.active = _restore(legacy)
            with wheel.db:
                wheel.db.execute("UPDATE runtime_state SET value=? WHERE key='active'", (json.dumps(legacy),))
        shown=asyncio.run(session.prepare())
        assert shown['item']['id']=='new'
        assert all(set(configs)=={'a','b'} for configs,_ in model.requests)
        assert any(e['kind']=='head-invalidated' and e['classifier_id']=='a' for e in events)
        assert any(e['kind']=='step-started' and e.get('trigger')=='shared-context-change' for e in events)
        prediction=next(e for e in reversed(events) if e['kind']=='prediction' and e['classifier_id']=='a')
        if wheel.active.head:
            assert wheel.active.head.provenance.source_model_provenance==wheel.model_context(wheel.active.config,TRAIN)
        assert prediction['cycle_id']==next(e for e in events if e['kind']=='step-started' and e.get('trigger')=='shared-context-change')['cycle_id']
    finally:session.close()


def test_cyclotron_undo_reopens_the_last_item_and_reuses_its_displayed_prediction(tmp_path):
    from .workspace_session import freeze_configuration
    from .flywheel import FittedClassifier, development_assignment
    from .classifier_config import ClassifierConfig
    store = WebStore(tmp_path / 'workspace.sqlite')
    for cid in ('a', 'b'):
        store.save_classifier(cid, cid, {'question': 'Choose', 'classes': [{'label': 'yes'}, {'label': 'no'}]})
    store.save_item_list('items', 'Items')
    item_id = next(str(n) for n in range(100) if not development_assignment('test:audit', str(n), rate=.2)
                   and not development_assignment('test', str(n)))
    store.upsert_list_items('items', [{'id': item_id, 'occurred_at': '2026-01-01', 'values': {'text': 'Paper'}}])
    config = freeze_configuration(store, {'classifier_ids': ['a', 'b'], 'item_list_id': 'items', 'max_requests': 10,
        'max_optimizer_calls': 3, 'optimize_every': 20, 'rubric_changes_every': 2, 'seed': 'test'})
    run = store.create_run('Undo', 'live', config, items=store.list_items('items'))
    class Model:
        model_identity = 'fake'; calls = 0
        async def classify_many(self, configs, target, training, **kwargs):
            self.calls += 1
            return BatchedAnswers({cid: {'decision': DecisionResult('yes', {'yes': .8, 'no': .2})}
                for cid in configs}, 'fake', {}, 1)
    model = Model(); events = []
    session = WorkspaceSession(store, run, tmp_path / 'run', model, agent([]), events.append)
    shown = asyncio.run(session.prepare())
    asyncio.run(session.feedback({'item_id': item_id, 'presentation_id': shown['prediction']['presentation_id'],
        'labels': [{'classifier_id': cid, 'label': 'yes'} for cid in ('a', 'b')]}, 'vote'))
    reviewed_predictions={cid:next(e for e in wheel.history(100000) if e['kind']=='prediction')['event_id']
                          for cid,wheel in session.wheels.items()}
    for wheel in session.wheels.values():
        wheel._emit({'kind':'prediction','target_id':item_id,'label':'no',
            'probabilities':{'yes':.05,'no':.95},'version':'retrospective-fixture'})
        train, dev, _ = session.partitions(wheel.initial.task.name)
        wheel._activate(FittedClassifier(ClassifierConfig(wheel.initial.task, rubric='Old learned guidance'),
            training_evidence=wheel._evidence(train), development_evidence=wheel._evidence(dev)))
    def lose_ack(event):
        events.append(event)
        if event['kind'] == 'human-feedback' and event['action'] == 'retracted' and event['classifier_id'] == 'a':
            raise RuntimeError('retraction acknowledgement lost')
    session.observer = lose_ack
    with pytest.raises(RuntimeError, match='acknowledgement'):
        session.undo_feedback('undo')
    session.close()
    session = WorkspaceSession(store, run, tmp_path / 'run', model, agent([]), events.append)
    result = session.undo_feedback('undo')
    assert result['undone'] == item_id
    assert {e['classifier_id']:e['prediction_event_id'] for e in events
            if e['kind']=='displayed-prediction-reused'}==reviewed_predictions
    assert store.item_labels('items', item_id, 1) == []
    assert store.item_results('items', item_id, 1)[0]['payload'] == shown['prediction']
    assert asyncio.run(session.prepare())['prediction'] == shown['prediction']
    assert all(not w.active.config.rubric for w in session.wheels.values())
    session.close()
    session = WorkspaceSession(store, run, tmp_path / 'run', model, agent([]), events.append)
    try:
        before = len(events)
        assert session.undo_feedback('undo') == result
        assert len(events) == before
        asyncio.run(session.feedback({'item_id': item_id, 'presentation_id': shown['prediction']['presentation_id'],
            'labels': [{'classifier_id': cid, 'label': 'no', 'comment': 'Corrected review'} for cid in ('a', 'b')]}, 'new-vote'))
        assert {row['label'] for row in store.item_labels('items', item_id, 1)} == {'no'}
        assert all(session.partitions(cid)[0][0].label == 'no' for cid in ('a', 'b'))
        assert len([e for e in events if e['kind'] == 'human-feedback' and e['action'] == 'retracted']) == 2
        assert model.calls == 1
        for cid in ('a','b'):
            metrics=next(e['metrics'] for e in reversed(events) if e['kind']=='cycle-metrics' and e['classifier_id']==cid)
            assert metrics['accuracy']==0
            assert metrics['calibration']['samples'][0]['prediction_event_id']==reviewed_predictions[cid]
    finally:
        session.close()


def test_corrected_cyclotron_feedback_invalidates_learning_and_survives_restart_without_paid_calls(tmp_path):
    from .workspace_session import freeze_configuration
    from .flywheel import FittedClassifier, development_assignment
    from .classifier_config import ClassifierConfig
    store = WebStore(tmp_path / 'workspace.sqlite')
    for cid in ('a', 'b'):
        store.save_classifier(cid, cid, {'question': 'Choose', 'classes': [{'label': 'yes'}, {'label': 'no'}]})
    store.save_item_list('items', 'Items')
    # Ensure this fixture's vote is training, not a protected audit vote.
    item_id = next(str(n) for n in range(100) if not development_assignment('test:audit', str(n), rate=.2)
                   and not development_assignment('test', str(n)))
    store.upsert_list_items('items', [{'id': item_id, 'occurred_at': '2026-01-01', 'values': {'text': 'Paper'}}])
    config = freeze_configuration(store, {'classifier_ids': ['a', 'b'], 'item_list_id': 'items',
        'max_requests': 10, 'max_optimizer_calls': 3, 'optimize_every': 20, 'rubric_changes_every': 2, 'seed': 'test'})
    run = store.create_run('Corrections', 'live', config, items=store.list_items('items'))
    class Model:
        model_identity = 'fake'
        calls = 0
        async def classify_many(self, configs, target, training, **kwargs):
            self.calls += 1
            return BatchedAnswers({cid: {'decision': DecisionResult('yes', {'yes': .8, 'no': .2})}
                                  for cid in configs}, 'fake', {}, 1)
    model = Model(); events = []
    session = WorkspaceSession(store, run, tmp_path / 'run', model, agent([]), events.append)
    shown = asyncio.run(session.prepare())
    asyncio.run(session.feedback({'item_id': item_id, 'presentation_id': shown['prediction']['presentation_id'],
        'labels': [{'classifier_id': cid, 'label': 'yes', 'comment': 'Original'} for cid in ('a', 'b')]}, 'vote'))
    wheel = session.wheels['a']; train, dev, _ = session.partitions('a')
    wheel._activate(FittedClassifier(ClassifierConfig(wheel.initial.task, rubric='Learned from old explanation'),
                                    training_evidence=wheel._evidence(train), development_evidence=wheel._evidence(dev)))
    previous = wheel.active.fingerprint
    payload = {'item_id': item_id, 'labels': [{'classifier_id': 'a', 'label': 'no',
        'comment': 'Correct explanation', 'expected_feedback_id': 'vote:a'}]}
    original_history = wheel.history(100000)
    with pytest.raises(ValueError, match='reload'):
        session.correct_feedback({'item_id': item_id, 'labels': [{'classifier_id': 'a', 'label': 'no',
            'expected_feedback_id': 'stale-vote'}]}, 'stale')
    assert wheel.history(100000) == original_history
    original_prediction=next(e for e in original_history if e['kind']=='prediction')
    # A retrospective score is not the prediction the labeler reviewed.
    wheel._emit({'kind':'prediction','target_id':item_id,'label':'no',
        'probabilities':{'yes':.05,'no':.95},'version':'retrospective-fixture',
        'decision_model_label':'no','decision_model_probabilities':{'yes':.05,'no':.95}})
    result = session.correct_feedback(payload, 'correction')
    assert result['corrected'] == item_id
    assert wheel.active.config.rubric == '' and wheel.active.fingerprint != previous
    assert session.partitions('a')[0][0].label == 'no'
    assert session.partitions('a')[0][0].context['human_feedback'] == 'Correct explanation'
    assert session.partitions('a')[0][0].initial_answer_value == 'yes'
    from .optimizer_agent import FeedbackBriefing
    feedback = FeedbackBriefing.build(wheel.initial.task, session.partitions('a')[0],
        current={}, protected=session.partitions('a')[2]).payload['feedback'][0]
    assert feedback['initial_answer_value'] == 'yes'
    assert feedback['prediction_matches_label'] is False
    assert session.partitions('b')[0][0].label == 'yes'
    assert wheel.optimizer_context['human_explanations'] == ['Correct explanation']
    assert all(e == original_history[index] for index, e in enumerate(wheel.history(100000)[:len(original_history)]))
    assert store.item_results('items', item_id, 1)[0]['payload'] == shown['prediction']
    assert {v['classifier_id']: v['label'] for v in store.item_labels('items', item_id, 1)} == {'a': 'no', 'b': 'yes'}
    before = len(wheel.history(100000)); session.close()
    session = WorkspaceSession(store, run, tmp_path / 'run', model, agent([]), events.append)
    try:
        assert session.correct_feedback(payload, 'correction') == result
        assert session.partitions('a')[0][0].initial_answer_value == 'yes'
        assert len(session.wheels['a'].history(100000)) == before
        with pytest.raises(ValueError, match='different content'):
            session.correct_feedback({'item_id': item_id, 'labels': [{'classifier_id': 'a', 'label': 'yes',
                'expected_feedback_id': 'vote:a'}]}, 'correction')
        metrics = next(e['metrics'] for e in reversed(events) if e['kind'] == 'cycle-metrics' and e['classifier_id'] == 'a')
        assert metrics['count'] == 1 and metrics['accuracy'] == 0
        assert metrics['calibration']['samples'][0]['prediction_event_id']==original_prediction['event_id']
        assert metrics['calibration']['ece']==pytest.approx(.8)
        # An explanation-only edit must also invalidate inferred guidance.
        wheel = session.wheels['a']; train, dev, _ = session.partitions('a')
        wheel._activate(FittedClassifier(ClassifierConfig(wheel.initial.task, rubric='Inferred guidance'),
            training_evidence=wheel._evidence(train), development_evidence=wheel._evidence(dev)))
        session.correct_feedback({'item_id': item_id, 'labels': [{'classifier_id': 'a', 'label': 'no',
            'comment': 'A more precise explanation', 'expected_feedback_id': 'correction:a'}]}, 'comment-edit')
        assert wheel.active.config.rubric == ''
        assert wheel.optimizer_context['human_explanations'] == ['A more precise explanation']
        # An API acknowledgement loss after durable feedback must not leave
        # the old inferred rubric current after explicit command recovery.
        train, dev, _ = session.partitions('a')
        wheel._activate(FittedClassifier(ClassifierConfig(wheel.initial.task, rubric='Old inferred guidance'),
            training_evidence=wheel._evidence(train), development_evidence=wheel._evidence(dev)))
        interrupted = {'item_id': item_id, 'labels': [{'classifier_id': 'a', 'label': 'yes',
            'comment': 'Recovered correction', 'expected_feedback_id': 'comment-edit:a'}]}
        def lose_ack(event):
            events.append(event)
            if event['kind'] == 'human-feedback' and event['feedback']['id'] == 'interrupted:a':
                raise RuntimeError('API acknowledgement lost')
        session.observer = lose_ack
        with pytest.raises(RuntimeError, match='acknowledgement'):
            session.correct_feedback(interrupted, 'interrupted')
        session.close()
        session = WorkspaceSession(store, run, tmp_path / 'run', model, agent([]), events.append)
        session.correct_feedback(interrupted, 'interrupted')
        assert session.wheels['a'].active.config.rubric == ''
        assert len([e for e in session.wheels['a'].history(100000)
                    if e['kind'] == 'human-feedback' and e['feedback']['id'] == 'interrupted:a']) == 1
        assert model.calls == 1
    finally:
        session.close()


def test_protected_feedback_corrections_remain_protected_and_never_enter_learning_context(tmp_path):
    from types import SimpleNamespace
    from .workspace_session import freeze_configuration
    from .feedback import FeedbackItem, LABEL_SOURCE_VETTED
    from .flywheel import FittedClassifier
    from .classifier_config import ClassifierConfig
    store = WebStore(tmp_path / 'workspace.sqlite')
    store.save_classifier('a', 'A', {'question': 'Choose', 'classes': [{'label': 'yes'}, {'label': 'no'}]})
    store.save_item_list('items', 'Items')
    store.upsert_list_items('items', [{'id': 'paper', 'occurred_at': '2026-01-01', 'values': {'text': 'Paper'}}])
    config = freeze_configuration(store, {'classifier_ids': ['a'], 'item_list_id': 'items', 'max_requests': 10,
        'max_optimizer_calls': 3, 'optimize_every': 20, 'rubric_changes_every': 2, 'seed': 'test'})
    run = store.create_run('Protected', 'live', config, items=store.list_items('items'))
    session = WorkspaceSession(store, run, tmp_path / 'run', SimpleNamespace(model_identity='fake'), agent([]), lambda _: None)
    try:
        wheel = session.wheels['a']
        wheel.record_feedback_event(FeedbackItem('original', 'paper', 'a', final_answer_value='yes',
            edit_comment_value='Sealed explanation', label_source=LABEL_SOURCE_VETTED,
            selection_propensity=1., review_provenance='human'), assignment='final_audit')
        store.update_item(run['id'], 'paper', reviewed=True)
        wheel._activate(FittedClassifier(ClassifierConfig(wheel.initial.task, rubric='Unrelated training rubric')))
        version = wheel.active.fingerprint
        session.correct_feedback({'item_id': 'paper', 'labels': [{'classifier_id': 'a', 'label': 'no',
            'comment': 'Corrected sealed explanation', 'expected_feedback_id': 'original'}]}, 'correction')
        assert wheel.active.fingerprint == version
        assert session.partitions('a')[:2] == ((), ())
        assert wheel.optimizer_context['human_explanations'] == []
        corrected = next(e for e in reversed(wheel.history(100000)) if e['kind'] == 'human-feedback')
        assert corrected['assignment'] == 'final_audit'
        session.config['input_mode'] = 'replay'
        with pytest.raises(ValueError, match='frozen replay'):
            session.correct_feedback({'item_id': 'paper', 'labels': [{'classifier_id': 'a', 'label': 'yes'}]}, 'replay-edit')
    finally:
        session.close()


def test_protected_audit_votes_do_not_trigger_learning_or_enter_optimizer_context(tmp_path):
    from types import SimpleNamespace
    from .workspace_session import freeze_configuration
    from .feedback import FeedbackItem,LABEL_SOURCE_VETTED
    store=WebStore(tmp_path/'db')
    store.save_classifier('a','A',{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    config=freeze_configuration(store,{'classifier_ids':['a'],'item_list_id':'items','max_requests':10,'max_optimizer_calls':3,'optimize_every':1,'rubric_changes_every':1,'seed':'test'})
    run=store.create_run('Audit','live',config,items=store.list_items('items'))
    session=WorkspaceSession(store,run,tmp_path/'run',SimpleNamespace(model_identity='fake'),agent([]),lambda e:None)
    checks=[];calls=[]
    wheel=session.wheels['a']
    async def forbidden(*args,**kwargs):calls.append(True);raise AssertionError('protected feedback triggered optimization')
    wheel.step=forbidden
    try:
        for identifier,label,role,comment in [('earlier','yes','training','Eligible explanation'),('one','no','scoreboard','Sealed audit explanation')]:
            wheel.record_feedback_event(FeedbackItem(identifier,identifier,'a',final_answer_value=label,edit_comment_value=comment,label_source=LABEL_SOURCE_VETTED,selection_propensity=1.,review_provenance='human'),assignment=role)
        asyncio.run(session.optimize('a',SimpleNamespace(check_trigger=lambda stage,**check:checks.append((stage,check)))))
        assert not calls
        assert all(not check['due'] for _,check in checks)
        assert 'Sealed audit explanation' not in str(wheel.optimizer_context)
    finally:session.close()


def test_restart_finishes_a_cycle_with_already_durable_feedback_without_repeating_prediction_or_optimization(tmp_path):
    from .workspace_session import freeze_configuration
    from .feedback import FeedbackItem,LABEL_SOURCE_VETTED
    from .models import Item
    store=WebStore(tmp_path/'web.sqlite')
    store.save_classifier('a','A',{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List')
    store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    config=freeze_configuration(store,{'classifier_ids':['a'],'item_list_id':'list','max_requests':10,'max_optimizer_calls':3,'optimize_every':20,'rubric_changes_every':2,'seed':'test'})
    run=store.create_run('Restart','live',config,items=store.list_items('list'))
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            return BatchedAnswers({'a':{'decision':DecisionResult('yes',{'yes':.7,'no':.3})}},'fake',{},1)
    model=Model();events=[]
    session=WorkspaceSession(store,run,tmp_path/'run',model,agent([]),events.append)
    shown=asyncio.run(session.prepare())
    wheel=session.wheels['a'];cycle=wheel.resume_cycle(Item('one',{'text':'Text'}))
    wheel.record_feedback_event(FeedbackItem('vote:a','one','a',initial_answer_value='yes',final_answer_value='yes',edit_comment_value='Knowledge bases',label_source=LABEL_SOURCE_VETTED,selection_propensity=1.,review_provenance='interactive-human-vote'),assignment='training')
    # This durable point represents a process loss after recording feedback,
    # before metrics and cycle completion. No completed work may be retried.
    cycle.suspend();session.close()
    session=WorkspaceSession(store,run,tmp_path/'run',model,agent([]),events.append)
    async def forbidden(*args,**kwargs):raise AssertionError('completed feedback must not repeat optimization')
    session.optimize=forbidden
    try:
        asyncio.run(session.feedback({'item_id':'one','presentation_id':shown['prediction']['presentation_id'],'labels':[{'classifier_id':'a','label':'yes','comment':'Knowledge bases'}]},'vote'))
        assert model.calls==1
        history=session.wheels['a'].history(100000)
        assert len([e for e in history if e['kind']=='human-feedback'])==1
        assert len([e for e in history if e['kind']=='cycle-completed'])==1
        metrics=next(e for e in reversed(history) if e['kind']=='cycle-metrics')
        assert metrics['cycle_id']==next(e['cycle_id'] for e in history if e['kind']=='prediction')
        assert asyncio.run(session.prepare())=={'finished':True}
    finally:session.close()


def test_backfill_predicts_from_zero_then_replays_old_feedback_with_a_new_human_label(tmp_path):
    store=WebStore(tmp_path/'web.sqlite')
    for key in ('a','b'):
        store.save_classifier(key,key,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List');store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Paper'}}])
    from .workspace_session import freeze_configuration
    config=freeze_configuration(store,{'classifier_ids':['a'],'item_list_id':'list','max_requests':10,'max_optimizer_calls':3,'optimize_every':20,'rubric_changes_every':2,'seed':'seed'})
    parent=store.create_run('Anthus','live',config,items=store.list_items('list'))
    store.label_item('a',1,'list','one',1,'no','Original explanation','original')
    run=store.extend_cyclotron(parent['id'],['b'],name='Anthus')
    class Model:
        model_identity='fake'
        async def classify_many(self,configs,target,training,**kwargs):
            assert all(not rows for rows in training.values())
            assert all(not c.rubric and not c.example_ids for c in configs.values())
            return BatchedAnswers({key:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})} for key in configs},'fake',{},1)
    events=[];session=WorkspaceSession(store,run,tmp_path/'run',Model(),agent([]),events.append)
    try:
        shown=asyncio.run(session.prepare())
        assert shown['prediction']['recorded_labels'][0]['comment']=='Original explanation'
        asyncio.run(session.feedback({'item_id':'one','presentation_id':shown['prediction']['presentation_id'],'labels':[{'classifier_id':'b','label':'yes','comment':'New rubric'}]},'new-vote'))
        feedback=[e['feedback'] for e in events if e['kind']=='human-feedback']
        assert {f['item_id'] for f in feedback}=={'one'}
        assert {f['edit_comment_value'] for f in feedback}=={'Original explanation','New rubric'}
        assert len(store.item_labels('list','one',1))==2
        # Undo only labels provided in this session, not immutable source votes
        # that were replayed to backfill the newly added classifier.
        result=session.undo_feedback('undo-new-vote')
        assert result['classifier_ids']==['b']
        assert [(r['classifier_id'],r['request_id']) for r in store.item_labels('list','one',1)]==[('a','original')]
        assert asyncio.run(session.prepare())['prediction']==shown['prediction']
        asyncio.run(session.feedback({'item_id':'one','presentation_id':shown['prediction']['presentation_id'],
            'labels':[{'classifier_id':'b','label':'no','comment':'Corrected new-classifier vote'}]},'corrected-new-vote'))
        assert {r['classifier_id']:r['label'] for r in store.item_labels('list','one',1)}=={'a':'no','b':'no'}
    finally:session.close()


def test_two_classifiers_share_prediction_and_keep_feedback_separate_after_restart(tmp_path):
    store=WebStore(tmp_path/'web.sqlite')
    for key in ('a','b'):store.save_classifier(key,key,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List');store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    config={'classifier_ids':['a','b'],'item_list_id':'list','max_requests':10,'max_optimizer_calls':3,'optimize_every':20,'rubric_changes_every':2,'seed':'test'}
    from .workspace_session import freeze_configuration
    config=freeze_configuration(store,config)
    run=store.create_run('Test','live',config,items=store.list_items('list'))
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            return BatchedAnswers({key:{'decision':DecisionResult('yes',{'yes':.7,'no':.3})} for key in configs},'fake',{},1)
    model=Model();events=[]
    session=WorkspaceSession(store,run,tmp_path/'run',model,agent([]),events.append)
    shown=asyncio.run(session.prepare()); assert len(shown['prediction']['classifiers'])==2
    assert model.calls==1
    session.close()
    session=WorkspaceSession(store,run,tmp_path/'run',model,agent([]),events.append)
    asyncio.run(session.feedback({'item_id':'one','presentation_id':shown['prediction']['presentation_id'],
        'labels':[{'classifier_id':'a','label':'yes','comment':'My first rubric'},{'classifier_id':'b','label':'no','comment':'Different rubric'}]},'vote'))
    assert model.calls==1
    labels=store.item_labels('list','one',1)
    assert {v['classifier_id']:v['label'] for v in labels}=={'a':'yes','b':'no'}
    assert store.item_results('list','one',1)[0]['payload']['classifiers']['a']['label']=='yes'
    assert asyncio.run(session.prepare())=={'finished':True}
    feedback=[event for event in events if event['kind']=='human-feedback']
    assert {e['classifier_id'] for e in feedback}=={'a','b'}
    assert all(e['metrics']['window_size']==200 and e['metrics']['count']==1 for e in events if e['kind']=='cycle-metrics')
    session.close()


def test_application_traces_and_predictions_name_pinned_definition_revisions_after_restart(tmp_path):
    from .cyclotron_runtime import CyclotronRuntime
    store=WebStore(tmp_path/'web.sqlite')
    for key in ('a','b'):
        store.save_classifier(key,key,{'question':'Original question','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_cyclotron_definition('card','Card',[{'id':'a','revision':1},{'id':'b','revision':1}],{})
    store.save_item_list('list','List')
    store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    runtime=CyclotronRuntime(store,tmp_path/'runs',model_factory=lambda _:None,sink_factory=lambda _:None)
    run=runtime.create_run('Pinned',{'cyclotron_id':'card','item_list_id':'list','max_requests':10,
        'max_optimizer_calls':3,'optimize_every':20})
    class Model:
        model_identity='fake';calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            assert all(config.task.instructions=='Original question' for config in configs.values())
            return BatchedAnswers({cid:{'decision':DecisionResult('yes',{'yes':.7,'no':.3})}
                                  for cid in configs},'fake',{},1)
    class Sink:
        def __init__(self):self.events=[]
        def ingest(self,source,event):
            store.append_event(run['id'],source,event)
            self.events.append(event)
    sink=Sink();model=Model()
    session=WorkspaceSession(store,run,tmp_path/'run',model,agent([]),sink)
    shown=asyncio.run(session.prepare())
    session.close()
    store.save_classifier('a','Renamed',{'question':'New question','classes':[{'label':'yes'},{'label':'no'}]})
    session=WorkspaceSession(store,store.run(run['id']),tmp_path/'run',model,agent([]),sink)
    try:
        assert asyncio.run(session.prepare())==shown
        asyncio.run(session.feedback({'item_id':'one','presentation_id':shown['prediction']['presentation_id'],
            'labels':[{'classifier_id':cid,'label':'yes','comment':'Explained vote'} for cid in ('a','b')]},'vote'))
        assert model.calls==1
        for stored in store.all_events(run['id']):
            event=stored['payload']
            if event.get('classifier_id') in ('a','b'):
                assert event['classifier_revision']==1
            else:
                assert event['classifier_revisions']=={'a':1,'b':1}
            assert event['cyclotron_definition_revision']==run['config']['cyclotron_definition_revision']
            assert event['cyclotron_definition_fingerprint']==run['config']['cyclotron_definition_fingerprint']
        assert {'prediction','human-feedback','cycle-metrics'} <= {e['kind'] for e in sink.events}
        assert all(row['classifier_revision']==1 for row in shown['prediction']['classifiers'].values())
        for event in sink.events:
            if event['kind']=='prediction':
                assert event['version']==shown['prediction']['classifiers'][event['classifier_id']]['version']
        assert all(row['classifier_revision']==1 for row in store.item_labels('list','one',1))
        assert all(row['comment']=='Explained vote' for row in store.item_labels('list','one',1))
    finally:session.close()


def test_optimizer_traces_belong_only_to_the_classifier_that_requested_them(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from .workspace_session import freeze_configuration
    store=WebStore(tmp_path/'web.sqlite')
    for key in ('a','b'):store.save_classifier(key,key,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List');store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    config=freeze_configuration(store,{'classifier_ids':['a','b'],'item_list_id':'list','max_requests':10,'max_optimizer_calls':3,'optimize_every':20,'rubric_changes_every':2,'seed':'test'})
    run=store.create_run('Test','live',config,items=store.list_items('list'))
    events=[]
    session=WorkspaceSession(store,run,tmp_path/'run',SimpleNamespace(model_identity='fake'),agent([]),events.append)
    try:
        session.wheels['a'].optimizer.observer({'kind':'optimizer-request','messages':[]})
        assert [event['classifier_id'] for event in events if event['kind']=='optimizer-request']==['a']
        transport=session.wheels['a'].optimizer.complete
        transport.calls=3;transport.max_calls=3
        monkeypatch.setattr('decision_flywheel.workspace_session.LabelTransitionTrigger.check',lambda *args:{'due':True,'reason':'test cadence','details':{}})
        cycle=SimpleNamespace(check_trigger=lambda *args,**kwargs:None)
        warnings=asyncio.run(session.optimize('a',cycle))
        assert warnings[0]['reason']=='optimizer call limit reached; labeling can continue'
        assert transport.calls==3
        assert len([event for event in events if event['kind']=='optimizer-request'])==1
        assert any(event['kind']=='optimization-paused' for event in events)
    finally:session.close()


@pytest.mark.parametrize('failure',[StepFailed,RuntimeError])
def test_failed_learning_does_not_lose_votes_or_block_the_remaining_classifier_labels(tmp_path,failure):
    from .workspace_session import freeze_configuration
    from .observability import StepFailed
    store=WebStore(tmp_path/'web.sqlite')
    for key in ('a','b'):store.save_classifier(key,key,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List');store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    config=freeze_configuration(store,{'classifier_ids':['a','b'],'item_list_id':'list','max_requests':10,'max_optimizer_calls':3,'optimize_every':20,'rubric_changes_every':2,'seed':'test'})
    run=store.create_run('Test','live',config,items=store.list_items('list'))
    class Model:
        model_identity='fake'
        async def classify_many(self,configs,target,training,**kwargs):
            return BatchedAnswers({key:{'decision':DecisionResult('yes',{'yes':.7,'no':.3})} for key in configs},'fake',{},1)
    events=[];session=WorkspaceSession(store,run,tmp_path/'run',Model(),agent([]),events.append)
    shown=asyncio.run(session.prepare())
    attempts=[]
    async def fail_learning(identifier,cycle):
        attempts.append(identifier)
        raise failure('optimization step did not complete')
    session.optimize=fail_learning
    payload={'item_id':'one','presentation_id':shown['prediction']['presentation_id'],'labels':[{'classifier_id':key,'label':'yes'} for key in ('a','b')]}
    try:
        result=asyncio.run(session.feedback(payload,'vote'))
        assert result['reviewed']=='one'
        assert len(result['optimization_warnings'])==2
        assert len(store.item_labels('list','one',1))==2
        assert len([e for e in events if e['kind']=='cycle-metrics'])==2
        # Recovery of a persisted command must not repeat learning or votes.
        store.update_item(run['id'],'one',prediction=shown['prediction'],reviewed=False)
        asyncio.run(session.feedback(payload,'vote'))
        assert attempts==['a','b']
        assert len(store.item_labels('list','one',1))==2
        stages=[]
        async def resume_learning(identifier,cycle,*,resume_stages=()):
            stages.append((identifier,resume_stages))
            return []
        session.optimize=resume_learning
        result=asyncio.run(session.resume_optimization())
        assert len(result['resumed'])==2
        assert stages==[(key,('rubric','questions','examples','classifier')) for key in ('a','b')]
        assert len(store.item_labels('list','one',1))==2
    finally:session.close()
