"""Whole native console composition migrates without changing command order."""
import copy
from dataclasses import replace

import pytest

from .console_snapshot import ConsoleArchive
from .catalog_storage_test import populated
from .batch_storage import StorageScope
from .storage_payloads import FilePayloads,S3Payloads
from .storage_payloads_test import S3
from .web_store import WebStore


def console(path):
    store=populated(path)
    definition=store.save_cyclotron_definition('card','Card',[{'id':'topic','revision':2}],{})
    run=store.create_run('First','live',{'cyclotron_id':'card','cyclotron_definition_revision':definition['revision'],
        'classifiers':[store.classifier('topic',2)]},items=[{'id':'z','text':'First item'},{'id':'a','text':'Second item'}],
        frozen_feedback=[('z','topic',{'label':'sport','comment':'Keep frozen explanation'})],
        initial_events=[('initial',{'kind':'prediction','explanation':'Keep displayed explanation'})])
    store.update_item(run['id'],'z',prediction={'label':'sport','explanation':'Keep displayed explanation'})
    first=store.command(run['id'],'first-request','prepare',{})
    assert store.claim_command()['id']==first['id']
    second=store.create_run('Second','live',{})
    store.command(second['id'],'second-request','prepare',{})
    with store.connect() as db: db.execute("UPDATE sqlite_sequence SET seq=200 WHERE name='web_events'")
    return store,run,second


@pytest.mark.parametrize('backend',['file','s3'])
def test_console_roundtrip_preserves_definition_pins_commands_items_cursors_and_explanations(tmp_path,backend):
    source,run,second=console(tmp_path/'source.sqlite')
    snapshot=source.export_storage()
    scope=StorageScope('account','workspace')
    payloads=(FilePayloads(tmp_path/'payloads',scope,max_bytes=1000000) if backend=='file' else
        S3Payloads(S3(),'synthetic-bucket','111111111111',scope,max_bytes=1000000))
    archive=ConsoleArchive(payloads,scope,max_records=1000,max_bytes=1000000)
    publication=archive.publish(snapshot)
    assert archive.publish(snapshot)==publication
    assert archive.read(publication.manifest)==snapshot
    with pytest.raises(PermissionError): archive.read(replace(publication.manifest,scope=StorageScope('other','workspace')))
    destination=WebStore(tmp_path/'destination.sqlite')
    destination.restore_storage(archive.read(publication.manifest))
    assert destination.export_storage()==snapshot
    assert destination.runs()==source.runs()
    assert destination.items(run['id'])==source.items(run['id'])
    assert destination.current_item(run['id'])==source.current_item(run['id'])
    assert destination.all_events(run['id'])==source.all_events(run['id'])
    assert destination.jobs(run['id'])==source.jobs(run['id'])
    assert destination.run_cyclotron_definition(run['id'])==source.run_cyclotron_definition(run['id'])
    assert destination.item_labels('papers','one',2)==source.item_labels('papers','one',2)
    # Explicit recovery retains uncertain running work and dispatches only the
    # genuinely unstarted second command, never the first command again.
    destination.recover_interrupted()
    assert destination.jobs(run['id'])[0]['status']=='interrupted'
    assert destination.claim_command()['run_id']==second['id']
    assert destination.claim_command() is None
    event=destination.append_event(second['id'],'next',{'kind':'synthetic'})
    assert event['sequence']==201


def test_console_restore_rejects_changed_references_duplicate_ordinals_and_missing_tables_before_writes(tmp_path):
    source,_,_=console(tmp_path/'source.sqlite')
    snapshot=source.export_storage()
    for mutate in (
        lambda value:value['tables']['web_jobs'][0].update(run_id='foreign'),
        lambda value:value['tables']['web_items'][1].update(_rowid=value['tables']['web_items'][0]['_rowid']),
        lambda value:value['tables'].pop('cyclotron_definitions'),
        lambda value:value['tables']['run_cyclotron_definitions'][0].update(definition_revision=99),
        lambda value:value['sequences'].update(web_events=0),
    ):
        candidate=copy.deepcopy(snapshot); mutate(candidate)
        destination=WebStore(tmp_path/'destination.sqlite')
        with pytest.raises(ValueError): destination.restore_storage(candidate)
        assert destination.runs()==[] and destination.classifiers()==[]
    with pytest.raises(ValueError): source.export_storage(max_records=1)
    with pytest.raises(ValueError): source.export_storage(max_bytes=100)


def test_unknown_native_columns_are_never_silently_dropped_from_console_export(tmp_path):
    source,_,_=console(tmp_path/'source.sqlite')
    with source.connect() as db: db.execute('ALTER TABLE web_items ADD COLUMN future_priming TEXT')
    with pytest.raises(ValueError,match='schema'): source.export_storage()


def test_console_export_uses_one_snapshot_across_catalog_runs_and_event_watermarks(tmp_path,monkeypatch):
    from contextlib import contextmanager
    source,run,_=console(tmp_path/'source.sqlite')
    before=source.export_storage()
    writer=WebStore(tmp_path/'source.sqlite')
    connect=source.connect;changed=[]
    @contextmanager
    def exporting():
        with connect() as db:
            def refresh(statement):
                if statement.startswith('SELECT rowid AS _rowid,') and not changed:
                    changed.append(True)
                    writer.set_status(run['id'],'paused')
                    writer.append_event(run['id'],'during-export',{'kind':'synthetic','comment':'After snapshot'})
            db.set_trace_callback(refresh)
            yield db
    monkeypatch.setattr(source,'connect',exporting)
    assert source.export_storage()==before
    assert changed==[True] and source.export_storage()!=before


def test_console_restore_rolls_back_every_domain_after_native_failure(tmp_path,monkeypatch):
    from contextlib import contextmanager
    import sqlite3
    source,_,_=console(tmp_path/'source.sqlite')
    snapshot=source.export_storage()
    destination=WebStore(tmp_path/'destination.sqlite')
    connect=destination.connect
    @contextmanager
    def interrupted():
        with connect() as db:
            db.set_authorizer(lambda action,table,*args:
                sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and table=='web_jobs' else sqlite3.SQLITE_OK)
            yield db
    monkeypatch.setattr(destination,'connect',interrupted)
    with pytest.raises(sqlite3.DatabaseError): destination.restore_storage(snapshot)
    monkeypatch.setattr(destination,'connect',connect)
    assert all(not rows for rows in destination.export_storage()['tables'].values())


def test_run_pin_cannot_be_changed_independently_of_frozen_definition(tmp_path):
    source,run,_=console(tmp_path/'source.sqlite')
    source.save_cyclotron_definition('card','Other',[{'id':'topic','revision':1}],{})
    snapshot=source.export_storage()
    link=snapshot['tables']['run_cyclotron_definitions'][0]
    link['definition_revision']=2
    destination=WebStore(tmp_path/'destination.sqlite')
    with pytest.raises(ValueError,match='pin'): destination.restore_storage(snapshot)
    assert destination.runs()==[]


def test_legacy_definition_backfill_then_console_migration_preserves_native_behavior(tmp_path):
    from .cyclotron_definitions_test import legacy_editions
    path=tmp_path/'legacy.sqlite'
    source=WebStore(path)
    legacy_editions(source)
    source=WebStore(path)
    snapshot=source.export_storage()
    destination=WebStore(tmp_path/'destination.sqlite')
    destination.restore_storage(snapshot)
    assert destination.export_storage()==snapshot
    assert destination.runs()==source.runs()
    assert destination.cyclotron_definition_versions('legacy')==source.cyclotron_definition_versions('legacy')
    assert destination.item_labels('list','paper',1)==source.item_labels('list','paper',1)
