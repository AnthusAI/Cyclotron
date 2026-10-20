"""Request-local sharing authorization using fresh Prospectus account checks.

The host supplies ``resolve_session(connection)`` from its verified server-side
session machinery. Every invocation must freshly validate session existence,
JWT issuer/signature/client/token_use/expiry/subject and canonical person binding.
It must never return a cached ``VerifiedAccountSession`` or trust request identity
fields. Pinning the verified token digest also pins its JWT subject; person_id is
the canonical Prospectus person, not an email or a raw Cognito subject.

``for_request`` captures one connection and immutable session/account/person/token
binding. Supply its three methods to one request's SharingService. Do not reuse a
request adapter for another request, a queue job, or a background worker. Existing
host CSRF and transport authentication remain required before mutating requests.
No identity creation/resolution endpoint is called and no credential is created.

AccountsClient console checks are a membership ceiling, never publication,
engine, source-read, provider-budget or account-administration permission. Owner
issue/revoke additionally require the exact current owner role AND a mandatory
``authorize_owner_scope(principal, spec, operation)`` decision for the complete
engine/binding/output/consumer/publication/billing specification. Consumer use
checks only its own account's console:read membership AND mandatory
``authorize_consumer_scope(principal, grant, operation)`` permission for that exact
publication and infer/deliver operation. Consumers need no owner-account access.
Both trusted callbacks must query current authoritative entitlements, return None
or raise, and receive no authority from a request body's assertions. There is no
default scope permission. Session and membership are checked again after the
callback awaits; owner role is checked again too.

SharingService still owns live grant/revision/expiry/binding checks. The host must
separately provide its canonical source validator and preserve source access,
execution ownership and provider-budget guards. This module installs no routes,
transport, receipt signer or production authority.
"""

import asyncio
import inspect
from dataclasses import dataclass, field
from hashlib import sha256
from hmac import compare_digest

from .account_sessions import SessionRejected
from .prospectus_accounts import AccountAccessDenied, AccountsClient, VerifiedAccountSession
from .publication_sharing import GrantSpec, SharingDenied, SharingGrant, SharingUnavailable, VerifiedPrincipal


async def _invoke(callback, *args):
    """Close all failures without reflecting private dependency error messages."""
    try:
        result = callback(*args)
        return await result if inspect.isawaitable(result) else result
    except (AccountAccessDenied, SessionRejected, SharingDenied, PermissionError):
        raise SharingDenied("sharing authorization refused") from None
    except Exception:
        raise SharingUnavailable("sharing authorization unavailable") from None


@dataclass(frozen=True)
class _SessionBinding:
    principal: VerifiedPrincipal
    session_ref: str = field(repr=False)
    token_digest: str = field(repr=False)

    @classmethod
    def capture(cls, session):
        if type(session) is not VerifiedAccountSession:
            raise SharingDenied("verified request session required") from None
        if (
            not isinstance(session.access_token, str)
            or not 0 < len(session.access_token) <= 16384
            or "\r" in session.access_token
            or "\n" in session.access_token
            or not isinstance(session.session_ref, str)
            or not 0 < len(session.session_ref) <= 512
        ):
            raise SharingDenied("verified request session required") from None
        try:
            principal = VerifiedPrincipal(session.account_id, session.person_id)
            fingerprint = sha256(session.access_token.encode()).hexdigest()
        except (ValueError, TypeError, UnicodeError):
            raise SharingDenied("verified request session required") from None
        return cls(principal, session.session_ref, fingerprint)

    def matches(self, current):
        return (
            self.principal == current.principal
            and self.session_ref == current.session_ref
            and compare_digest(self.token_digest, current.token_digest)
        )


@dataclass(frozen=True)
class _RequestAuthorization:
    _accounts: AccountsClient = field(repr=False)
    _resolve_session: object = field(repr=False)
    _owner_scope: object = field(repr=False)
    _consumer_scope: object = field(repr=False)
    _connection: object = field(repr=False)
    _binding: _SessionBinding = field(repr=False)

    async def _session(self):
        session = await _invoke(self._resolve_session, self._connection)
        if not self._binding.matches(_SessionBinding.capture(session)):
            raise SharingDenied("request session binding changed") from None
        return session

    async def _fresh(self, operation):
        session = await self._session()
        answer = await _invoke(
            asyncio.to_thread,
            self._accounts.authorize,
            session.access_token,
            session.account_id,
            session.person_id,
            operation,
        )
        # The central account round trip itself is awaited, so a changed local
        # session must not be accepted when that answer arrives.
        await self._session()
        return self._binding.principal, answer

    async def resolve_principal(self):
        principal, _ = await self._fresh("console:read")
        return principal

    async def _owner(self, principal, spec):
        current, answer = await self._fresh("console:write")
        if current != principal or current.account_id != spec.owner_account_id or answer["role"] != "owner":
            raise SharingDenied("exact owner authority required") from None

    async def authorize_owner(self, principal, spec, operation):
        if type(principal) is not VerifiedPrincipal or type(spec) is not GrantSpec or operation not in ("issue", "revoke"):
            raise SharingDenied("exact sharing management scope required") from None
        await self._owner(principal, spec)
        if await _invoke(self._owner_scope, principal, spec, operation) is not None:
            raise SharingDenied("explicit owner scope permission required") from None
        await self._owner(principal, spec)

    async def _consumer(self, principal, grant):
        current, _ = await self._fresh("console:read")
        if current != principal or current.account_id != grant.spec.consumer_account_id:
            raise SharingDenied("exact consumer authority required") from None

    async def authorize_use(self, principal, grant, operation):
        if (
            type(principal) is not VerifiedPrincipal
            or type(grant) is not SharingGrant
            or operation not in ("infer", "deliver")
            or grant.status != "active"
            or operation not in grant.spec.allowed_operations
        ):
            raise SharingDenied("exact active sharing use scope required") from None
        await self._consumer(principal, grant)
        if await _invoke(self._consumer_scope, principal, grant, operation) is not None:
            raise SharingDenied("explicit consumer scope permission required") from None
        await self._consumer(principal, grant)


class ProspectusSharingAuthorization:
    """Bind fresh host session checks to the sharing core's three trusted ports."""

    def __init__(self, accounts, *, resolve_session, authorize_owner_scope, authorize_consumer_scope):
        if not isinstance(accounts, AccountsClient):
            raise TypeError("AccountsClient required")
        if not all(callable(callback) for callback in (resolve_session, authorize_owner_scope, authorize_consumer_scope)):
            raise TypeError("verified session and explicit owner and consumer scope callbacks required")
        self._accounts = accounts
        self._resolve_session = resolve_session
        self._owner_scope = authorize_owner_scope
        self._consumer_scope = authorize_consumer_scope

    async def for_request(self, connection):
        session = await _invoke(self._resolve_session, connection)
        request = _RequestAuthorization(
            self._accounts,
            self._resolve_session,
            self._owner_scope,
            self._consumer_scope,
            connection,
            _SessionBinding.capture(session),
        )
        await request.resolve_principal()
        return request
