"""Synthetic account boundaries: no network, real credentials, or model calls."""
from types import SimpleNamespace
from uuid import uuid4

from botocore.credentials import Credentials
from fastapi.testclient import TestClient
import httpx
import pytest

from .prospectus_accounts import (AccountAccessDenied, AccountWorkspace,
    AccountWorkspaceAccess, AccountsClient, ProspectusUnavailable, SigV4Transport,
    VerifiedAccountSession)
from .web_api import create_app
from .web_store import WebStore


def test_signing_uses_fresh_temporary_credentials_and_forwards_only_the_human_token():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={'accounts': []})
    session = SimpleNamespace(get_credentials=lambda: Credentials('synthetic', 'fake-secret', 'temporary'))
    transport = SigV4Transport('https://accounts.example.test/api', 'us-east-1', session=session,
        client=httpx.Client(transport=httpx.MockTransport(respond)))
    AccountsClient(transport).accounts('verified-access-jwt')
    assert str(seen[0].url) == 'https://accounts.example.test/api/v1/accounts'
    assert seen[0].headers['authorization'].startswith('AWS4-HMAC-SHA256 ')
    assert 'x-prospectus-human-token' in seen[0].headers['authorization']
    assert seen[0].headers['x-amz-security-token'] == 'temporary'
    assert seen[0].headers['x-prospectus-human-token'] == 'verified-access-jwt'
    assert 'x-prospectus-person' not in seen[0].headers
    session.get_credentials = lambda: Credentials('rotated', 'fake-secret', 'next-temporary')
    AccountsClient(transport).accounts('verified-access-jwt')
    assert 'Credential=rotated/' in seen[1].headers['authorization']
    assert seen[1].headers['x-amz-security-token'] == 'next-temporary'


def test_long_lived_credentials_and_redirects_fail_closed_without_exposing_tokens():
    session = SimpleNamespace(get_credentials=lambda: Credentials('synthetic', 'fake-secret'))
    transport = SigV4Transport('https://accounts.example.test', 'us-east-1', session=session)
    with pytest.raises(ProspectusUnavailable, match='temporary runtime credentials required'):
        AccountsClient(transport).accounts('private-jwt')
    session.get_credentials = lambda: Credentials('synthetic', 'fake-secret', 'temporary')
    transport.client = httpx.Client(follow_redirects=True, transport=httpx.MockTransport(lambda _: httpx.Response(302,
        headers={'Location': 'https://untrusted.example.test'})))
    with pytest.raises(ProspectusUnavailable, match='account service unavailable'):
        AccountsClient(transport).accounts('private-jwt')


def test_account_creation_preserves_the_callers_idempotency_key():
    calls = []
    client = AccountsClient(lambda *args: calls.append(args) or {'account': {'id': 'account-a'}})
    key = str(uuid4())
    for _ in range(2):
        assert client.create_account('verified', ' Example ', key) == {'id': 'account-a'}
    assert calls[0] == calls[1] == ('POST', '/v1/accounts', 'verified', {'name': 'Example'}, key)
    with pytest.raises(ValueError):
        client.create_account('verified', 'Example', 'not-a-uuid')


def test_unknown_people_can_resolve_their_verified_identity_explicitly():
    calls = []
    client = AccountsClient(lambda *args: calls.append(args) or {'person': {'id': 'person'}})
    assert client.resolve_identity('verified')['person']['id'] == 'person'
    assert calls == [('POST', '/v1/identities/resolve', 'verified', {})]


@pytest.mark.parametrize('change', [dict(allowed=False), dict(accountId='other'),
    dict(personId='other'), dict(product='other'), dict(role='unknown'), dict(operation='console:write'), dict(revision=0)])
def test_an_authorization_response_is_bound_to_person_account_and_product(change):
    result = dict(allowed=True, accountId='account-a', personId='person-a', product='cyclotron', role='member', operation='console:read', revision=1)
    client = AccountsClient(lambda *args: result | change)
    with pytest.raises(AccountAccessDenied):
        client.authorize('verified', 'account-a', 'person-a', 'console:read')


def test_viewer_cannot_write_even_if_a_malformed_central_response_allows_it():
    client = AccountsClient(lambda *args: dict(allowed=True, accountId='a', personId='p', product='cyclotron', role='viewer', operation='console:write', revision=1))
    with pytest.raises(AccountAccessDenied):
        client.authorize('verified', 'a', 'p', 'console:write')


@pytest.mark.parametrize('identifier', ['..', '../other', 'a/b', '%2F', 'a\\b'])
def test_account_ids_cannot_change_the_signed_route(identifier):
    def never(*args):
        pytest.fail('invalid account ID reached the transport')
    with pytest.raises(AccountAccessDenied):
        AccountsClient(never).authorize('verified', identifier, 'person', 'console:read')


def hosted_client(tmp_path):
    stores = {name: WebStore(tmp_path / f'{name}.sqlite') for name in ('a', 'b')}
    for name, store in stores.items():
        store.create_run(name=f'Only {name}', mode='recorded', config={})
    operations = []
    def transport(method, path, token, body=None):
        operations.append(body['operation'])
        account = path.split('/')[3]
        if token == 'revoked' or account not in stores:
            raise AccountAccessDenied('account access refused')
        return dict(allowed=True, accountId=account, personId='person', product='cyclotron', role='viewer' if token == 'viewer' else 'member', operation=body['operation'], revision=1)
    async def session(connection):
        token = connection.cookies.get('synthetic-session')
        if not token:
            return None
        return VerifiedAccountSession(token, 'person', connection.cookies.get('selected-account', 'a'))
    access = AccountWorkspaceAccess(AccountsClient(transport), session,
        lambda account: AccountWorkspace(stores[account]))
    app = create_app(WebStore(tmp_path / 'unscoped.sqlite'), account_access=access)
    return TestClient(app), stores, operations


def test_hosted_requests_switch_accounts_without_leaking_the_unscoped_store(tmp_path):
    client, stores, operations = hosted_client(tmp_path)
    with client:
        assert client.post('/graphql', json={'query': '{runs{id}}'}).status_code == 401
        assert client.post('/graphql', json={'query': '{runs{id}}'}, headers={
            'X-Prospectus-Human-Token': 'forged', 'X-Prospectus-Person': 'person',
            'X-Prospectus-Role': 'owner'}).status_code == 401
        client.cookies.set('synthetic-session', 'member')
        for account in ('a', 'b', 'a'):
            client.cookies.set('selected-account', account)
            result = client.post('/graphql', json={'query': '{runs{id name}}'}).json()
            assert [r['name'] for r in result['data']['runs']] == [f'Only {account}']
        foreign_id = stores['b'].runs()[0]['id']
        result = client.post('/graphql', json={'query': 'query($id:ID!){run(runId:$id){name}}', 'variables': {'id': foreign_id}}).json()
        assert result.get('errors') and 'Only b' not in str(result)
        client.cookies.set('selected-account', 'missing')
        result = client.post('/graphql', json={'query': '{runs{id name}}'}).json()
        assert result.get('errors') and not result.get('data')
    assert operations == ['console:read'] * 5


def test_viewer_mutations_and_revoked_sessions_fail_before_touching_the_store(tmp_path):
    client, stores, operations = hosted_client(tmp_path)
    with client:
        client.cookies.set('synthetic-session', 'viewer')
        result = client.post('/graphql', json={'query': 'mutation {createRun(name:"Forbidden",mode:"recorded",config:{}){id}}'}).json()
        assert result.get('errors')
        assert len(stores['a'].runs()) == 1
        client.cookies.set('synthetic-session', 'revoked')
        assert client.post('/graphql', json={'query': '{runs{id}}'}).json().get('errors')
        assert client.post('/session', json={'token': 'anything'}).status_code == 403
    assert operations == ['console:write', 'console:read']


def test_timeline_cannot_read_a_run_from_a_different_account(tmp_path):
    client, stores, operations = hosted_client(tmp_path)
    foreign_run = stores['b'].runs()[0]['id']
    with client:
        client.cookies.set('synthetic-session', 'member')
        response = client.get(f'/runs/{foreign_run}/timeline')
        assert response.status_code == 404
        assert 'Only b' not in response.text
    assert operations == ['console:read']


def test_websocket_operations_reauthorize_and_cannot_bypass_viewer_permissions(tmp_path):
    client, stores, operations = hosted_client(tmp_path)
    with client:
        client.cookies.set('synthetic-session', 'viewer')
        with client.websocket_connect('/graphql', subprotocols=['graphql-transport-ws']) as socket:
            socket.send_json({'type': 'connection_init'})
            assert socket.receive_json()['type'] == 'connection_ack'
            socket.send_json({'type': 'subscribe', 'id': 'read', 'payload': {'query': '{runs{name}}'}})
            message = socket.receive_json()
            assert message['payload']['data']['runs'][0]['name'] == 'Only a'
            assert socket.receive_json()['type'] == 'complete'
            socket.send_json({'type': 'subscribe', 'id': 'write', 'payload': {
                'query': 'mutation {createRun(name:"Forbidden",mode:"recorded",config:{}){id}}'}})
            message = socket.receive_json()
            assert message['type'] == 'error' or message['payload'].get('errors')
            assert len(stores['a'].runs()) == 1
    assert operations == ['console:read', 'console:write']


def test_subscription_reads_only_authorized_account_events(tmp_path):
    client, stores, operations = hosted_client(tmp_path)
    run_id = stores['a'].runs()[0]['id']
    stores['a'].append_event(run_id, 'fixture', {'kind': 'prediction', 'content': 'Alpha only'})
    with client:
        client.cookies.set('synthetic-session', 'viewer')
        with client.websocket_connect('/graphql', subprotocols=['graphql-transport-ws']) as socket:
            socket.send_json({'type': 'connection_init'})
            assert socket.receive_json()['type'] == 'connection_ack'
            socket.send_json({'type': 'subscribe', 'id': 'events', 'payload': {
                'query': 'subscription($id:ID!){runEvents(runId:$id){payload}}', 'variables': {'id': run_id}}})
            message = socket.receive_json()
            assert message['payload']['data']['runEvents']['payload']['content'] == 'Alpha only'
            socket.send_json({'type': 'complete', 'id': 'events'})
    assert operations == ['console:read', 'console:read']


def test_hosted_mode_cannot_mix_shared_token_lan_or_global_worker_authority(tmp_path):
    for options in ({'token': 'shared'}, {'allow_unauthenticated_lan': True}, {'service': object()}):
        with pytest.raises(ValueError, match='hosted account access cannot use'):
            create_app(WebStore(tmp_path / 'unused.sqlite'), account_access=object(), **options)


def test_transport_timings_correlate_fixed_routes_without_logging_any_proof_or_account(capsys):
    import json
    from .prospectus_accounts import account_request_id
    session = SimpleNamespace(get_credentials=lambda: Credentials('synthetic', 'private-secret', 'private-runtime-token'))
    transport = SigV4Transport('https://accounts.example.test/api', 'us-east-1', session=session,
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'private': 'response-body'}))))
    correlation = account_request_id.set('synthetic-request')
    try:
        transport('POST', '/v1/accounts/private-account/authorize', 'private-human-token', {'operation': 'console:read'})
    finally:
        account_request_id.reset(correlation)
    output = capsys.readouterr().out
    entry = json.loads(output)
    assert entry['requestId'] == 'synthetic-request'
    assert entry['route'] == '/v1/accounts/{account}/authorize'
    assert entry['status'] == 200 and entry['durationMs'] >= 0
    assert all(value not in output for value in ('private-secret', 'private-runtime-token', 'private-human-token', 'private-account', 'response-body'))
