"""Write-once record lake: history as small immutable files, beside a small control journal.

A lake is one ordered collection of records for a scope, a domain and a kind.
Each record is a separate create-only object, never mutated or deleted by the
application, so history is no longer rewritten with every commit. Only small
control state keeps the compare-and-set journal (journal.py).

Ordering. A record's position is a sequence number, allocated as the next number
after the lake's coverage. Coverage is two plain fields kept inside the journal's
control state: ``last`` (the highest sequence the state covers) and ``skipped``
(sequences at or below ``last`` that hold abandoned writes). A reader trusts a
record only if the state it holds covers it, so a write that never reached the
state is invisible. Create-only writes make two writers racing for one sequence
safe: the loser sees a different record there, skips it and takes the next one.

Commit protocol (``commit_with_records``). Write the new records first (harmless
if abandoned), then commit one small journal change whose coverage names them.
That change is the only moment they become real, and it is all-or-nothing.

Covered reads use plain GETs by sequence, so they need no list permission.
Listing (``sequences``, ``orphans``) is for audit and a later janitor.
"""

import copy
import os
import re
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, runtime_checkable

from .batch_storage import StorageScope
from .journal import commit_with_contract, decode, encode
from .s3_transient import cause_name, error_code, log, read_with_one_retry
from .storage_errors import StorageConflict, StorageLimit, StorageUncertain
from .storage_payloads import FilePayloads, S3Payloads

SCHEMA = "cyclotron-record/v1"
SEQUENCE_WIDTH = 12
# The widest sequence the fixed-width record filename can spell. A filename format, not a budget.
MAX_SEQUENCE = 10**SEQUENCE_WIDTH - 1
RECORDS_KEY = "records"
_NAME = re.compile(r"[a-zA-Z0-9_-]+")
_FILE = re.compile(rf"(\d{{{SEQUENCE_WIDTH}}})\.json")
_EXISTS = ("PreconditionFailed", "ConditionalRequestConflict", "412", "409")


class RecordConflict(StorageConflict):
    """A different record already holds that sequence. Nothing was overwritten."""


class RecordLimit(StorageLimit):
    """A sequence falls outside the fixed-width record filename format. Nothing was written."""


class RecordUncertain(StorageUncertain):
    """The create may or may not have happened. Repeating the identical record is safe."""


class RecordUnavailable(RuntimeError):
    """A record could not be read. ``cause`` is the underlying exception class name only."""

    def __init__(self, message, cause=None):
        super().__init__(message + (f" (cause: {cause})" if cause else ""))
        self.cause = cause


class RecordCorrupt(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordRef:
    """Names one record by position and digest, for control state that wants to cite it."""

    domain: str
    kind: str
    seq: int
    digest: str
    length: int

    def to_json(self):
        return {"domain": self.domain, "kind": self.kind, "seq": self.seq, "digest": self.digest, "length": self.length}


@dataclass(frozen=True)
class Coverage:
    """Which sequences the control state vouches for: 1..last, except ``skipped``."""

    last: int = 0
    skipped: tuple = ()

    def __post_init__(self):
        skipped = tuple(self.skipped)
        object.__setattr__(self, "skipped", skipped)
        if (
            type(self.last) is not int
            or not 0 <= self.last <= MAX_SEQUENCE
            or any(type(s) is not int or not 1 <= s < self.last for s in skipped)
            or list(skipped) != sorted(set(skipped))
        ):
            raise ValueError("valid record coverage required")

    def covers(self, seq):
        return type(seq) is int and 1 <= seq <= self.last and seq not in self.skipped

    def sequences(self, after=0, limit=None):
        found = []
        for seq in range(max(after, 0) + 1, self.last + 1):
            if limit is not None and len(found) >= limit:
                break
            if seq not in self.skipped:
                found.append(seq)
        return found

    def to_json(self):
        return {"last": self.last, "skipped": list(self.skipped)}

    @classmethod
    def from_json(cls, value):
        if not isinstance(value, dict) or set(value) != {"last", "skipped"} or not isinstance(value["skipped"], list):
            raise ValueError("valid record coverage required")
        return cls(value["last"], tuple(value["skipped"]))


def coverage_in(state, lake):
    """The coverage a control state holds for a lake; an empty one if it has none yet."""
    held = (state.get(RECORDS_KEY) or {}).get(lake.name)
    return Coverage() if held is None else Coverage.from_json(held)


def with_coverage(state, lake, coverage):
    """A copy of ``state`` whose coverage for the lake is ``coverage``."""
    changed = dict(state)
    changed[RECORDS_KEY] = {**(state.get(RECORDS_KEY) or {}), lake.name: coverage.to_json()}
    return changed


@runtime_checkable
class Records(Protocol):
    scope: StorageScope
    domain: str
    kind: str
    name: str

    def put(self, seq, value) -> RecordRef: ...
    def read(self, seq): ...
    def sequences(self, after=0, limit=None) -> list: ...
    def append(self, coverage, values): ...
    def read_covered(self, coverage, after=0, limit=None) -> list: ...
    def orphans(self, coverage) -> list: ...


class RecordLake:
    """Shared record logic. A back end supplies only ``_create``, ``_fetch`` and ``_list``."""

    def __init__(self, scope, domain, kind):
        if not isinstance(scope, StorageScope) or any(not isinstance(v, str) or not _NAME.fullmatch(v) for v in (domain, kind)):
            raise ValueError("fixed scope, domain and kind required")
        self.scope, self.domain, self.kind = scope, domain, kind
        self.name = f"{domain}/{kind}"
        self.namespace = f"{scope.key}/records/{domain}/{kind}"

    # Back-end primitives -------------------------------------------------
    def _create(self, seq, content):
        """Create-only write. True if created, False if the sequence already exists."""
        raise NotImplementedError

    def _fetch(self, seq):
        """The stored bytes, or None if absent."""
        raise NotImplementedError

    def _list(self, after, limit):
        """Stored sequences above ``after`` in ascending order, at most ``limit``."""
        raise NotImplementedError

    # Records ---------------------------------------------------------------
    def _encode(self, seq, value):
        if type(seq) is not int or not 1 <= seq <= MAX_SEQUENCE:
            raise RecordLimit("record sequence outside the record filename format")
        return encode({"schema": SCHEMA, "namespace": self.namespace, "seq": seq, "value": value})

    def _ref(self, seq, content):
        return RecordRef(self.domain, self.kind, seq, sha256(content).hexdigest(), len(content))

    def put(self, seq, value):
        """Create the record at ``seq``. Repeating the identical record succeeds; a different one conflicts."""
        content = self._encode(seq, value)
        if not self._create(seq, content):
            existing = self._fetch(seq)
            if existing != content:
                raise RecordConflict("a different record already holds this sequence")
        return self._ref(seq, content)

    def encoded_size(self, seq, value):
        """Bytes the record would occupy at ``seq``."""
        return len(self._encode(seq, value))

    def stored_ref(self, seq):
        """The reference of the bytes actually stored at ``seq`` (digest and length), for verification."""
        content = self._fetch(seq)
        if content is None:
            raise RecordUnavailable("record missing; never use an index as fallback")
        return self._ref(seq, content)

    def read(self, seq):
        content = self._fetch(seq)
        if content is None:
            raise RecordUnavailable("record missing; never use an index as fallback")
        try:
            envelope = decode(content)
            if (
                set(envelope) != {"schema", "namespace", "seq", "value"}
                or envelope["schema"] != SCHEMA
                or envelope["namespace"] != self.namespace
                or envelope["seq"] != seq
            ):
                raise ValueError()
            return envelope["value"]
        except Exception:
            raise RecordCorrupt("record envelope does not match its position") from None

    def sequences(self, after=0, limit=None):
        """Every stored sequence above ``after``, ascending, orphans included. Needs list access."""
        return self._list(max(after, 0), limit)

    def append(self, coverage, values):
        """Write ``values`` after ``coverage`` and return the coverage that would include them, plus their refs.

        Nothing is visible to readers until a journal commit stores the returned coverage.
        """
        values = list(values)
        for value in values:
            self._encode(MAX_SEQUENCE, value)  # prove everything is canonical JSON before writing anything
        seq, skipped, refs = coverage.last, list(coverage.skipped), []
        for value in values:
            while True:
                seq += 1
                try:
                    refs.append(self.put(seq, value))
                    break
                except RecordConflict:
                    # An abandoned or competing write holds this sequence. Skip it, take the next; the control state will say so.
                    skipped.append(seq)
        return Coverage(seq, tuple(skipped)), refs

    def read_covered(self, coverage, after=0, limit=None):
        """(seq, value) for covered records above ``after`` in order, by GET only."""
        return [(seq, self.read(seq)) for seq in coverage.sequences(after, limit)]

    def orphans(self, coverage):
        """Stored sequences the coverage does not vouch for. Needs list access."""
        return [seq for seq in self.sequences() if not coverage.covers(seq)]


class MemoryRecordLake(RecordLake):
    """In-process lake for tests. Pass the same ``store`` dict to model reopening."""

    def __init__(self, scope, domain, kind, *, store=None):
        super().__init__(scope, domain, kind)
        self.store = {} if store is None else store

    def _create(self, seq, content):
        return self.store.setdefault((self.namespace, seq), content) is content

    def _fetch(self, seq):
        return self.store.get((self.namespace, seq))

    def _list(self, after, limit):
        found = sorted(s for (n, s) in self.store if n == self.namespace and s > after)
        return found if limit is None else found[:limit]


class FileRecordLake(RecordLake):
    """Trusted local directory, byte-identical in layout to the S3 keys under the payload root."""

    def __init__(self, payloads, domain, kind):
        if not isinstance(payloads, FilePayloads):
            raise ValueError("injected scoped file payload adapter required")
        super().__init__(payloads.scope, domain, kind)
        self.directory = payloads.root / self.namespace

    def _path(self, seq):
        return self.directory / f"{seq:0{SEQUENCE_WIDTH}d}.json"

    def _create(self, seq, content):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            # Fully write and fsync, then link: create-only, and never a partly written record.
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix="tmp-", delete=False) as temporary:
                name = temporary.name
                try:
                    temporary.write(content)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    os.link(name, self._path(seq))
                except FileExistsError:
                    return False
                finally:
                    os.unlink(name)
            return True
        except OSError:
            raise RecordUncertain("file record write outcome uncertain; repeat the identical record") from None

    def _fetch(self, seq):
        try:
            with open(self._path(seq), "rb") as stream:
                return stream.read()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RecordUnavailable("record unavailable", cause_name(error)) from None

    def _list(self, after, limit):
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return []
        except OSError as error:
            raise RecordUnavailable("record listing unavailable", cause_name(error)) from None
        found = sorted(int(m.group(1)) for m in map(_FILE.fullmatch, names) if m and int(m.group(1)) > after)
        return found if limit is None else found[:limit]


class S3RecordLake(RecordLake):
    """Create-only S3 objects at ``<account>/<workspace>/records/<domain>/<kind>/<seq>.json``.

    Uses the injected client, bucket and owner of an S3Payloads adapter. ``If-None-Match: *`` is the same
    guarantee the payload store relies on; the bucket policy enforces it for the whole ``records`` prefix.
    """

    def __init__(self, payloads, domain, kind):
        if not isinstance(payloads, S3Payloads):
            raise ValueError("injected scoped S3 payload adapter required")
        # As for S3Journal: a hidden SDK retry could turn a lost successful PUT into a later 412.
        meta = getattr(payloads.client, "meta", None)
        if meta is not None and getattr(meta, "config", None) is not None:
            if (meta.config.retries or {}).get("total_max_attempts") != 1:
                raise ValueError("conditional S3 client requires total_max_attempts=1")
        super().__init__(payloads.scope, domain, kind)
        self.client, self.bucket, self.owner = payloads.client, payloads.bucket, payloads.owner

    def _key(self, seq):
        return f"{self.namespace}/{seq:0{SEQUENCE_WIDTH}d}.json"

    def _create(self, seq, content):
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(seq),
                Body=content,
                ExpectedBucketOwner=self.owner,
                IfNoneMatch="*",
                ContentType="application/json",
            )
            return True
        except Exception as error:
            if error_code(error) in _EXISTS:
                return False
            raise RecordUncertain("S3 record write outcome uncertain; repeat the identical record") from None

    def _get(self, seq):
        stream = None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(seq), ExpectedBucketOwner=self.owner)
            stream = response["Body"]
            length = response["ContentLength"]
            if type(length) is not int or length < 1:
                raise ValueError("invalid record object")
            content = stream.read(length + 1)
            if len(content) != length:
                raise ValueError("incomplete record object")
            return content
        finally:
            if stream is not None:
                stream.close()

    def _fetch(self, seq):
        try:
            # A GET is idempotent, so one transient failure is retried. A missing key is not transient.
            return read_with_one_retry(lambda: self._get(seq), "record")
        except Exception as error:
            if error_code(error) == "NoSuchKey":
                return None
            log.warning("record unavailable: %s", cause_name(error))
            raise RecordUnavailable("record unavailable", cause_name(error)) from None

    def _list(self, after, limit):
        found, token = [], None
        try:
            while limit is None or len(found) < limit:
                request = {"Bucket": self.bucket, "Prefix": self.namespace + "/", "ExpectedBucketOwner": self.owner}
                if after:
                    request["StartAfter"] = self._key(after)
                if token:
                    request["ContinuationToken"] = token
                page = read_with_one_retry(lambda request=request: self.client.list_objects_v2(**request), "record listing")
                for item in page.get("Contents", []):
                    match = _FILE.fullmatch(item["Key"].rsplit("/", 1)[-1])
                    if match and int(match.group(1)) > after:
                        found.append(int(match.group(1)))
                token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
                if not token:
                    break
        except Exception as error:
            log.warning("record listing unavailable: %s", cause_name(error))
            raise RecordUnavailable("record listing unavailable", cause_name(error)) from None
        found.sort()
        return found if limit is None else found[:limit]


def commit_with_records(journal, expected, writes, change=None, *, invalidate=None, invalidate_on_conflict=False, check=None):
    """Write records first, then flip the head: one all-or-nothing logical change.

    ``writes`` is a sequence of ``(lake, values)``, at most one entry per lake. Each lake's new records are
    created, then ONE journal commit stores the updated coverage together with whatever ``change(state, refs)``
    edits in place (``refs`` maps ``lake.name`` to that lake's ``RecordRef`` list). If anything fails before or
    during that commit the records stay in the lake as orphans that no state covers, so nobody can read them.
    Returns ``(JournalRecord, refs)``. Conflict and uncertain outcomes surface as the shared storage
    exceptions, exactly as ``commit_with_contract`` reports them; retrying a conflict stays the caller's call.

    ``check(refs)``, when given, runs after every page is written and before the head moves. Raising from it
    abandons the commit: the pages stay as orphans and the control state is untouched.
    """
    if not isinstance(expected.value, dict):
        raise ValueError("record coverage lives in a control state that is a JSON object")
    scope = journal.boundary[0]
    state, refs = copy.deepcopy(expected.value), {}
    for lake, values in writes:
        if lake.scope != scope:
            raise PermissionError("record lake scope refused")
        if lake.name in refs:
            raise ValueError("one batch of records per lake in a commit")
        coverage, refs[lake.name] = lake.append(coverage_in(state, lake), values)
        state = with_coverage(state, lake, coverage)
    if check is not None:
        check(refs)
    if change is not None:
        change(state, refs)
    record = commit_with_contract(journal, expected, state, invalidate=invalidate, invalidate_on_conflict=invalidate_on_conflict)
    return record, refs
