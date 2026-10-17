"""Outcomes every storage adapter reports the same way, whatever its backend."""
from contextlib import contextmanager
import sqlite3


class StorageConflict(RuntimeError):
    """A competing writer changed the record first; the write did not happen."""


class StorageUncertain(RuntimeError):
    """The write may or may not have happened; the handle must be reopened and reconciled."""


class StorageLimit(ValueError):
    """A configured size or record budget would be exceeded; nothing was truncated."""


class StorageMissing(LookupError):
    """The requested record does not exist."""


@contextmanager
def refusing_duplicate_identity(message):
    """SQLite adapters report a repeated identity as the ValueError the S3 adapters raise."""
    try: yield
    except sqlite3.IntegrityError as error: raise ValueError(message) from error
