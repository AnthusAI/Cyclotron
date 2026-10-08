import pytest
from .web_store import WebStore


def classifier(store, identifier):
    return store.save_classifier(identifier, identifier, {'question':'Choose', 'classes':[{'label':'yes'}, {'label':'no'}]})


def test_scorecard_definitions_exist_without_runs_and_pin_ordered_classifier_revisions(tmp_path):
    store = WebStore(tmp_path/'db')
    classifier(store, 'a'); classifier(store, 'b')
    original = store.save_scorecard_definition('card', 'Card', [{'id':'b','revision':1}, {'id':'a','revision':1}], {'model':'jev'})
    assert original['revision'] == 1
    assert store.save_scorecard_definition('card', 'Card', original['classifiers'], {'model':'jev'}) == original
    updated = store.save_scorecard_definition('card', 'Renamed', [{'id':'a','revision':1}], {'model':'kev'})
    assert updated['revision'] == 2
    assert store.scorecard_definition('card', 1) == original
    assert updated['fingerprint'] != original['fingerprint']
    store.activate_scorecard_definition('card', 1)
    assert store.scorecard_definition('card') == original


def test_classifier_edits_create_revisions_for_each_active_containing_scorecard(tmp_path):
    store = WebStore(tmp_path/'db'); classifier(store, 'a')
    for identifier in ('one','two'):
        store.save_scorecard_definition(identifier, identifier, [{'id':'a','revision':1}], {})
    store.save_classifier('a', 'A revised', {'question':'New question', 'classes':[{'label':'yes'}, {'label':'no'}]})
    for identifier in ('one','two'):
        assert store.scorecard_definition(identifier)['classifiers'] == [{'id':'a','revision':2}]
        assert store.scorecard_definition(identifier, 1)['classifiers'] == [{'id':'a','revision':1}]


def test_invalid_membership_does_not_partially_create_a_version(tmp_path):
    store = WebStore(tmp_path/'db'); classifier(store, 'a')
    store.save_scorecard_definition('card','Card',[{'id':'a','revision':1}],{})
    with pytest.raises(ValueError):
        store.save_scorecard_definition('card','Broken',[{'id':'a','revision':999}],{})
    assert len(store.scorecard_definition_versions('card')) == 1


def test_invalid_shared_settings_cannot_publish_a_partial_revision(tmp_path):
    store=WebStore(tmp_path/'db');classifier(store,'a')
    original=store.save_scorecard_definition('card','Card',[{'id':'a','revision':1}],{})
    with pytest.raises(ValueError):
        store.save_scorecard_definition('card','Changed',original['classifiers'],{'optimize_every':0})
    assert store.scorecard_definition_versions('card')==[original]


def test_existing_run_editions_migrate_idempotently_without_rewriting_runs(tmp_path):
    path=tmp_path/'db';store=WebStore(path)
    classifier(store,'a');classifier(store,'b')
    parent=store.create_run('Original','live',{'classifiers':[store.classifier('a')],'item_list_id':'list'})
    store.save_item_list('list','List')
    edition=store.extend_scorecard(parent['id'],['b'],name='Anthus')
    frozen=store.run(edition['id'])
    reopened=WebStore(path)
    identifier=edition['config']['scorecard_id']
    assert reopened.scorecard_definition(identifier)['revision']==2
    assert reopened.scorecard_definition(identifier,1)['classifiers']==[{'id':'a','revision':1}]
    assert reopened.run(edition['id'])==frozen
    assert len(WebStore(path).scorecard_definition_versions(identifier))==2


def legacy_editions(store, *, invalid_pin=False):
    """Build an isolated database with the pre-definition edition schema."""
    import json
    classifier(store,'a');classifier(store,'b')
    store.save_item_list('list','List')
    store.upsert_list_items('list',[{'id':'paper','occurred_at':'2026-01-01',
        'values':{'text':'Original abstract'},'provenance':{'source':'synthetic fixture'}}])
    items=store.list_items('list')
    first=store.create_run('Old run','live',{'classifiers':[store.classifier('a')],'item_list_id':'list'},items=items)
    config={'classifiers':[store.classifier('a'),store.classifier('b')],'item_list_id':'list'}
    second=store.create_run('New run','live',config,items=items)
    store.label_item('a',1,'list','paper',1,'yes','Keep this exact explanation','vote')
    store.append_event(first['id'],'prediction',{'kind':'prediction','classifier_id':'a','target_id':'paper',
        'label':'yes','version':'synthetic-original','probabilities':{'yes':.8,'no':.2}})
    with store.connect() as db:
        db.execute('INSERT INTO scorecards VALUES (?,?,?)',('legacy','Legacy',2))
        for revision,run in enumerate((first,second),1):
            db.execute('INSERT INTO scorecard_versions VALUES (?,?,?,?,?)',
                ('legacy',revision,run['id'],first['id'] if revision==2 else None,run['created_at']))
        # This fixture alone predates all three tables. No production data is touched.
        for table in ('run_scorecard_definitions','scorecard_catalog','scorecard_definitions'):
            db.execute(f'DROP TABLE {table}')
        if invalid_pin:
            db.execute('UPDATE web_runs SET config=? WHERE id=?',
                (json.dumps({**config,'scorecard_definition_revision':999}),second['id']))
    second=store.run(second['id'])
    return first,second


def test_pre_definition_databases_migrate_without_changing_votes_explanations_items_or_traces(tmp_path):
    path=tmp_path/'legacy.sqlite';store=WebStore(path)
    runs=legacy_editions(store)
    before=[(store.run(run['id']),store.items(run['id']),store.all_events(run['id'])) for run in runs]
    votes=store.item_labels('list','paper',1)
    reopened=WebStore(path)
    assert reopened.scorecard_definition('legacy')['classifiers']==[{'id':'a','revision':1},{'id':'b','revision':1}]
    definitions=reopened.scorecard_definition_versions('legacy')
    assert len(definitions)==2
    for _ in range(2):
        reopened=WebStore(path)
        assert reopened.scorecard_definition_versions('legacy')==definitions
        assert [(reopened.run(run['id']),reopened.items(run['id']),reopened.all_events(run['id'])) for run in runs]==before
        assert reopened.item_labels('list','paper',1)==votes
        assert [reopened.run_scorecard_definition(run['id'])['revision'] for run in runs]==[1,2]


def test_an_invalid_legacy_pin_rolls_back_all_migrated_definitions_before_retry(tmp_path):
    import sqlite3
    path=tmp_path/'legacy.sqlite';store=WebStore(path)
    runs=legacy_editions(store,invalid_pin=True)
    with pytest.raises(ValueError,match='unknown scorecard definition'):WebStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM scorecard_definitions').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM run_scorecard_definitions').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM scorecard_catalog').fetchone()[0]==0
    assert store.run(runs[0]['id'])==runs[0]
    assert store.run(runs[1]['id'])==runs[1]
    assert store.item_labels('list','paper',1)[0]['comment']=='Keep this exact explanation'


def test_restoring_and_editing_old_membership_appends_history_without_replacing_newer_definitions(tmp_path):
    store=WebStore(tmp_path/'db')
    for key in ('a','b','c'):classifier(store,key)
    first=store.save_scorecard_definition('card','Original',[{'id':'a','revision':1},{'id':'b','revision':1}],{})
    second=store.save_scorecard_definition('card','Reordered',[{'id':'b','revision':1},{'id':'a','revision':1}],{})
    third=store.save_scorecard_definition('card','Removed A',[{'id':'b','revision':1}],{})
    store.activate_scorecard_definition('card',first['revision'])
    store.save_classifier('a','A changed',{'question':'New criteria','classes':[{'label':'yes'},{'label':'no'}]})
    fourth=store.scorecard_definition('card')
    assert fourth['revision']==4 and fourth['name']=='Original'
    assert fourth['classifiers']==[{'id':'a','revision':2},{'id':'b','revision':1}]
    assert store.scorecard_definition_versions('card')==[first,second,third,fourth]
    assert store.classifier('a',1)['config']['question']=='Choose'


def test_run_definition_registration_retains_shared_trigger_cadences(tmp_path):
    store=WebStore(tmp_path/'db');classifier(store,'a');classifier(store,'b')
    store.save_item_list('list','List')
    first=store.create_run('First','live',{'classifiers':[store.classifier('a')],'item_list_id':'list','optimize_every':7,'rubric_changes_every':3,
        'optimizer_transport':'litellm'})
    extended=store.extend_scorecard(first['id'],['b'],name='Card')
    definition=store.run_scorecard_definition(extended['id'])
    assert definition['settings']['optimize_every']==7
    assert definition['settings']['rubric_changes_every']==3
    assert definition['settings']['optimizer_transport']=='litellm'


def test_run_edition_numbers_cannot_overwrite_independent_definition_revisions(tmp_path):
    path=tmp_path/'db';store=WebStore(path)
    for key in ('a','b','c'):classifier(store,key)
    store.save_item_list('list','List')
    first=store.create_run('First','live',{'classifiers':[store.classifier('a')],'item_list_id':'list'})
    second=store.extend_scorecard(first['id'],['b'],name='Card')
    store=WebStore(path);identifier=second['config']['scorecard_id']
    revised=store.save_scorecard_definition(identifier,'Renamed',[{'id':'b','revision':1}],{})
    third=store.extend_scorecard(second['id'],['c'],name='Card')
    reopened=WebStore(path)
    assert reopened.scorecard_definition(identifier,revised['revision'])==revised
    pinned=reopened.run_scorecard_definition(third['id'])
    assert [row['id'] for row in pinned['classifiers']]==['a','b','c']
    assert pinned['revision']!=revised['revision']
