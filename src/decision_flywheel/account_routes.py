"""Same-origin OAuth and accounts BFF for the existing Python console."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from urllib.parse import urlencode

from fastapi import HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import httpx

from .account_sessions import CognitoTokenVerifier, OAuthSessions, SessionRejected
from .prospectus_accounts import (AccountAccessDenied, AccountWorkspaceAccess,
    ProspectusUnavailable, VerifiedAccountSession)


class HostedAccounts:
    def __init__(self, config, accounts, workspace_for_account, *, verifier=None, sessions=None, token_client=None,
                 google_identity=None):
        self.config = config
        self.accounts = accounts
        self.google_identity = google_identity
        self.verifier = verifier or CognitoTokenVerifier(config.issuer, config.client_id)
        self.sessions = sessions or OAuthSessions(self.verifier)
        self.token_client = token_client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.owns_token_client = token_client is None
        self.access = AccountWorkspaceAccess(accounts, self.resolve_session, workspace_for_account)

    async def _session(self, connection):
        identifier = connection.cookies.get(self.config.cookie_name)
        session = await asyncio.to_thread(self.sessions.get, identifier)
        if not session:
            return None
        try:
            claims = await asyncio.to_thread(self.verifier.access, session.access_token)
            if claims['sub'] != session.subject:
                raise SessionRejected('session identity changed')
            if self.google_identity:
                person = await asyncio.to_thread(self.google_identity.me, session.access_token)
                if not person or person['id'] != session.person_id:
                    raise SessionRejected('session identity enrollment changed')
            return session
        except (SessionRejected, AccountAccessDenied):
            await asyncio.to_thread(self.sessions.revoke, identifier)
            return None

    async def resolve_session(self, connection):
        session = await self._session(connection)
        account_id = connection.query_params.get('account')
        if not session or not session.person_id or not account_id:
            return None
        return VerifiedAccountSession(session.access_token, session.person_id, account_id, session_ref=session.session_ref)

    async def _require(self, request):
        session = await self._session(request)
        if not session:
            raise HTTPException(401, 'sign-in required')
        return session

    async def _csrf(self, request):
        session = await self._require(request)
        csrf = request.headers.get('x-csrf-token', '')
        if request.headers.get('origin') != self.config.public_origin or not csrf.isascii() or not hmac.compare_digest(csrf, session.csrf_token):
            raise HTTPException(403, 'request origin or session check failed')
        return session

    async def _listing(self, session):
        result = await asyncio.to_thread(self.accounts.accounts, session.access_token)
        person = result.get('person')
        accounts = result.get('accounts')
        if not isinstance(accounts, list) or person is not None and (not isinstance(person, dict) or not isinstance(person.get('id'), str) or not person['id']):
            raise ProspectusUnavailable('invalid account service response')
        cleaned = []
        for account in accounts:
            if (not isinstance(account, dict) or not isinstance(account.get('id'), str)
                    or not isinstance(account.get('name'), str) or account.get('role') not in ('owner', 'admin', 'member', 'viewer')):
                raise ProspectusUnavailable('invalid account service response')
            entitlement = account.get('entitlement')
            if (not isinstance(entitlement, dict) or entitlement.get('product') != self.accounts.product
                    or entitlement.get('accountId') != account['id'] or entitlement.get('status') not in ('active', 'disabled')
                    or not isinstance(entitlement.get('capabilities'), list)
                    or any(value not in ('console:read', 'console:write', 'members:read', 'members:write') for value in entitlement['capabilities'])):
                raise ProspectusUnavailable('invalid account entitlement response')
            cleaned.append({'id': account['id'], 'name': account['name'], 'role': account['role'],
                'entitlement': {key: entitlement[key] for key in ('accountId', 'product', 'status', 'capabilities')}})
        if person is None and cleaned:
            raise ProspectusUnavailable('invalid account identity response')
        if self.google_identity and (person is None or person['id'] != session.person_id):
            raise AccountAccessDenied('account identity service is not aligned')
        person_id = person['id'] if person else None
        if session.person_id != person_id:
            session.person_id = person_id
            await asyncio.to_thread(self.sessions.save, session)
        return cleaned

    async def _state(self, session):
        accounts = await self._listing(session)
        can_write = False
        if session.account_id:
            try:
                await asyncio.to_thread(self.accounts.authorize, session.access_token,
                    session.account_id, session.person_id, 'console:read')
            except AccountAccessDenied:
                session.account_id = None
                await asyncio.to_thread(self.sessions.save, session)
            if session.account_id:
                try:
                    await asyncio.to_thread(self.accounts.authorize, session.access_token,
                        session.account_id, session.person_id, 'console:write')
                    can_write = True
                except AccountAccessDenied:
                    pass
        return {'authenticated': True, 'csrfToken': session.csrf_token, 'personId': session.person_id,
            'accounts': accounts, 'selectedAccountId': session.account_id, 'canWrite': can_write}

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
            return JSONResponse({'detail': 'account access refused'}, status_code=403)

        @app.exception_handler(ProspectusUnavailable)
        async def unavailable(request, error):
            return JSONResponse({'detail': 'account service unavailable; try again'}, status_code=503)

        @app.get('/auth/login')
        async def login():
            try:
                challenge = await asyncio.to_thread(self.google_identity.challenge) if self.google_identity else None
                pending = await asyncio.to_thread(self.sessions.begin, challenge)
            except SessionRejected:
                raise HTTPException(503, 'sign-in unavailable; try again') from None
            query = urlencode({'client_id': self.config.client_id, 'response_type': 'code',
                'redirect_uri': self.config.callback_url, 'scope': 'openid email profile',
                'identity_provider': 'Google', 'state': pending.state, 'nonce': pending.nonce,
                'code_challenge': pending.code_challenge, 'code_challenge_method': 'S256'})
            response = RedirectResponse(self.config.authorization_origin + '/oauth2/authorize?' + query, status_code=303)
            response.set_cookie(self.config.transaction_cookie, pending.browser_cookie,
                max_age=300, httponly=True, secure=self.config.secure, samesite='lax', path='/')
            return response

        @app.get('/auth/callback')
        async def callback(request: Request):
            try:
                pending = await asyncio.to_thread(self.sessions.consume, request.query_params.get('state'), request.cookies.get(self.config.transaction_cookie))
                code = request.query_params.get('code')
                if not code or len(code) > 2048 or request.query_params.get('error'):
                    raise SessionRejected('invalid authorization response')
                result = await self.token_client.post(self.config.authorization_origin + '/oauth2/token', data={
                    'grant_type': 'authorization_code', 'client_id': self.config.client_id,
                    'redirect_uri': self.config.callback_url, 'code': code, 'code_verifier': pending.code_verifier},
                    follow_redirects=False, timeout=10)
                if result.status_code != 200 or len(result.content) > 65536:
                    raise SessionRejected('authorization exchange failed')
                tokens = result.json()
                access_token, id_token = tokens['access_token'], tokens['id_token']
                if self.google_identity:
                    if not pending.challenge_id:
                        raise SessionRejected('identity challenge required')
                    claims = await asyncio.to_thread(self.verifier.token_pair, access_token, id_token,
                        pending.nonce, require_session_binding=True)
                    person = await asyncio.to_thread(self.google_identity.resolve, access_token, id_token, pending.challenge_id)
                else:
                    claims = await asyncio.to_thread(self.verifier.token_pair, access_token, id_token, pending.nonce)
                identifier, session = await asyncio.to_thread(self.sessions.create, access_token, claims)
                if self.google_identity:
                    session.person_id = person['id']
                    await asyncio.to_thread(self.sessions.save, session)
                await asyncio.to_thread(self.sessions.revoke, request.cookies.get(self.config.cookie_name))
                response = RedirectResponse('/', status_code=303)
                response.set_cookie(self.config.cookie_name, identifier,
                    max_age=max(0, int(session.expires_at - self.sessions.clock())),
                    httponly=True, secure=self.config.secure, samesite='lax', path='/')
            except Exception:
                response = HTMLResponse('<h1>Sign-in could not complete</h1><p>The login expired or could not be verified.</p><a href="/auth/login">Start sign-in again</a>', status_code=400)
            response.delete_cookie(self.config.transaction_cookie, path='/', secure=self.config.secure, httponly=True, samesite='lax')
            response.headers['Referrer-Policy'] = 'no-referrer'
            return response

        @app.get('/api/session')
        async def state(request: Request):
            session = await self._session(request)
            if not session:
                return {'authenticated': False, 'loginUrl': '/auth/login'}
            return await self._state(session)

        @app.get('/api/runs/{run_id}/events')
        async def event_page(request: Request, run_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
            await self._require(request)
            workspace = await self.access(request, 'console:read')
            try:
                rows = await asyncio.to_thread(workspace.store.events, run_id, after=after, limit=limit)
            except ValueError:
                raise HTTPException(404, 'unknown run') from None
            events = []
            length = 16
            for row in rows:
                event = {'sequence': row['sequence'], 'sourceId': row['source_id'], 'payload': row['payload']}
                size = len(json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()) + 1
                if length + size > 1048576:
                    if not events:
                        raise HTTPException(413, 'event exceeds page budget; operator review required')
                    break
                events.append(event)
                length += size
            return JSONResponse({'events': events}, headers={'Cache-Control': 'private, no-store'})

        @app.post('/api/accounts/select')
        async def select(request: Request):
            session = await self._csrf(request)
            try:
                body = await request.json()
                account_id = body['accountId']
            except (ValueError, KeyError, TypeError):
                raise HTTPException(400, 'account identifier required') from None
            await self._listing(session)
            await asyncio.to_thread(self.accounts.authorize, session.access_token,
                account_id, session.person_id, 'console:read')
            session.account_id = account_id
            await asyncio.to_thread(self.sessions.save, session)
            return {'selectedAccountId': account_id}

        @app.post('/api/accounts')
        async def create(request: Request):
            session = await self._csrf(request)
            if self.google_identity:
                # Isolated identity and legacy account stores are not assumed
                # unified: prove their person mapping before any account write.
                await self._listing(session)
            try:
                body = await request.json()
                account = await asyncio.to_thread(self.accounts.create_account, session.access_token,
                    body['name'], request.headers.get('idempotency-key', ''))
            except (ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(400, 'account name and UUID idempotency key required') from None
            if not isinstance(account.get('id'), str):
                raise ProspectusUnavailable('invalid account service response')
            await self._listing(session)
            await asyncio.to_thread(self.accounts.authorize, session.access_token,
                account['id'], session.person_id, 'console:read')
            session.account_id = account['id']
            await asyncio.to_thread(self.sessions.save, session)
            return JSONResponse({'selectedAccountId': session.account_id}, status_code=201)

        @app.post('/auth/logout')
        async def logout(request: Request):
            await self._csrf(request)
            await asyncio.to_thread(self.sessions.revoke, request.cookies.get(self.config.cookie_name))
            response = JSONResponse({'authenticated': False})
            response.delete_cookie(self.config.cookie_name, path='/', secure=self.config.secure, httponly=True, samesite='lax')
            return response
