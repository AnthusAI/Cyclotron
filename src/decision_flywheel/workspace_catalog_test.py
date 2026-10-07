"""Catalog specifications: source-neutral items and immutable classifier revisions."""
import pytest
from .web_store import WebStore


def configuration():
    return {'question':'Choose a topic', 'classes':[{'label':'science'}, {'label':'sport'}, {'label':'business'}]}


def test_classifier_revisions_preserve_order_and_old_configuration(tmp_path):
    store = WebStore(tmp_path/'web.sqlite')
    first = store.save_classifier('topics', 'Topics', configuration())
    updated = {**configuration(), 'question':'Choose the primary topic'}
    second = store.save_classifier('topics', 'Topics', updated)
    assert first['revision'] == 1 and second['revision'] == 2
    assert store.classifier('topics', 1)['config'] == configuration()
    assert store.classifier('topics')['config']['classes'][0]['label'] == 'science'
    assert store.save_classifier('topics', 'Topics', updated)['revision'] == 2
    with pytest.raises(ValueError):
        store.save_classifier('bad', 'Bad', {'question':'?', 'classes':[{'label':'same'}, {'label':'same'}]})


def test_classifier_history_returns_immutable_revisions_in_order_without_changing_active_membership(tmp_path):
    store=WebStore(tmp_path/'web.sqlite')
    first=store.save_classifier('topics','Topics',configuration())
    second=store.save_classifier('topics','Topics renamed',{**configuration(),'question':'New question'})
    assert store.classifier_versions('topics')==[first,second]
    assert store.classifier('topics')['revision']==2
    with pytest.raises(ValueError):store.classifier_versions('missing')


def test_item_upserts_are_idempotent_revisioned_and_chronological(tmp_path):
    store = WebStore(tmp_path/'web.sqlite')
    store.save_item_list('papers', 'Papers')
    later = {'id':'b', 'occurred_at':'2026-01-02', 'values':{'text':'Later'}, 'provenance':{'source':'fixture'}}
    earlier = {**later, 'id':'a', 'occurred_at':'2026-01-01', 'values':{'text':'Earlier'}}
    assert store.upsert_list_items('papers', [later, earlier]) == {'inserted':2,'updated':0,'unchanged':0}
    assert store.upsert_list_items('papers', [later])['unchanged'] == 1
    changed = {**earlier, 'values':{'text':'Revised'}}
    assert store.upsert_list_items('papers', [changed])['updated'] == 1
    assert [item['id'] for item in store.list_items('papers')] == ['a', 'b']
    assert store.item_revision('papers','a',1)['values']['text'] == 'Earlier'
    assert store.item_lists()[0]['count'] == 2


def test_invalid_item_batch_is_atomic_and_lists_do_not_share_items(tmp_path):
    store = WebStore(tmp_path/'web.sqlite')
    for name in ('a','b'): store.save_item_list(name,name)
    with pytest.raises(ValueError):
        store.upsert_list_items('a',[{'id':'good','values':{},'occurred_at':'2026-01-01'}, {'id':'bad','values':{},'occurred_at':'yesterday'}])
    assert store.list_items('a') == [] and store.list_items('b') == []


def test_one_item_has_independent_classifier_labels_and_corrections_preserve_history(tmp_path):
    store = WebStore(tmp_path/'web.sqlite')
    for classifier in ('topics','priority'): store.save_classifier(classifier,classifier,configuration())
    store.save_item_list('papers','Papers')
    store.upsert_list_items('papers',[{'id':'a','occurred_at':'2026-01-01','values':{'text':'Paper'}}])
    def vote(classifier,label,request):
        return store.label_item(classifier,1,'papers','a',1,label,'Because of its subject',request)
    first = vote('topics','science','vote-1')
    vote('priority','sport','vote-2')
    assert vote('topics','science','vote-1') == first
    vote('topics','business','vote-3')
    assert [(row['classifier_id'],row['label']) for row in store.item_labels('papers','a',1)] == [('priority','sport'),('topics','business')]
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM item_feedback').fetchone()[0] == 3
    with pytest.raises(ValueError): vote('topics','invalid','vote-4')
    with pytest.raises(ValueError): vote('topics','sport','vote-1')


def test_retracting_a_current_label_preserves_history_without_resurrecting_superseded_votes(tmp_path):
    store = WebStore(tmp_path / 'workspace.sqlite')
    store.save_classifier('topics', 'Topics', configuration())
    store.save_item_list('papers', 'Papers')
    store.upsert_list_items('papers', [{'id': 'a', 'occurred_at': '2026-01-01', 'values': {'text': 'Paper'}}])
    store.label_item('topics', 1, 'papers', 'a', 1, 'science', 'First', 'first')
    store.label_item('topics', 1, 'papers', 'a', 1, 'business', 'Correction', 'corrected')
    with pytest.raises(ValueError, match='current'):
        store.retract_item_label('first', 'stale-undo')
    result = store.retract_item_label('corrected', 'undo')
    assert store.item_labels('papers', 'a', 1) == []
    assert WebStore(tmp_path / 'workspace.sqlite').item_labels('papers', 'a', 1) == []
    assert store.retract_item_label('corrected', 'undo') == result
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM item_feedback').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM item_feedback_retractions').fetchone()[0] == 1
    store.label_item('topics', 1, 'papers', 'a', 1, 'sport', 'New review', 'new')
    assert store.item_labels('papers', 'a', 1)[0]['label'] == 'sport'
    with pytest.raises(ValueError, match='different content'):
        store.retract_item_label('new', 'undo')
