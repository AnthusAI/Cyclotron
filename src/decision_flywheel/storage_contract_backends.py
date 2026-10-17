"""Every storage domain opened on both back ends, for the shared behaviour specs.

SQLite lives in a temporary directory; S3 is the synthetic conditional-write
fake, so no credentials, network or cloud SDK are involved.
"""
from uuid import uuid4

from .s3_journal_test import ConditionalS3, journal

BACKEND_NAMES = ('sqlite', 's3')


class StorageBackend:
    """Opens and reopens one storage domain over the same underlying records."""

    def __init__(self, backend_name, directory):
        self.backend_name = backend_name
        self.directory = directory
        self.fake_s3 = ConditionalS3() if backend_name == 's3' else None
        self.opened_handles = []

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
        if self.backend_name == 'sqlite':
            from .embedded_storage import SQLiteEmbeddedStorage
            return SQLiteEmbeddedStorage(self.directory / 'embedded.sqlite3')
        from .s3_embedded_storage import S3EmbeddedStorage
        storage = S3EmbeddedStorage(journal(self.fake_s3, name='contract-embedded'), max_records=1000)
        storage.initialize()
        storage.acquire(str(uuid4()))
        return storage

    def _release_handle(self, handle):
        if self.backend_name == 's3':
            handle.release()
        else:
            handle.close()


class RuntimeBackend(StorageBackend):
    def _open_handle(self):
        if self.backend_name == 'sqlite':
            from .runtime_storage import SQLiteRuntimeStateStorage
            return SQLiteRuntimeStateStorage(self.directory / 'runtime.sqlite3')
        from .s3_runtime_storage import S3RuntimeStores
        stores = S3RuntimeStores(journal(self.fake_s3, name='contract-runtime'), max_records=1000)
        stores.initialize()
        stores.acquire(str(uuid4()))
        return stores.runtime_storage


class ReviewBackend(StorageBackend):
    def _open_handle(self):
        if self.backend_name == 'sqlite':
            from .review_storage import SQLiteReviewStorage
            return SQLiteReviewStorage(self.directory / 'review.sqlite3')
        from .s3_review_storage import S3ReviewStorage
        storage = S3ReviewStorage(journal(self.fake_s3, name='contract-review'), max_records=1000)
        storage.initialize()
        return storage


class EvaluationBackend(StorageBackend):
    def _open_handle(self):
        if self.backend_name == 'sqlite':
            from .evaluation_storage import SQLiteEvaluationStorage
            return SQLiteEvaluationStorage(self.directory / 'evaluation.sqlite3')
        from .s3_evaluation_storage import S3EvaluationStorage
        storage = S3EvaluationStorage(journal(self.fake_s3, name='contract-evaluation'), max_records=1000)
        storage.initialize()
        storage.acquire(str(uuid4()))
        return storage

    def _release_handle(self, handle):
        if self.backend_name == 's3':
            handle.close()


class BatchBackend(StorageBackend):
    def _open_handle(self):
        if self.backend_name == 'sqlite':
            from .batch_storage import SQLiteBatchStorage
            return SQLiteBatchStorage(self.directory / 'batch.sqlite3')
        from .s3_batch_storage import S3BatchStorage
        storage = S3BatchStorage(journal(self.fake_s3, name='contract-batch'), max_records=1000)
        storage.initialize()
        return storage
