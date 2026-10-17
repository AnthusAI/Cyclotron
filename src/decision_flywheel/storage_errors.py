"""Outcomes every storage adapter reports the same way, whatever its backend."""


class StorageConflict(RuntimeError):
    """A competing writer changed the record first; the write did not happen."""


class StorageUncertain(RuntimeError):
    """The write may or may not have happened; the handle must be reopened and reconciled."""


class StorageLimit(ValueError):
    """A configured size or record budget would be exceeded; nothing was truncated."""


class StorageMissing(LookupError):
    """The requested record does not exist."""
