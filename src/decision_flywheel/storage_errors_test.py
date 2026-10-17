"""One exception family and one release contract for every storage adapter."""
from uuid import uuid4
import pytest
from .s3_journal_test import ConditionalS3,journal
from .embedded_cyclotron_test import DEFINITION,Model


def open_stores(client):
    from .s3_embedded_storage import S3CyclotronStores
    return S3CyclotronStores(journal(client,name='root'),max_records=10000,
        journal_for=lambda domain,name:journal(client,name=domain+'-'+name))


def test_a_constructor_failure_while_opening_leaves_no_owned_runtime(monkeypatch):
    from .embedded_cyclotron import Cyclotron
    client=ConditionalS3()
    def failing_constructor(*arguments,**options):raise RuntimeError('synthetic constructor failure')
    monkeypatch.setattr('decision_flywheel.embedded_cyclotron.DecisionFlywheel',failing_constructor)
    with pytest.raises(RuntimeError,match='synthetic constructor failure'):
        Cyclotron.open(None,DEFINITION,Model(),storage=open_stores(client))
    monkeypatch.undo()
    with Cyclotron.open(None,DEFINITION,Model(),storage=open_stores(client)) as reopened:
        assert set(reopened.wheels)=={spec.id for spec in DEFINITION.classifiers}


def test_releasing_the_composition_closes_every_runtime_it_opened():
    from .s3_runtime_storage import RuntimeOwnershipError
    client=ConditionalS3();stores=open_stores(client);stores.acquire()
    runtime=stores.runtime_for_classifier('relevant')
    stores.release()
    assert stores.runtimes=={} and runtime.authority is None
    again=open_stores(client);again.acquire();again.runtime_for_classifier('relevant')
    with pytest.raises(RuntimeOwnershipError):open_stores(client).acquire()
    again.release()


def embedded_adapter(client):
    from .s3_embedded_storage import S3EmbeddedStorage
    root=S3EmbeddedStorage(journal(client,name='contract-embedded'),max_records=1000)
    root.initialize();root.acquire(str(uuid4()))
    return root,lambda number:root.put_meta('key',str(number))

def runtime_adapter(client):
    from .s3_runtime_storage import S3RuntimeStores
    runtime=S3RuntimeStores(journal(client,name='contract-runtime'),max_records=1000)
    runtime.initialize();runtime.acquire(str(uuid4()))
    return runtime,lambda number:runtime.runtime_storage.put_state('key',str(number))

def review_adapter(client):
    from .s3_review_storage import S3ReviewStorage
    review=S3ReviewStorage(journal(client,name='contract-review'),max_records=1000);review.initialize()
    return review,lambda number:review.bind_study({f'key-{number}':str(number)})

def console_adapter(client):
    from .s3_console_storage import S3ConsoleStorage
    console=S3ConsoleStorage(journal(client,name='contract-console'),max_records=1000);console.initialize()
    return console,lambda number:console.save_item_list(f'list-{number}','List')

ADAPTERS=[embedded_adapter,runtime_adapter,review_adapter,console_adapter]


@pytest.mark.parametrize('adapter',ADAPTERS)
def test_an_uncertain_write_raises_the_shared_type_and_refuses_the_handle_afterwards(adapter):
    from .storage_errors import StorageUncertain
    client=ConditionalS3();storage,write=adapter(client)
    head=storage.journal.head_key
    client.lose=lambda key:key==head
    with pytest.raises(StorageUncertain):write(1)
    with pytest.raises(RuntimeError):write(2)


@pytest.mark.parametrize('adapter',[embedded_adapter,review_adapter,console_adapter])
def test_a_conflicting_transaction_raises_the_shared_type(adapter):
    from .storage_errors import StorageConflict
    client=ConditionalS3();storage,write=adapter(client)
    competitor=journal(client,name=storage.journal.namespace.rsplit('/',1)[1])
    def commit_competing_change(request):
        record=competitor.read();competitor.commit(record,{**record.value})
    with pytest.raises(StorageConflict):
        with storage.transaction():
            write(1)
            client.before=commit_competing_change


def test_the_journal_outcomes_are_members_of_the_shared_family():
    from .s3_journal import JournalConflict,JournalUncertain,JournalLimit
    from .storage_errors import StorageConflict,StorageUncertain,StorageLimit
    assert issubclass(JournalConflict,StorageConflict)
    assert issubclass(JournalUncertain,StorageUncertain)
    assert issubclass(JournalLimit,StorageLimit) and issubclass(StorageLimit,ValueError)


def test_a_missing_decision_fingerprint_raises_storage_missing_in_both_adapters(tmp_path):
    from .embedded_storage import SQLiteEmbeddedStorage
    from .storage_errors import StorageMissing
    sqlite_storage=SQLiteEmbeddedStorage(tmp_path/'missing.sqlite3')
    s3_storage,_=embedded_adapter(ConditionalS3())
    for storage in (sqlite_storage,s3_storage):
        with pytest.raises(StorageMissing):storage.decision_fingerprint('absent')


def test_an_exceeded_record_budget_raises_storage_limit():
    from .s3_embedded_storage import S3EmbeddedStorage
    from .storage_errors import StorageLimit
    root=S3EmbeddedStorage(journal(name='budget'),max_records=1);root.initialize();root.acquire(str(uuid4()))
    with pytest.raises(StorageLimit):
        with root.transaction():
            root.put_meta('first','1');root.put_meta('second','2')
