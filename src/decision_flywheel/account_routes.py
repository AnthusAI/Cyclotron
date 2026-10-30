"""Same-origin OAuth and accounts BFF for the existing Python console."""

import asyncio
import hashlib
import hmac
import json
import math
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .account_sessions import CognitoTokenVerifier, OAuthSessions, SessionRejected
from .prospectus_accounts import (
    AccountAccessDenied,
    AccountWorkspaceAccess,
    ProspectusUnavailable,
    VerifiedAccountSession,
    account_request_id,
)


class AccountIdentityChanged(AccountAccessDenied):
    """Fresh account listing no longer proves the original enrolled person."""


def bearer_token(connection):
    """The CLI's access token from ``Authorization: Bearer``, or None when the request carries none."""
    header = connection.headers.get("authorization")
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or not token.isascii() or " " in token.strip():
        return ""
    return token.strip()


def bearer_identifier(token):
    # A fixed identifier per token, so every request with the same token finds the same server-side session.
    return "cli." + hashlib.sha256(token.encode()).hexdigest()


class HostedAccounts:
    def __init__(
        self, config, accounts, workspace_for_account, *, verifier=None, sessions=None, token_client=None, google_identity=None
    ):
        self.config = config
        self.accounts = accounts
        self.google_identity = google_identity
        self.verifier = verifier or CognitoTokenVerifier(config.issuer, config.client_id)
        self.sessions = sessions or OAuthSessions(self.verifier)
        self.token_client = token_client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.owns_token_client = token_client is None
        self.access = AccountWorkspaceAccess(
            accounts, self.resolve_session, workspace_for_account, resolve_session_pending=self.resolve_session_pending
        )

    async def _timed(self, phase, operation, *args):
        started = time.monotonic()
        try:
            return await asyncio.to_thread(operation, *args)
        finally:
            print(
                json.dumps(
                    {
                        "event": "console_phase",
                        "requestId": account_request_id.get(),
                        "phase": phase,
                        "durationMs": round((time.monotonic() - started) * 1000),
                    }
                )
            )

    async def _session_pending(self, connection, *, verify_person=True):
        """Return the locally proven session and the still-owed person check, so a caller can overlap it with other work.

        A CLI sends its own access token as ``Authorization: Bearer``. It is checked exactly like a browser
        session's token and kept as a server-side session under a fixed identifier, so queued work re-checks it the
        same way. A request may carry a cookie or a bearer token, never both.
        """
        token = bearer_token(connection)
        if token is not None:
            if not token or connection.cookies.get(self.config.cookie_name):
                return None, None
            identifier = bearer_identifier(token)
            session = await self._bearer_session(identifier, token)
        else:
            identifier = connection.cookies.get(self.config.cookie_name)
            session = await self._timed("session_read", self.sessions.get, identifier)
        if not session:
            return None, None
        try:
            claims = await self._timed("token_verify", self.verifier.access, session.access_token)
            if claims["sub"] != session.subject:
                raise SessionRejected("session identity changed")
            if self.google_identity:
                auth_time, lineage = claims.get("auth_time"), claims.get("origin_jti")
                if (
                    type(auth_time) not in (int, float)
                    or not math.isfinite(auth_time)
                    or auth_time > self.sessions.clock() + 30
                    or not isinstance(lineage, str)
                    or not lineage
                    or not lineage.isascii()
                ):
                    raise SessionRejected("session binding is invalid")
        except (SessionRejected, AccountAccessDenied):
            await asyncio.to_thread(self.sessions.revoke, identifier)
            return None, None
        if not (self.google_identity and verify_person):
            return session, None

        async def confirm_person():
            try:
                person = await asyncio.to_thread(self.google_identity.me, session.access_token)
                if not person or person["id"] != session.person_id:
                    raise SessionRejected("session identity enrollment changed")
            except (SessionRejected, AccountAccessDenied):
                await asyncio.to_thread(self.sessions.revoke, identifier)
                return False
            return True

        return session, confirm_person

    async def _bearer_session(self, identifier, token):
        """The server-side session for a CLI's access token. A new token must belong to a person already enrolled
        through a browser sign-in: the CLI cannot enroll anyone, because enrollment needs the signed identity
        challenge."""
        try:
            claims = await self._timed("token_verify", self.verifier.access, token)
            session = await self._timed("session_read", self.sessions.get, identifier)
            if session is not None:
                return session if session.access_token == token and session.subject == claims["sub"] else None
            person = None
            if self.google_identity:
                person = await asyncio.to_thread(self.google_identity.me, token)
                if not person:
                    return None
            session = await asyncio.to_thread(self.sessions.adopt, identifier, token, claims)
            if session.access_token != token or session.subject != claims["sub"]:
                return None
            if person is not None and session.person_id != person["id"]:
                if session.person_id is not None:
                    return None
                session.person_id = person["id"]
                await asyncio.to_thread(self.sessions.save, session)
            return session
        except (SessionRejected, AccountAccessDenied):
            return None

    async def _session(self, connection, *, verify_person=True):
        session, confirm_person = await self._session_pending(connection, verify_person=verify_person)
        if confirm_person is not None and not await confirm_person():
            return None
        return session

    async def resolve_session_pending(self, connection):
        session, confirm_person = await self._session_pending(connection)
        account_id = connection.query_params.get("account")
        if not session or not session.person_id or not account_id:
            if confirm_person is not None:
                await confirm_person()
            return None, None
        verified = VerifiedAccountSession(session.access_token, session.person_id, account_id, session_ref=session.session_ref)
        return verified, confirm_person

    async def resolve_session(self, connection):
        verified, confirm_person = await self.resolve_session_pending(connection)
        if confirm_person is not None and not await confirm_person():
            return None
        return verified

    async def _require(self, request, *, verify_person=True):
        session = await self._session(request, verify_person=verify_person)
        if not session:
            raise HTTPException(401, "sign-in required")
        return session

    async def _csrf(self, request, *, verify_person=True):
        session = await self._require(request, verify_person=verify_person)
        if bearer_token(request):
            # A bearer request carries no ambient credential: a cross-site page cannot make a browser send it.
            return session
        csrf = request.headers.get("x-csrf-token", "")
        if (
            request.headers.get("origin") != self.config.public_origin
            or not csrf.isascii()
            or not hmac.compare_digest(csrf, session.csrf_token)
        ):
            raise HTTPException(403, "request origin or session check failed")
        return session

    async def _listing(self, session):
        result = await asyncio.to_thread(self.accounts.accounts, session.access_token)
        person = result.get("person")
        accounts = result.get("accounts")
        if (
            not isinstance(accounts, list)
            or person is not None
            and (not isinstance(person, dict) or not isinstance(person.get("id"), str) or not person["id"])
        ):
            raise ProspectusUnavailable("invalid account service response")
        cleaned = []
        for account in accounts:
            if (
                not isinstance(account, dict)
                or not isinstance(account.get("id"), str)
                or not isinstance(account.get("name"), str)
                or account.get("role") not in ("owner", "admin", "member", "viewer")
            ):
                raise ProspectusUnavailable("invalid account service response")
            entitlement = account.get("entitlement")
            if (
                not isinstance(entitlement, dict)
                or entitlement.get("product") != self.accounts.product
                or entitlement.get("accountId") != account["id"]
                or entitlement.get("status") not in ("active", "disabled")
                or not isinstance(entitlement.get("capabilities"), list)
                or any(
                    value not in ("console:read", "console:write", "members:read", "members:write")
                    for value in entitlement["capabilities"]
                )
            ):
                raise ProspectusUnavailable("invalid account entitlement response")
            cleaned.append(
                {
                    "id": account["id"],
                    "name": account["name"],
                    "role": account["role"],
                    "entitlement": {key: entitlement[key] for key in ("accountId", "product", "status", "capabilities")},
                }
            )
        if person is None and cleaned:
            raise ProspectusUnavailable("invalid account identity response")
        if self.google_identity and (person is None or person["id"] != session.person_id):
            raise AccountIdentityChanged("account identity service is not aligned")
        person_id = person["id"] if person else None
        if session.person_id != person_id:
            session.person_id = person_id
            await asyncio.to_thread(self.sessions.save, session)
        return cleaned

    async def _state(self, session, *, require_selected=False):
        async def permission(operation):
            try:
                await asyncio.to_thread(
                    self.accounts.authorize, session.access_token, session.account_id, session.person_id, operation
                )
                return True
            except AccountAccessDenied:
                return False

        # These are independent authority reads, not a cached authorization lease.
        # Await all three before returning any account state. Actual workspace
        # operations still reauthorize at their execution boundary.
        if session.account_id and session.person_id:
            accounts, can_read, can_write = await asyncio.gather(
                self._listing(session), permission("console:read"), permission("console:write")
            )
        else:
            accounts = await self._listing(session)
            can_read, can_write = (
                await asyncio.gather(permission("console:read"), permission("console:write"))
                if session.account_id
                else (False, False)
            )
        if session.account_id and not can_read:
            if require_selected:
                raise AccountAccessDenied("account access refused")
            else:
                session.account_id = None
                await asyncio.to_thread(self.sessions.save, session)
            can_write = False
        return {
            "authenticated": True,
            "csrfToken": session.csrf_token,
            "personId": session.person_id,
            "accounts": accounts,
            "selectedAccountId": session.account_id,
            "canWrite": can_write,
        }

    def install(self, app):
        previous_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application):
            async with previous_lifespan(application):
                try:
                    yield
                finally:
                    if self.owns_token_client:
                        await self.token_client.aclose()

        app.router.lifespan_context = lifespan

        @app.exception_handler(SessionRejected)
        async def stale_session(request, error):
            return JSONResponse({"detail": "session changed; refresh authentication"}, status_code=401)

        @app.exception_handler(AccountAccessDenied)
        async def denied(request, error):
            return JSONResponse({"detail": "account access refused"}, status_code=403)

        @app.exception_handler(ProspectusUnavailable)
        async def unavailable(request, error):
            return JSONResponse({"detail": "account service unavailable; try again"}, status_code=503)

        @app.get("/auth/login")
        async def login():
            try:
                challenge = await asyncio.to_thread(self.google_identity.challenge) if self.google_identity else None
                pending = await asyncio.to_thread(self.sessions.begin, challenge)
            except SessionRejected:
                raise HTTPException(503, "sign-in unavailable; try again") from None
            query = urlencode(
                {
                    "client_id": self.config.client_id,
                    "response_type": "code",
                    "redirect_uri": self.config.callback_url,
                    "scope": "openid email profile",
                    "identity_provider": "Google",
                    "state": pending.state,
                    "nonce": pending.nonce,
                    "code_challenge": pending.code_challenge,
                    "code_challenge_method": "S256",
                }
            )
            response = RedirectResponse(self.config.authorization_origin + "/oauth2/authorize?" + query, status_code=303)
            response.set_cookie(
                self.config.transaction_cookie,
                pending.browser_cookie,
                max_age=300,
                httponly=True,
                secure=self.config.secure,
                samesite="lax",
                path="/",
            )
            return response

        @app.get("/auth/callback")
        async def callback(request: Request):
            try:
                pending = await asyncio.to_thread(
                    self.sessions.consume, request.query_params.get("state"), request.cookies.get(self.config.transaction_cookie)
                )
                code = request.query_params.get("code")
                if not code or request.query_params.get("error"):
                    raise SessionRejected("invalid authorization response")
                result = await self.token_client.post(
                    self.config.authorization_origin + "/oauth2/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": self.config.client_id,
                        "redirect_uri": self.config.callback_url,
                        "code": code,
                        "code_verifier": pending.code_verifier,
                    },
                    follow_redirects=False,
                    timeout=10,
                )
                if result.status_code != 200:
                    raise SessionRejected("authorization exchange failed")
                tokens = result.json()
                access_token, id_token = tokens["access_token"], tokens["id_token"]
                if self.google_identity:
                    if not pending.challenge_id:
                        raise SessionRejected("identity challenge required")
                    claims = await asyncio.to_thread(
                        self.verifier.token_pair, access_token, id_token, pending.nonce, require_session_binding=True
                    )
                    person = await asyncio.to_thread(
                        self.google_identity.resolve,
                        access_token,
                        id_token,
                        pending.challenge_id,
                        challenge_token=pending.challenge_token,
                    )
                else:
                    claims = await asyncio.to_thread(self.verifier.token_pair, access_token, id_token, pending.nonce)
                identifier, session = await asyncio.to_thread(self.sessions.create, access_token, claims)
                if self.google_identity:
                    session.person_id = person["id"]
                    await asyncio.to_thread(self.sessions.save, session)
                await asyncio.to_thread(self.sessions.revoke, request.cookies.get(self.config.cookie_name))
                response = RedirectResponse("/", status_code=303)
                response.set_cookie(
                    self.config.cookie_name,
                    identifier,
                    max_age=max(0, int(session.expires_at - self.sessions.clock())),
                    httponly=True,
                    secure=self.config.secure,
                    samesite="lax",
                    path="/",
                )
            except Exception:
                response = HTMLResponse(
                    '<h1>Sign-in could not complete</h1><p>The login expired or could not be verified.</p><a href="/auth/login">Start sign-in again</a>',
                    status_code=400,
                )
            response.delete_cookie(
                self.config.transaction_cookie, path="/", secure=self.config.secure, httponly=True, samesite="lax"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        @app.get("/api/session")
        async def state(request: Request):
            session = await self._session(request, verify_person=False)
            if not session:
                return {"authenticated": False, "loginUrl": "/auth/login"}
            try:
                # Central listing itself verifies the current enrolled session
                # family and person. Never accept null/mismatch or a cached grant.
                return await self._state(session)
            except AccountIdentityChanged:
                await asyncio.to_thread(self.sessions.revoke, request.cookies.get(self.config.cookie_name))
                return {"authenticated": False, "loginUrl": "/auth/login"}

        @app.get("/api/runs/{run_id}/events")
        async def event_page(request: Request, run_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1)):
            if not await self.access.authenticated(request):
                raise HTTPException(401, "sign-in required")
            workspace = await self.access(request, "console:read")
            rows, cursor = [], after
            # Any page size: read the store's own pages until the requested count or the end of the run.
            while len(rows) < limit:
                try:
                    page = await asyncio.to_thread(workspace.store.events, run_id, after=cursor)
                except ValueError:
                    raise HTTPException(404, "unknown run") from None
                if not page:
                    break
                rows.extend(page[: limit - len(rows)])
                cursor = page[-1]["sequence"]
            events = [{"sequence": row["sequence"], "sourceId": row["source_id"], "payload": row["payload"]} for row in rows]
            return JSONResponse({"events": events}, headers={"Cache-Control": "private, no-store"})

        @app.post("/api/accounts/select")
        async def select(request: Request):
            session = await self._csrf(request, verify_person=False)
            try:
                body = await request.json()
                account_id = body["accountId"]
            except (ValueError, KeyError, TypeError):
                raise HTTPException(400, "account identifier required") from None
            if not session.person_id:
                await self._listing(session)
            selected = replace(session, account_id=account_id)
            try:
                result = await self._state(selected, require_selected=True)
            except AccountIdentityChanged:
                await asyncio.to_thread(self.sessions.revoke, request.cookies.get(self.config.cookie_name))
                raise SessionRejected("session identity enrollment changed") from None
            session.account_id = selected.account_id
            await self._timed("session_write", self.sessions.save, session)
            return result

        @app.post("/api/accounts")
        async def create(request: Request):
            session = await self._csrf(request)
            if self.google_identity:
                # Isolated identity and legacy account stores are not assumed
                # unified: prove their person mapping before any account write.
                await self._listing(session)
            try:
                body = await request.json()
                account = await asyncio.to_thread(
                    self.accounts.create_account, session.access_token, body["name"], request.headers.get("idempotency-key", "")
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(400, "account name and UUID idempotency key required") from None
            if not isinstance(account.get("id"), str):
                raise ProspectusUnavailable("invalid account service response")
            await self._listing(session)
            await asyncio.to_thread(
                self.accounts.authorize, session.access_token, account["id"], session.person_id, "console:read"
            )
            session.account_id = account["id"]
            await asyncio.to_thread(self.sessions.save, session)
            return JSONResponse({"selectedAccountId": session.account_id}, status_code=201)

        @app.post("/auth/logout")
        async def logout(request: Request):
            await self._csrf(request)
            await asyncio.to_thread(self.sessions.revoke, request.cookies.get(self.config.cookie_name))
            response = JSONResponse({"authenticated": False})
            response.delete_cookie(self.config.cookie_name, path="/", secure=self.config.secure, httponly=True, samesite="lax")
            return response
