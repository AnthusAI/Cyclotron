"""Compare-and-swap journal on a trusted local directory for tests and single-machine use.

Coordination objects are replaced atomically while holding an exclusive lock
file, so cooperating processes on one machine get the same conditional-write
semantics the S3 journal gets from the service.
"""

import fcntl
import os
import tempfile
from hashlib import sha256
from pathlib import Path

from .journal import CanonicalJournal, JournalConflict, JournalUnavailable, JournalUncertain, decode, encode
from .storage_payloads import FilePayloads


class LocalFileJournal(CanonicalJournal):
    def __init__(self, payloads, domain, name, *, create_only_initialization=False):
        if not isinstance(payloads, FilePayloads):
            raise ValueError("injected scoped file payload adapter required")
        super().__init__(
            payloads,
            domain,
            name,
            create_only_initialization=create_only_initialization,
        )
        self.boundary = (payloads.scope, str(payloads.root))
        self.directory = payloads.root / payloads.scope.account / payloads.scope.workspace / "coordination" / domain / name

    def _path(self, key):
        return self.directory / Path(key).name

    @staticmethod
    def _etag(content):
        return '"' + sha256(content).hexdigest() + '"'

    def _read_current(self, key):
        try:
            with open(self._path(key), "rb") as stream:
                content = stream.read()
        except FileNotFoundError:
            return None
        if not content:
            raise ValueError("invalid coordination object")
        return content

    def _get(self, key, *, missing=False, creation_probe=False):
        try:
            content = self._read_current(key)
            if content is not None:
                return decode(content), self._etag(content)
        except Exception:
            raise JournalUnavailable("canonical file coordination unavailable; do not initialize or dispatch") from None
        if missing:
            return None
        raise JournalUnavailable("canonical file coordination unavailable; do not initialize or dispatch")

    def _put(self, key, value, etag=None):
        content = encode(value)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with open(self.directory / ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                current = self._read_current(key)
                if (current is None) != (etag is None) or (current is not None and self._etag(current) != etag):
                    raise JournalConflict("file head changed; read current authority before retry")
                with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as temporary:
                    try:
                        temporary.write(content)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                        os.replace(temporary.name, self._path(key))
                    except BaseException:
                        if os.path.exists(temporary.name):
                            os.unlink(temporary.name)
                        raise
            return self._etag(content)
        except JournalConflict:
            raise
        except Exception:
            raise JournalUncertain("file write outcome uncertain; no dispatch or automatic reset") from None
