"""One small mutable document per key beside a control journal, trusted only through the journal's coverage.

A control journal must not hold an index that grows with its keys (one row per item or per decision), and
a record lake is ordered by sequence, not by key. A keyed document fills that gap: one object per key at
``<journal prefix>/keys/<kind>/<sha256(key)>.json``, found with one GET.

Each document holds a few stamped entries ``[at, value]``. ``at`` is the sequence of a record in a commit
lake that the same logical commit writes (record_lake.py). An entry counts only when the journal's coverage
of that lake covers ``at``, so a document written for a commit whose head never moved is invisible, exactly
like an orphan record. The newest covered entry is the document's value.

Writing (``store``) happens after the commit record is written and before the head moves. It keeps the
newest covered entry, any entry above the coverage (perhaps a fenced-out writer's, still in flight) and adds
the new one; entries at or below the coverage that it does not cover are abandoned writes and are dropped.
So a document holds the current value plus at most the writes still in flight: its size is set by the
layout, never by a cap. Writes are compare-and-set on the object's ETag, so two writers never lose each
other's entries.

API (shared by every journal that needs a lookup by key):

* ``keyed_documents_for(journal)`` -- the documents beside a canonical S3 or file journal.
* ``documents.load(kind, key)`` -- the stored ``Document``, or None when the key has none. Absence is
  ``NoSuchKey`` only (the roles hold ``s3:ListBucket``); any other failure, AccessDenied included, raises
  ``KeyedUnavailable`` and never reads as absent.
* ``documents.store(kind, key, document, coverage, at, value)`` -- the conditional put: create-only when
  ``document`` is None, else ``If-Match`` its ETag; a changed document is read again and merged again.
  ``coverage`` is the head's current coverage of the commit lake, ``at`` the new commit record's sequence.
* ``resolve(document, coverage)`` -- the head-coverage rule: the newest entry the coverage covers, else None.
"""

import fcntl
import os
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .journal import decode, encode
from .s3_transient import cause_name, log, read_with_one_retry
from .storage_errors import StorageConflict, StorageUncertain
from .storage_payloads import FilePayloads, S3Payloads

SCHEMA = "cyclotron-keyed/v1"
_EXISTS = ("PreconditionFailed", "ConditionalRequestConflict", "412", "409")


class KeyedConflict(StorageConflict):
    """The document changed since it was read. Nothing was overwritten."""


class KeyedUncertain(StorageUncertain):
    """The document write may or may not have happened."""


class KeyedUnavailable(RuntimeError):
    """A document could not be read. ``cause`` is the underlying exception class name only."""

    def __init__(self, message, cause=None):
        super().__init__(message + (f" (cause: {cause})" if cause else ""))
        self.cause = cause


class KeyedCorrupt(RuntimeError):
    pass


@dataclass(frozen=True)
class Document:
    """One key's stored document: its ETag and its stamped entries, oldest first."""

    etag: str
    entries: tuple


def resolve(document, coverage):
    """The value of the newest entry the coverage vouches for, or None when there is none."""
    if document is None:
        return None
    covered = [entry for entry in document.entries if coverage.covers(entry[0])]
    return covered[-1][1] if covered else None


def merged(document, coverage, at, value):
    """The entries a write of ``value`` at ``at`` leaves: the newest covered one, those in flight, the new one."""
    entries = document.entries if document is not None else ()
    covered = [entry for entry in entries if coverage.covers(entry[0])]
    kept = covered[-1:] + [entry for entry in entries if entry[0] > coverage.last and entry[0] != at]
    return tuple(sorted(kept, key=lambda entry: entry[0])) + ((at, value),)


class KeyedDocuments:
    """Shared document logic. A back end supplies ``_get`` (bytes, etag), ``_create`` and ``_replace``."""

    def __init__(self, prefix):
        self.prefix = prefix

    def path(self, kind, key):
        return f"{self.prefix}/keys/{kind}/{sha256(key.encode()).hexdigest()}.json"

    def _encode(self, kind, key, entries):
        return encode({"schema": SCHEMA, "prefix": self.prefix, "kind": kind, "key": key, "entries": [list(e) for e in entries]})

    def _decode(self, kind, key, content, etag):
        try:
            value = decode(content)
            if (
                set(value) != {"schema", "prefix", "kind", "key", "entries"}
                or value["schema"] != SCHEMA
                or (value["prefix"], value["kind"], value["key"]) != (self.prefix, kind, key)
                or not isinstance(value["entries"], list)
                or any(not isinstance(e, list) or len(e) != 2 or type(e[0]) is not int for e in value["entries"])
            ):
                raise ValueError()
        except Exception:
            raise KeyedCorrupt("keyed document does not match its key") from None
        return Document(etag, tuple((at, held) for at, held in value["entries"]))

    def load(self, kind, key):
        """The stored document, or None when the key has none."""
        found = self._get(self.path(kind, key))
        return None if found is None else self._decode(kind, key, *found)

    def store(self, kind, key, document, coverage, at, value):
        """Write ``value`` stamped ``at`` onto ``document`` (as last read; None if absent). Returns the new document.

        A changed document is read again and merged again: only the owner and fenced-out writers write here,
        and merging keeps every entry that may still matter.
        """
        path = self.path(kind, key)
        while True:
            entries = merged(document, coverage, at, value)
            content = self._encode(kind, key, entries)
            try:
                etag = self._create(path, content) if document is None else self._replace(path, content, document.etag)
                return Document(etag, entries)
            except KeyedConflict:
                document = self.load(kind, key)


class S3KeyedDocuments(KeyedDocuments):
    def __init__(self, payloads, prefix):
        if not isinstance(payloads, S3Payloads):
            raise ValueError("injected scoped S3 payload adapter required")
        meta = getattr(payloads.client, "meta", None)
        if meta is not None and getattr(meta, "config", None) is not None:
            if (meta.config.retries or {}).get("total_max_attempts") != 1:
                raise ValueError("conditional S3 client requires total_max_attempts=1")
        super().__init__(prefix)
        self.client, self.bucket, self.owner = payloads.client, payloads.bucket, payloads.owner

    def _fetch(self, path):
        stream = None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=path, ExpectedBucketOwner=self.owner)
            stream = response["Body"]
            length, etag = response["ContentLength"], response["ETag"]
            if type(length) is not int or length < 1 or not isinstance(etag, str) or not etag:
                raise ValueError("invalid keyed object")
            content = stream.read(length + 1)
            if len(content) != length:
                raise ValueError("incomplete keyed object")
            return content, etag
        finally:
            if stream is not None:
                stream.close()

    def _get(self, path):
        try:
            return read_with_one_retry(lambda: self._fetch(path), "keyed document")
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code == "NoSuchKey":
                return None  # only a real absence; a refused GET (AccessDenied) is an error below
            log.warning("keyed document unavailable: %s", cause_name(error))
            raise KeyedUnavailable("keyed document unavailable", cause_name(error)) from None

    def _put(self, path, content, condition):
        try:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=path,
                Body=content,
                ExpectedBucketOwner=self.owner,
                ContentType="application/json",
                **condition,
            )
            etag = response.get("ETag")
            if not isinstance(etag, str) or not etag:
                raise ValueError("missing keyed ETag")
            return etag
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") in _EXISTS:
                raise KeyedConflict("keyed document changed") from None
            raise KeyedUncertain("keyed document write outcome uncertain") from None

    def _create(self, path, content):
        return self._put(path, content, {"IfNoneMatch": "*"})

    def _replace(self, path, content, etag):
        return self._put(path, content, {"IfMatch": etag})


class FileKeyedDocuments(KeyedDocuments):
    """Trusted local directory, byte-identical in layout to the S3 keys under the payload root."""

    def __init__(self, payloads, prefix):
        if not isinstance(payloads, FilePayloads):
            raise ValueError("injected scoped file payload adapter required")
        super().__init__(prefix)
        self.root = Path(payloads.root)

    @staticmethod
    def _etag(content):
        return '"' + sha256(content).hexdigest() + '"'

    def _read(self, path):
        try:
            with open(self.root / path, "rb") as stream:
                return stream.read()
        except FileNotFoundError:
            return None

    def _get(self, path):
        try:
            content = self._read(path)
        except OSError as error:
            raise KeyedUnavailable("keyed document unavailable", cause_name(error)) from None
        return None if content is None else (content, self._etag(content))

    def _write(self, path, content, etag):
        target = self.root / path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target.parent / ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                current = self._read(path)
                if (current is None) != (etag is None) or (current is not None and self._etag(current) != etag):
                    raise KeyedConflict("keyed document changed")
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix="tmp-", delete=False) as temporary:
                    try:
                        temporary.write(content)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                        os.replace(temporary.name, target)
                    except BaseException:
                        if os.path.exists(temporary.name):
                            os.unlink(temporary.name)
                        raise
            return self._etag(content)
        except KeyedConflict:
            raise
        except Exception:
            raise KeyedUncertain("file keyed document write outcome uncertain") from None

    def _create(self, path, content):
        return self._write(path, content, None)

    def _replace(self, path, content, etag):
        return self._write(path, content, etag)


def keyed_documents_for(journal):
    """The keyed documents beside a canonical journal, under its own coordination prefix and payload adapter."""
    prefix = journal.head_key.rsplit("/", 1)[0]
    payloads = journal.payloads
    if isinstance(payloads, S3Payloads):
        return S3KeyedDocuments(payloads, prefix)
    if isinstance(payloads, FilePayloads):
        return FileKeyedDocuments(payloads, prefix)
    raise ValueError("an S3 or file payload adapter is required for keyed documents")
