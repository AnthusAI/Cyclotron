"""Where catalog rows go once the head no longer needs them: one create-only record per key.

The console catalog head keeps only what can still change together: unfinished jobs and their claims and
actors, items waiting for a human, rows written by the newest commit, and small counters. Every other job,
item and item revision is a create-only record found by its key or by its position in its run or list, so a
commit rewrites only the small head and costs the same on the first decision and the thousandth.

A record is written only from state a head has already committed, before the commit that drops the row from
the head. A key therefore names exactly one content: repeating the write is harmless, and a reader that does
not find a row in the head it holds finds it here. Records live under ``<scope>/records/catalog/<kind>/<seq>``,
each kind named by a digest of the identity it stands for:

- ``jobs-<run>/<n>``, ``job-<job id>/1`` and ``request-<digest>/1``: one finished job (row, actor, claim and
  its position ``n`` in the run), found by position, by id, or by its run and request id.
- ``items-<run>/<n>``: which item holds position ``n`` in the run. ``item-<digest>/<slot>``: that item's row,
  once per time it left the head. A later slot is a later state; the newest slot is the item.
- ``revisions-<digest>/<n>`` and ``revision-<digest>/<revision>``: one item revision, by its position in the
  list and by its list, item id and revision.

A catalog written before this layout keeps its last whole-state blob as the ``legacy`` layer: its rows are
read from that immutable blob, and rows that changed since are found in the head or the records above.
"""

import json
from hashlib import sha256

from .journal import decode, encode
from .record_lake import SCHEMA as RECORD_SCHEMA
from .record_lake import FileRecordLake, RecordConflict, RecordCorrupt, S3RecordLake
from .storage_payloads import FilePayloads, PayloadRef, S3Payloads

DOMAIN = "catalog"
COLD_TABLES = ("web_jobs", "web_items", "list_item_revisions")
TERMINAL = ("completed", "failed", "interrupted")


def digest(*parts):
    return sha256(encode(list(parts))).hexdigest()


def hot_key(*parts):
    """The text key a head uses for a row in its small per-row maps."""
    return json.dumps(list(parts), separators=(",", ":"), ensure_ascii=False)


def row_key(table, row):
    if table == "web_jobs":
        return row["id"]
    if table == "web_items":
        return hot_key(row["run_id"], row["id"])
    return hot_key(row["list_id"], row["id"], row["revision"])


def partition(table, row):
    return row["list_id"] if table == "list_item_revisions" else row["run_id"]


def in_review(row):
    """An item shown to a human and not yet reviewed. It stays in the head: ``current_item`` reads only the head."""
    return row["prediction"] is not None and row["reviewed"] == 0


class ColdRows:
    """Create-only records keyed by identity, on the same store as the catalog journal."""

    def __init__(self, payloads):
        if not isinstance(payloads, (S3Payloads, FilePayloads)):
            raise ValueError("an S3 or file payload adapter is required for catalog records")
        self.payloads = payloads
        self._lakes = {}
        # Records never change once written, so what this handle has read stays true. Absences are never kept.
        self._seen = {}

    def _lake(self, kind):
        lake = self._lakes.get(kind)
        if lake is None:
            factory = S3RecordLake if isinstance(self.payloads, S3Payloads) else FileRecordLake
            lake = self._lakes[kind] = factory(self.payloads, DOMAIN, kind)
        return lake

    def get(self, kind, seq):
        """The record at ``seq`` of ``kind``, or None when there is none."""
        if (kind, seq) in self._seen:
            return self._seen[kind, seq]
        lake = self._lake(kind)
        content = self._fetch(lake, seq)
        if content is None:
            return None
        try:
            envelope = decode(content)
            if (
                set(envelope) != {"schema", "namespace", "seq", "value"}
                or envelope["schema"] != RECORD_SCHEMA
                or envelope["namespace"] != lake.namespace
                or envelope["seq"] != seq
            ):
                raise ValueError()
        except Exception:
            raise RecordCorrupt("catalog record envelope does not match its position") from None
        self._seen[kind, seq] = envelope["value"]
        return envelope["value"]

    @staticmethod
    def _fetch(lake, seq):
        """The stored bytes, or None when the key holds nothing.

        Lookups here ask about keys that may not exist yet (has this request been seen, does this item have a
        newer state), so they rely on S3 telling a missing key apart: the roles hold ListBucket on the books
        bucket, so a missing key is NoSuchKey. Anything else, AccessDenied included, raises: a lookup that cannot
        prove absence refuses rather than acting as if the row did not exist.
        """
        return lake._fetch(seq)

    def put(self, kind, seq, value):
        """Create the record. True when it is (now) this value; False when a different value already holds the key."""
        try:
            self._lake(kind).put(seq, value)
        except RecordConflict:
            return False
        self._seen[kind, seq] = decode(encode(value))
        return True

    # Keys ------------------------------------------------------------------
    @staticmethod
    def run_jobs(run_id):
        return "jobs-" + digest("jobs", run_id)

    @staticmethod
    def job(job_id):
        return "job-" + digest("job", job_id)

    @staticmethod
    def request(run_id, request_id):
        return "request-" + digest("request", run_id, request_id)

    @staticmethod
    def run_items(run_id):
        return "items-" + digest("items", run_id)

    @staticmethod
    def item(run_id, item_id):
        return "item-" + digest("item", run_id, item_id)

    @staticmethod
    def list_revisions(list_id):
        return "revisions-" + digest("revisions", list_id)

    @staticmethod
    def revision(list_id, item_id):
        return "revision-" + digest("revision", list_id, item_id)


class Legacy:
    """Read-only index over the whole-state blob a catalog had when it was first written in this layout."""

    def __init__(self, value):
        tables = value["snapshot"]["tables"]
        claims, actors = value.get("claims", {}), value.get("actors", {})
        self.jobs, self.requests, self.job_order, self.job_counts = {}, {}, {}, {}
        for row in tables["web_jobs"]:
            n = self.job_counts[row["run_id"]] = self.job_counts.get(row["run_id"], 0) + 1
            self.jobs[row["id"]] = {"row": row, "ordinal": n, "actor": actors.get(row["id"]), "claim": claims.get(row["id"])}
            self.requests[row["run_id"], row["request_id"]] = row["id"]
            self.job_order[row["run_id"], n] = row["id"]
        self.items, self.item_order, self.item_counts = {}, {}, {}
        for row in tables["web_items"]:
            n = self.item_counts[row["run_id"]] = self.item_counts.get(row["run_id"], 0) + 1
            self.items[row["run_id"], row["id"]] = {"row": row, "ordinal": n}
            self.item_order[row["run_id"], n] = row["id"]
        self.revisions, self.revision_order, self.revision_counts, self.latest = {}, {}, {}, {}
        for row in tables["list_item_revisions"]:
            n = self.revision_counts[row["list_id"]] = self.revision_counts.get(row["list_id"], 0) + 1
            self.revisions[row["list_id"], row["id"], row["revision"]] = {"row": row, "ordinal": n}
            self.revision_order[row["list_id"], n] = (row["list_id"], row["id"], row["revision"])
            key = (row["list_id"], row["id"])
            self.latest[key] = max(self.latest.get(key, 0), row["revision"])


def legacy_ref(payloads, held):
    return PayloadRef(payloads.scope, held["digest"], held["length"])


def load_legacy(payloads, held, upgrade):
    """The legacy index for a head's ``legacy`` reference: the immutable transaction blob, decoded and indexed."""
    transaction = decode(payloads.get(legacy_ref(payloads, held)))
    return Legacy(upgrade(transaction["value"]))


def is_evictable(table, row):
    if table == "web_jobs":
        return row["status"] in TERMINAL
    if table == "web_items":
        return not in_review(row)
    return True
