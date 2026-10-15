"""Known domain migrations preserve source IDs and reject incomplete evidence."""
import asyncio
from copy import deepcopy
import sqlite3
import pytest
from .domain_snapshots import embedded_codec,review_codec,evaluation_codec,DomainArchive
from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import DEFINITION,Model,item
from .reviewer_store import ReviewStore
from .review_storage_test import article
from .evaluation_storage import SQLiteEvaluationStorage
from .batch_storage import StorageScope
from .storage_payloads import FilePayloads,S3Payloads
from .storage_payloads_test import S3


@pytest.mark.parametrize('backend',['file','synthetic-s3'])
@pytest.mark.parametrize('domain',['embedded','review','evaluation'])
def test_complete_native_domain_roundtrip_preserves_every_row_and_observable_order(tmp_path,domain,backend):
    if domain=='embedded':
        owner=Cyclotron.open(tmp_path/'engine',DEFINITION,Model())
        decision=asyncio.run(owner.decide(item('one')))
        asyncio.run(owner.review(decision.decision_id,'include',review_id='vote',explanation='Exact explanation 雪'))
        codec,db=embedded_codec(),owner.root_storage.db
    elif domain=='review':
        owner=ReviewStore(tmp_path/'review.sqlite',study_seed='test',rolling_audit_rate=0,final_audit_rate=0)
        owner.import_articles([article('one'),article('two')])
        shown=owner.record_prediction('one','include',.8,'synthetic','a'*64,0)
        owner.record_vote('one','exclude',comment='Exact explanation 雪',presentation_id=shown.id)
        owner.undo_last_vote()
        owner.record_skip('two')
        codec,db=review_codec(),owner.storage.db
    else:
        owner=SQLiteEvaluationStorage(tmp_path/'evaluation.sqlite')
        owner.bind_clock('plan','2026-01-01T00:00:00+00:00')
        owner.append_trace('one','{ "note":"Exact 雪" }')
        owner.append_trace('deleted','{}')
        db=sqlite3.connect(owner.path)
        db.execute("DELETE FROM trace WHERE key='deleted'");db.commit()
        codec=evaluation_codec()
    snapshot=codec.export(db)
    destination=sqlite3.connect(tmp_path/'destination.sqlite')
    codec.restore(destination,snapshot)
    assert codec.export(destination)==snapshot
    scope=StorageScope('account','workspace')
    payloads=(FilePayloads(tmp_path/'payloads',scope,max_bytes=64000000) if backend=='file' else
        S3Payloads(S3(),'synthetic-bucket','111111111111',scope,max_bytes=64000000))
    archive=DomainArchive(codec,payloads,scope,max_records=10000,max_bytes=64000000)
    assert archive.read(archive.publish(snapshot).manifest)==snapshot
    if domain=='evaluation':
        assert destination.execute('INSERT INTO trace(key,payload) VALUES (?,?)',('later','{}')).lastrowid==3
        destination.commit();db.close()
    else: owner.close()
    destination.close()


def test_reviewer_bad_presentation_reference_is_rejected_before_destination_side_effects(tmp_path):
    owner=ReviewStore(tmp_path/'review.sqlite',study_seed='test')
    owner.import_articles([article()])
    shown=owner.record_prediction('one','include',.8,'synthetic','a'*64,0)
    owner.record_vote('one','include',presentation_id=shown.id)
    codec=review_codec();snapshot=deepcopy(codec.export(owner.storage.db))
    snapshot['tables']['review_events'][0]['presentation_id']='missing'
    destination=sqlite3.connect(':memory:')
    with pytest.raises(ValueError): codec.restore(destination,snapshot)
    assert destination.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[]
    destination.close();owner.close()


def test_domain_restore_rollback_keeps_destination_empty_on_failure_mid_composition(tmp_path,monkeypatch):
    owner=ReviewStore(tmp_path/'review.sqlite',study_seed='test')
    owner.import_articles([article()])
    codec=review_codec();snapshot=codec.export(owner.storage.db)
    destination=sqlite3.connect(':memory:')
    insert=codec._insert
    def fail(db,table,rows):
        if db is destination and table=='articles': raise RuntimeError('synthetic interruption')
        return insert(db,table,rows)
    monkeypatch.setattr(codec,'_insert',fail)
    with pytest.raises(RuntimeError): codec.restore(destination,snapshot)
    assert destination.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[]
    destination.close();owner.close()


def test_domain_unknown_schema_bad_ordinals_and_budgets_fail_explicitly(tmp_path):
    owner=ReviewStore(tmp_path/'review.sqlite',study_seed='test')
    owner.import_articles([article()])
    codec=review_codec()
    with pytest.raises(ValueError,match='record budget'): codec.export(owner.storage.db,max_records=1)
    with pytest.raises(ValueError,match='byte budget'): codec.export(owner.storage.db,max_bytes=10)
    snapshot=codec.export(owner.storage.db)
    snapshot['tables']['articles'][0]['_rowid']=0
    with pytest.raises(ValueError): codec.validate(snapshot)
    owner.storage.db.execute('CREATE TABLE future(id TEXT)')
    with pytest.raises(ValueError,match='schema'): codec.export(owner.storage.db)
    owner.close()


def test_migrated_reviewer_store_reopens_with_same_active_feedback_and_displayed_prediction(tmp_path):
    path=tmp_path/'source.sqlite'
    owner=ReviewStore(path,study_seed='test',rolling_audit_rate=0,final_audit_rate=0)
    owner.import_articles([article()])
    shown=owner.record_prediction('one','include',.8,'synthetic','a'*64,0)
    vote=owner.record_vote('one','exclude',comment='Preserve rationale',presentation_id=shown.id)
    snapshot=owner.storage.export_storage()
    owner.close()
    destination=tmp_path/'destination.sqlite'
    db=sqlite3.connect(destination)
    review_codec().restore(db,snapshot);db.close()
    restored=ReviewStore(destination,study_seed='test',rolling_audit_rate=0,final_audit_rate=0)
    assert restored.events_for('one')==(vote,)
    assert restored.presentations_for('one')==(shown,)
    assert restored.learning_feedback()[0].initial_answer_value=='include'
    assert restored.learning_feedback()[0].comment=='Preserve rationale'
    assert restored.prediction_metrics().scored_votes==1
    restored.close()


def test_migrated_embedded_root_reopens_against_matching_native_bundle_without_provider_replay(tmp_path):
    from .embedded_storage import SQLiteEmbeddedStorage
    source=tmp_path/'source'
    model=Model()
    with Cyclotron.open(source,DEFINITION,model) as owner:
        decision=asyncio.run(owner.decide(item('one')))
        asyncio.run(owner.review(decision.decision_id,'include',review_id='review',explanation='Original rationale'))
        before=owner.subscribe()
        root_snapshot=owner.root_storage.export_storage()
        owner.snapshot(tmp_path/'matching.tar.gz')
    destination=tmp_path/'restored'
    Cyclotron.restore(tmp_path/'matching.tar.gz',destination)
    (destination/'cyclotron.sqlite3').unlink()
    root=SQLiteEmbeddedStorage(destination/'cyclotron.sqlite3')
    root.restore_storage(root_snapshot);root.close()
    fresh=Model()
    with Cyclotron.open(destination,DEFINITION,fresh) as restored:
        assert restored.subscribe()==before
        assert restored.labels()[0]['explanation']=='Original rationale'
        assert asyncio.run(restored.decide(item('one')))==decision
        assert fresh.calls==0


def test_migrated_evaluation_storage_retains_clock_original_trace_and_sequence(tmp_path):
    source=SQLiteEvaluationStorage(tmp_path/'source.sqlite')
    source.bind_clock('plan','2026-01-01T00:00:00+00:00')
    first=source.append_trace('one','{ "first":true }')
    source.append_trace('discarded','{}')
    with sqlite3.connect(source.path) as db: db.execute("DELETE FROM trace WHERE key='discarded'")
    snapshot=source.export_storage()
    restored=SQLiteEvaluationStorage(tmp_path/'destination.sqlite')
    restored.restore_storage(snapshot)
    assert restored.bind_clock('plan','2099-01-01T00:00:00+00:00')=='2026-01-01T00:00:00+00:00'
    assert restored.append_trace('one','{"later":true}')==first
    # Native INSERT OR IGNORE retains the retry's gap in AUTOINCREMENT.
    assert restored.append_trace('new','{}')[0]==4


def test_legacy_reviewer_column_upgrades_remain_migratable(tmp_path):
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE articles(id TEXT PRIMARY KEY,title TEXT NOT NULL,abstract TEXT NOT NULL,submitted_at TEXT NOT NULL,categories_json TEXT NOT NULL,assignment TEXT NOT NULL)')
        db.execute('INSERT INTO articles VALUES (?,?,?,?,?,?)',('old','Old title','Old abstract','2026-01-01','["cs.AI"]','train'))
    owner=ReviewStore(path,study_seed='test')
    snapshot=owner.storage.export_storage()
    db=sqlite3.connect(tmp_path/'restored.sqlite')
    review_codec().restore(db,snapshot)
    assert db.execute("SELECT authors,journal_ref FROM articles WHERE id='old'").fetchone()==('Not provided',None)
    db.close();owner.close()
