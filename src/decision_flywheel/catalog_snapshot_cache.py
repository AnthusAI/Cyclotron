"""A small, bounded, per-process memory of decoded and validated catalog snapshots.

Journal transactions are content-addressed and immutable, so a snapshot can be reused for exactly as long as
the live head still names it. The journal revalidates with its cheap head read on every use; this memory only
spares the payload read, the JSON decode and the full validation. Cached values are shared and must be
treated as read-only: every write path copies before it changes anything.
"""

from collections import OrderedDict
from threading import Lock

DEFAULT_MAXIMUM_ENTRIES = 4
DEFAULT_MAXIMUM_SERIALIZED_BYTES = 32 * 1024 * 1024


class CatalogSnapshotCache:
    def __init__(self, *, maximum_entries=DEFAULT_MAXIMUM_ENTRIES, maximum_serialized_bytes=DEFAULT_MAXIMUM_SERIALIZED_BYTES):
        if type(maximum_entries) is not int or maximum_entries < 1:
            raise ValueError("positive entry bound required")
        if type(maximum_serialized_bytes) is not int or maximum_serialized_bytes < 1:
            raise ValueError("positive byte bound required")
        self.maximum_entries = maximum_entries
        self.maximum_serialized_bytes = maximum_serialized_bytes
        self._entries = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def _key(journal):
        return (getattr(journal, "boundary", None), journal.namespace)

    def get(self, journal):
        with self._lock:
            record = self._entries.get(self._key(journal))
            if record is not None:
                self._entries.move_to_end(self._key(journal))
            return record

    def put(self, journal, record):
        if record.reference.length > self.maximum_serialized_bytes:
            return
        with self._lock:
            self._entries[self._key(journal)] = record
            self._entries.move_to_end(self._key(journal))
            while len(self._entries) > self.maximum_entries or self._serialized_bytes() > self.maximum_serialized_bytes:
                self._entries.popitem(last=False)

    def forget(self, journal):
        with self._lock:
            self._entries.pop(self._key(journal), None)

    def _serialized_bytes(self):
        return sum(record.reference.length for record in self._entries.values())

    def __len__(self):
        with self._lock:
            return len(self._entries)
