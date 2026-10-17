"""Complete native runtime migration retains evidence, order and lazy schemas."""
from copy import deepcopy
import sqlite3
import pytest
from .runtime_snapshot import export_runtime, restore_runtime, RuntimeArchive
from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import TASK,FakeModel,agent
from .feature_bank import FeatureBank
from .hypothesis_ledger import HypothesisLedger
from .learning_storage import ARTIFACTS
from .batch_storage import StorageScope
from .storage_payloads import FilePayloads, S3Payloads
from .storage_payloads_test import S3


def populated(path):
    wheel=DecisionFlywheel(path,ClassifierConfig(TASK),FakeModel(),agent([]))
    wheel._emit({'kind':'prediction','cycle_number':8,'target_id':'one','decision_model_label':'include'})
    wheel.runtime_cache.begin_answer('old',None)
    wheel.runtime_cache.complete_answer('old','{ "answers": "exact 雪" }',previous=None)
    wheel.runtime_cache.begin_answer('old',('complete','{ "answers": "exact 雪" }'))
    wheel.runtime_cache.complete_answer('old','{"answers":"new"}',previous=('complete','{ "answers": "exact 雪" }'),current_key='new')
    wheel.runtime_cache.begin_round('trial')
    wheel.runtime_cache.complete_round('trial','{"answer_dependencies":[]}')
    for kind in ARTIFACTS:
        wheel.learning_storage.ensure(kind)
        wheel.learning_storage.begin(kind,'second','{"pending_evaluation":true}')
        wheel.learning_storage.complete(kind,'second','{ "answer_dependencies":[], "source":9007199254740993 }')
        wheel.learning_storage.begin(kind,'first','{"progress":"not automatically retried"}')
    FeatureBank(storage=wheel.feature_storage).register({'name':'test','instructions':'Test?','labels':['yes','no']},rationale='exact reason',evidence={'source':'one'})
    wheel.control_storage.save('idea','{ "id":"idea", "operator":"revise", "parents":["ancestor"], "addresses":["cause"] }')
    ledger=HypothesisLedger(storage=wheel.hypothesis_storage,clock=lambda:'2026-01-01T00:00:00Z')
    ledger.record_evaluations('idea',[{'item_id':'one','correct':True,'label':'include','choice':'include','confidence':.8}],'v1')
    ledger.record_evaluations('idea',[{'item_id':'one','correct':False,'label':'include','choice':'exclude','confidence':.8}],'v2')
    ledger.record_verdict('idea','advanced','original explanation',{'net':1})
    ledger.record_verdict('idea','retired','later explanation',{'net':0})
    ledger.record_cause(1,{'id':'cause','name':'ambiguity'})
    ledger.record_element_evaluation('test',1,question='Test?',source='cause',n=1,metrics={'auc_label':.5},kept=True,reason='retained')
    wheel.shadow_storage.ensure()
    wheel.shadow_storage.join('idea','rubric',1)
    wheel.shadow_storage.scored('idea','one',2)
    wheel.shadow_storage.incumbent('inc','original rubric',2)
    wheel.shadow_storage.look(('idea','inc',1,20,3,0,.03,1,2))
    return wheel


def test_all_runtime_domains_round_trip_exactly_including_ordinals_raw_json_and_history(tmp_path):
    wheel=populated(tmp_path/'source.sqlite')
    snapshot=export_runtime(wheel.db)
    assert len(snapshot['present_tables'])==20
    restored=sqlite3.connect(tmp_path/'restored.sqlite')
    restore_runtime(restored,snapshot)
    assert export_runtime(restored)==snapshot
    ledger=HypothesisLedger(restored)
    assert ledger.correctness_vector('idea',['one'])==[False]
    assert [v['verdict'] for v in ledger.verdicts('idea')]==['advanced','retired']
    assert ledger.lineage('idea')['parents']==['ancestor']
    assert restored.execute("SELECT payload FROM control_ideas").fetchone()[0].startswith('{ "id"')
    restored.close();wheel.close()


@pytest.mark.parametrize('backend',['file','synthetic-s3'])
def test_immutable_runtime_archive_reuses_shared_payload_transport(tmp_path,backend):
    wheel=populated(tmp_path/'source.sqlite')
    snapshot=export_runtime(wheel.db)
    scope=StorageScope('account','workspace')
    payloads=(FilePayloads(tmp_path/'payloads',scope,max_bytes=64000000) if backend=='file' else
        S3Payloads(S3(),'synthetic-bucket','111111111111',scope,max_bytes=64000000))
    archive=RuntimeArchive(payloads,scope,max_records=1000,max_bytes=64000000)
    published=archive.publish(snapshot)
    assert archive.read(published.manifest)==snapshot
    wheel.close()


@pytest.mark.parametrize('corruption',['ordinal','duplicate','coercion','absent_table','json','future'])
def test_invalid_runtime_import_fails_before_creating_or_writing_tables(tmp_path,corruption):
    wheel=populated(tmp_path/'source.sqlite')
    snapshot=deepcopy(export_runtime(wheel.db))
    if corruption=='ordinal': snapshot['tables']['idea_verdicts'][0]['_rowid']=0
    if corruption=='duplicate': snapshot['tables']['control_ideas'].append(deepcopy(snapshot['tables']['control_ideas'][0]))
    if corruption=='coercion': snapshot['tables']['shadow_looks'][0]['look']='1'
    if corruption=='absent_table': snapshot['present_tables'].remove('idea_verdicts')
    if corruption=='json': snapshot['tables']['control_ideas'][0]['payload']='{invalid'
    if corruption=='future': snapshot['tables']['future']=[]
    destination=sqlite3.connect(tmp_path/'destination.sqlite')
    with pytest.raises(ValueError): restore_runtime(destination,snapshot)
    assert destination.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[]
    destination.close();wheel.close()


def test_unknown_or_changed_native_schema_and_read_budgets_fail_explicitly(tmp_path):
    wheel=populated(tmp_path/'source.sqlite')
    with pytest.raises(ValueError,match='record budget'): export_runtime(wheel.db,max_records=1)
    with pytest.raises(ValueError,match='byte budget'): export_runtime(wheel.db,max_bytes=10)
    wheel.db.execute('CREATE TABLE future(id TEXT)')
    with pytest.raises(ValueError,match='schema'): export_runtime(wheel.db)
    wheel.db.execute('DROP TABLE future')
    wheel.db.execute('ALTER TABLE control_ideas ADD COLUMN future TEXT')
    with pytest.raises(ValueError,match='schema'): export_runtime(wheel.db)
    wheel.close()


def test_lazy_table_absence_and_pending_work_are_preserved_and_nonempty_restore_is_refused(tmp_path):
    wheel=DecisionFlywheel(tmp_path/'source.sqlite',ClassifierConfig(TASK),FakeModel(),agent([]))
    wheel.runtime_cache.begin_answer('uncertain',None)
    snapshot=export_runtime(wheel.db)
    assert len(snapshot['present_tables'])==5
    restored=sqlite3.connect(tmp_path/'restored.sqlite')
    restore_runtime(restored,snapshot)
    assert export_runtime(restored)==snapshot
    assert restored.execute('SELECT status FROM runtime_answers').fetchone()[0]=='pending'
    with pytest.raises(ValueError,match='empty'): restore_runtime(restored,snapshot)
    restored.close();wheel.close()


def test_native_restore_rolls_back_all_schema_and_rows_after_mid_restore_failure(tmp_path,monkeypatch):
    from . import runtime_snapshot
    wheel=populated(tmp_path/'source.sqlite')
    snapshot=export_runtime(wheel.db)
    destination=sqlite3.connect(tmp_path/'destination.sqlite')
    insert=runtime_snapshot._insert
    def fail(db,table,rows):
        if db is destination and table=='feature_bank': raise RuntimeError('synthetic interrupted restore')
        return insert(db,table,rows)
    monkeypatch.setattr(runtime_snapshot,'_insert',fail)
    with pytest.raises(RuntimeError,match='interrupted restore'): restore_runtime(destination,snapshot)
    assert destination.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[]
    destination.close();wheel.close()


def test_runtime_export_is_one_snapshot_while_another_process_updates_two_domains(tmp_path):
    import threading
    path=tmp_path/'source.sqlite'
    wheel=populated(path)
    before=export_runtime(wheel.db)
    start,done=threading.Event(),threading.Event()
    failures=[]
    def writer():
        try:
            assert start.wait(5)
            with sqlite3.connect(path,timeout=5) as db:
                db.execute("UPDATE control_ideas SET payload=?",('{"id":"idea","changed":true}',))
                db.execute("UPDATE idea_verdicts SET reason='changed concurrently'")
        except BaseException as error: failures.append(error)
        finally: done.set()
    worker=threading.Thread(target=writer)
    worker.start()
    def trace(sql):
        if sql.startswith('SELECT COUNT(*)') and not start.is_set():
            start.set()
            assert done.wait(5)
    wheel.db.set_trace_callback(trace)
    try: snapshot=export_runtime(wheel.db)
    finally:
        wheel.db.set_trace_callback(None)
        worker.join(5)
    assert not failures and not worker.is_alive()
    assert snapshot==before
    assert export_runtime(wheel.db)!=before
    wheel.close()


def test_native_runtime_store_restore_reopens_the_real_cache_without_provider_replay(tmp_path):
    import asyncio
    from datetime import datetime,timezone
    from .runtime_storage import SQLiteRuntimeStateStorage
    from .flywheel_test import TRAIN,DEV
    from .decision_cache import CacheOptions
    config=ClassifierConfig(TASK)
    now=datetime(2026,1,1,tzinfo=timezone.utc)
    source=DecisionFlywheel(tmp_path/'source.sqlite',config,FakeModel(),agent([]))
    answer=asyncio.run(source._answers(config,DEV[0].item,TRAIN,now))
    dependency=source.answer_dependency(config,DEV[0].item,TRAIN,now)
    snapshot=source.runtime_storage.export_storage()
    source.close()
    destination=SQLiteRuntimeStateStorage(tmp_path/'restored.sqlite')
    destination.restore_storage(snapshot)
    assert destination.export_storage()==snapshot
    destination.close()
    model=FakeModel()
    restored=DecisionFlywheel(tmp_path/'restored.sqlite',config,model,agent([]))
    cached=asyncio.run(restored._answers(config,DEV[0].item,TRAIN,now,CacheOptions(policy='cache_only')))
    assert cached==answer and model.calls==0
    assert restored.answer_dependencies_current([dependency])
    restored.close()
