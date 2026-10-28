"""Canonical S3 transactions; DynamoDB is never involved in authority.

A small conditional head publishes one immutable value+parent record. No SQL,
provider call or caller callback runs inside the CAS primitive. The journal
logic itself lives in journal.py; this module supplies the S3 conditional
reads and writes.
"""

import weakref

from .journal import CanonicalJournal, JournalConflict, JournalUnavailable, JournalUncertain, decode, encode
from .journal import uuid as uuid
from .s3_transient import cause_name, error_code, log, read_with_one_retry
from .storage_payloads import S3Payloads

# Per injected client: (bucket, owner) -> {anchor key: verified anchor} (see CanonicalJournal._anchors).
# Keyed weakly by the client, as the payload caches are.
_ANCHORS_BY_CLIENT = weakref.WeakKeyDictionary()


class S3Journal(CanonicalJournal):
    def __init__(self, payloads, domain, name, *, create_only_initialization=False):
        if not isinstance(payloads, S3Payloads):
            raise ValueError("injected scoped S3 payload adapter required")
        # Hidden SDK retries can turn a lost successful PUT into a later 412.
        # Pure mutation replay would then duplicate append-only records. Real
        # botocore clients must perform exactly one attempt; the domain retries
        # only explicit service conflicts. Test clients implement this contract.
        meta = getattr(payloads.client, "meta", None)
        if meta is not None and getattr(meta, "config", None) is not None:
            retries = meta.config.retries or {}
            if retries.get("total_max_attempts") != 1:
                raise ValueError("conditional S3 client requires total_max_attempts=1")
        super().__init__(
            payloads,
            domain,
            name,
            create_only_initialization=create_only_initialization,
        )
        self.boundary = (payloads.scope, payloads.bucket, payloads.owner)

    def _anchors(self):
        try:
            per_client = _ANCHORS_BY_CLIENT.setdefault(self.payloads.client, {})
        except TypeError:
            return None
        return per_client.setdefault((self.payloads.bucket, self.payloads.owner), {})

    def _fetch(self, key):
        stream = None
        try:
            response = self.payloads.client.get_object(
                Bucket=self.payloads.bucket, Key=key, ExpectedBucketOwner=self.payloads.owner
            )
            stream = response["Body"]
            length = response["ContentLength"]
            etag = response["ETag"]
            if type(length) is not int or length < 1 or not isinstance(etag, str) or not etag:
                raise ValueError("invalid coordination object")
            content = stream.read(length + 1)
            if len(content) != length:
                raise ValueError("incomplete coordination object")
            return decode(content), etag
        finally:
            if stream is not None:
                stream.close()

    def _get(self, key, *, missing=False, creation_probe=False):
        try:
            # Reading the anchor or head is idempotent: one transient failure is retried. _put never is.
            return read_with_one_retry(lambda: self._fetch(key), "coordination")
        except Exception as error:
            code = error_code(error)
            if missing and (code == "NoSuchKey" or (creation_probe and code == "AccessDenied")):
                return None
            log.warning("canonical S3 coordination unavailable: %s", cause_name(error))
            raise JournalUnavailable(
                "canonical S3 coordination unavailable; do not initialize or dispatch", cause_name(error)
            ) from None

    def _put(self, key, value, etag=None):
        try:
            response = self.payloads.client.put_object(
                Bucket=self.payloads.bucket,
                Key=key,
                Body=encode(value),
                ExpectedBucketOwner=self.payloads.owner,
                ContentType="application/json",
                **({"IfNoneMatch": "*"} if etag is None else {"IfMatch": etag}),
            )
            result = response.get("ETag")
            if not isinstance(result, str) or not result:
                raise ValueError("missing committed ETag")
            return result
        except Exception as error:
            if error_code(error) in (
                "PreconditionFailed",
                "ConditionalRequestConflict",
                "412",
                "409",
            ):
                raise JournalConflict("S3 head changed; read current authority before retry") from None
            raise JournalUncertain("S3 write outcome uncertain; no dispatch or automatic reset") from None
