"""Immutable canonical bytes, independent of indexes and coordination decisions.

Clients, bucket owner and account/workspace scope are fixed by the host after
authorization. Importing this module neither imports an AWS SDK nor finds creds.
"""

import os
import re
import tempfile
import weakref
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from .batch_storage import StorageScope
from .s3_transient import cause_name, log, read_with_one_retry


class PayloadCorrupt(RuntimeError):
    pass


class PayloadUnavailable(RuntimeError):
    """The payload could not be read. ``cause`` is the underlying exception class name only."""

    def __init__(self, message, cause=None):
        super().__init__(message + (f" (cause: {cause})" if cause else ""))
        self.cause = cause


class PayloadUncertain(RuntimeError):
    pass


@dataclass(frozen=True)
class PayloadRef:
    scope: StorageScope
    digest: str
    length: int

    def __post_init__(self):
        if (
            not isinstance(self.scope, StorageScope)
            or not isinstance(self.digest, str)
            or not re.fullmatch("[a-f0-9]{64}", self.digest)
            or type(self.length) is not int
            or self.length < 0
        ):
            raise ValueError("valid scoped payload reference required")


class Payloads(Protocol):
    def put(self, content: bytes) -> PayloadRef: ...
    def get(self, reference: PayloadRef) -> bytes: ...


class _Payloads:
    def __init__(self, scope):
        if not isinstance(scope, StorageScope):
            raise ValueError("fixed storage scope required")
        self.scope = scope

    def _reference(self, content):
        if not isinstance(content, bytes):
            raise ValueError("payload bytes required")
        return PayloadRef(self.scope, sha256(content).hexdigest(), len(content))

    def _check(self, reference):
        if not isinstance(reference, PayloadRef) or reference.scope != self.scope:
            raise PermissionError("payload scope refused")

    def _verified(self, content, reference):
        if len(content) != reference.length or sha256(content).hexdigest() != reference.digest:
            raise PayloadCorrupt("canonical payload digest or length mismatch")
        return content


class FilePayloads(_Payloads):
    """Trusted local root and cooperating writers; byte-identical export layout."""

    def __init__(self, root, scope):
        super().__init__(scope)
        self.root = Path(root)
        self.directory = self.root / scope.account / scope.workspace / "payloads"

    def _path(self, digest):
        return self.directory / (digest + ".blob")

    def put(self, content):
        reference = self._reference(content)
        self.directory.mkdir(parents=True, exist_ok=True)
        # Fully write/fsync before atomic create-only linking. A crash can leave
        # an orphan temporary file, never a partly published canonical payload.
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as temporary:
            name = temporary.name
            try:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                try:
                    os.link(name, self._path(reference.digest))
                except FileExistsError:
                    self.get(reference)
            finally:
                os.unlink(name)
        return reference

    def get(self, reference):
        self._check(reference)
        try:
            descriptor = os.open(self._path(reference.digest), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                if os.fstat(stream.fileno()).st_size != reference.length:
                    raise PayloadCorrupt("canonical payload length mismatch")
                return self._verified(stream.read(reference.length + 1), reference)
        except PayloadCorrupt:
            raise
        except OSError as error:
            raise PayloadUnavailable("canonical payload unavailable", cause_name(error)) from None


# Verified bytes of content-addressed S3 objects, shared by every adapter on the same injected client for the
# life of the process. An object under a digest is create-only and never changes, so once one GET has verified
# its digest and length, no later read in this process needs to fetch it again. Only verified reads are kept
# (never the bytes of a put), so a payload altered under its digest before this process first read it still
# fails closed. Keyed weakly by the client; a client that cannot be weakly referenced shares nothing.
_VERIFIED_BY_CLIENT = weakref.WeakKeyDictionary()


def _verified_cache(client):
    try:
        return _VERIFIED_BY_CLIENT.setdefault(client, {})
    except TypeError:
        return None


class S3Payloads(_Payloads):
    """Native create-only S3 writes; SDK client is explicitly injected.

    Repeated identical bytes are safe after a lost response. No mutable head,
    dispatch permission or authoritative DynamoDB counter is introduced here.
    """

    def __init__(self, client, bucket, owner, scope):
        super().__init__(scope)
        if (
            not isinstance(bucket, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
            or not isinstance(owner, str)
            or not re.fullmatch(r"[0-9]{12}", owner)
        ):
            raise ValueError("fixed bucket and expected bucket owner required")
        self.client, self.bucket, self.owner = client, bucket, owner

    def _key(self, digest):
        return f"{self.scope.key}/payloads/{digest}.blob"

    def put(self, content):
        reference = self._reference(content)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(reference.digest),
                Body=content,
                ExpectedBucketOwner=self.owner,
                IfNoneMatch="*",
                ContentType="application/octet-stream",
                Metadata={"sha256": reference.digest},
            )
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
                self.get(reference)
            else:
                raise PayloadUncertain("canonical publication uncertain; retry identical bytes") from None
        return reference

    def _fetch(self, reference):
        stream = None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(reference.digest), ExpectedBucketOwner=self.owner)
            stream = response["Body"]
            if type(response.get("ContentLength")) is not int or response["ContentLength"] != reference.length:
                raise PayloadCorrupt("canonical payload length mismatch")
            return self._verified(stream.read(reference.length + 1), reference)
        finally:
            if stream is not None:
                stream.close()

    def get(self, reference):
        self._check(reference)
        cache = _verified_cache(self.client)
        key = (self.bucket, self.owner, self.scope.key, reference.digest, reference.length)
        if cache is not None and key in cache:
            return cache[key]
        try:
            # A GET is idempotent, so one transient failure is retried; corruption never is.
            content = read_with_one_retry(lambda: self._fetch(reference), "payload")
        except PayloadCorrupt:
            raise
        except Exception as error:
            log.warning("canonical payload unavailable: %s", cause_name(error))
            raise PayloadUnavailable("canonical payload unavailable", cause_name(error)) from None
        if cache is not None:
            cache[key] = content
        return content
