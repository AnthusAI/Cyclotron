"""Outcomes every storage adapter reports the same way, whatever its backend."""

import random
import sqlite3
import time
from contextlib import contextmanager

CONFLICT_WAIT_SECONDS = 0.05
LONGEST_CONFLICT_WAIT_SECONDS = 2.0


class StorageConflict(RuntimeError):
    """A competing writer changed the record first; the write did not happen."""


class StorageUncertain(RuntimeError):
    """The write may or may not have happened; the handle must be reopened and reconciled."""


class CommandNotReady(RuntimeError):
    """A queued command is not at the head of its run's queue yet, or the run is still busy; nothing was claimed."""


class StorageLimit(ValueError):
    """A configured size or record budget would be exceeded; nothing was truncated."""


class StorageMissing(LookupError):
    """The requested record does not exist."""


def conflict_attempts(*, sleep=None, jitter=random.random):
    """Tries of one conditional write, for as long as it takes.

    A lost write means another writer's landed, so the catalog is always moving and a retry can always land;
    running out of tries would turn ordinary traffic into a failed job. Each loser waits a growing, random
    moment so a burst of writers spreads out instead of colliding again.
    """
    wait = CONFLICT_WAIT_SECONDS
    yield
    while True:
        (sleep or time.sleep)(wait * jitter())
        wait = min(wait * 2, LONGEST_CONFLICT_WAIT_SECONDS)
        yield


@contextmanager
def refusing_duplicate_identity(message):
    """SQLite adapters report a repeated identity as the ValueError the S3 adapters raise."""
    try:
        yield
    except sqlite3.IntegrityError as error:
        raise ValueError(message) from error
