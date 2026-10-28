"""Server-side Prospectus accounts adapter; no local identity or membership database.

The host must verify its session access JWT before passing it here. Prospectus
independently verifies that JWT and the SigV4 runtime identity on every call.
"""

import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from urllib.parse import quote, urlsplit
from uuid import UUID

account_request_id = ContextVar("account_request_id", default=None)


def log_console_phase(phase, started, **fields):
    """One structured timing line per phase; callers pass only non-secret descriptors."""
    print(
        json.dumps(
            {
                "event": "console_phase",
                "requestId": account_request_id.get(),
                "phase": phase,
                **fields,
                "durationMs": round((time.monotonic() - started) * 1000),
            }
        )
    )


class AccountAccessDenied(Exception):
    """Central authorization failed; never fall back to the local workspace."""


class ProspectusUnavailable(Exception):
    """Central authorization is unavailable; fail closed without logging tokens."""


class SigV4Transport:
    """Use the SDK credential chain and refresh credentials for each request.

    Production hosts must supply temporary runtime credentials through their
    execution environment. Static credentials are explicitly rejected.
    """

    def __init__(self, endpoint, region, *, session=None, client=None):
        import httpx
        from botocore.session import get_session

        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username
            or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/?", parsed.path)
        ):
            raise ValueError("Prospectus endpoint must be an HTTPS origin with an optional fixed API stage")
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.session = session or get_session()
        self.client = client or httpx.Client(timeout=10, follow_redirects=False)

    def __call__(self, method, path, human_token, body=None, idempotency_key=None, *, identity_token=None):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        challenge = path == "/v1/identities/google/challenge" and method == "POST"
        resolution = path == "/v1/identities/google/resolve" and method == "POST"
        if (
            not (
                path == "/v1/accounts"
                or path.startswith("/v1/accounts/")
                or path == "/v1/identities/resolve"
                or challenge
                or resolution
                or path == "/v1/identities/me"
                and method == "GET"
            )
            or "?" in path
            or "#" in path
        ):
            raise ValueError("unsupported accounts path")

        def valid_token(value):
            return (
                isinstance(value, str) and 0 < len(value) <= 16384 and not any(character in value for character in ("\r", "\n"))
            )

        if (
            not challenge
            and not valid_token(human_token)
            or challenge
            and (human_token is not None or body != {})
            or resolution
            and not valid_token(identity_token)
            or not resolution
            and identity_token is not None
        ):
            raise AccountAccessDenied("verified session required")
        started = time.monotonic()
        status = "unavailable"
        try:
            credentials = self.session.get_credentials().get_frozen_credentials()
            if not credentials.token:
                raise ProspectusUnavailable("temporary runtime credentials required")
            headers = {"Content-Type": "application/json"}
            if human_token is not None:
                headers["X-Prospectus-Human-Token"] = human_token
            if identity_token is not None:
                headers["X-Prospectus-Identity-Token"] = identity_token
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            request = AWSRequest(
                method=method,
                url=self.endpoint + path,
                data=json.dumps(body, separators=(",", ":")).encode() if body is not None else b"",
                headers=headers,
            )
            SigV4Auth(credentials, "execute-api", self.region).add_auth(request)
            prepared = request.prepare()
            response = self.client.request(
                method, prepared.url, content=prepared.body, headers=dict(prepared.headers), follow_redirects=False, timeout=10
            )
            status = response.status_code
            if response.status_code in (401, 403, 409):
                raise AccountAccessDenied("account access refused")
            if response.status_code not in (200, 201):
                raise ProspectusUnavailable("account service unavailable")
            result = response.json()
            if not isinstance(result, dict):
                raise ProspectusUnavailable("invalid account service response")
            return result
        except (AccountAccessDenied, ProspectusUnavailable):
            raise
        except Exception:
            raise ProspectusUnavailable("account service unavailable") from None
        finally:
            # Fixed route labels and timings only: never log actor/account IDs,
            # proof headers, OAuth values, response bodies or exception strings.
            route = "/v1/accounts/{account}/authorize" if path.startswith("/v1/accounts/") else path
            print(
                json.dumps(
                    {
                        "event": "prospectus_call",
                        "requestId": account_request_id.get(),
                        "method": method,
                        "route": route,
                        "status": status,
                        "durationMs": round((time.monotonic() - started) * 1000),
                    }
                )
            )


class AccountsClient:
    def __init__(self, transport, *, product="cyclotron"):
        self.transport = transport
        self.product = product

    def accounts(self, human_token):
        return self.transport("GET", "/v1/accounts", human_token)

    def current_person(self, human_token):
        """GET /v1/identities/me: the token holder's own Person or {"person": null}; accepts no selector."""
        return self.transport("GET", "/v1/identities/me", human_token)

    def resolve_identity(self, human_token):
        return self.transport("POST", "/v1/identities/resolve", human_token, {})

    def create_account(self, human_token, name, idempotency_key):
        identifier = UUID(idempotency_key)
        if identifier.version != 4:
            raise ValueError("account creation requires a UUIDv4 idempotency key")
        key = str(identifier)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("account name required")
        result = self.transport("POST", "/v1/accounts", human_token, {"name": name.strip()}, key)
        if not isinstance(result.get("account"), dict):
            raise ProspectusUnavailable("invalid account service response")
        return result["account"]

    def authorize(self, human_token, account_id, person_id, operation):
        if operation not in ("console:read", "console:write"):
            raise ValueError("unknown console operation")
        if not all(isinstance(value, str) and value for value in (human_token, account_id, person_id)):
            raise AccountAccessDenied("verified session and selected account required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", account_id):
            raise AccountAccessDenied("invalid account identifier")
        result = self.transport(
            "POST", f"/v1/accounts/{quote(account_id, safe='')}/authorize", human_token, {"operation": operation}
        )
        if (
            result.get("allowed") is not True
            or result.get("accountId") != account_id
            or result.get("personId") != person_id
            or result.get("product") != self.product
            or result.get("operation") != operation
            or type(result.get("revision")) is not int
            or result["revision"] < 1
            or result.get("role") not in ("owner", "admin", "member", "viewer")
            or operation == "console:write"
            and result.get("role") == "viewer"
        ):
            raise AccountAccessDenied("account access refused")
        return result


@dataclass(frozen=True)
class VerifiedAccountSession:
    """Returned only by the host's verified, server-side OAuth session resolver."""

    access_token: str = field(repr=False)
    person_id: str
    account_id: str
    session_ref: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AccountWorkspace:
    store: object
    service: object = None
    account_id: str = ""
    request_session: VerifiedAccountSession | None = field(default=None, repr=False)
    priming: object = field(default=None, repr=False)
    held_out: object = field(default=None, repr=False)


class AccountWorkspaceAccess:
    """Resolve an account-owned store only after fresh central authorization.

    resolve_session verifies issuer/signature/sub/client_id/token_use/expiry;
    it must never construct a session from email or caller-supplied identity
    headers. workspace_for_account maps a central ID to a provisioned workspace,
    never a browser-supplied SQLite path. Neither callback persists memberships.
    """

    def __init__(self, accounts, resolve_session, workspace_for_account, *, resolve_session_pending=None):
        self.accounts = accounts
        self.resolve_session = resolve_session
        self.resolve_session_pending = resolve_session_pending
        self.workspace_for_account = workspace_for_account

    @staticmethod
    def _request_scratch(connection):
        """Per-HTTP-request memory only; a long-lived websocket never gets one, so it re-proves access every time."""
        scope = getattr(connection, "scope", None)
        if not isinstance(scope, dict) or scope.get("type") != "http":
            return None
        scratch = getattr(connection.state, "_cyclotron_access_scratch", None)
        if scratch is None:
            scratch = {}
            connection.state._cyclotron_access_scratch = scratch
        return scratch

    async def _authorization_outcome(self, session, operation):
        import asyncio

        try:
            await asyncio.to_thread(
                self.accounts.authorize, session.access_token, session.account_id, session.person_id, operation
            )
        except Exception as error:
            return error
        return None

    async def _resolve(self, connection, prefetch_operation):
        """Resolve the session once per request; overlap the person check with this request's authorization."""
        import asyncio

        remembered = self._request_scratch(connection)
        memory = remembered if remembered is not None else {}
        if "session" in memory:
            return memory["session"], memory
        if self.resolve_session_pending is None:
            session = await self.resolve_session(connection)
        else:
            session, confirm_person = await self.resolve_session_pending(connection)
            if confirm_person is not None and prefetch_operation is None:
                if not await confirm_person():
                    session = None
            elif confirm_person is not None:
                confirmed, outcome = await asyncio.gather(
                    confirm_person(), self._authorization_outcome(session, prefetch_operation), return_exceptions=True
                )
                if isinstance(confirmed, BaseException):
                    raise confirmed
                if not confirmed:
                    session = None
                else:
                    memory["authorization"] = (prefetch_operation, outcome)
        memory["session"] = session
        return session, memory

    async def authenticated(self, connection):
        prefetch = "console:read" if self._request_scratch(connection) is not None else None
        session, _ = await self._resolve(connection, prefetch)
        return isinstance(session, VerifiedAccountSession)

    async def __call__(self, connection, operation):
        import asyncio

        session, memory = await self._resolve(connection, operation)
        if not isinstance(session, VerifiedAccountSession):
            raise AccountAccessDenied("verified session required")
        prefetched = memory.pop("authorization", None)
        if prefetched is not None and prefetched[0] == operation:
            if prefetched[1] is not None:
                raise prefetched[1]
        else:
            await asyncio.to_thread(
                self.accounts.authorize, session.access_token, session.account_id, session.person_id, operation
            )
        workspace = await asyncio.to_thread(self.workspace_for_account, session.account_id)
        return replace(workspace, account_id=session.account_id, request_session=session)
