"""Encrypted, bounded OAuth state in canonical S3; no index grants authority.

A confirmed CAS consumes each login. Unknown consume outcomes never permit a
code exchange. Session updates cannot resurrect logout or replace its actor.
All methods are blocking and must run off the HTTP event loop.
"""

import base64
import hmac
import re
import secrets
import time
from dataclasses import asdict

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .account_sessions import ConsoleSession, PendingLogin, SessionRejected, digest
from .journal import JournalConflict, decode, encode
from .prospectus_accounts import ProspectusUnavailable


class SessionStoreUnavailable(ProspectusUnavailable):
    pass


class KMSSessionCipher:
    """One KMS envelope key per record revision, bound to app and record context."""

    def __init__(self, client, key_arn, *, application):
        if not re.fullmatch(r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[a-f0-9-]{36}", key_arn):
            raise ValueError("exact KMS key ARN required")
        if not isinstance(application, str) or not re.fullmatch(r"[a-z0-9-]+", application):
            raise ValueError("fixed application context required")
        self.client, self.key_arn, self.application = client, key_arn, application

    def _context(self, kind, reference):
        if kind not in ("pending", "session") or not re.fullmatch(r"[a-f0-9]{64}", reference):
            raise ValueError("invalid session encryption scope")
        return {"application": self.application, "purpose": "oauth-session", "kind": kind, "record": reference}

    def encrypt(self, kind, reference, content):
        context = self._context(kind, reference)
        result = self.client.generate_data_key(KeyId=self.key_arn, KeySpec="AES_256", EncryptionContext=context)
        if result.get("KeyId") != self.key_arn or len(result["Plaintext"]) != 32:
            raise ValueError("unexpected session encryption key")
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(result["Plaintext"]).encrypt(nonce, content, encode(context))
        return {
            name: base64.b64encode(value).decode()
            for name, value in (("key", result["CiphertextBlob"]), ("nonce", nonce), ("body", encrypted))
        }

    def decrypt(self, kind, reference, envelope):
        context = self._context(kind, reference)
        if set(envelope) != {"key", "nonce", "body"}:
            raise ValueError("invalid envelope")
        values = {
            key: base64.b64decode(value, validate=True)
            for key, value in envelope.items()
            if isinstance(value, str)
        }
        if set(values) != set(envelope) or len(values["nonce"]) != 12:
            raise ValueError("invalid envelope")
        result = self.client.decrypt(KeyId=self.key_arn, CiphertextBlob=values["key"], EncryptionContext=context)
        if result.get("KeyId") != self.key_arn or len(result["Plaintext"]) != 32:
            raise ValueError("unexpected session encryption key")
        return AESGCM(result["Plaintext"]).decrypt(values["nonce"], values["body"], encode(context))


class S3OAuthSessions:
    def __init__(self, verifier, *, journal_for, cipher, clock=time.time, max_age=3600, capacity=5000):
        if type(capacity) is not int or not 1 <= capacity <= 5000 or not 0 < max_age <= 3600:
            raise ValueError("bounded session limits required")
        self.verifier, self._journal, self.cipher = verifier, journal_for, cipher
        self.clock, self.max_age, self.capacity = clock, max_age, capacity

    @staticmethod
    def reference(identifier):
        if not isinstance(identifier, str) or not identifier.isascii() or not 1 <= len(identifier) <= 256:
            raise SessionRejected("invalid session reference")
        return digest(identifier)

    def _reserve(self, kind, reference, expiry):
        quota = self._journal("quota", digest(kind))
        for _ in range(8):
            record = quota.initialize({"schema": "oauth-quota/v1", "entries": {}})
            value = record.value
            if set(value) != {"schema", "entries"} or value["schema"] != "oauth-quota/v1":
                raise ValueError("invalid quota")
            entries = {key: exp for key, exp in value["entries"].items() if exp > self.clock()}
            if len(entries) >= self.capacity:
                raise SessionRejected("session capacity reached; try later")
            entries[reference] = expiry
            try:
                quota.commit(record, {"schema": "oauth-quota/v1", "entries": entries})
                return
            except JournalConflict:
                continue
        raise SessionStoreUnavailable("session store busy")

    def _new(self, kind, reference, expiry, payload):
        self._reserve(kind, reference, expiry)
        value = {
            "schema": "oauth-record/v1",
            "kind": kind,
            "reference": reference,
            "active": True,
            "expiresAt": expiry,
            "encrypted": self.cipher.encrypt(kind, reference, encode(payload)),
        }
        record = self._journal(kind, reference).initialize(value)
        if record.value != value:
            raise SessionRejected("session collision")
        return record

    def _read(self, kind, reference):
        journal = self._journal(kind, reference)
        if not journal.exists():
            return None
        record = journal.read()
        value = record.value
        if (
            set(value) != {"schema", "kind", "reference", "active", "expiresAt", "encrypted"}
            or value["schema"] != "oauth-record/v1"
            or value["kind"] != kind
            or value["reference"] != reference
            or type(value["active"]) is not bool
            or type(value["expiresAt"]) not in (int, float)
        ):
            raise ValueError("invalid session record")
        return record

    def begin(self, challenge=None):
        try:
            expiry = min(self.clock() + 300, challenge.expires_at) if challenge else self.clock() + 300
            if expiry <= self.clock():
                raise SessionRejected("identity challenge expired")
            pending = PendingLogin(
                secrets.token_urlsafe(32),
                challenge.nonce if challenge else secrets.token_urlsafe(32),
                secrets.token_urlsafe(64),
                secrets.token_urlsafe(32),
                expiry,
                challenge.challenge_id if challenge else None,
                challenge.challenge_token if challenge else None,
            )
            self._new("pending", self.reference(pending.state), expiry, asdict(pending))
            return pending
        except (SessionRejected, SessionStoreUnavailable):
            raise
        except Exception:
            raise SessionStoreUnavailable("login storage unavailable") from None

    def consume(self, state, browser_cookie):
        try:
            reference = self.reference(state)
            record = self._read("pending", reference)
            if not record or not record.value["active"] or record.value["expiresAt"] <= self.clock():
                raise SessionRejected("login expired; start again")
            # Commit before decrypting/returning. A lost receipt is never permission.
            self._journal("pending", reference).commit(record, {**record.value, "active": False})
            pending = PendingLogin(**decode(self.cipher.decrypt("pending", reference, record.value["encrypted"])))
            if (
                pending.state != state
                or pending.expires_at != record.value["expiresAt"]
                or pending.expires_at <= self.clock()
                or not isinstance(browser_cookie, str)
                or not browser_cookie.isascii()
                or not hmac.compare_digest(pending.browser_cookie, browser_cookie)
            ):
                raise SessionRejected("login expired; start again")
            return pending
        except Exception:
            raise SessionRejected("login expired; start again") from None

    def create(self, access_token, claims):
        try:
            expiry = min(float(claims["exp"]), self.clock() + self.max_age)
            if expiry <= self.clock():
                raise SessionRejected("session expired")
            identifier = secrets.token_urlsafe(48)
            reference = self.reference(identifier)
            session = ConsoleSession(access_token, claims["sub"], expiry, secrets.token_urlsafe(32), session_ref=reference)
            record = self._new("session", reference, expiry, self._payload(session))
            session.storage_revision = record.revision
            return identifier, session
        except (SessionRejected, SessionStoreUnavailable):
            raise
        except Exception:
            raise SessionStoreUnavailable("session storage unavailable") from None

    @staticmethod
    def _payload(session):
        return {key: value for key, value in asdict(session).items() if key not in ("session_ref", "storage_revision")}

    def get(self, identifier):
        if identifier is None:
            return None
        try:
            reference = self.reference(identifier)
        except SessionRejected:
            return None
        return self.get_ref(reference)

    def get_ref(self, reference):
        try:
            if not isinstance(reference, str) or not re.fullmatch(r"[a-f0-9]{64}", reference):
                raise SessionRejected("invalid session reference")
            record = self._read("session", reference)
            if not record or not record.value["active"] or record.value["expiresAt"] <= self.clock():
                return None
            payload = decode(self.cipher.decrypt("session", reference, record.value["encrypted"]))
            session = ConsoleSession(**payload, session_ref=reference, storage_revision=record.revision)
            if session.expires_at != record.value["expiresAt"]:
                raise ValueError("session expiry mismatch")
            return session
        except SessionRejected:
            raise
        except Exception:
            raise SessionStoreUnavailable("session storage unavailable") from None

    def save(self, session):
        try:
            reference = session.session_ref
            current = self.get_ref(reference)
            if (
                not current
                or current.storage_revision != session.storage_revision
                or any(
                    getattr(current, key) != getattr(session, key)
                    for key in ("access_token", "subject", "expires_at", "csrf_token")
                )
                or current.person_id is not None
                and current.person_id != session.person_id
            ):
                raise SessionRejected("session changed; refresh authentication")
            record = self._read("session", reference)
            if record.revision != session.storage_revision:
                raise SessionRejected("session changed")
            value = {**record.value, "encrypted": self.cipher.encrypt("session", reference, encode(self._payload(session)))}
            saved = self._journal("session", reference).commit(record, value)
            session.storage_revision = saved.revision
        except (SessionRejected, SessionStoreUnavailable):
            raise
        except JournalConflict:
            raise SessionRejected("session changed; refresh authentication") from None
        except Exception:
            raise SessionStoreUnavailable("session update unavailable") from None

    def revoke(self, identifier):
        if identifier is None:
            return
        try:
            reference = self.reference(identifier)
            for _ in range(8):
                record = self._read("session", reference)
                if not record or not record.value["active"]:
                    return
                try:
                    self._journal("session", reference).commit(record, {**record.value, "active": False})
                    return
                except JournalConflict:
                    continue
            raise SessionStoreUnavailable("session store busy")
        except (SessionRejected, SessionStoreUnavailable):
            raise
        except Exception:
            raise SessionStoreUnavailable("logout storage unavailable") from None
