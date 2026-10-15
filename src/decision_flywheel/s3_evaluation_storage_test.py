"""S3 frozen evaluation retains clock, trace and exclusive whole-plan execution."""
import asyncio
from uuid import uuid4
import pytest
from .s3_journal_test import journal


def storage(j=None):
    from .s3_evaluation_storage import S3EvaluationStorage
    s=S3EvaluationStorage(j or journal(name='evaluation'),max_records=1000)
    s.initialize();s.acquire(str(uuid4()));return s


def test_evaluation_native_snapshot_import_preserves_first_trace_and_deleted_watermark(tmp_path,monkeypatch):
    from .evaluation_storage import SQLiteEvaluationStorage
    import sqlite3
    native=SQLiteEvaluationStorage(tmp_path/'native.sqlite')
    native.bind_clock('plan','2026-01-01T00:00:00+00:00');first=native.append_trace('first','{ "raw":"雪" }')
    native.append_trace('deleted','{}')
    with sqlite3.connect(native.path) as db:db.execute("DELETE FROM trace WHERE key='deleted'")
    snapshot=native.export_storage()
    def forbidden(*args,**kwargs):raise AssertionError('cloud evaluation opened SQLite')
    monkeypatch.setattr('sqlite3.connect',forbidden)
    s=storage();s.restore_storage(snapshot)
    assert s.export_storage()==snapshot
    assert s.bind_clock('plan','2099-01-01T00:00:00+00:00')=='2026-01-01T00:00:00+00:00'
    assert s.append_trace('first','{"changed":true}')==first
    assert s.append_trace('next','{}')[0]==4
    s.close()


def test_another_evaluator_cannot_claim_the_same_plan_by_timeout_or_restarting():
    from .s3_evaluation_storage import S3EvaluationStorage
    from .s3_runtime_storage import RuntimeOwnershipError
    s=storage();other=S3EvaluationStorage(journal(s.journal.payloads.client,name='evaluation'),max_records=1000)
    other.initialize()
    with pytest.raises(RuntimeOwnershipError):other.acquire(str(uuid4()))
    s.close();other.acquire(str(uuid4()))
    with pytest.raises(RuntimeOwnershipError):s.append_trace('stale','{}')
    other.close()


def test_real_frozen_evaluator_uses_s3_clock_trace_and_endpoint_ledgers_without_local_files(monkeypatch):
    from dataclasses import asdict
    from .matched_run_plan_test import source
    from .matched_run_plan import plan_matched_runs
    from .matched_run_evaluation import evaluate_matched_runs
    from .matched_run_evaluation_test import Model
    from .classifier_config import ClassifierConfig
    from .models import DecisionTask
    from .flywheel import FittedClassifier
    from .s3_batch_storage import S3BatchStorage
    endpoints={side:source(side) for side in ('before','after')}
    for endpoint in endpoints.values():endpoint['checkpoint']['payload']['classifiers']={'topic':{'state':asdict(FittedClassifier(ClassifierConfig(DecisionTask('topic',('yes','no'),'Choose'))))}}
    plan=plan_matched_runs(**endpoints);s=storage();models={side:Model() for side in endpoints}
    def batches(side):
        result=S3BatchStorage(journal(s.journal.payloads.client,name='endpoint-'+side),max_records=1000)
        result.initialize();return result
    def forbidden(*args,**kwargs):raise AssertionError('cloud evaluator opened SQLite or local directory')
    monkeypatch.setattr('sqlite3.connect',forbidden);monkeypatch.setattr('pathlib.Path.mkdir',forbidden)
    def execute():return asyncio.run(evaluate_matched_runs(plan,endpoints,models,None,max_requests=8,observer=lambda _:None,
        execution_storage=s,batch_storage_factory=batches))
    first=execute();second=execute()
    assert first['requests']==8 and second['new_requests']==0
    assert first['classifiers']==second['classifiers']
    assert sum(len(m.requests) for m in models.values())==8
    s.close()


def test_uncertain_trace_receipt_preserves_original_event_and_requires_reopen():
    from .s3_journal import JournalUncertain
    from .s3_runtime_storage import RuntimeOwnershipError
    s=storage();s.bind_clock('plan','2026-01-01T00:00:00+00:00')
    s.journal.payloads.client.lose=lambda key:key==s.journal.head_key
    with pytest.raises(JournalUncertain):s.append_trace('event','{ "original":true }')
    with pytest.raises(RuntimeOwnershipError):s.append_trace('event','{"replacement":true}')
    s.close();fresh=storage(journal(s.journal.payloads.client,name='evaluation'))
    assert fresh.append_trace('event','{"replacement":true}')==(1,'{ "original":true }')
    assert fresh.events()==[(1,'{ "original":true }')]
    fresh.close()
