"""Forward-evaluation storage keeps ownership/evidence distinct from policy."""
import sqlite3
import pytest
from .shadow_storage import SQLiteShadowStorage


def test_forward_scoring_identity_and_look_identity_survive_restart(tmp_path):
    path=tmp_path/'runtime.sqlite'
    db=sqlite3.connect(path)
    store=SQLiteShadowStorage(db)
    assert store.incumbent_ids()==set()
    store.ensure()
    store.join('b','rubric',3)
    store.join('a','rubric',3)
    assert [row[0] for row in store.active()]==['a','b']
    store.scored('a','item',4)
    store.scored('a','item',5)
    store.incumbent('inc','exact rubric',4)
    store.look(('a','inc',1,20,5,0,.02,1,4))
    with pytest.raises(sqlite3.IntegrityError): store.look(('a','inc',1,20,5,0,.02,1,4))
    db.close()
    db=sqlite3.connect(path)
    store=SQLiteShadowStorage(db)
    assert store.calls()==1 and store.looks_taken('a','inc')==1
    assert store.incumbent_ids()=={'inc'}
    store.set_status('a','evicted')
    assert store.statuses()=={'a':'evicted','b':'active'}
    db.close()
