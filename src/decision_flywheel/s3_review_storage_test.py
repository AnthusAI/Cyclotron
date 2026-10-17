"""Reviewer source fidelity and racing human actions on canonical S3."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from .review_storage_test import article
from .reviewer_store import ReviewStore
from .s3_journal_test import journal


def storage(j=None):
    from .s3_review_storage import S3ReviewStorage
    s=S3ReviewStorage(j or journal(name='review'),max_records=1000);s.initialize();return s


def test_real_reviewer_preserves_displayed_prediction_human_rationale_and_undo_without_sqlite(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('cloud reviewer opened SQLite or local directory')
    monkeypatch.setattr('sqlite3.connect',forbidden);monkeypatch.setattr('pathlib.Path.mkdir',forbidden)
    s=storage();review=ReviewStore(None,study_seed='test',rolling_audit_rate=0,final_audit_rate=0,storage=s)
    review.import_articles([article()]);shown=review.record_prediction('one','include',.8,'synthetic','a'*64,0)
    vote=review.record_vote('one','exclude',comment='Exact human rationale 雪',presentation_id=shown.id)
    assert review.learning_feedback()[0].comment=='Exact human rationale 雪'
    assert review.learning_feedback()[0].initial_answer_value=='include'
    assert review.prediction_metrics().scored_votes==1;review.close()
    reopened=ReviewStore(None,study_seed='test',rolling_audit_rate=0,final_audit_rate=0,storage=storage(journal(s.journal.payloads.client,name='review')))
    assert reopened.events_for('one')==(vote,) and reopened.presentations_for('one')==(shown,)
    assert reopened.undo_last_vote().id=='one' and reopened.current_label('one') is None
    reopened.close()


def test_independent_s3_reviewers_cannot_create_two_active_votes():
    s=storage();review=ReviewStore(None,study_seed='test',storage=s);review.import_articles([article()])
    def vote(label):
        other=ReviewStore(None,study_seed='test',storage=storage(journal(s.journal.payloads.client,name='review')))
        try:other.record_vote('one',label);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(vote,('include','exclude')))==[False,True]
    assert len(review.events_for('one'))==1


def test_native_review_migration_roundtrips_raw_records_without_sqlite_cloud_validation(tmp_path,monkeypatch):
    native=ReviewStore(tmp_path/'native.sqlite',study_seed='test',rolling_audit_rate=0,final_audit_rate=0)
    native.import_articles([article()]);shown=native.record_prediction('one','include',.8,'synthetic','a'*64,0)
    native.record_vote('one','exclude',comment='Exact source rationale',presentation_id=shown.id)
    snapshot=native.storage.export_storage();native.close()
    def forbidden(*args,**kwargs):raise AssertionError('cloud reviewer migration opened SQLite')
    monkeypatch.setattr('sqlite3.connect',forbidden)
    s=storage();s.restore_storage(snapshot)
    assert s.export_storage()==snapshot
    bad=s.export_storage();bad['tables']['review_events'][0]['presentation_id']='foreign'
    target=storage(journal(s.journal.payloads.client,name='review-target'))
    with pytest.raises(ValueError):target.restore_storage(bad)
    assert target.articles()==[]


def test_conflicting_study_bind_rolls_back_new_metadata_and_limit_rejection_keeps_source_empty():
    from .s3_review_storage import S3ReviewStorage
    s=storage();s.bind_study({'rolling_audit_rate':'0.2'})
    with pytest.raises(ValueError):s.bind_study({'study_seed':'new','rolling_audit_rate':'0.9'})
    assert s.export_storage()['tables']['study_metadata']==[{'_rowid':1,'key':'rolling_audit_rate','value':'0.2'}]
    limited=S3ReviewStorage(journal(s.journal.payloads.client,name='tiny-review'),max_records=1);limited.initialize()
    with pytest.raises(ValueError):limited.bind_study({'seed':'one','another':'two'})
    assert limited.export_storage()['tables']['study_metadata']==[]
