"""Exercise the real Python BFF against Prospectus's isolated identity factory.

Usage: PYTHONPATH=src python scripts/verify_google_identity_contract.py PATH_TO_PROSPECTUS [--filesystem [--accounts-handler | --durable-accounts]]
Requires the optional accounts/web Python dependencies, Node and that checkout's
locked tsx installation. Uses synthetic RSA keys, disposable memory/filesystem
storage, mock HTTP and a JSON-lines child only. Filesystem mode also needs the
central checkout's built native Virtuus bridge. Gateway IAM context is a fixture,
not deployed IAM proof. --accounts-handler selects real logic over memory accounts;
--durable-accounts instead uses the actual canonical account filesystem service.
"""
import argparse
from contextlib import ExitStack
import json
import logging
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from botocore.credentials import Credentials
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt

from decision_flywheel.account_routes import HostedAccounts
from decision_flywheel.account_sessions import CognitoTokenVerifier, OAuthConfig
from decision_flywheel.google_identity import GoogleIdentityClient
from decision_flywheel.prospectus_accounts import AccountAccessDenied, AccountsClient, AccountWorkspace, SigV4Transport
from decision_flywheel.web_api import create_app
from decision_flywheel.web_store import WebStore


class Harness:
    def __init__(self, repository):
        self.repository = repository
        self.child = subprocess.Popen([shutil.which('node') or 'node', '--import', 'tsx',
            'accounts/testing/google-harness.ts'], cwd=repository,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def call(self, message):
        self.child.stdin.write(json.dumps(message) + '\n')
        self.child.stdin.flush()
        if not select.select([self.child.stdout], [], [], 15)[0]:
            raise RuntimeError('Synthetic identity harness did not respond')
        response = json.loads(self.child.stdout.readline())
        if 'harnessError' in response:
            raise RuntimeError('Synthetic identity harness rejected input')
        return response

    def close(self):
        self.child.terminate()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()
        for pipe in (self.child.stdin, self.child.stdout, self.child.stderr):
            pipe.close()

    def restart(self, configuration):
        self.close()
        self.__init__(self.repository)
        assert self.call(configuration) == {'configured': True}


def verify(repository, *, filesystem=False, accounts_handler=False, durable_accounts=False):
    real_accounts = accounts_handler or durable_accounts
    now = int(time.time())
    products, private_keys, public_keys = [], {}, {}
    for index, product in enumerate(('cyclotron', 'papyrus')):
        issuer = f'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Synthetic{index}'
        client = f'synthetic{index}'
        products.append(dict(product=product, roleArn=f'arn:aws:iam::111111111111:role/Synthetic{index}',
            issuer=issuer, clientIds=[client], capabilities=['console:read', 'console:write'] + (
                ['members:read', 'members:write'] if durable_accounts else []), enabled=True,
            googleFederation=dict(policy='google-only-unlinked-v1', reviewedPolicyId='synthetic-review-v1', clientId=client)))
        public_keys[issuer] = {}
        for purpose in ('access', 'id'):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            private_keys[index, purpose] = key
            public_keys[issuer][purpose] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {
                'kid': purpose, 'alg': 'RS256', 'use': 'sig'}

    def pair(index, nonce, *, access_changes=None, id_changes=None):
        product = products[index]
        common = dict(iss=product['issuer'], sub=f'different-pool-subject-{index}', auth_time=now,
            origin_jti=f'synthetic-family-{index}', iat=now, exp=now + 1800)
        access = common | dict(token_use='access', client_id=product['clientIds'][0], jti=str(uuid4())) | (access_changes or {})
        identity = common | dict(token_use='id', aud=product['clientIds'][0], jti=str(uuid4()), nonce=nonce,
            email='irrelevant@example.invalid', identities=[dict(providerName='Google', providerType='Google',
            userId='same-global-google-subject')]) | (id_changes or {})
        return {purpose: jwt.encode(claims, private_keys[index, purpose], algorithm='RS256', headers={'kid': purpose})
            for purpose, claims in (('access', access), ('id', identity))}

    def event(index, method, path, headers, body):
        return dict(httpMethod=method, path=path, headers=dict(headers), body=body,
            requestContext=dict(identity=dict(userArn=f'arn:aws:sts::111111111111:assumed-role/Synthetic{index}/test',
                accountId='111111111111')))

    identity_directory = tempfile.TemporaryDirectory(prefix='cyclotron-google-identity-')
    configuration = dict(op='configure', nowSeconds=now, products=products, keys=public_keys)
    if filesystem:
        configuration['filesystemRoot'] = str(Path(identity_directory.name).resolve())
    if accounts_handler:
        configuration['accountsFixture'] = True
    if durable_accounts:
        configuration['durableAccounts'] = True
    harness = Harness(repository)
    try:
        assert harness.call(configuration) == {'configured': True}
        with tempfile.TemporaryDirectory(prefix='cyclotron-google-contract-') as directory:
            accounts, stores, resolutions = {}, {}, []
            def bff(index):
                product = products[index]
                state = {}
                def central(request):
                    request_event = event(index, request.method, request.url.path, request.headers, request.content.decode())
                    if request.url.path.endswith('/resolve'):
                        resolutions.append(request_event)
                    result = harness.call(dict(op='request', event=request_event))
                    return httpx.Response(result['statusCode'], headers=result['headers'], content=result['body'])
                signed = SigV4Transport('https://synthetic-identity.example.invalid', 'us-east-1',
                    session=SimpleNamespace(get_credentials=lambda: Credentials('synthetic', 'fake', 'temporary')),
                    client=httpx.Client(transport=httpx.MockTransport(central)))
                identity = GoogleIdentityClient(signed)
                def account_transport(method, path, access_token, body=None, key=None):
                    person = identity.me(access_token)
                    if not person:
                        raise AccountAccessDenied('synthetic account membership missing')
                    summary = accounts.get(person['id'])
                    if method == 'GET':
                        return {'person': person, 'accounts': [summary | {'entitlement': summary['entitlement'] | {
                            'product': product['product']}}] if summary else []}
                    if path == '/v1/accounts':
                        account_id = str(uuid4())
                        summary = dict(id=account_id, name=body['name'], role='owner', entitlement=dict(accountId=account_id,
                            product=product['product'], status='active', capabilities=['console:read', 'console:write']))
                        accounts[person['id']] = summary
                        stores[account_id] = WebStore(Path(directory) / (account_id + '.sqlite'))
                        stores[account_id].create_run('Shared synthetic workspace', 'recorded', {})
                        return {'account': summary}
                    if not summary or path != f'/v1/accounts/{summary["id"]}/authorize':
                        raise AccountAccessDenied('synthetic account membership missing')
                    return dict(allowed=True, accountId=summary['id'], personId=person['id'], product=product['product'],
                        role='owner', operation=body['operation'], revision=1)
                def exchange(request):
                    proof = pair(index, state['nonce'], access_changes=state.get('access_changes'), id_changes=state.get('id_changes'))
                    return httpx.Response(200, json={'access_token': proof['access'], 'id_token': proof['id']})
                verifier = CognitoTokenVerifier(product['issuer'], product['clientIds'][0], jwks={'keys': list(public_keys[product['issuer']].values())})
                config = OAuthConfig(product['issuer'], product['clientIds'][0], 'https://synthetic.auth.example.invalid',
                    f'https://synthetic{index}.example.invalid')
                gateway = HostedAccounts(config, AccountsClient(signed if real_accounts else account_transport, product=product['product']),
                    lambda identifier: AccountWorkspace(stores[identifier]), verifier=verifier, google_identity=identity,
                    token_client=httpx.AsyncClient(transport=httpx.MockTransport(exchange)))
                app = create_app(None, account_access=gateway.access)
                gateway.install(app)
                return TestClient(app, base_url=config.public_origin), gateway, state

            def login(client, state, *, expected=303):
                redirect = client.get('/auth/login', follow_redirects=False)
                assert redirect.status_code == 303
                query = parse_qs(urlsplit(redirect.headers['location']).query)
                state['nonce'] = query['nonce'][0]
                callback = dict(state=query['state'][0], code='synthetic-code')
                assert client.get('/auth/callback', params=callback, follow_redirects=False).status_code == expected
                return callback

            first, first_gateway, first_state = bff(0)
            second, second_gateway, second_state = bff(1)
            with first, second, ExitStack() as extra_clients:
                callback = login(first, first_state)
                first_session = first.get('/api/session').json()
                assert first_session['personId'] and first_session['accounts'] == []
                headers = {'Origin': first_gateway.config.public_origin, 'X-CSRF-Token': first_session['csrfToken'],
                    'Idempotency-Key': str(uuid4())}
                owner_creation_headers = dict(headers)
                created = first.post('/api/accounts', json={'name': 'Synthetic shared team'}, headers=headers)
                assert created.status_code == 201
                account_id = created.json()['selectedAccountId']
                if real_accounts:
                    assert first.post('/api/accounts', json={'name': 'Synthetic shared team'}, headers=headers).json()['selectedAccountId'] == account_id
                    assert first.post('/api/accounts', json={'name': 'Changed replay'}, headers=headers).status_code == 403
                    assert len(first.get('/api/session').json()['accounts']) == 1
                    stores[account_id] = WebStore(Path(directory) / (account_id + '.sqlite'))
                    stores[account_id].create_run('Shared synthetic workspace', 'recorded', {})
                login(second, second_state)
                second_session = second.get('/api/session').json()
                assert first_session['personId'] == second_session['personId']
                headers = {'Origin': second_gateway.config.public_origin, 'X-CSRF-Token': second_session['csrfToken']}
                second_account_id, second_name = account_id, 'Shared synthetic workspace'
                if real_accounts:
                    # Same Person/membership does not grant a second product's
                    # entitlement. Exercise the real denial instead of seeding
                    # an implicit grant into the synthetic account adapter.
                    assert second_session['accounts'] == []
                    assert second.post('/api/accounts/select', json={'accountId': account_id}, headers=headers).status_code == 403
                    created_second = second.post('/api/accounts', json={'name': 'Papyrus synthetic team'},
                        headers=headers | {'Idempotency-Key': str(uuid4())})
                    assert created_second.status_code == 201
                    second_account_id = created_second.json()['selectedAccountId']
                    assert second_account_id != account_id
                    second_name = 'Papyrus synthetic workspace'
                    stores[second_account_id] = WebStore(Path(directory) / (second_account_id + '.sqlite'))
                    stores[second_account_id].create_run(second_name, 'recorded', {})
                else:
                    assert second_session['accounts'][0]['id'] == account_id
                assert second.post('/api/accounts/select', json={'accountId': second_account_id}, headers=headers).status_code == 200
                assert second.post('/graphql', params={'account': second_account_id}, json={'query': '{runs{name}}'}).json()['data']['runs'][0]['name'] == second_name
                assert first.post('/graphql', params={'account': account_id}, json={'query': '{runs{name}}'}).json()['data']['runs'][0]['name'] == 'Shared synthetic workspace'
                if durable_accounts:
                    # Membership changes use the actual signed central endpoint
                    # with a verified BFF owner's access evidence. There is no
                    # browser membership-management route in this console slice.
                    owner_access = first_gateway.sessions.get(first.cookies.get(first_gateway.config.cookie_name)).access_token
                    def membership(target_account, person, role, status='active'):
                        return first_gateway.accounts.transport('POST', f'/v1/accounts/{target_account}/memberships',
                            owner_access, dict(personId=person, role=role, status=status))
                    def read_revision(target_account):
                        return first_gateway.accounts.authorize(owner_access, target_account,
                            first_session['personId'], 'console:read')['revision']
                    def workspace_request(client, selected, query):
                        # Expected authorization failures should not print full
                        # Strawberry traces. Assertions still inspect responses.
                        logger = logging.getLogger('strawberry.execution')
                        previous = logger.disabled
                        logger.disabled = True
                        try:
                            return client.post('/graphql', params={'account': selected}, json={'query': query}).json()
                        finally:
                            logger.disabled = previous
                    member_client, member_gateway, member_state = bff(0)
                    extra_clients.enter_context(member_client)
                    member_changes = dict(sub='second-canonical-person', origin_jti='second-person-family')
                    member_state['access_changes'] = member_changes
                    member_state['id_changes'] = member_changes | {'identities': [dict(providerName='Google',
                        providerType='Google', userId='different-global-google-subject')]}
                    login(member_client, member_state)
                    member_session = member_client.get('/api/session').json()
                    member_id = member_session['personId']
                    assert member_id != first_session['personId']  # equal email never merges people
                    member_headers = {'Origin': member_gateway.config.public_origin, 'X-CSRF-Token': member_session['csrfToken']}
                    assert member_client.post('/api/accounts/select', json={'accountId': account_id}, headers=member_headers).status_code == 403
                    beta = first.post('/api/accounts', json={'name': 'Second Cyclotron account'},
                        headers=owner_creation_headers | {'Idempotency-Key': str(uuid4())})
                    assert beta.status_code == 201
                    beta_id = beta.json()['selectedAccountId']
                    stores[beta_id] = WebStore(Path(directory) / (beta_id + '.sqlite'))
                    stores[beta_id].create_run('Second Cyclotron workspace', 'recorded', {})
                    membership(account_id, member_id, 'viewer')
                    before_retry = read_revision(account_id)
                    membership(account_id, member_id, 'viewer')
                    assert read_revision(account_id) == before_retry  # identical update does not duplicate audit
                    membership(beta_id, member_id, 'member')
                    member_accounts = member_client.get('/api/session').json()['accounts']
                    assert {row['id']: row['role'] for row in member_accounts} == {account_id: 'viewer', beta_id: 'member'}
                    assert member_client.post('/api/accounts/select', json={'accountId': account_id}, headers=member_headers).status_code == 200
                    assert member_client.get('/api/session').json()['canWrite'] is False
                    assert workspace_request(member_client, account_id, '{runs{name}}')['data']['runs'][0]['name'] == 'Shared synthetic workspace'
                    mutation = 'mutation {createRun(name:"Member-created run",mode:"recorded",config:{}){id}}'
                    denied = workspace_request(member_client, account_id, mutation)
                    assert denied.get('errors') and len(stores[account_id].runs()) == 1
                    assert member_client.post('/api/accounts/select', json={'accountId': beta_id}, headers=member_headers).status_code == 200
                    assert member_client.get('/api/session').json()['canWrite'] is True
                    assert workspace_request(member_client, beta_id, mutation)['data']['createRun']['id']
                    assert len(stores[beta_id].runs()) == 2 and len(stores[account_id].runs()) == 1
                    assert member_client.post('/api/accounts/select', json={'accountId': str(uuid4())}, headers=member_headers).status_code == 403
                    assert member_client.get('/api/session').json()['selectedAccountId'] == beta_id
                    assert first.post('/api/accounts/select', json={'accountId': account_id}, headers=owner_creation_headers).status_code == 200
                    durable_owner_accounts = first.get('/api/session').json()['accounts']
                    durable_product_accounts = second.get('/api/session').json()['accounts']
                    durable_member_accounts = member_client.get('/api/session').json()['accounts']
                if filesystem:
                    # Identity is durable; real-handler MemoryAccountStore is
                    # deliberately reset. External Python account fixture mode
                    # stays in memory outside the restarted central process.
                    harness.restart(configuration)
                    restored_first = first.get('/api/session').json()
                    assert restored_first['personId'] == first_session['personId']
                    restored = second.get('/api/session').json()
                    assert restored['personId'] == first_session['personId']
                    if durable_accounts:
                        assert restored_first['accounts'] == durable_owner_accounts
                        assert restored['accounts'] == durable_product_accounts
                        assert restored_first['selectedAccountId'] == account_id and restored['selectedAccountId'] == second_account_id
                        restored_member = member_client.get('/api/session').json()
                        assert restored_member['accounts'] == durable_member_accounts
                        assert restored_member['selectedAccountId'] == beta_id and restored_member['canWrite'] is True
                        assert workspace_request(member_client, beta_id, '{runs{name}}')['data']['runs']
                        assert member_client.post('/api/accounts/select', json={'accountId': account_id}, headers=member_headers).status_code == 200
                        assert member_client.get('/api/session').json()['canWrite'] is False
                        denied = workspace_request(member_client, account_id, mutation)
                        assert denied.get('errors') and len(stores[account_id].runs()) == 1
                        assert member_client.post('/api/accounts/select', json={'accountId': beta_id}, headers=member_headers).status_code == 200
                        membership(account_id, member_id, 'viewer', 'revoked')
                        assert member_client.post('/api/accounts/select', json={'accountId': account_id}, headers=member_headers).status_code == 403
                        assert member_client.get('/api/session').json()['selectedAccountId'] == beta_id
                        assert second.post('/api/accounts/select', json={'accountId': account_id}, headers=headers).status_code == 403
                        # Persist a second revocation, then prove both the live
                        # BFF and another central process refuse old authority.
                        membership(beta_id, member_id, 'member', 'revoked')
                        revoked = member_client.get('/api/session').json()
                        assert revoked['accounts'] == [] and revoked['selectedAccountId'] is None
                        denied = workspace_request(member_client, beta_id, mutation)
                        assert denied.get('errors') and len(stores[beta_id].runs()) == 2
                        harness.restart(configuration)
                        revoked = member_client.get('/api/session').json()
                        assert revoked['personId'] == member_id and revoked['accounts'] == []
                        assert member_client.post('/api/accounts/select', json={'accountId': beta_id}, headers=member_headers).status_code == 403
                        repeated = first.post('/api/accounts', json={'name': 'Synthetic shared team'}, headers=owner_creation_headers)
                        assert repeated.status_code == 201 and repeated.json()['selectedAccountId'] == account_id
                        assert first.post('/api/accounts', json={'name': 'Changed replay'}, headers=owner_creation_headers).status_code == 403
                        assert first.get('/api/session').json()['accounts'] == durable_owner_accounts
                    elif accounts_handler:
                        assert restored_first['accounts'] == restored['accounts'] == []
                        assert restored_first['selectedAccountId'] is None and restored['selectedAccountId'] is None
                        assert second.post('/api/accounts/select', json={'accountId': second_account_id}, headers=headers).status_code == 403
                    else:
                        assert restored['selectedAccountId'] == second_account_id
                        assert second.post('/graphql', params={'account': second_account_id}, json={'query': '{runs{name}}'}).json()['data']['runs'][0]['name'] == second_name
                enrollment_count = len(resolutions)
                assert first.get('/auth/callback', params=callback).status_code == 400
                assert len(resolutions) == enrollment_count  # state replay did not reach central
                assert harness.call(dict(op='request', event=resolutions[0]))['statusCode'] == 200
                changed = dict(resolutions[0], headers=dict(resolutions[0]['headers']))
                changed['headers']['x-prospectus-identity-token'] = pair(0, first_state['nonce'])['id']
                assert harness.call(dict(op='request', event=changed))['statusCode'] == 409
                session = second_gateway.sessions.get(second.cookies.get(second_gateway.config.cookie_name))
                session.access_token = pair(1, second_state['nonce'])['access']
                assert second.get('/api/session').json()['personId'] == first_session['personId']
                session.access_token = pair(1, second_state['nonce'], access_changes={'origin_jti': 'new-login-family'})['access']
                assert second.get('/api/session').json()['authenticated'] is False
                assert len(resolutions) == enrollment_count  # continuation never enrolls a new family

            # New browser sessions cannot bypass proof validation or gain a
            # central association from a malformed/ambiguous provider profile.
            rejected = 0
            for changes in (dict(aud='wrong-client'), dict(iss=products[1]['issuer']), dict(origin_jti='other-family'),
                dict(nonce='wrong-nonce'), dict(identities=[dict(providerName='Google', providerType='Google', userId='a'),
                    dict(providerName='Google', providerType='Google', userId='b')])):
                client, _, state = bff(0)
                state['id_changes'] = changes
                with client:
                    login(client, state, expected=400)
                    assert client.get('/api/session').json()['authenticated'] is False
                rejected += 1
            print(json.dumps(dict(result='PASS', pools=2, canonicalPeople=2 if durable_accounts else 1, accountPersonContinuity=True,
                invalidProofCases=rejected, localStateReplay='denied', centralExactRetry='idempotent',
                centralChangedProof='409', continuationFamily='enforced',
                identityStorage='virtuus-filesystem' if filesystem else 'memory',
                centralRestartVerified=filesystem,
                accountStorage='virtuus-filesystem' if durable_accounts else 'central-memory-fixture' if accounts_handler else 'python-memory-fixture',
                accountHandler='actual' if real_accounts else 'synthetic',
                accountPersonDirectory='actual' if real_accounts else 'synthetic',
                accountCreateRetry='verified-across-restart' if durable_accounts else 'verified' if accounts_handler else 'not-checked',
                crossProductEntitlement='denied' if real_accounts else 'synthetic-grant',
                manyToManyRoles='viewer/member/owner verified' if durable_accounts else 'not-checked',
                revocationAfterRestart='denied' if durable_accounts else 'not-checked',
                durableSelectionVerified=durable_accounts,
                accountResetAfterRestart=filesystem and accounts_handler,
                deployedIamVerified=False, liveGoogleVerified=False)))
    finally:
        harness.close()
        identity_directory.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prospectus', type=Path)
    parser.add_argument('--filesystem', action='store_true', help='Use disposable durable identity files and restart the central harness')
    parser.add_argument('--accounts-handler', action='store_true', help='Use real central account handler/PersonDirectory over MemoryAccountStore; requires --filesystem')
    parser.add_argument('--durable-accounts', action='store_true', help='Use actual canonical accounts and verify memberships/revocation across restarts; requires --filesystem')
    arguments = parser.parse_args()
    if (arguments.accounts_handler or arguments.durable_accounts) and not arguments.filesystem:
        parser.error('Account handler modes require --filesystem')
    if arguments.accounts_handler and arguments.durable_accounts:
        parser.error('Choose durable accounts or memory accounts fixture, not both')
    verify(arguments.prospectus.resolve(), filesystem=arguments.filesystem,
        accounts_handler=arguments.accounts_handler, durable_accounts=arguments.durable_accounts)
