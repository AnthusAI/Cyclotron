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
