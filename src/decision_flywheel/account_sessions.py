"""Pinned Cognito verification and bounded, opaque server-side OAuth sessions."""

import base64
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field
from hashlib import sha256
from threading import RLock
from urllib.parse import urlsplit

import jwt


class SessionRejected(Exception):
    """Invalid or expired authentication; messages never contain token values."""


def digest(value):
    return sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    client_id: str
    authorization_origin: str
    public_origin: str
    allow_insecure_loopback: bool = False

    def __post_init__(self):
        if not re.fullmatch(r"https://cognito-idp\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9_-]+", self.issuer):
            raise ValueError("a pinned Cognito issuer is required")
        if not self.client_id or len(self.client_id) > 128:
            raise ValueError("a dedicated console app client is required")
        auth = urlsplit(self.authorization_origin)
        if auth.scheme != "https" or not auth.hostname or auth.path or auth.query or auth.fragment or auth.username:
            raise ValueError("authorization endpoint must be an HTTPS origin")
        public = urlsplit(self.public_origin)
        loopback = self.allow_insecure_loopback and public.hostname in ("127.0.0.1", "localhost", "::1")
        if (
            (public.scheme != "https" and not (loopback and public.scheme == "http"))
            or not public.hostname
            or public.path
            or public.query
            or public.fragment
            or public.username
        ):
            raise ValueError("public origin must use HTTPS; insecure loopback is test-only")

    @property
    def callback_url(self):
        return self.public_origin + "/auth/callback"

    @property
    def secure(self):
        return self.public_origin.startswith("https://")

    @property
    def cookie_name(self):
        return "__Host-cyclotron-session" if self.secure else "cyclotron-test-session"

    @property
    def transaction_cookie(self):
        return "__Host-cyclotron-oauth" if self.secure else "cyclotron-test-oauth"


class CognitoTokenVerifier:
    def __init__(self, issuer, client_id, *, jwks=None):
        self.issuer = issuer
        self.client_id = client_id
        self.keys = jwt.PyJWKSet.from_dict(jwks).keys if jwks is not None else None
        self.jwks = None if jwks is not None else jwt.PyJWKClient(issuer + "/.well-known/jwks.json", lifespan=300, timeout=5)

    def _verify(self, token, kind):
        try:
            if not isinstance(token, str) or not token or len(token) > 16384:
                raise SessionRejected("invalid session token")
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise SessionRejected("invalid token signature")
            if self.keys is not None:
                key = next(key.key for key in self.keys if key.key_id == header["kid"] and key.algorithm_name == "RS256")
            else:
                key = self.jwks.get_signing_key_from_jwt(token).key
            options = {"require": ["iss", "sub", "exp", "iat", "token_use"], "verify_aud": kind == "id"}
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.client_id if kind == "id" else None,
                options=options,
            )
            if (
                claims.get("token_use") != kind
                or not isinstance(claims["sub"], str)
                or not claims["sub"]
                or kind == "access"
                and claims.get("client_id") != self.client_id
            ):
                raise SessionRejected("invalid token claims")
            return claims
        except Exception:
            raise SessionRejected("invalid or expired session token") from None

    def access(self, token):
        return self._verify(token, "access")

    def token_pair(self, access_token, id_token, nonce, *, require_session_binding=False):
        access = self.access(access_token)
        identity = self._verify(id_token, "id")
        if (
            identity["sub"] != access["sub"]
            or not isinstance(identity.get("nonce"), str)
            or not identity["nonce"].isascii()
            or not hmac.compare_digest(identity["nonce"], nonce)
        ):
            raise SessionRejected("identity token does not match the login")
        if "at_hash" in identity:
            expected = base64.urlsafe_b64encode(sha256(access_token.encode()).digest()[:16]).decode().rstrip("=")
            if (
                not isinstance(identity["at_hash"], str)
                or not identity["at_hash"].isascii()
                or not hmac.compare_digest(identity["at_hash"], expected)
            ):
                raise SessionRejected("identity token does not match the access token")
        if require_session_binding:
            auth_time = access.get("auth_time")
            lineage = access.get("origin_jti")
            if (
                identity.get("aud") != self.client_id
                or type(auth_time) is not int
                or type(identity.get("auth_time")) is not int
                or identity["auth_time"] != auth_time
                or not 0 <= time.time() - auth_time <= 300
                or not isinstance(lineage, str)
                or not 0 < len(lineage) <= 256
                or not lineage.isascii()
                or not isinstance(identity.get("origin_jti"), str)
                or not identity["origin_jti"].isascii()
                or not hmac.compare_digest(lineage, identity["origin_jti"])
            ):
                raise SessionRejected("identity evidence does not match the login session")
        return access


@dataclass(frozen=True, repr=False)
class PendingLogin:
    state: str
    nonce: str
    code_verifier: str
    browser_cookie: str
    expires_at: float
    challenge_id: str | None = None

    @property
    def code_challenge(self):
        return base64.urlsafe_b64encode(sha256(self.code_verifier.encode()).digest()).decode().rstrip("=")


@dataclass
class ConsoleSession:
    access_token: str = field(repr=False)
    subject: str
    expires_at: float
    csrf_token: str = field(repr=False)
    person_id: str | None = None
    account_id: str | None = None
    session_ref: str | None = field(default=None, repr=False, compare=False)
    storage_revision: int = field(default=0, repr=False, compare=False)


class OAuthSessions:
    """Single-process sessions; restarts sign out users instead of persisting JWTs.

    Only hashed opaque session IDs are indexed. No roles or memberships are
    cached. A future multi-process host must replace this store, not replicate it.
    """

    def __init__(self, verifier, *, clock=time.time, max_age=3600, capacity=5000):
        self.verifier = verifier
        self.clock = clock
        self.max_age = max_age
        self.capacity = capacity
        self.lock = RLock()
        self.pending = {}
        self.sessions = {}

    def _purge(self):
        now = self.clock()
        self.pending = {key: value for key, value in self.pending.items() if value.expires_at > now}
        self.sessions = {key: value for key, value in self.sessions.items() if value.expires_at > now}

    def begin(self, challenge=None):
        with self.lock:
            self._purge()
            if len(self.pending) >= self.capacity:
                raise SessionRejected("too many pending logins; try again later")
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
            )
            self.pending[digest(pending.state)] = pending
            return pending

    def consume(self, state, browser_cookie):
        with self.lock:
            self._purge()
            if not isinstance(state, str) or not isinstance(browser_cookie, str):
                raise SessionRejected("login expired; start again")
            pending = self.pending.pop(digest(state), None)
            if not pending or not hmac.compare_digest(pending.browser_cookie, browser_cookie):
                raise SessionRejected("login expired; start again")
            return pending

    def create(self, access_token, claims):
        with self.lock:
            self._purge()
            if len(self.sessions) >= self.capacity:
                raise SessionRejected("too many active sessions; try again later")
            identifier = secrets.token_urlsafe(48)
            session = ConsoleSession(
                access_token, claims["sub"], min(float(claims["exp"]), self.clock() + self.max_age), secrets.token_urlsafe(32)
            )
            session.session_ref = digest(identifier)
            self.sessions[digest(identifier)] = session
            return identifier, session

    def get(self, identifier):
        with self.lock:
            self._purge()
            return self.sessions.get(digest(identifier)) if isinstance(identifier, str) else None

    def revoke(self, identifier):
        with self.lock:
            if isinstance(identifier, str):
                self.sessions.pop(digest(identifier), None)

    def save(self, session):
        with self.lock:
            self._purge()
            if self.sessions.get(session.session_ref) is not session:
                raise SessionRejected("session changed; refresh authentication")

    @staticmethod
    def reference(identifier):
        return digest(identifier)

    def get_ref(self, reference):
        with self.lock:
            self._purge()
            return self.sessions.get(reference)
