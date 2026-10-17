"""Review source transactions preserve what was shown and which vote is active."""
import pytest

from .review_storage import SQLiteReviewStorage
from .reviewer_store import Article, ReviewStore


def article(identifier='one'):
    return Article(identifier,'Title','Abstract','2026-01-01',('cs.AI',))


def test_reviewer_algorithms_delegate_without_touching_the_native_connection(tmp_path):
    store=ReviewStore(tmp_path/'review.sqlite',study_seed='test',rolling_audit_rate=0,final_audit_rate=0)
    class Forbidden:
        def __getattr__(self,name): raise AssertionError('review algorithm issued SQL outside repository')
    store._connection=Forbidden()
    store.import_articles([article()])
    shown=store.record_prediction('one','include',.8,'synthetic','a'*64,0)
    store.record_vote('one','exclude',comment='Exact explanation 雪',presentation_id=shown.id)
    assert store.learning_feedback()[0].comment=='Exact explanation 雪'
    assert store.learning_feedback()[0].initial_answer_value=='include'
    assert store.prediction_metrics().scored_votes==1
    assert store.undo_last_vote().id=='one'
    assert store.current_label('one') is None
    store.close()


def test_conflicting_study_configuration_does_not_leave_partial_new_metadata(tmp_path):
    path=tmp_path/'review.sqlite'
    storage=SQLiteReviewStorage(path)
    storage.bind_study({'study_seed':'original','rolling_audit_rate':'0.2','final_audit_rate':'0.1'})
    storage.db.execute("DELETE FROM study_metadata WHERE key='study_seed'")
    storage.db.commit()
    with pytest.raises(ValueError): storage.bind_study({'study_seed':'new','rolling_audit_rate':'0.9','final_audit_rate':'0.1'})
    assert storage.db.execute("SELECT 1 FROM study_metadata WHERE key='study_seed'").fetchone() is None
    storage.close()


def test_racing_votes_cannot_create_two_active_human_actions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path=tmp_path/'review.sqlite'
    with ReviewStore(path,study_seed='test') as store: store.import_articles([article()])
    barrier=Barrier(2)
    def vote(label):
        with ReviewStore(path,study_seed='test') as store:
            barrier.wait()
            try: store.record_vote('one',label);return True
            except ValueError: return False
    with ThreadPoolExecutor(max_workers=2) as pool: assert sorted(pool.map(vote,('include','exclude')))==[False,True]
    with ReviewStore(path,study_seed='test') as store: assert len(store.events_for('one'))==1
