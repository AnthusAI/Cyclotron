"""Shared-request storage specifications, synthetic and cloud-free."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier
from uuid import uuid4

import pytest

from .batch_storage import BatchConflict, BatchInProgress, SQLiteBatchStorage


def test_reservation_is_idempotent_but_only_one_dispatch_claim_is_granted(tmp_path):
    store = SQLiteBatchStorage(tmp_path/'shared.sqlite')
    owner, fingerprint = str(uuid4()), 'a'*64
    first = store.reserve(fingerprint, 1, owner)
    assert store.reserve(fingerprint, 1, owner) == first and store.requests() == 1
    assert store.claim(fingerprint, owner) is True
    assert store.claim(fingerprint, owner) is False
    store.settle(fingerprint, owner, 'complete', '{"result": "α", "n": 9007199254740993}')
    assert store.reserve(fingerprint, 1, owner, policy='refresh').generation == 1
    assert store.requests() == 1
    store.close()


def test_pending_paid_work_is_not_stolen_and_stale_completion_is_fenced(tmp_path):
    store = SQLiteBatchStorage(tmp_path/'shared.sqlite')
    first, second, fingerprint = str(uuid4()), str(uuid4()), 'b'*64
    store.reserve(fingerprint, 2, first)
    assert store.claim(fingerprint, first)
    with pytest.raises(BatchInProgress):
        store.reserve(fingerprint, 2, second, retry_failed=True)
    # Operator-only reconciliation: old execution must already be stopped.
    store.settle(fingerprint, first, 'interrupted')
    assert store.reserve(fingerprint, 2, second, retry_failed=True).generation == 2
    assert store.claim(fingerprint, second)
    with pytest.raises(BatchConflict): store.settle(fingerprint, first, 'complete', '{}')
    store.settle(fingerprint, second, 'complete', '{}')
    store.close()


def test_two_connections_cannot_exceed_one_reserved_request(tmp_path):
    path = tmp_path/'shared.sqlite'
    barrier = Barrier(2)
    def reserve(index):
        barrier.wait()
        store = SQLiteBatchStorage(path)
        try:
            try:
                store.reserve(str(index)*64, 1, str(uuid4()))
                return True
            except RuntimeError: return False
        finally: store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, (1,2))) == [False,True]
    store = SQLiteBatchStorage(path)
    assert store.requests() == 1
    store.close()


def test_refresh_export_and_restore_preserve_payload_text_history_and_attempts(tmp_path):
    source = SQLiteBatchStorage(tmp_path/'source.sqlite')
    owner, second = str(uuid4()), str(uuid4())
    source.reserve('c'*64, 3, owner); source.claim('c'*64, owner)
    payload = '{ "exact": "雪", "large": 9007199254740993 }'
    source.settle('c'*64, owner, 'complete', payload)
    source.reserve('c'*64, 3, second, policy='refresh'); source.claim('c'*64, second)
    source.settle('c'*64, second, 'failed')
    snapshot = source.export()
    destination = SQLiteBatchStorage(tmp_path/'destination.sqlite')
    destination.restore(json.loads(json.dumps(snapshot)))
    assert destination.export() == snapshot and snapshot['history'][0][4] == payload
    with pytest.raises(BatchConflict): destination.restore(snapshot)
    source.close(); destination.close()


def test_legacy_sqlite_cache_opens_without_erasing_generations_or_raw_results(tmp_path):
    path = tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE batches(key TEXT PRIMARY KEY,status TEXT,payload TEXT); CREATE TABLE attempts(id INTEGER PRIMARY KEY,key TEXT); CREATE TABLE batch_history(id INTEGER PRIMARY KEY,key TEXT,generation INTEGER,status TEXT,payload TEXT);')
        db.execute('INSERT INTO batches VALUES (?,?,?)',('d'*64,'complete','{ "old": true }'))
        db.execute('INSERT INTO attempts(key) VALUES (?)',('d'*64,))
    store = SQLiteBatchStorage(path)
    record = store.get('d'*64)
    assert record.payload == '{ "old": true }' and record.generation == store.requests() == 1
    store.close()


def test_discovery_is_bounded_and_separate_workspace_files_do_not_share_authority(tmp_path):
    store = SQLiteBatchStorage(tmp_path/'one.sqlite')
    for fingerprint in ('1'*64,'2'*64,'3'*64): store.reserve(fingerprint, 3, str(uuid4()))
    page, cursor = store.page(limit=2)
    assert [row.fingerprint for row in page] == ['1'*64,'2'*64]
    page, cursor = store.page(after=cursor,limit=2)
    assert [row.fingerprint for row in page] == ['3'*64] and cursor is None
    other = SQLiteBatchStorage(tmp_path/'two.sqlite')
    assert other.requests() == 0 and other.get('1'*64) is None
    with pytest.raises(ValueError): store.page(limit=0)
    store.close(); other.close()


def test_old_operation_replay_returns_its_original_generation_without_new_attempt(tmp_path):
    store = SQLiteBatchStorage(tmp_path/'shared.sqlite')
    first,second = str(uuid4()),str(uuid4())
    store.reserve('e'*64,3,first); store.claim('e'*64,first)
    store.settle('e'*64,first,'complete','{"version": 1}')
    store.reserve('e'*64,3,second,policy='refresh'); store.claim('e'*64,second)
    store.settle('e'*64,second,'complete','{"version": 2}')
    original = store.reserve('e'*64,3,first,policy='refresh')
    assert original.generation==1 and original.payload=='{"version": 1}' and store.requests()==2
    assert store.get('e'*64).generation==2
    with pytest.raises(BatchConflict): store.reserve('f'*64,3,first)
    store.close()


def test_invalid_migration_rejects_before_any_destination_write(tmp_path):
    source = SQLiteBatchStorage(tmp_path/'source.sqlite')
    source.reserve('f'*64,1,str(uuid4()))
    snapshot = source.export()
    snapshot['batches'][0][3] = 3
    destination = SQLiteBatchStorage(tmp_path/'destination.sqlite')
    with pytest.raises(ValueError): destination.restore(snapshot)
    assert destination.requests()==0 and destination.export()['batches']==[]
    with pytest.raises(ValueError): source.export(max_records=1)
    source.close(); destination.close()


def test_import_rejects_reused_operation_ids_before_changing_destination(tmp_path):
    source = SQLiteBatchStorage(tmp_path/'source.sqlite')
    for fingerprint in ('a'*64,'b'*64): source.reserve(fingerprint,2,str(uuid4()))
    snapshot = source.export()
    snapshot['attempts'][1][2] = snapshot['attempts'][0][2]
    snapshot['batches'][1][4] = snapshot['attempts'][0][2]
    destination = SQLiteBatchStorage(tmp_path/'destination.sqlite')
    with pytest.raises(ValueError): destination.restore(snapshot)
    assert destination.requests()==0
    source.close(); destination.close()


def test_unclaimed_reservation_cannot_be_published_as_provider_completion(tmp_path):
    store = SQLiteBatchStorage(tmp_path/'shared.sqlite')
    owner = str(uuid4())
    store.reserve('a'*64,1,owner)
    with pytest.raises(BatchConflict): store.settle('a'*64,owner,'complete','{}')
    store.close()


def test_restart_preserves_claim_and_separate_processes_cannot_dispatch_it_twice(tmp_path):
    import os
    import subprocess
    import sys
    path = tmp_path/'restart.sqlite'
    owner,fingerprint = str(uuid4()),'a'*64
    store = SQLiteBatchStorage(path)
    store.reserve(fingerprint,1,owner)
    store.close()
    script = ('from decision_flywheel.batch_storage import SQLiteBatchStorage; import sys; '
              'store=SQLiteBatchStorage(sys.argv[1]); '
              'print(int(store.claim(sys.argv[2],sys.argv[3]))); store.close()')
    processes = [subprocess.Popen([sys.executable,'-c',script,str(path),fingerprint,owner],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy()) for _ in range(2)]
    results = [process.communicate(timeout=10) for process in processes]
    assert all(process.returncode==0 for process in processes),results
    assert sorted(result[0].strip() for result in results)==['0','1']
    restarted = SQLiteBatchStorage(path)
    assert restarted.requests()==1 and restarted.get(fingerprint).claimed
    with pytest.raises(BatchInProgress): restarted.reserve(fingerprint,2,str(uuid4()),retry_failed=True)
    restarted.close()


def test_concurrent_refresh_never_pairs_old_payload_with_new_generation(tmp_path):
    from threading import Event
    path = tmp_path/'shared.sqlite'
    reader,writer = SQLiteBatchStorage(path),SQLiteBatchStorage(path)
    fingerprint = 'a'*64
    done = Event()
    def refresh():
        try:
            for generation in range(1,101):
                owner = str(uuid4())
                writer.reserve(fingerprint,100,owner,policy='refresh')
                writer.claim(fingerprint,owner)
                writer.settle(fingerprint,owner,'complete',json.dumps({'generation':generation}))
        finally: done.set()
    observations = 0
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(refresh)
        while not done.is_set():
            record = reader.get(fingerprint)
            if record and record.status=='complete':
                assert json.loads(record.payload)['generation']==record.generation
                observations += 1
        future.result(timeout=10)
    assert reader.get(fingerprint).generation==100 and observations>0
    reader.close(); writer.close()


def test_default_shared_storage_opens_and_restarts_with_aws_imports_and_network_forbidden(tmp_path):
    import os
    import subprocess
    import sys
    script = '''
import builtins, socket, sys
original_import = builtins.__import__
def isolated_import(name, *args, **kwargs):
    if name.split('.')[0] in ('boto3','botocore'):
        raise AssertionError('default storage imported AWS SDK')
    return original_import(name, *args, **kwargs)
builtins.__import__ = isolated_import
def forbidden(*args, **kwargs): raise AssertionError('default storage opened network')
socket.socket.connect = forbidden
socket.create_connection = forbidden
from decision_flywheel.shared_decisions import SharedDecisions
store = SharedDecisions(sys.argv[1],None,max_requests=1,observer=lambda event:None)
assert store.requests == 0
store.close()
store = SharedDecisions(sys.argv[1],None,max_requests=1,observer=lambda event:None)
assert store.requests == 0
store.close()
'''
    result = subprocess.run([sys.executable,'-c',script,str(tmp_path/'local.sqlite')],
        env=os.environ.copy(),capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
