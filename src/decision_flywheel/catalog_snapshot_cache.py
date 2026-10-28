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
        # Indexes of the immutable legacy blobs a catalog head may name, by digest: one per catalog, never stale.
        self._legacy = {}
        # Runs whose job this process holds a confirmed claim on, each with the newest record that showed it.
        self._holds = {}

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

    def remember_legacy(self, digest, legacy):
        with self._lock:
            self._legacy[digest] = legacy

    def legacy(self, digest):
        with self._lock:
            return self._legacy.get(digest)

    # Claim holds ----------------------------------------------------------------------------------------
    # While a worker holds a claim on a run's running job, nothing but that worker can change the run's own
    # rows (the catalog refuses other writers), so a read of those rows can use the newest record that showed
    # the claim instead of reading the head again. ``still_held(value, claim)`` decides from a record's value.
    def hold(self, journal, run_id, claim, record):
        with self._lock:
            self._holds[self._key(journal), run_id] = (claim, record)

    def held(self, journal, run_id):
        with self._lock:
            found = self._holds.get((self._key(journal), run_id))
            return None if found is None else found[1]

    def observe(self, journal, record, still_held):
        """Refresh every hold on this journal from a newer record, and drop each one the record no longer shows."""
        with self._lock:
            for key, (claim, held) in list(self._holds.items()):
                if key[0] != self._key(journal) or held.revision > record.revision:
                    continue
                if still_held(record.value, claim):
                    self._holds[key] = (claim, record)
                else:
                    del self._holds[key]

    def forget(self, journal):
        with self._lock:
            self._entries.pop(self._key(journal), None)

    def _serialized_bytes(self):
        return sum(record.reference.length for record in self._entries.values())

    def __len__(self):
        with self._lock:
            return len(self._entries)
