"""Exercise real OAuth/BFF routes with RSA tokens and fake central transport."""
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from .account_sessions_test import identity, ISSUER, CLIENT
from .account_sessions import OAuthConfig, OAuthSessions
from .account_routes import HostedAccounts
from .prospectus_accounts import AccountAccessDenied, AccountsClient, AccountWorkspace
from .web_api import create_app
from .web_store import WebStore


@pytest.fixture
def hosted(tmp_path, identity):
    verifier, token = identity
    state = {'person': None, 'accounts': [], 'calls': [], 'exchanges': [], 'nonce': None}
    account_id = str(uuid4())
    person_id = str(uuid4())
    store = WebStore(tmp_path / 'account.sqlite')
    store.create_run('Private account run', 'recorded', {})
    state['stores'] = {account_id: store}
    def central(method, path, access_token, body=None, key=None):
        verifier.access(access_token)
        state['calls'].append((method, path, body, key))
        if method == 'GET':
            return {'person': state['person'], 'accounts': state['accounts']}
        if path == '/v1/identities/resolve':
            state['person'] = {'id': person_id}
            return {'person': state['person']}
        if path == '/v1/accounts':
            state['person'] = {'id': person_id}
            account = {'id': account_id, 'name': body['name'], 'role': 'owner',
                'entitlement': {'accountId': account_id, 'product': 'cyclotron', 'status': 'active', 'capabilities': ['console:read', 'console:write']}}
            state['accounts'] = [account]
            return {'account': account}
        selected = next((account for account in state['accounts'] if path == f'/v1/accounts/{account["id"]}/authorize'), None)
        if not selected:
            raise AccountAccessDenied('account access refused')
        return dict(allowed=True, accountId=selected['id'], personId=person_id, product='cyclotron', role=selected['role'], operation=body['operation'], revision=1)
    def exchange(request):
        state['exchanges'].append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={'access_token': token(), 'id_token': token('id', nonce=state['nonce'])})
    config = OAuthConfig(ISSUER, CLIENT, 'https://synthetic.auth.us-east-1.amazoncognito.com', 'https://console.example.test')
    gateway = HostedAccounts(config, AccountsClient(central), lambda identifier: AccountWorkspace(state['stores'][identifier]),
        verifier=verifier, token_client=httpx.AsyncClient(transport=httpx.MockTransport(exchange)))
    app = create_app(None, account_access=gateway.access)
    gateway.install(app)
    return TestClient(app, base_url=config.public_origin), gateway, state, account_id


def sign_in(client, state):
    login = client.get('/auth/login', follow_redirects=False)
    query = parse_qs(urlsplit(login.headers['location']).query)
    state['nonce'] = query['nonce'][0]
    assert query['identity_provider'] == ['Google']
    assert query['code_challenge_method'] == ['S256']
    result = client.get('/auth/callback', params={'state': query['state'][0], 'code': 'synthetic-code'}, follow_redirects=False)
    assert result.status_code == 303
    return query, result


def test_login_callback_verifies_pkce_nonce_and_sets_only_an_opaque_secure_cookie(hosted):
    client, gateway, state, _ = hosted
    with client:
        query, callback = sign_in(client, state)
        cookies = callback.headers.get_list('set-cookie')
        assert any('__Host-cyclotron-session=' in cookie and 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie for cookie in cookies)
        assert 'access_token' not in str(cookies)
        assert state['exchanges'][0]['redirect_uri'] == ['https://console.example.test/auth/callback']
        assert state['exchanges'][0]['client_id'] == [CLIENT]
        assert len(state['exchanges'][0]['code_verifier'][0]) >= 43
        assert client.get('/auth/callback', params={'state': query['state'][0], 'code': 'again'}).status_code == 400
        assert client.get('/api/session').json()['authenticated'] is True
        assert client.get('/api/session').json()['personId'] is None
        assert all(call[0] == 'GET' for call in state['calls'])  # sign-in never creates a central person/account


def test_unknown_user_creates_and_selects_a_central_account_without_browser_tokens(hosted):
    client, _, state, account_id = hosted
    with client:
        assert client.get('/api/session').json() == {'authenticated': False, 'loginUrl': '/auth/login'}
        sign_in(client, state)
        session = client.get('/api/session').json()
        key = str(uuid4())
        headers = {'Origin': str(client.base_url).rstrip('/'), 'X-CSRF-Token': session['csrfToken'], 'Idempotency-Key': key}
        response = client.post('/api/accounts', json={'name': 'My team'}, headers=headers)
        assert response.status_code == 201
        result = client.get('/api/session').json()
        assert result['selectedAccountId'] == account_id
        assert result['accounts'][0]['name'] == 'My team'
        assert result['canWrite'] is True
        assert 'access_token' not in str(result) and 'synthetic-code' not in str(result)
        creation = [call for call in state['calls'] if call[1] == '/v1/accounts' and call[0] == 'POST']
        assert creation == [('POST', '/v1/accounts', {'name': 'My team'}, key)]
        assert client.post('/graphql', json={'query': '{runs{name}}'}).status_code == 401
        assert client.post('/graphql', params={'account': account_id}, json={'query': '{runs{name}}'}).json()['data']['runs'][0]['name'] == 'Private account run'


def test_account_changes_require_exact_origin_and_csrf_and_foreign_accounts_are_denied(hosted):
    client, _, state, _ = hosted
    with client:
        sign_in(client, state)
        session = client.get('/api/session').json()
        for headers in ({}, {'Origin': 'https://evil.example.test', 'X-CSRF-Token': session['csrfToken']},
                        {'Origin': 'https://console.example.test', 'X-CSRF-Token': 'forged'}):
            assert client.post('/api/accounts', json={'name': 'Forbidden'}, headers=headers).status_code == 403
        headers = {'Origin': 'https://console.example.test', 'X-CSRF-Token': session['csrfToken']}
        assert client.post('/api/accounts/select', json={'accountId': str(uuid4())}, headers=headers).status_code == 403
        assert not any(call[0] == 'POST' and call[1] == '/v1/accounts' for call in state['calls'])


def test_logout_revokes_server_session_and_an_old_cookie_cannot_reopen_it(hosted):
    client, gateway, state, _ = hosted
    with client:
        sign_in(client, state)
        cookie = client.cookies.get(gateway.config.cookie_name)
        session = client.get('/api/session').json()
        headers = {'Origin': 'https://console.example.test', 'X-CSRF-Token': session['csrfToken']}
        assert client.post('/auth/logout', headers=headers).status_code == 200
        client.cookies.set(gateway.config.cookie_name, cookie)
        assert client.get('/api/session').json()['authenticated'] is False


def test_callback_rejects_a_different_browser_before_token_exchange(hosted):
    client, _, state, _ = hosted
    with client:
        login = client.get('/auth/login', follow_redirects=False)
        query = parse_qs(urlsplit(login.headers['location']).query)
        client.cookies.clear()
        assert client.get('/auth/callback', params={'state': query['state'][0], 'code': 'stolen'}).status_code == 400
        assert state['exchanges'] == []


def test_callback_rejects_a_signed_id_token_with_the_wrong_nonce(hosted):
    client, gateway, state, _ = hosted
    with client:
        login = client.get('/auth/login', follow_redirects=False)
        query = parse_qs(urlsplit(login.headers['location']).query)
        state['nonce'] = 'does-not-match-the-login'
        response = client.get('/auth/callback', params={'state': query['state'][0], 'code': 'synthetic'})
        assert response.status_code == 400
        assert not client.cookies.get(gateway.config.cookie_name)
        assert client.get('/api/session').json()['authenticated'] is False


def test_viewer_state_and_direct_mutations_remain_read_only(hosted):
    client, _, state, account_id = hosted
    with client:
        sign_in(client, state)
        session = client.get('/api/session').json()
        headers = {'Origin': 'https://console.example.test', 'X-CSRF-Token': session['csrfToken'], 'Idempotency-Key': str(uuid4())}
        assert client.post('/api/accounts', json={'name': 'Viewer fixture'}, headers=headers).status_code == 201
        state['accounts'][0]['role'] = 'viewer'
        assert client.get('/api/session').json()['canWrite'] is False
        mutation = 'mutation {createRun(name:"Forbidden",mode:"recorded",config:{}){id}}'
        assert client.post('/graphql', params={'account': account_id}, json={'query': mutation}).json().get('errors')
        assert len(client.post('/graphql', params={'account': account_id}, json={'query': '{runs{id}}'}).json()['data']['runs']) == 1


def test_another_tabs_selection_cannot_redirect_an_older_workspaces_writes(hosted, tmp_path):
    client, _, state, alpha_id = hosted
    with client:
        sign_in(client, state)
        session = client.get('/api/session').json()
        headers = {'Origin': 'https://console.example.test', 'X-CSRF-Token': session['csrfToken'], 'Idempotency-Key': str(uuid4())}
        assert client.post('/api/accounts', json={'name': 'Alpha'}, headers=headers).status_code == 201
        beta_id = str(uuid4())
        beta = WebStore(tmp_path / 'beta.sqlite')
        beta.create_run('Beta only', 'recorded', {})
        state['stores'][beta_id] = beta
        state['accounts'].append({'id': beta_id, 'name': 'Beta', 'role': 'owner',
            'entitlement': {'accountId': beta_id, 'product': 'cyclotron', 'status': 'active', 'capabilities': ['console:read', 'console:write']}})
        assert client.post('/api/accounts/select', json={'accountId': beta_id}, headers=headers).status_code == 200
        mutation = 'mutation {createRun(name:"Alpha delayed write",mode:"recorded",config:{}){id}}'
        assert client.post('/graphql', params={'account': alpha_id}, json={'query': mutation}).json()['data']['createRun']['id']
        assert [run['name'] for run in state['stores'][alpha_id].runs()] == ['Alpha delayed write', 'Private account run']
        assert [run['name'] for run in beta.runs()] == ['Beta only']
