"""Immutable canonical bytes, independent of indexes and coordination decisions.

Clients, bucket owner and account/workspace scope are fixed by the host after
authorization. Importing this module neither imports an AWS SDK nor finds creds.
"""

import os
import re
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from .batch_storage import StorageScope


class PayloadCorrupt(RuntimeError):
    pass


class PayloadUnavailable(RuntimeError):
    pass


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
    def __init__(self, scope, max_bytes):
        if not isinstance(scope, StorageScope) or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("fixed scope and explicit positive byte budget required")
        self.scope, self.max_bytes = scope, max_bytes

    def _reference(self, content):
        if not isinstance(content, bytes) or len(content) > self.max_bytes:
            raise ValueError("payload exceeds configured byte budget")
        return PayloadRef(self.scope, sha256(content).hexdigest(), len(content))

    def _check(self, reference):
        if not isinstance(reference, PayloadRef) or reference.scope != self.scope:
            raise PermissionError("payload scope refused")
        if reference.length > self.max_bytes:
            raise ValueError("payload exceeds configured byte budget")

    def _verified(self, content, reference):
        if len(content) != reference.length or sha256(content).hexdigest() != reference.digest:
            raise PayloadCorrupt("canonical payload digest or length mismatch")
        return content


class FilePayloads(_Payloads):
    """Trusted local root and cooperating writers; byte-identical export layout."""

    def __init__(self, root, scope, *, max_bytes):
        super().__init__(scope, max_bytes)
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
        except OSError:
            raise PayloadUnavailable("canonical payload unavailable") from None


class S3Payloads(_Payloads):
    """Native create-only S3 writes; SDK client is explicitly injected.

    Repeated identical bytes are safe after a lost response. No mutable head,
    dispatch permission or authoritative DynamoDB counter is introduced here.
    """

    def __init__(self, client, bucket, owner, scope, *, max_bytes):
        super().__init__(scope, max_bytes)
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

    def get(self, reference):
        self._check(reference)
        stream = None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(reference.digest), ExpectedBucketOwner=self.owner)
            stream = response["Body"]
            if type(response.get("ContentLength")) is not int or response["ContentLength"] != reference.length:
                raise PayloadCorrupt("canonical payload length mismatch")
            return self._verified(stream.read(reference.length + 1), reference)
        except PayloadCorrupt:
            raise
        except Exception:
            raise PayloadUnavailable("canonical payload unavailable") from None
        finally:
            if stream is not None:
                stream.close()
