"""Hypothesis algorithms consume evidence records without SQL or lost errors."""
import sqlite3
import pytest
from .hypothesis_storage import SQLiteHypothesisStorage
from .hypothesis_ledger import HypothesisLedger
from .learning_storage import SQLiteControlStorage


def test_equal_time_evaluations_and_append_verdict_order_retain_native_precedence(tmp_path):
    db=sqlite3.connect(tmp_path/'runtime.sqlite')
    storage=SQLiteHypothesisStorage(db)
    ledger=HypothesisLedger(storage=storage,clock=lambda:'same timestamp')
    ledger.record_evaluations('idea',[{'item_id':'one','correct':True}],'v1')
    ledger.record_evaluations('idea',[{'item_id':'one','correct':False}],'v2')
    ledger.record_verdict('idea','advanced','original rationale',{'note':'雪'})
    ledger.record_verdict('idea','retired','later rationale')
    ledger.db=object()  # legacy SQL access on the algorithm would fail.
    assert ledger.correctness_vector('idea',['one'])==[False]
    assert ledger._latest('idea',context_version='v1')=={'one':True}
    assert [row['reason'] for row in ledger.verdicts('idea')]==['original rationale','later rationale']
    assert ledger.running_score('idea')['n']==2
    assert ledger.running_score('idea','same timestamp',strict=True)['n']==0
    db.close()


def test_optional_control_table_absence_is_distinct_from_storage_failure(tmp_path):
    db=sqlite3.connect(tmp_path/'runtime.sqlite')
    ledger=HypothesisLedger(storage=SQLiteHypothesisStorage(db))
    assert ledger.lineage('missing')['parents']==[]
    assert ledger.operator_outcomes()=={}
    controls=SQLiteControlStorage(db)
    controls.save('idea','{"id":"idea","operator":"revise","parents":["ancestor"]}')
    assert ledger.lineage('idea')['parents']==['ancestor']
    db.close()
    with pytest.raises(sqlite3.ProgrammingError): ledger.lineage('idea')
