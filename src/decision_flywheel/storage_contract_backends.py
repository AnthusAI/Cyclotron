"""Every storage domain opened on every back end, for the shared behaviour specs.

SQLite lives in a temporary directory; S3 is the synthetic conditional-write
fake and the file journal uses a temporary directory, so no credentials,
network or cloud SDK are involved.
"""

from uuid import uuid4

from .batch_storage import StorageScope
from .file_journal import LocalFileJournal
from .s3_journal_test import ConditionalS3, journal
from .storage_payloads import FilePayloads

BACKEND_NAMES = ("sqlite", "s3", "file-journal")


def local_file_journal(directory, name, scope=None):
    payloads = FilePayloads(directory, scope or StorageScope("account-a", "workspace-a"), max_bytes=1000000)
    return LocalFileJournal(payloads, "shared-batches", name, max_bytes=1000000, max_commits=100)


class StorageBackend:
    """Opens and reopens one storage domain over the same underlying records."""

    def __init__(self, backend_name, directory):
        self.backend_name = backend_name
        self.directory = directory
        self.fake_s3 = ConditionalS3() if backend_name == "s3" else None
        self.opened_handles = []

    def journal_named(self, name):
        if self.backend_name == "s3":
            return journal(self.fake_s3, name=name)
        return local_file_journal(self.directory / "journals", name)

    def open(self):
        handle = self._open_handle()
        self.opened_handles.append(handle)
        return handle

    def reopen(self, handle):
        self.release(handle)
        return self.open()

    def release(self, handle):
        if handle in self.opened_handles:
            self.opened_handles.remove(handle)
        self._release_handle(handle)

    def release_everything(self):
        for handle in list(self.opened_handles):
            self.release(handle)

    def _open_handle(self):
        raise NotImplementedError

    def _release_handle(self, handle):
        handle.close()


class EmbeddedBackend(StorageBackend):
    def _open_handle(self):
        if self.backend_name == "sqlite":
            from .embedded_storage import SQLiteEmbeddedStorage

            return SQLiteEmbeddedStorage(self.directory / "embedded.sqlite3")
        from .s3_embedded_storage import S3EmbeddedStorage

        storage = S3EmbeddedStorage(self.journal_named("contract-embedded"), max_records=1000)
        storage.initialize()
        storage.acquire(str(uuid4()))
        return storage

    def _release_handle(self, handle):
        if self.backend_name != "sqlite":
            handle.release()
        else:
            handle.close()


class RuntimeBackend(StorageBackend):
    def _open_handle(self):
        if self.backend_name == "sqlite":
            from .runtime_storage import SQLiteRuntimeStateStorage

            return SQLiteRuntimeStateStorage(self.directory / "runtime.sqlite3")
        from .s3_runtime_storage import S3RuntimeStores

        stores = S3RuntimeStores(self.journal_named("contract-runtime"), max_records=1000)
        stores.initialize()
        stores.acquire(str(uuid4()))
        return stores.runtime_storage


class ReviewBackend(StorageBackend):
    def _open_handle(self):
        if self.backend_name == "sqlite":
            from .review_storage import SQLiteReviewStorage

            return SQLiteReviewStorage(self.directory / "review.sqlite3")
        from .s3_review_storage import S3ReviewStorage

        storage = S3ReviewStorage(self.journal_named("contract-review"), max_records=1000)
        storage.initialize()
        return storage


class EvaluationBackend(StorageBackend):
    def _open_handle(self):
        if self.backend_name == "sqlite":
            from .evaluation_storage import SQLiteEvaluationStorage

            return SQLiteEvaluationStorage(self.directory / "evaluation.sqlite3")
        from .s3_evaluation_storage import S3EvaluationStorage

        storage = S3EvaluationStorage(self.journal_named("contract-evaluation"), max_records=1000)
        storage.initialize()
        storage.acquire(str(uuid4()))
        return storage

    def _release_handle(self, handle):
        if self.backend_name != "sqlite":
            handle.close()


BATCH_BACKEND_NAMES = BACKEND_NAMES + ("s3+memory-lake", "s3+s3-lake", "file-journal+file-lake")


class BatchBackend(StorageBackend):
    """The ledger on every back end, and with raw replies in a record lake as well as embedded.

    A ``<journal>+<lake>`` name keeps replies in a lake of that kind beside the journal.
    """

    def __init__(self, backend_name, directory):
        journal_name, _, self.lake_name = backend_name.partition("+")
        super().__init__(journal_name, directory)
        self.memory_lake = {}

    def lake_for(self, journal):
        from .record_lake import FileRecordLake, MemoryRecordLake, S3RecordLake

        kind = dict(domain="shared-batches", kind="replies", max_record_bytes=100000)
        if self.lake_name == "memory-lake":
            return MemoryRecordLake(journal.boundary[0], store=self.memory_lake, **kind)
        if self.lake_name == "file-lake":
            return FileRecordLake(journal.payloads, **kind)
        return S3RecordLake(journal.payloads, **kind) if self.lake_name else None

    def _open_handle(self):
        if self.backend_name == "sqlite":
            from .batch_storage import SQLiteBatchStorage

            return SQLiteBatchStorage(self.directory / "batch.sqlite3")
        from .s3_batch_storage import S3BatchStorage

        journal = self.journal_named("contract-batch")
        storage = S3BatchStorage(journal, max_records=1000, replies=self.lake_for(journal))
        storage.initialize()
        return storage
